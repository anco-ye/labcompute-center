"""Portable task I/O helpers; no scientific acceptance or transfer file hashes."""
import http.client
import json
import math
from pathlib import Path
import re
import socket
import time
import urllib.error
import urllib.request

RELEASE = '2026-10-24-task-support-v1'


def vector_array(vector, dtype=None):
    """Copy a native vector before its owner resizes/frees it, including empty vectors."""
    import numpy as np
    if callable(getattr(vector, 'as_numpy', None)):
        value = vector.as_numpy()
    elif callable(getattr(vector, 'to_python', None)):
        value = vector.to_python()
    else:
        value = vector
    return np.array(value, dtype=dtype, copy=True)


def _read_timeout(response, remaining):
    # urllib's response file retains the socket after HTTPConnection releases it.
    raw = getattr(getattr(response, 'fp', None), 'raw', None)
    sock = getattr(raw, '_sock', None)
    if sock is not None:
        sock.settimeout(max(.001, min(60, remaining)))


def download_resource(url, destination, *, expected_size, opener=urllib.request.urlopen,
                      validate_url=lambda url: None, attempts=6, deadline_seconds=900,
                      chunk_bytes=8*1024**2, verify=None, adopt_prefix=False,
                      sleep=time.sleep, clock=time.monotonic):
    """Bound each read and total transfer time; preserve and bind prefixes on failure.

    verify is only for an existing input/code identity policy, never implicitly a
    task/result transfer hash. Unbound old prefixes require explicit provenance.
    """
    if (isinstance(expected_size, bool) or not isinstance(expected_size, int)
            or expected_size <= 0 or attempts < 1 or chunk_bytes < 1
            or not math.isfinite(deadline_seconds) or deadline_seconds <= 0):
        raise ValueError('Invalid download budget')
    if urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
        raise ValueError('HTTP(S) resource required')
    validate_url(url)
    final = Path(destination)
    final.parent.mkdir(parents=True, exist_ok=True)
    partial = final.with_name(final.name+'.part')
    identity = partial.with_name(partial.name+'.json')
    if any(p.is_symlink() for p in (final, partial, identity, final.parent)):
        raise ValueError('Unsafe resource path')
    binding = dict(url=url, size=expected_size)
    if identity.exists():
        prior = json.loads(identity.read_text())
        if any(prior.get(k) != v for k, v in binding.items()):
            raise ValueError('Resource identity changed; existing bytes retained')
        binding = prior
    elif partial.exists() and not adopt_prefix:
        raise ValueError('Prefix provenance missing; existing bytes retained')
    if final.exists():
        if final.stat().st_size != expected_size or verify and not verify(final):
            raise ValueError('Existing resource differs; original retained')
        return final
    if partial.exists() and partial.stat().st_size > expected_size:
        raise ValueError('Oversized prefix retained')
    # Per-task transfer ownership must be serialized by the caller's task lock.
    identity.write_text(json.dumps(binding))
    until = clock()+deadline_seconds
    failures = 0
    while (partial.stat().st_size if partial.exists() else 0) < expected_size:
        remaining_time = until-clock()
        if remaining_time <= 0:
            raise TimeoutError('Resource deadline exceeded; prefix retained')
        offset = partial.stat().st_size if partial.exists() else 0
        end = min(expected_size, offset+chunk_bytes)-1
        headers = {'Accept-Encoding': 'identity', 'Range': f'bytes={offset}-{end}'}
        if binding.get('validator'):
            headers['If-Range'] = binding['validator']
        request = urllib.request.Request(url, headers=headers)
        try:
            with opener(request, timeout=min(60, remaining_time)) as response:
                validate_url(response.geturl())
                validator = response.headers.get('ETag') or response.headers.get('Last-Modified')
                if binding.get('validator') and validator != binding['validator']:
                    raise ValueError('Resource validator changed; prefix retained')
                status = response.status
                count = end-offset+1
                if status == 206:
                    if response.headers.get('Content-Range') != f'bytes {offset}-{end}/{expected_size}':
                        raise ValueError('Invalid resource range; prefix retained')
                elif status == 200 and offset == 0:
                    count = expected_size  # Servers without Range can serve a fresh file.
                else:
                    raise ValueError('Server did not resume requested range; prefix retained')
                if (response.headers.get('Content-Length') != str(count)
                        or response.headers.get('Content-Encoding', 'identity') != 'identity'):
                    raise ValueError('Invalid resource length/encoding; prefix retained')
                if validator and not binding.get('validator'):
                    binding['validator'] = validator
                    identity.write_text(json.dumps(binding))
                with partial.open('ab') as output:
                    while count:
                        remaining_time = until-clock()
                        if remaining_time <= 0:
                            raise TimeoutError('Resource deadline exceeded; prefix retained')
                        _read_timeout(response, remaining_time)
                        try:
                            reader = getattr(response, 'read1', response.read)
                            block = reader(min(1024**2, count))
                        except http.client.IncompleteRead as error:
                            if len(error.partial) > count:
                                raise ValueError('Oversized resource fragment')
                            output.write(error.partial)
                            raise
                        if not block:
                            raise OSError('Short resource response')
                        if len(block) > count:
                            raise ValueError('Oversized resource response')
                        output.write(block)
                        count -= len(block)
        except urllib.error.HTTPError as error:
            if error.code not in (408, 429, 500, 502, 503, 504):
                raise
            failures += 1
            if failures >= attempts or clock() >= until:
                raise
            sleep(min(2**(failures-1), 8, max(0, until-clock())))
        except (OSError, urllib.error.URLError, http.client.IncompleteRead):
            failures += 1
            if failures >= attempts or clock() >= until:
                raise
            sleep(min(2**(failures-1), 8, max(0, until-clock())))
    if clock() > until:
        raise TimeoutError('Resource deadline exceeded; prefix retained')
    if final.exists():
        raise FileExistsError('Destination appeared during transfer; both retained')
    if partial.stat().st_size != expected_size or verify and not verify(partial):
        raise ValueError('Resource verification failed; prefix retained')
    partial.replace(final)
    return final


