"""Resolve retired observer incidents using administrator-registered handoffs.

This module never dispatches work. Call with unfiltered node telemetry and jobs
whose has_result was computed by the center from its stored result artifacts.
The administrator config is not a task-upload field or an instruction source.
"""
import hashlib
import json
import math
from pathlib import Path


def _normal(value):
    return str(value).replace('\\', '/').casefold()


def _timestamp(value, now):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 < value <= now)


def _retired_placeholder(row, entry):
    """Allow only the exact absent-file waiting_batches display placeholder."""
    if entry.get('retired_wait_placeholder') is not True:
        return False
    evidence = entry.get('retirement', {}).get('evidence', {})
    if not isinstance(evidence, dict) or evidence.get('old_runner_state_exists') is not False:
        return False
    exact = dict(kind='batch', name=entry['science_batch'], lifecycle='queued',
                 process_state='unknown', status='等待前序批次', completed=0,
                 total=len(entry['expected_identities']), percent=0,
                 running=[], source_age_seconds=None)
    if any(key not in row or row[key] != value for key, value in exact.items()):
        return False
    if any(isinstance(row[key], bool) for key in ('completed', 'total', 'percent')):
        return False
    if (row.get('pid') is not None or row.get('process_refs') not in (None, [])
            or row.get('observed_running') not in (None, False)
            or row.get('generation') or row.get('control_id') or row.get('actions')):
        return False
    # Source timestamps prove this is no longer an absent-file placeholder.
    if any(value is not None for key, value in row.items()
           if 'mtime' in key.lower() or key in ('state_observed_at', 'source_timestamp',
                                               'progress_updated_at', 'source_updated_at')):
        return False
    return True


def _evidence(entry, jobs, nodes, now):
    node_id, task = entry['node'], _normal(entry['task'])
    authorization = entry['authorization']
    if authorization.get('authority') != 'TRANSFERRED_TO_COMPUTE_CENTER':
        raise ValueError('dispatch authority was not transferred')
    if not _timestamp(authorization.get('granted_at'), now):
        raise ValueError('invalid authorization time')
    manifest_text = authorization['manifest_utf8']
    digest = hashlib.sha256(manifest_text.encode('utf-8')).hexdigest()
    if digest != authorization['manifest_sha256']:
        raise ValueError('authorized manifest hash mismatch')
    manifest = json.loads(manifest_text)
    template = authorization.get('identity_format', '{id}')
    identities = [template.format_map(t) for t in manifest['tasks']]
    expected = entry['expected_identities']
    if (not isinstance(expected, list) or not 1 <= len(expected) <= 200
            or any(not isinstance(x, str) or not x for x in expected)
            or len(set(expected)) != len(expected) or identities != expected):
        raise ValueError('expected identities do not match authorized manifest')
    retirement = entry['retirement']
    if (retirement.get('entry_disabled') is not True
            or retirement.get('generation_stopped') is not True
            or retirement.get('node') != node_id
            or _normal(retirement.get('task', '')) != task
            or not retirement.get('entry') or not retirement.get('evidence')):
        raise ValueError('missing exact old-entry retirement evidence')
    if (not _timestamp(retirement.get('observed_at'), now)
            or retirement['observed_at'] < authorization['granted_at']):
        raise ValueError('retirement evidence predates handoff')
    current = [n for n in nodes if n.get('id') == node_id]
    if len(current) != 1 or not 0 <= now-current[0].get('last_seen', 0) < 120:
        raise ValueError('old node observation is unavailable')
    sims = current[0].get('telemetry', {}).get('simulations')
    if not isinstance(sims, list):
        raise ValueError('unfiltered old-node observations required')
    old_rows = [s for s in sims if _normal(s.get('id', '')) == task]
    if len(old_rows) > 1 or any(not _retired_placeholder(s, entry) for s in old_rows):
        raise ValueError('old task reappeared in node observations')
    batch_id, science_batch = entry['batch_id'], entry['science_batch']
    matching = []
    for job in jobs:
        spec = job.get('spec', {})
        if spec.get('science_identity') not in expected:
            continue
        if (spec.get('source') != 'existing_science_queue'
                or spec.get('science_batch') != science_batch
                or job.get('batch_id') != batch_id):
            raise ValueError('identity has ambiguous central ownership')
        matching.append(job)
    # Exact batch scope prevents a partially matching replacement from closing
    # an incident belonging to a larger or differently frozen manifest.
    group = [j for j in jobs if j.get('batch_id') == batch_id
             and j.get('spec', {}).get('science_batch') == science_batch]
    if {j.get('spec', {}).get('science_identity') for j in group} != set(expected):
        raise ValueError('central batch identity set does not match handoff')
    mapped = []
    for identity in expected:
        attempts = [j for j in matching if j['spec']['science_identity'] == identity]
        if any(j.get('status') not in ('succeeded', 'failed', 'cancelled') for j in attempts):
            raise ValueError('identity has unfinished central attempts')
        successes = [j for j in attempts if j.get('status') == 'succeeded']
        if len(successes) != 1 or successes[0].get('has_result') is not True:
            raise ValueError('identity lacks a unique successful result')
        mapped.append(dict(identity=identity, job_id=successes[0]['id'],
                           attempt_ids=[j['id'] for j in attempts]))
    return dict(reason='superseded_by_verified_handoff', at=now,
                authorization=dict(authority=authorization['authority'],
                    granted_at=authorization['granted_at'], manifest_sha256=digest),
                retirement=retirement, batch_id=batch_id, science_batch=science_batch,
                mapped_jobs=mapped, old_node_observed_at=current[0]['last_seen'],
                retired_wait_placeholder=old_rows[0] if old_rows else None,
                scientific_review='not_asserted',
                detail='旧监控入口已退役，精确授权身份已转交中心且执行结果已保存；科学复核状态保持独立。')


