"""Retirement policy for a center-only dispatch cutover; results stay on disk."""
import json
from pathlib import Path

TERMINAL = {'succeeded', 'failed', 'cancelled'}

def load(config_path):
    path = Path(config_path).parent / 'management-policy.json'
    if not path.exists():
        return {}
    # A malformed policy must not silently reopen retired dispatch.
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if data.get('schema_version') != 1:
        raise ValueError('Unsupported management policy')
    return data

def archived(job, policy):
    explicit = job.get('id') in policy.get('archived_job_ids', [])
    stopped = job['status'] in TERMINAL or (job['status'] == 'uploading' and job.get('execution_stopped'))
    return bool((explicit and stopped) or (job['status'] in TERMINAL and job['created'] <= policy.get('archive_before', 0)))

def filter_legacy(telemetry, node, policy):
    if node not in policy.get('retired_nodes', []):
        return
    # Never hide evidence of an unexpected new external execution.
    telemetry['simulations'] = [s for s in telemetry.get('simulations', [])
        if s.get('observed_running') or s.get('process_state') == 'running'
        or (s.get('activity', {}).get('working_processes') or 0) > 0]
    telemetry.pop('controller_observation', None)
    telemetry.pop('terminal_evidence', None)
