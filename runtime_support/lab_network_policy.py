"""Finite HTTP inactivity bounds, installed only in newly launched task processes."""
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import tempfile

RELEASE = '2026-10-24-network-policy-v1'


def bounded_timeout(value, limit):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return min(value, limit)
    return limit


def external(connection):
    host = str(getattr(connection, 'host', '')).strip('[]').lower()
    if host in ('localhost', ''):
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def install():
    cls = http.client.HTTPConnection
    if getattr(cls, '_lab_network_policy', None) == RELEASE:
        return
    original_send, original_response = cls.send, cls.getresponse

    def send(self, data):
        if external(self):
            self.timeout = bounded_timeout(self.timeout, 20)
            if self.sock is not None:
                self.sock.settimeout(bounded_timeout(self.sock.gettimeout(), 60))
        return original_send(self, data)

    def response(self, *args, **kwargs):
        if external(self) and self.sock is not None:
            self.sock.settimeout(bounded_timeout(self.sock.gettimeout(), 60))
        return original_response(self, *args, **kwargs)

    cls.send, cls.getresponse = send, response
    cls._lab_network_policy = RELEASE
    folder = os.environ.get('LAB_JOB_DIR')
    if folder:
        try:
            # Independent children publish independent receipts; no shared tmp path.
            path = Path(folder)/('network-policy-'+str(os.getpid())+'.json')
            with tempfile.NamedTemporaryFile(mode='w', dir=folder, delete=False, suffix='.policy-tmp') as stream:
                json.dump(dict(release=RELEASE, pid=os.getpid(), connect_seconds=20, read_seconds=60), stream)
                temp = Path(stream.name)
            os.replace(temp, path)
        except OSError:
            pass
