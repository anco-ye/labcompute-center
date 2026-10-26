"""Stable, disk-backed batch archives for sized and resumable HTTP downloads."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time
import uuid
import zipfile


class BatchArchives:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def archive(self, rows):
        rows = sorted(rows, key=lambda row: row['id'])
        identity = []
        for row in rows:
            source = Path(row['result'])
            stat = source.stat()
            identity.append([row['id'], str(source.resolve()), stat.st_size,
                             stat.st_mtime_ns, row['spec']['name'], row['status']])
        key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        target = self.directory / (key + '.zip')
        with self.lock:
            if target.exists():
                return target, key
            # Delete only expired generated archives; Windows keeps open downloads safe.
            for old in self.directory.glob('*.zip'):
                if time.time() - old.stat().st_mtime > 86400:
                    try: old.unlink()
                    except OSError: pass
            required = sum(item[2] for item in identity) + 1024**3
            if shutil.disk_usage(self.directory).free < required:
                raise OSError('批次打包空间不足，请先下载各计算单元的结果')
            partial = self.directory / (key + '.' + uuid.uuid4().hex + '.partial')
            try:
                with zipfile.ZipFile(partial, 'w', allowZip64=True) as archive:
                    for row in rows:
                        with open(row['result'], 'rb') as source, archive.open(row['id'] + '.zip', 'w', force_zip64=True) as output:
                            shutil.copyfileobj(source, output, 1024**2)
                    archive.writestr('tasks.json', json.dumps([
                        dict(id=row['id'], name=row['spec']['name'], status=row['status'])
                        for row in rows], ensure_ascii=False))
                # Results are immutable after delivery. Detect a source replaced mid-copy.
                for row, item in zip(rows, identity):
                    stat = Path(row['result']).stat()
                    if (stat.st_size, stat.st_mtime_ns) != (item[2], item[3]):
                        raise OSError('结果正在更新，请重试下载')
                os.replace(partial, target)
            finally:
                partial.unlink(missing_ok=True)
            return target, key
