"""Bounded, read-only progress adapters; application stages are never task ETA."""
import json
import math
import re
from pathlib import Path


def count(value):
    try:valid=not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value) and value>=0
    except OverflowError:valid=False
    if not valid:
        raise ValueError('Invalid progress number')
    return value


def normalize(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid progress')
    result = {}
    if 'completed' in value or 'total' in value:
        done, total = count(value['completed']), count(value['total'])
        if total <= 0 or done > total:
            raise ValueError('Invalid progress range')
        result.update(completed=done, total=total)
    for key in ('stage', 'unit'):
        if key in value:
            if not isinstance(value[key], str) or len(value[key]) > 120:
                raise ValueError('Invalid progress label')
            result[key] = value[key]
    if 'scope' in value:
        if value['scope'] not in ('overall', 'stage'):
            raise ValueError('Invalid progress scope')
        result['scope'] = value['scope']
    metrics = value.get('metrics', [])
    if not isinstance(metrics, list) or len(metrics) > 8:
        raise ValueError('Invalid metrics')
    if metrics:
        result['metrics'] = []
        for metric in metrics:
            if not isinstance(metric,dict):raise ValueError('Invalid metric')
            name = metric.get('name')
            if not isinstance(name, str) or not 1 <= len(name) <= 80:
                raise ValueError('Invalid metric name')
            row = normalize({k: v for k, v in metric.items() if k in ('completed', 'total', 'unit')})
            if 'completed' not in row:
                raise ValueError('Metric requires an actual count and goal')
            result['metrics'].append(dict(row, name=name))
    if not result:
        raise ValueError('Empty progress')
    return result


def safe_file(work, relative):
    root = Path(work).resolve()
    path = Path(work)/relative
    # Never follow task-created links into another job or reference directory.
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('Progress path escape')
    return path


def tail(path, limit=65536):
    with path.open('rb') as stream:
        size = path.stat().st_size
        stream.seek(max(0, size-limit))
        payload = stream.read(limit)
    if b'\x00' in payload:
        return payload.decode('utf-16-le', errors='replace')
    return payload.decode('utf-8-sig', errors='replace')


def collect(work, kind, started, task_log=None):
    """Prefer explicit goals; inspect only known files from this execution."""
    root = Path(work)
    try:
        path = safe_file(root, 'progress.json')
        if path.stat().st_mtime >= started and path.stat().st_size <= 4096:
            value = normalize(json.loads(path.read_text(encoding='utf-8-sig')))
            return dict(value, source='progress.json', updated_at=path.stat().st_mtime)
    except (OSError, ValueError, TypeError, KeyError):
        pass
    text = ''
    try:
        if task_log is not None and Path(task_log).stat().st_mtime >= started:
            text = tail(Path(task_log))
            # Environment preparation logs must not become scientific progress.
            if '[running]' not in text and Path(task_log).stat().st_size < 65536:
                text = ''
            elif '[running]' in text:
                text = text.rsplit('[running]', 1)[1]
            for line in reversed(text.splitlines()):
                if line.startswith('LAB_PROGRESS '):
                    try:
                        return dict(normalize(json.loads(line[13:])), source='stdout', updated_at=Path(task_log).stat().st_mtime)
                    except (ValueError, TypeError, KeyError):
                        continue
    except (OSError, ValueError):
        pass
    if kind == 'comsol':
        try:
            path = safe_file(root, 'comsol_batch.log')
            if path.stat().st_mtime < started:
                return {}
            matches = list(re.finditer(r'Current Progress:\s*(\d+(?:\.\d+)?)\s*%\s*-\s*([^\r\n]{1,160})', tail(path)))
            if matches:
                match = matches[-1]
                if not 0<=float(match[1])<=100:return {}
                label = match[2].strip()
                names = {'Assembling matrices':'组装矩阵', 'Assembling residual':'组装残差',
                         'Assembling sparsity pattern':'组装稀疏结构', 'Matrix factorization':'矩阵分解',
                         'Solving linear system':'求解线性系统'}
                return dict(completed=float(match[1]), total=100, scope='stage',
                            stage=names.get(label, label)[:120], source='comsol_batch.log', updated_at=path.stat().st_mtime)
        except (OSError, ValueError):
            pass
    return {}


def apply(job, progress, now, observed_at=None):
    """Only fresh overall counts can drive a proportional task ETA."""
    if not progress or job['status'] not in ('running', 'paused', 'pause_requested'):
        return
    try:
        value = normalize(progress)
    except (ValueError, KeyError, TypeError):
        return
    updated = progress.get('updated_at', observed_at)
    fresh = (isinstance(updated, (int, float)) and not isinstance(updated,bool) and 0 <= now-updated < 120
             and (observed_at is None or 0 <= now-observed_at < 35))
    job.update(progress=value, progress_fresh=fresh, progress_source=progress.get('source', 'task'))
    if isinstance(updated,(int,float)) and not isinstance(updated,bool) and math.isfinite(updated):
        job['progress_updated_at']=updated
    if fresh and value.get('scope', 'overall') == 'overall' and value.get('total', 0) > 0:
        job['progress_percent'] = value['completed']/value['total']*100
    elif value.get('scope')=='stage' and value.get('total', 0) > 0:
        job['stage_progress_percent'] = value['completed']/value['total']*100
    elif value.get('total',0)>0:
        job['last_progress_percent']=value['completed']/value['total']*100
