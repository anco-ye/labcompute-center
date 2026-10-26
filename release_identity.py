"""Startup source identity: a disk deployment is never a running release."""
import hashlib
import json
import os
import time
from pathlib import Path

def source_files(root, component):
    root=Path(root)
    if component=='web':
        names=[p.name for p in root.glob('*.py')]
    elif component=='bridge':
        names=['external/ssh_bridge.py','agent.py','common.py','resource_policy.py',
               'resource_metadata.py','execution_contract.py','release_identity.py']
    elif component=='broker':
        names=['execution_broker.py','common.py','resource_policy.py',
               'resource_metadata.py','release_identity.py']
    else:raise ValueError('Unknown component')
    return sorted(set(names))

def snapshot(root,component):
    root=Path(root)
    files={name:hashlib.sha256((root/name).read_bytes()).hexdigest()
           for name in source_files(root,component)}
    digest=hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return dict(component=component,digest=digest,pid=os.getpid(),started_at=time.time(),files=files)

def status(root,loaded):
    """Compare the immutable startup snapshot with disk without rewriting it."""
    root=Path(root);changed=[]
    for name,expected in loaded['files'].items():
        try:actual=hashlib.sha256((root/name).read_bytes()).hexdigest()
        except OSError:actual=None
        if actual!=expected:changed.append(name)
    return dict(loaded,disk_matches_loaded=not changed,changed_files=changed)

def public(loaded):
    return {k:loaded[k] for k in ('component','digest','pid','started_at')}
