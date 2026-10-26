"""Bounded, identity-bound result downloads; never retry a computation."""
import http.client
import json
from pathlib import Path
import re
import time
import urllib.error
import urllib.request
import uuid
import zipfile


def download(client, path, destination, *, chunk_bytes=8*1024**2, retries=6):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix('.partial')
    metadata = partial.with_suffix('.partial.json')

    def open_read(request):
        try:
            return client.opener.open(request, timeout=60)
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise
        client.authenticate()
        return client.opener.open(request, timeout=60)

    with open_read(urllib.request.Request(client.base + path, method='HEAD')) as response:
        total = int(response.headers['Content-Length'])
        etag = response.headers.get('ETag', '')
    if total <= 0 or not etag or etag.startswith('W/'):
        raise ValueError('Result download requires a length and strong ETag')
    identity = dict(server=client.base, path=path, length=total, etag=etag)
    if destination.exists():
        raise FileExistsError(destination)
    try:
        saved = json.loads(metadata.read_text())
    except (OSError, ValueError):
        saved = None
    if partial.exists() and (saved != identity or partial.stat().st_size > total):
        # Preserve an unverified/older partial rather than mixing result revisions.
        partial.rename(partial.with_name(partial.name + '.unverified-' + uuid.uuid4().hex))
    pending = metadata.with_suffix('.json.tmp')
    pending.write_text(json.dumps(identity))
    pending.replace(metadata)
    failures = 0
    offset = partial.stat().st_size if partial.exists() else 0
    while offset < total:
        end = min(total - 1, offset + chunk_bytes - 1)
        request = urllib.request.Request(client.base + path, headers={
            'Range': f'bytes={offset}-{end}', 'If-Range': etag, 'Accept-Encoding': 'identity'})
        try:
            with open_read(request) as response:
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                if (response.status != 206 or response.headers.get('ETag') != etag or not match
                        or tuple(map(int, match.groups())) != (offset, end, total)):
                    raise ValueError('Result changed or server did not honor the requested range')
                with partial.open('ab') as output:
                    while offset <= end:
                        try:
                            block = response.read(min(256*1024, end + 1 - offset))
                        except http.client.IncompleteRead as error:
                            output.write(error.partial)
                            offset += len(error.partial)
                            raise OSError('Result connection closed early') from error
                        if not block:
                            raise OSError('Result connection closed early')
                        output.write(block)
                        offset += len(block)
            failures = 0
        except urllib.error.HTTPError as error:
            if error.code not in (408, 429, 500, 502, 503, 504):
                raise
            failures += 1
        except (OSError, http.client.HTTPException):
            failures += 1
        if failures:
            if failures > retries:
                raise OSError('Result transfer retry limit reached; verified partial retained')
            time.sleep(min(failures, 5))
    if partial.stat().st_size != total:
        raise OSError('Result length mismatch; partial retained')
    with zipfile.ZipFile(partial) as archive:
        if archive.testzip() is not None:
            raise OSError('Result ZIP integrity check failed; partial retained')
    partial.replace(destination)
    metadata.unlink(missing_ok=True)
    return destination
