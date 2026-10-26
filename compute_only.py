"""Disable explicit scientific review stages without judging calculation outputs."""
import json
import zipfile
from functools import lru_cache
from pathlib import Path

REVIEW_STAGES = frozenset({
    'independent_pair_source_and_trace_review',
    'independent_short_source_and_numerical_review',
    'independent_existing_short_review',
    'independent_long_numeric_and_reference_review',
    'three_condition_summary',
    'scientific_review', 'scientific_acceptance',
})


def declared_review(spec):
    return next((spec.get(k) for k in ('stage', 'purpose', 'execution_role')
                 if spec.get(k) in REVIEW_STAGES), None)


@lru_cache(maxsize=256)
def _stages(path, size, modified):
    with zipfile.ZipFile(path) as archive:
        if 'task_table.json' not in archive.namelist():
            return {}
        if archive.getinfo('task_table.json').file_size > 4 * 1024**2:
            return {}
        try:
            table = json.loads(archive.read('task_table.json'))
        except (ValueError, UnicodeError):
            return {}
        return {key: declared_review(value) for key, value in table.items()
                if isinstance(value, dict)} if isinstance(table, dict) else {}


def review_stage(package, spec):
    direct = declared_review(spec)
    if direct:
        return direct
    try:
        path = Path(package); stat = path.stat()
    except OSError:
        return None
    stages = _stages(str(path), stat.st_size, stat.st_mtime_ns)
    keys = [spec.get('task_key', spec.get('id'))]
    args = spec.get('args', [])
    for i, arg in enumerate(args):
        if arg == '--task' and i + 1 < len(args):
            keys.append(args[i + 1])
        elif arg.startswith('--task='):
            keys.append(arg.split('=', 1)[1])
    return next((stages[key] for key in keys if stages.get(key)), None)


def require_computation(package, spec):
    stage = review_stage(package, spec)
    if stage:
        raise ValueError('Scientific review is disabled; computation-only center: ' + stage)
