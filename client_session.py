"""Reusable authenticated sessions for unattended center clients.

Cookies are credentials: keep the cache beside the protected client config.
Only reads retry after session expiry; mutations are never replayed here.
"""
from contextlib import contextmanager
import gzip
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import re
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


@contextmanager
def session_lock(path):
    with path.open('a+b') as stream:
        os.chmod(path, 0o600)
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            if not stream.read(1):
                stream.write(b'0'); stream.flush()
            stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class CenterClient:
    def __init__(self, config, cache_dir=None):
        if isinstance(config, (str, Path)):
            path = Path(config)
            cfg = json.loads(path.read_text(encoding='utf-8-sig'))
            cache_dir = cache_dir or path.parent / '.center-sessions'
        else:
            cfg = dict(config)
        if cache_dir is None:
            raise ValueError('A protected session cache directory is required')
        self.cfg = cfg
        self.base = cfg['server'].rstrip('/')
        if not self.base.startswith('https://'):
            raise ValueError('中心接口必须使用 HTTPS')
        directory = Path(cache_dir)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        key = hashlib.sha256((self.base + '\n' + cfg['username']).encode()).hexdigest()
        self.cache = directory / (key + '.cookies')
        self.lock = directory / (key + '.lock')
        self.backoff = directory / (key + '.retry-after')
        self.jar = http.cookiejar.LWPCookieJar(str(self.cache))
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=cfg['ca_file'])),
            urllib.request.HTTPCookieProcessor(self.jar))
        self.authenticate()

    def _save(self):
        fd, name = tempfile.mkstemp(dir=self.cache.parent)
        os.close(fd)
        try:
            self.jar.save(name, ignore_discard=True)
            os.chmod(name, 0o600)
            os.replace(name, self.cache)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def authenticate(self):
        with session_lock(self.lock):
            self.jar.clear()
            if self.cache.exists():
                try:
                    self.jar.load(ignore_discard=True)
                except (OSError, http.cookiejar.LoadError):
                    self.jar.clear()
            page = self.opener.open(self.base + '/', timeout=30).read().decode()
            match = re.search(r'name="csrf" content="([^"]+)', page)
            if not match:
                retry_at = float(self.backoff.read_text()) if self.backoff.exists() else 0
                if retry_at > time.time():
                    raise RuntimeError(f'登录限流冷却中，{int(retry_at-time.time())+1} 秒后可重试')
                csrf = re.search(r'name="csrf" value="([^"]+)', page)
                if not csrf:
                    raise RuntimeError('中心登录页面缺少 CSRF 字段')
                data = urllib.parse.urlencode(dict(csrf=csrf[1], username=self.cfg['username'],
                                                   password=self.cfg['password'])).encode()
                try:
                    page = self.opener.open(self.base + '/login', data, timeout=30).read().decode()
                except urllib.error.HTTPError as exc:
                    detail = exc.read().decode(errors='replace')
                    try:
                        detail = str(json.loads(detail).get('error', detail))
                    except (ValueError, AttributeError):
                        pass
                    if exc.code == 403 and '尝试次数过多' in detail:
                        self.backoff.write_text(str(time.time() + 300))
                        os.chmod(self.backoff, 0o600)
                    raise RuntimeError(f'中心登录失败 HTTP {exc.code}: {detail[:300]}') from exc
                match = re.search(r'name="csrf" content="([^"]+)', page)
                if not match:
                    raise RuntimeError('中心账号登录失败')
            self.csrf = match[1]
            self._save()
            self.backoff.unlink(missing_ok=True)

    def get(self, path):
        def read():
            request=urllib.request.Request(self.base+path,headers={'Accept-Encoding':'gzip'})
            with self.opener.open(request,timeout=30) as response:
                data=response.read()
                return gzip.decompress(data) if response.headers.get('Content-Encoding')=='gzip' else data
        try:return read()
        except urllib.error.HTTPError as exc:
            if exc.code!=401:raise
        self.authenticate()
        return read()

    def state(self):
        return json.loads(self.get('/api/state'))

    def download_result(self, job_id, destination, **options):
        if not re.fullmatch('[a-f0-9]{32}', job_id):
            raise ValueError('Invalid result job ID')
        from result_transfer import download
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with session_lock(destination.with_suffix('.download.lock')):
            if destination.exists():
                import zipfile
                with zipfile.ZipFile(destination) as archive:
                    if archive.testzip() is not None:
                        raise OSError('Existing result ZIP integrity check failed')
                return destination
            return download(self, '/api/jobs/' + job_id + '/result', destination, **options)

    def post(self, path, data=None):
        request = urllib.request.Request(self.base + path, json.dumps(data or {}).encode(),
            headers={'X-CSRF-Token': self.csrf, 'Content-Type': 'application/json'})
        return json.loads(self.opener.open(request, timeout=30).read())
