"""Publish download-only runtime snapshots; never install or control runtimes."""
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone, timedelta


def catalog(nodes, now):
    result=[]
    for node in nodes:
        telemetry=node['telemetry']
        if isinstance(telemetry,str):telemetry=json.loads(telemetry)
        for rid,runtime in telemetry.get('runtimes',{}).items():
            caps=telemetry.get('scheduling_policy',{}).get('runtime_max_parallel',{})
            if rid in caps:runtime=dict(runtime,max_parallel=caps[rid])
            result.append(dict(runtime,os=telemetry.get('os','windows'),execution_backend=telemetry.get('execution_backend','native-worker'),id=rid,node=node['id'],node_label=node['label'],
                               online=node['last_seen']>now-35,worker=telemetry.get('worker',True)))
    return result


def fingerprint(rows):
    stable=[]
    for row in rows:
        value={k:v for k,v in row.items() if k!='online'}
        if isinstance(value.get('packages'),list):value['packages']=sorted(value['packages'])
        stable.append(value)
    stable.sort(key=lambda row:(row['node'],row['id']))
    return hashlib.sha256(json.dumps(stable,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


class Publisher:
    def __init__(self, static, load_nodes, interval=60, output=None):
        self.static=Path(static);self.output=Path(output) if output else self.static
        self.load_nodes=load_nodes;self.interval=interval
        self.wake=threading.Event();self.stop=threading.Event();self.thread=None
        self.lock=threading.Lock();self.last_signature=None;self.file_versions=None

    def request(self):
        self.wake.set()

    def start(self):
        if self.thread and self.thread.is_alive():return
        self.thread=threading.Thread(target=self._loop,name='environment-snapshot-publisher',daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop.is_set():
            self.wake.clear()
            try:self.sync()
            except Exception:
                logging.getLogger(__name__).exception('Runtime snapshot publication failed; will retry')
            self.wake.wait(self.interval)

    def sync(self, now=None):
        with self.lock:
            now=time.time() if now is None else now
            rows=catalog(self.load_nodes(),now);signature=fingerprint(rows)
            self.output.mkdir(parents=True,exist_ok=True)
            snapshot_path=self.output/'运行环境快照.json';kit_path=self.output/'agent-preparation-kit.zip'
            template_path=self.static/'agent-preparation-kit.zip'
            version_paths=(snapshot_path,kit_path,template_path)
            def versions_now():
                return tuple((p.stat().st_mtime_ns,p.stat().st_size) if p.exists() else None for p in version_paths)
            versions=versions_now()
            if signature==self.last_signature and versions==self.file_versions:return False
            original_snapshot=snapshot_path.read_bytes() if snapshot_path.exists() else None
            original_kit=kit_path.read_bytes() if kit_path.exists() else None
            old_snapshot=original_snapshot or (self.static/'运行环境快照.json').read_bytes()
            old_kit=original_kit or template_path.read_bytes()
            template=template_path.read_bytes()
            template_hash=hashlib.sha256(template).hexdigest() if self.output!=self.static else None
            with zipfile.ZipFile(io.BytesIO(template)) as archive:
                entries=[(info,archive.read(info)) for info in archive.infolist()]
            inside=dict((info.filename,data) for info,data in entries)
            old=json.loads(old_snapshot)
            if (original_snapshot and original_kit and old.get('sync_mode')=='automatic'
                    and old.get('template_sha256')==template_hash and fingerprint(old.get('environments',[]))==signature
                    and json.loads(zipfile.ZipFile(io.BytesIO(old_kit)).read('运行环境快照.json'))==old):
                self.last_signature=signature;self.file_versions=versions;return False
            stamp=datetime.fromtimestamp(now,timezone(timedelta(hours=8), 'Asia/Shanghai'))
            snapshot=dict(snapshot_date=stamp.date().isoformat(),snapshot_at=stamp.isoformat(timespec='seconds'),
                          timezone='Asia/Shanghai',source='/api/environments',sync_mode='automatic',template_sha256=template_hash,platform='Windows x64; Linux x86_64 (SSH external)' if any(row.get('os')=='linux' for row in rows) else 'Windows x64',
                          note='由平台环境目录自动同步，按主机列出环境与版本。在线状态仅代表采集时刻；仅在线波动不重复打包。最新派发与可用状态以平台实时目录为准。',
                          environments=rows)
            payload=(json.dumps(snapshot,ensure_ascii=False,indent=2)+'\n').encode()
            readme=inside['README.md'].decode('utf-8')
            line='环境快照更新于 '+stamp.strftime('%Y-%m-%d %H:%M:%S')+'（北京时间），由平台 /api/environments 自动同步。环境版本、依赖、安装状态或节点目录变化后自动更新；在线状态仅代表采集时刻，最新可用状态以平台实时目录为准。'
            readme,count=re.subn(r'^环境快照[^\n]*',lambda match:line,readme,count=1,flags=re.M)
            if not count:readme=line+'\n\n'+readme
            buffer=io.BytesIO()
            with zipfile.ZipFile(buffer,'w') as archive:
                for info,data in entries:
                    if info.filename=='运行环境快照.json':data=payload
                    elif info.filename=='README.md':data=readme.encode()
                    archive.writestr(info,data)
            packed=buffer.getvalue()
            staged=[];published=[]
            try:
                for path,data in ((snapshot_path,payload),(kit_path,packed)):
                    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
                    staged.append((path,temp));temp.write_bytes(data)
                if versions!=versions_now():
                    raise RuntimeError('Preparation documents changed during snapshot generation; retrying')
                for path,temp in staged:
                    os.replace(temp,path);published.append(path)
            except Exception:
                for path in published:
                    rollback=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
                    try:
                        previous=original_snapshot if path==snapshot_path else original_kit
                        if previous is None:path.unlink(missing_ok=True)
                        else:
                            rollback.write_bytes(previous)
                            os.replace(rollback,path)
                    finally:rollback.unlink(missing_ok=True)
                raise
            finally:
                for _,temp in staged:temp.unlink(missing_ok=True)
            self.last_signature=signature
            self.file_versions=versions_now()
            return True
