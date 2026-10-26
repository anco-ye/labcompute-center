"""Short-lived download capabilities and immutable, single-range result reads."""
import base64, hashlib, hmac, json, re, time
from email.utils import parsedate_to_datetime
from pathlib import Path


def issue(key, value, ttl=300):
    value=dict(value,exp=int(time.time())+ttl)
    raw=json.dumps(value,separators=(',',':'),sort_keys=True).encode()
    body=base64.urlsafe_b64encode(raw).decode().rstrip('=')
    sig=hmac.new(bytes.fromhex(key),body.encode(),hashlib.sha256).hexdigest()
    return body+'.'+sig


def verify(key, token, node, now=None):
    if not isinstance(token,str) or len(token)>4096:raise PermissionError('Invalid capability')
    try:
        body,sig=token.split('.')
        expected=hmac.new(bytes.fromhex(key),body.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig,expected):raise ValueError()
        v=json.loads(base64.urlsafe_b64decode(body+'='*(-len(body)%4)))
        at=time.time() if now is None else now
        if v['node']!=node or type(v['exp']) is not int or not at<v['exp']<=at+600:raise ValueError()
        if not re.fullmatch('[a-f0-9]{32}',v['jid']):raise ValueError()
        if not re.fullmatch('[a-f0-9]{64}',v['lease_digest']):raise ValueError()
        if v.get('kind') not in ('locate','read'):raise ValueError()
        return v
    except (ValueError,KeyError,TypeError,json.JSONDecodeError):raise PermissionError('Invalid or expired capability') from None


def identity(path):
    s=Path(path).stat()
    return dict(size=s.st_size,mtime_ns=s.st_mtime_ns)


def etag(meta):return '"%x-%x"'%(meta['size'],meta['mtime_ns'])


def selection(meta, headers):
    tag=etag(meta);size=meta['size'];start=0;end=size-1;status=200
    if headers.get('If-Match') not in (None,'*',tag):return 412,0,-1
    if headers.get('If-None-Match') in (tag,'*'):return 304,0,-1
    value=headers.get('Range')
    validator=headers.get('If-Range',tag);matches=validator==tag
    if not matches:
        try:matches=int(parsedate_to_datetime(validator).timestamp())>=meta['mtime_ns']//1000000000
        except (TypeError,ValueError,OverflowError):pass
    if value and matches:
        m=re.fullmatch(r'bytes=(\d*)-(\d*)',value)
        if not m or not any(m.groups()):return 416,0,-1
        a,b=m.groups()
        if a:start=int(a);end=min(int(b),size-1) if b else size-1
        else:
            if int(b)==0:return 416,0,-1
            start=max(0,size-int(b))
        if start>=size or start>end:return 416,0,-1
        status=206
    return status,start,end
