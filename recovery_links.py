"""Administrator-owned legacy-to-center execution links; no dispatch or science gates."""
import json
from pathlib import Path


def normal(value):
    return str(value).replace('\\', '/').casefold()


def load(path):
    path = Path(path)
    if not path.exists():
        return []
    try:
        if path.stat().st_size > 2*1024*1024:return []
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        if data.get('schema_version') != 1 or not isinstance(data.get('links'),list) or len(data['links']) > 200:return []
        valid=[]
        for x in data['links']:
            if (not isinstance(x,dict) or not all(isinstance(x.get(k),str) and x[k] for k in ('node','task','batch_id','source_sha256'))
                    or not isinstance(x.get('snapshot'),dict) or not isinstance(x.get('task_keys'),list)
                    or not x['task_keys'] or any(not isinstance(k,str) or not k for k in x['task_keys'])):continue
            valid.append(x)
        return valid
    except (OSError,ValueError,TypeError,AttributeError):
        return []



def stopped_matches(sim, link):
    if normal(sim.get('id')) != normal(link['task']):
        return False
    if sim.get('observed_running') or sim.get('pid') or sim.get('process_refs') or sim.get('process_state') == 'running':
        return False
    return all(sim.get(k) == v for k, v in link['snapshot'].items())


def reconcile(c, path, jobs, nodes, now):
    mapped = {}
    for link in load(path):
        if not (link.get('entry_disabled') is True and link.get('generation_stopped') is True
                and link.get('retirement_evidence') and len(link.get('source_sha256','')) == 64):
            continue
        node = next((n for n in nodes if n['id'] == link['node']), None)
        if not node or not 0 <= now-node['last_seen'] < 120:
            continue
        old = [s for s in node['telemetry'].get('simulations',[]) if normal(s['id']) == normal(link['task'])]
        if any(not stopped_matches(s, link) for s in old):
            continue
        attempts = [j for j in jobs if j['batch_id'] == link['batch_id'] and j['spec'].get('task_key') in link['task_keys']]
        if {j['spec'].get('task_key') for j in attempts} != set(link['task_keys']):
            continue
        latest = [max((j for j in attempts if j['spec'].get('task_key') == k),key=lambda j:j.get('created',0)) for k in link['task_keys']]
        success = all(j['status']=='succeeded' and j.get('has_result') for j in latest)
        resolution = dict(reason='replaced_by_center_execution', batch_id=link['batch_id'],
            job_ids=[j['id'] for j in latest], at=now, completed=success,
            source_sha256=link['source_sha256'], retirement_evidence=link['retirement_evidence'], snapshot=link['snapshot'], task=link['task'])
        key=(link['node'], normal(link['task']));mapped[key]=resolution
        if success:
            row=c.execute('SELECT data FROM task_issues WHERE node=? AND task=?',key).fetchone()
            if row:
                data=json.loads(row[0]);data['resolution']=resolution
                c.execute("UPDATE task_issues SET state='resolved',resolved_at=COALESCE(resolved_at,?),last_seen=?,data=? WHERE node=? AND task=?",
                          (now,now,json.dumps(data,ensure_ascii=False),*key))
    return mapped