def reconcile(c, config_path, jobs, nodes, now):
    """Update eligible issues in caller transaction; never commit or run jobs.

    Missing/invalid administrator config is fail-closed. Return bounded reasons
    for operator logs rather than creating another incident for every poll.
    """
    result = dict(resolved=[], skipped=[])
    path = Path(config_path)
    if not path.exists():
        return result
    try:
        if path.stat().st_size > 2*1024*1024:
            raise ValueError('handoff registry exceeds 2 MiB')
        config = json.loads(path.read_text(encoding='utf-8-sig'))
        entries = config['handoffs']
        if config.get('schema_version') != 1 or not isinstance(entries, list) or len(entries) > 40:
            raise ValueError('invalid handoff registry')
        keys = [(entry['node'], _normal(entry['task'])) for entry in entries]
        if len(set(keys)) != len(keys):
            raise ValueError('duplicate legacy registration')
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        result['skipped'].append(dict(reason=str(exc)[:200]))
        return result
    for entry in entries:
        try:
            node, task = entry['node'], _normal(entry['task'])
            row = c.execute("SELECT data,first_seen FROM task_issues WHERE node=? AND task=? AND state='open'",
                            (node, task)).fetchone()
            if not row:
                continue
            if (not _timestamp(entry.get('issue_first_seen'), now)
                    or entry['issue_first_seen'] != row[1]):
                raise ValueError('incident instance differs from registered retirement')
            prior = json.loads(row[0])
            if prior.get('owner') != 'center' or prior.get('code') != 'monitor_unavailable':
                raise ValueError('issue is not a retired monitor incident')
            resolution = _evidence(entry, jobs, nodes, now)
            resolution['original_issue_first_seen'] = row[1]
            prior['resolution'] = resolution
            c.execute("UPDATE task_issues SET state='resolved',resolved_at=?,last_seen=?,data=? "
                      "WHERE node=? AND task=? AND state='open'",
                      (now, now, json.dumps(prior, ensure_ascii=False), node, task))
            result['resolved'].append(dict(node=node, task=task, batch_id=entry['batch_id']))
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            result['skipped'].append(dict(node=entry.get('node') if isinstance(entry, dict) else None,
                                          reason=str(exc)[:200]))
    return result
