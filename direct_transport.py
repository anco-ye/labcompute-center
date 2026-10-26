"""Verified HTTPS first; existing restricted SSH bridge routes when HTTPS is unreachable."""
import json,shlex,sys,urllib.request,urllib.error,ipaddress,time,threading
from pathlib import Path
from urllib.parse import urlsplit,parse_qs,urlunsplit

class SSHResponse:
    def __init__(self,client,stdout,value):self.client=client;self.stdout=stdout;self.status=value['status'];self.headers=value['headers']
    def read(self,size=-1):return self.stdout.read(size)
    def close(self):
        try:self.stdout.channel.close()
        finally:self.client.close()
    def __enter__(self):return self
    def __exit__(self,*args):self.close()

class NodeTransport:
    def __init__(self,nodes,https_open):
        self.nodes=nodes;self.https_open=https_open
        self._preferred={};self._lock=threading.Lock()
    @staticmethod
    def tail_url(node):
        """Only the explicitly configured Tailnet IP, with the same verified HTTPS port."""
        p=urlsplit(node['url']);tail=node.get('tail')
        if p.scheme!='https' or not tail:return None
        try:
            address=ipaddress.ip_address(tail)
            if address.version!=4 or address not in ipaddress.ip_network('100.64.0.0/10'):return None
        except ValueError:return None
        return urlunsplit(('https',str(address)+(':'+str(p.port) if p.port else ''),p.path,'',''))
    def endpoint(self,node):
        with self._lock:preferred,until=self._preferred.get(node['url'],(None,0))
        return preferred if until>time.monotonic() else node['url']
    @staticmethod
    def rewrite(request,original,target):
        url=request if isinstance(request,str) else request.full_url
        rewritten=target+url[len(original):]
        if isinstance(request,str):return rewritten
        headers={k:v for k,v in request.header_items() if k.lower()!='host'}
        return urllib.request.Request(rewritten,data=request.data,headers=headers,method=request.get_method())
    def __call__(self,request,timeout=60):
        url=request if isinstance(request,str) else request.full_url
        selected=next((n for n in self.nodes.values() if any(url.startswith(base+'/') for base in (n['url'],self.tail_url(n)) if base)),None)
        if not selected:raise ValueError('Unknown result node')
        original=selected['url'] if url.startswith(selected['url']+'/') else self.tail_url(selected)
        tail=self.tail_url(selected)
        if not selected.get('ssh_only'):
            preferred=self.endpoint(selected)
            endpoints=list(dict.fromkeys([preferred]+[x for x in (selected['url'],tail) if x]))
            for index,endpoint in enumerate(endpoints):
                try:
                    # Retry only connection establishment; a returned stream is never replayed.
                    response=self.https_open(self.rewrite(request,original,endpoint),timeout=min(timeout,3) if '/meta/' in urlsplit(url).path and index<len(endpoints)-1 else timeout)
                    with self._lock:self._preferred[selected['url']]=(endpoint,time.monotonic()+300)
                    return response
                except urllib.error.HTTPError:raise
                except (OSError,urllib.error.URLError):
                    if index==len(endpoints)-1 and not selected.get('bridge_config'):raise
        return self.ssh(selected,self.rewrite(request,original,selected['url']),timeout)
    def ssh(self,node,request,timeout):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'data/external-ssh-libs'))
        from external.ssh_bridge import Bridge
        b=Bridge(node['bridge_config']);c=b.connect();url=request if isinstance(request,str) else request.full_url;p=urlsplit(url)
        method='GET' if isinstance(request,str) else request.get_method();headers={} if isinstance(request,str) else {k.title():v for k,v in request.header_items()}
        req=dict(op=p.path.strip('/').split('/')[0],ticket=parse_qs(p.query).get('ticket',[''])[0],method=method,headers=headers)
        root=b.cfg['workspace'];command=shlex.join([b.cfg['remote_python'],'-B',root+'/download-service/node_result_service.py','--config',root+'/download-service/config.json','--stdio'])
        try:
            i,o,e=c.exec_command(command,timeout=timeout);i.write(json.dumps(req));i.flush();i.channel.shutdown_write();value=json.loads(o.readline(16384));return SSHResponse(c,o,value)
        except BaseException:c.close();raise
