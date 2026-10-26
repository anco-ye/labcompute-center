"""Independent read-only telemetry. Never writes task files or controls workers."""
import argparse
import json
import ssl
import time
import urllib.request
from pathlib import Path

import psutil
import task_progress
from common import read_json_shared


def once(config):
    cfg = json.loads(Path(config).read_text(encoding='utf-8-sig'))
    root = Path(cfg['work_dir'])
    states = read_json_shared(root/'active_jobs.json')
    observations = []
    for jid, job in list(states.items())[:200]:
        folder = root/jid
        try:
            if (folder/'exit.json').exists():
                continue
            runner = read_json_shared(folder/'runner.json')
            process = psutil.Process(runner['pid'])
            if not process.is_running() or abs(process.create_time()-runner['create_time']) > .01:
                continue
            usage = read_json_shared(folder/'usage.json')
            if usage.get('phase') != 'running' or not 0 <= time.time()-usage.get('at', 0) < 20:
                continue
            progress = task_progress.collect(folder/'work', job['spec']['kind'], runner['create_time'], folder/'task.log')
            observations.append(dict(id=jid, lease=job['lease'], pid=runner['pid'], created=runner['create_time'],
                                     progress=progress, elapsed_seconds=usage.get('elapsed_seconds')))
        except (OSError, ValueError, KeyError, TypeError, psutil.Error):
            continue
    req = urllib.request.Request(cfg['controller'].rstrip('/')+'/agent/'+cfg['id']+'/progress-observations',
        data=json.dumps(dict(items=observations,sampled_at=time.time())).encode(), headers={'Authorization':'Bearer '+cfg['token'], 'Content-Type':'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=cfg['ca_file'])))
    with opener.open(req, timeout=20) as response:
        return json.loads(response.read())


if __name__ == '__main__':
    parser = argparse.ArgumentParser();parser.add_argument('--config', required=True);args = parser.parse_args()
    psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    # Excluded from the scheduler's CPU pool, like the other center services.
    psutil.Process().cpu_affinity([psutil.cpu_count()-1])
    while True:
        try:
            once(args.config)
        except Exception as error:
            print(type(error).__name__, str(error)[:150], flush=True)
        time.sleep(10)
