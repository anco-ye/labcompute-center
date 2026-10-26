"""Independent resource sampling; never claims, starts or stops a task."""
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import ssl
import threading
import time
import urllib.request
import psutil
from common import atomic_json
from resource_monitor import PressureWindow,WindowsCounters

RELEASE='2026-10-25-independent-resource-sampling-v1'
FIELDS=('cpu_percent','memory_total_gb','memory_available_gb','memory_percent',
        'memory_commit_limit_gb','memory_committed_gb')
MAX_AGE=15


def receive(payload, now, previous=None):
    """Translate node sample age once; a delayed request cannot freshen a sample."""
    if not isinstance(payload,dict)or len(json.dumps(payload))>16384:raise ValueError('Invalid sample')
    sent=payload.get('sent_at');sampled=payload.get('sampled_at')
    if any(isinstance(x,bool)or not isinstance(x,(int,float))or not math.isfinite(x)for x in (sent,sampled)):
        raise ValueError('Invalid sample clock')
    center_sent=payload.get('center_sent_at')
    if (isinstance(center_sent,bool)or not isinstance(center_sent,(int,float))
        or not math.isfinite(center_sent)or not -2<=now-center_sent<=10):raise ValueError('Delayed resource publication')
    age=sent-sampled
    if not 0<=age<=MAX_AGE:raise ValueError('Expired sample')
    obs=payload.get('resource_observation');values=payload.get('values')
    if not isinstance(obs,dict)or not isinstance(values,dict):raise ValueError('Invalid observation')
    if obs.get('state')not in ('warming','unknown','normal','pressured')or not isinstance(obs.get('verified'),bool):
        raise ValueError('Invalid pressure state')
    source=payload.get('source',{})
    if not isinstance(source,dict):raise ValueError('Invalid sampler identity')
    if (source.get('release')!=RELEASE or not isinstance(source.get('pid'),int)or source['pid']<=0
        or not isinstance(source.get('created'),(int,float))or not math.isfinite(source['created'])
        or not isinstance(source.get('sequence'),int)or source['sequence']<1):raise ValueError('Invalid sampler identity')
    if previous:
        old=previous.get('source',{})
        if source['created']<old.get('created',0):raise ValueError('Old sampler generation')
        if (source['created'],source['pid'])==(old.get('created'),old.get('pid')) and source['sequence']<=old.get('sequence',0):
            raise ValueError('Repeated sample')
    for key in FIELDS:
        value=values.get(key)
        if isinstance(value,bool)or not isinstance(value,(int,float))or not math.isfinite(value)or value<0:
            raise ValueError('Invalid resource value: '+key)
    if values['memory_available_gb']>values['memory_total_gb']:raise ValueError('Invalid memory sample')
    # Include measured publication delay, not only the age at send time.
    observed_at=min(now,center_sent)-age
    if now-observed_at>MAX_AGE:raise ValueError('Expired sample in transport')
    result=copy.deepcopy(payload);result.update(received_at=now,at=observed_at)
    result['values']={k:values[k]for k in FIELDS};result['resource_observation']['at']=observed_at
    return result


def merge(telemetry, sample, now):
    """Resource freshness never renews the Agent's liveness or other telemetry."""
    if not sample:return False
    telemetry['independent_resource_sample']=sample
    if not 0<=now-sample.get('at',0)<=MAX_AGE:return False
    telemetry.update(sample['values']);telemetry['memory_commit_verified']=True
    telemetry['resource_observation']=sample['resource_observation']
    return True


