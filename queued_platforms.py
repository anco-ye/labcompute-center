"""Explicit platform changes for inspected, never-started COMSOL queue entries.

This maintenance operation keeps the submitted archive immutable. The caller must
verify model/dependency compatibility before applying its execution-spec overlay.
"""
import json
import re
import time
from copy import deepcopy
import platform_contract


def linux_comsol_spec(row, *, minimum_version, target=None):
    if (row['status'] != 'queued' or row.get('node') or row.get('lease')
            or row.get('started') is not None or row.get('compute_started')
            or row.get('finished') is not None or row.get('result')
            or row.get('execution_stopped') or row.get('control', 'run') != 'run'):
        raise ValueError('Only never-started, unleased queued work can be adapted')
    original = json.loads(row['spec'])
    if (original.get('kind') != 'comsol' or not original.get('entry', '').lower().endswith('.mph')
            or original.get('args') or original.get('requirements')
            or original.get('runtime_revision') or original.get('resource_calibration')):
        raise ValueError('Requires an inspected native MPH entry without prior execution calibration')
    if not re.fullmatch(r'\d+(?:\.\d+){0,3}', minimum_version):
        raise ValueError('Explicit numeric COMSOL minimum version required')
    if target is not None and not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', target):
        raise ValueError('Invalid target node')
    revised = deepcopy(original)
    # This task now has one Linux variant; future uploaded portable packages may
    # declare both platforms. Profiles remain distinct from Windows histories.
    revised['platforms'] = {'linux': dict(entry=original['entry'], runtime='comsol-linux',
                                         args=[], requirements=None,
                                         min_application_version=minimum_version)}
    revised = platform_contract.resolve(revised, 'linux')
    if target is not None:
        revised['target'] = target
    return revised


def apply(connection, job_id, *, expected_spec, minimum_version, target=None, now=None):
    """Apply inside the caller's BEGIN IMMEDIATE; refuse stale inspection evidence."""
    if not connection.in_transaction:
        raise ValueError('BEGIN IMMEDIATE transaction required')
    cursor = connection.execute('SELECT * FROM jobs WHERE id=?', (job_id,))
    record = cursor.fetchone()
    if record is None:
        raise ValueError('Unknown queued job')
    row = dict(zip([c[0] for c in cursor.description], record))
    if row['spec'] != expected_spec:
        raise ValueError('Task changed after compatibility inspection')
    revised = linux_comsol_spec(row, minimum_version=minimum_version, target=target)
    changed = connection.execute(
        "UPDATE jobs SET spec=?,updated=? WHERE id=? AND status='queued' AND spec=? "
        "AND node IS NULL AND lease IS NULL AND started IS NULL AND compute_started=0",
        (json.dumps(revised, ensure_ascii=False), time.time() if now is None else now,
         job_id, expected_spec)).rowcount
    if changed != 1:
        raise ValueError('Task was claimed or changed; no platform adaptation applied')
    return revised