def install_pip(argv, run, log_path, cache_dir, *, max_attempts=3,
                deadline_seconds=900, clock=time.monotonic):
    """Retry only transient pip transport failures; keep its explicit cache intact.

    run(command, remaining_seconds) must enforce the supplied process deadline.
    Existing complete pip cache entries are reusable. Pip versions without Range
    support may redownload an interrupted wheel; this never promises prefix reuse.
    """
    if max_attempts < 1 or deadline_seconds <= 0:
        raise ValueError('Invalid dependency installation bounds')
    path = Path(log_path)
    command = list(argv) + ['--cache-dir', str(cache_dir), '--timeout', '60',
                            '--retries', '2', '--disable-pip-version-check', '--no-input']
    deadline = clock() + deadline_seconds
    last_error = 'Dependency installation did not start'
    for attempt in range(max_attempts):
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError('Dependency installation deadline exceeded: ' + last_error)
        offset = path.stat().st_size if path.exists() else 0
        code = run(command, remaining)
        if code == 0:
            return 0
        with path.open('rb') as stream:
            stream.seek(max(offset, path.stat().st_size - 16384))
            tail = stream.read(16384).decode('utf-8', errors='replace')
        lines = [line.strip() for line in tail.splitlines() if line.strip()]
        errors = [line for line in lines if any(x in line for x in
                  ('ERROR:', 'ReadTimeoutError:', 'ConnectionError:', 'SSLError:'))]
        last_error = (errors[-1] if errors else (lines[-1] if lines else 'pip exit=' + str(code)))[:700]
        transient = any(x in tail for x in (
            'ReadTimeoutError', 'Read timed out', 'ConnectTimeout',
            'Connection reset by peer', 'RemoteDisconnected',
            'Temporary failure in name resolution', 'Network is unreachable',
            'SSLEOFError', 'IncompleteRead', 'ChunkedEncodingError'))
        permanent = any(x in tail for x in (
            'ResolutionImpossible', 'conflicting dependencies',
            'THESE PACKAGES DO NOT MATCH THE HASHES', 'CERTIFICATE_VERIFY_FAILED'))
        if permanent or not transient or attempt + 1 == max_attempts:
            raise RuntimeError('Dependency installation failed: ' + last_error)
    raise RuntimeError('Dependency installation failed: ' + last_error)