class Sampler:
    def __init__(self,config):
        self.cfg=config;self.root=Path(config['work_dir']);self.window=PressureWindow()
        self.counters=WindowsCounters();self.latest=None;self.lock=threading.Lock()
        self.stop=threading.Event();self.sequence=0;self.created=psutil.Process().create_time()
        self.source_digest=hashlib.sha256(Path(__file__).read_bytes()+Path(__file__).with_name('resource_monitor.py').read_bytes()).hexdigest()
    def collect(self):
        from agent import memory_commitment
        mem=psutil.virtual_memory();cpu=psutil.cpu_percent(interval=.1);now=time.time();managed=0
        try:states=json.loads((self.root/'active_jobs.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):states={}
        for jid in states:
            if len(jid)!=32 or any(x not in '0123456789abcdef'for x in jid):continue
            try:
                u=json.loads((self.root/jid/'usage.json').read_text(encoding='utf-8'))
                if 0<=now-u.get('at',0)<15:managed+=u.get('cpu_cores',0)
            except (OSError,ValueError):pass
        obs=self.window.observe(now,cpu_percent=cpu,total_gb=mem.total/2**30,
            available_gb=mem.available/2**30,reserve_gb=self.cfg.get('reserve_gb',4),
            managed_cores=managed,cpu_threads=psutil.cpu_count()or 1,counters=self.counters.read())
        self.sequence+=1
        return dict(sampled_at=now,resource_observation=obs,
            values=dict(cpu_percent=cpu,memory_total_gb=mem.total/2**30,
                memory_available_gb=mem.available/2**30,memory_percent=mem.percent,**memory_commitment()),
            source=dict(release=RELEASE,pid=os.getpid(),created=self.created,
                sequence=self.sequence,digest=self.source_digest))
    def sample_loop(self):
        while not self.stop.is_set():
            at=time.monotonic()
            try:
                result=self.collect()
                with self.lock:self.latest=result
            except Exception as exc:
                atomic_json(self.root/'resource-sampler-status.json',dict(at=time.time(),error=type(exc).__name__))
            self.stop.wait(max(.1,5-(time.monotonic()-at)))
    def payload(self):
        with self.lock:value=copy.deepcopy(self.latest)
        if value is not None:value['sent_at']=time.time()
        return value
    def run(self):
        ctx=ssl.create_default_context(cafile=self.cfg['ca_file'])
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=ctx))
        threading.Thread(target=self.sample_loop,daemon=True,name='resource-sampling').start()
        clock_offset=None;clock_checked=0
        while not self.stop.is_set():
            if clock_offset is None or time.monotonic()-clock_checked>60:
                try:
                    sent=time.time();probe=urllib.request.Request(self.cfg['controller'].rstrip('/')+'/agent/'+self.cfg['id']+'/resource-sample',
                        data=b'{"probe":true}',headers={'Authorization':'Bearer '+self.cfg['token'],'Content-Type':'application/json'})
                    with opener.open(probe,timeout=5)as response:server_time=json.loads(response.read())['server_time']
                    ended=time.time()
                    if 0<=ended-sent<=2:
                        clock_offset=server_time-(sent+ended)/2;clock_checked=time.monotonic()
                except Exception:pass
            value=self.payload() if clock_offset is not None else None
            if value is not None:value['center_sent_at']=time.time()+clock_offset
            if value is not None:
                try:
                    req=urllib.request.Request(self.cfg['controller'].rstrip('/')+'/agent/'+self.cfg['id']+'/resource-sample',
                        data=json.dumps(value).encode(),headers={'Authorization':'Bearer '+self.cfg['token'],'Content-Type':'application/json'})
                    with opener.open(req,timeout=5)as response:response.read()
                    atomic_json(self.root/'resource-sampler-status.json',dict(at=time.time(),source=value['source'],sampled_at=value['sampled_at'],published=True))
                except Exception as exc:
                    atomic_json(self.root/'resource-sampler-status.json',dict(at=time.time(),source=value['source'],sampled_at=value['sampled_at'],published=False,error=type(exc).__name__))
            self.stop.wait(5)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    cfg=json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    if os.name!='nt':raise RuntimeError('Windows PDH sampler only; Linux retains its native monitor')
    import msvcrt
    # A separate observer lock does not take ownership of Agent or native jobs.
    with (Path(cfg['work_dir'])/'resource-sampler.lock').open('a+b')as lock:
        lock.write(b'0');lock.flush();lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        Sampler(cfg).run()

if __name__=='__main__':main()
