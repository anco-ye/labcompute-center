"""Build a private offline worker bundle. Register its token hash explicitly."""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import shutil
from urllib.parse import urlsplit
try:
    from .build_bundle import build_app_archive
except ImportError:
    from build_bundle import build_app_archive


def enroll(node_id, python, controller, ca, wheels, output):
    if not node_id.replace('-', '').replace('_', '').isalnum():
        raise ValueError('Invalid node ID')
    if urlsplit(controller).scheme != 'https':
        raise ValueError('A verified HTTPS controller is required')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    if __import__('os').name != 'nt':output.chmod(0o700)
    token = secrets.token_hex(32)
    config = dict(id=node_id,token=token,controller=controller,ca_file='C:/LabComputeCenter/tls/ca.crt',work_dir='C:/LabComputeCenter/jobs',worker=True,reserve_gb=6,respect_external_jobs=True,poll_seconds=5,runtimes={'python':{'kind':'python','executable':'C:/LabComputeCenter/venv/Scripts/python.exe'}})
    root=Path(__file__).resolve().parents[1]
    for name,value in [('agent.json',config),('install.json',dict(controller=False,python=python,account_password=secrets.token_urlsafe(32)))]:
        path=output/name;path.write_text(json.dumps(value,indent=2))
        if __import__('os').name!='nt':path.chmod(0o600)
    shutil.copyfile(ca,output/'ca.crt');shutil.copytree(wheels,output/'wheels')
    for name in ('install.ps1','grant_batch.ps1','prepare_runtimes.ps1','install_execution_broker.ps1'):
        shutil.copyfile(root/'deploy'/name,output/name)
    build_app_archive(root,output/'app.zip')
    registration=dict(id=node_id,label=node_id,enabled=False,token_hash=hashlib.sha256(token.encode()).hexdigest())
    (output/'node-registration.json').write_text(json.dumps(registration,indent=2))
    return output


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ['id','python','controller','ca','wheels','output']:parser.add_argument('--'+name,required=True)
    a=parser.parse_args();print('Private bundle:',enroll(a.id,a.python,a.controller,a.ca,a.wheels,a.output))
    print('Register node-registration.json using your controller administration process; enable only after validating the node.')
