"""Unattended, idempotent ZIP publisher. Credentials stay in a local file.

python submit_batch.py package.zip --config publisher.json --submission-id run-001
Config: server, ca_file, username, password. Protect this file as a secret.
A new intended run needs a new submission ID; network retries reuse the same ID.
"""
import argparse
import json
from pathlib import Path
import urllib.request
import uuid
from common import atomic_json
from client_session import CenterClient


def submit(package,config,submission_id):
    package=Path(package);client=CenterClient(config)
    base=client.base;opener=client.opener
    boundary=uuid.uuid4().hex
    prefix=(f'--{boundary}\r\nContent-Disposition: form-data; name="package"; filename="batch.zip"\r\nContent-Type: application/zip\r\n\r\n').encode()
    suffix=f'\r\n--{boundary}--\r\n'.encode()
    def chunks():
        yield prefix
        with package.open('rb') as stream:
            while chunk:=stream.read(1024**2):yield chunk
        yield suffix
    request=urllib.request.Request(base+'/api/jobs',data=chunks(),headers={
        'X-CSRF-Token':client.csrf,'Idempotency-Key':submission_id,
        'Content-Type':'multipart/form-data; boundary='+boundary,
        'Content-Length':str(len(prefix)+package.stat().st_size+len(suffix))})
    result=json.loads(opener.open(request,timeout=300).read())
    atomic_json(package.with_suffix(package.suffix+'.receipt.json'),dict(submission_id=submission_id,**result))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package');parser.add_argument('--config',required=True);parser.add_argument('--submission-id',required=True)
    args=parser.parse_args()
    print(json.dumps(submit(args.package,args.config,args.submission_id),ensure_ascii=False))
