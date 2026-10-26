"""Broker's synchronous launch hook: grant only this account's physical folder."""
import argparse,json,re,subprocess
from pathlib import Path
from common import atomic_json
import storage_layout


def grant(root,prefix,sid):
    if not re.fullmatch('[a-f0-9]{17}',prefix) or not re.fullmatch(r'S-1-5-21-(?:\d+-){3}\d+',sid):raise ValueError('Invalid local execution identity')
    root=Path(root);cfg=json.loads((root/'config/agent.json').read_text(encoding='utf-8-sig'))
    candidates=[p.stem for p in (root/'broker/requests').glob(prefix+'*.json') if re.fullmatch('[a-f0-9]{32}',p.stem)]
    if len(candidates)!=1:raise ValueError('Execution request must be unique')
    jid=candidates[0];canonical=root/'jobs'/jid;physical=storage_layout.trusted_folder(root/'jobs',jid,cfg)
    if physical==canonical.absolute():return dict(job=jid,volume='default')
    subprocess.run(['icacls',str(physical),'/grant','*'+sid+':(OI)(CI)M','/Q'],check=True,capture_output=True)
    subprocess.run(['icacls',str(canonical),'/L','/grant','*'+sid+':(RX)','/Q'],check=True,capture_output=True)
    atomic_json(root/'broker/volume-acls'/(jid+'.json'),dict(job=jid,sid=sid,physical=str(physical),applied=True))
    return dict(job=jid,physical=str(physical))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--prefix',required=True);p.add_argument('--sid',required=True);a=p.parse_args();grant(a.root,a.prefix,a.sid)
