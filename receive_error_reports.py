"""Pull center-generated task reports into the submitting agent's local inbox.

Uses the same protected connection config as submit_batch.py. Run once or with
--watch; the submitting agent consumes the JSON files as data, not instructions.
Receipt does not resolve a problem. Only successful recovery evidence does.
"""
import argparse
import json
from pathlib import Path
import re
import time
from common import atomic_json
from client_session import CenterClient


def receive(config, inbox, batch_ids=None, client=None):
    client=client or CenterClient(config)
    data=json.loads(client.get('/api/issues/reports?owner=publisher'))
    inbox=Path(inbox);inbox.mkdir(parents=True,exist_ok=True)
    reports=[r for r in data['reports'] if not batch_ids or r.get('evidence',{}).get('batch_id') in batch_ids]
    for report in reports:
        key=report['report_id']
        if not re.fullmatch('[a-f0-9]{24}',key):raise ValueError('无效报告编号')
        atomic_json(inbox/(key+'.json'),dict(report,state='open',received_at=time.time()))
    # Keep old evidence, explicitly mark reports that the center has closed.
    active={r['report_id'] for r in reports}
    for path in inbox.glob('*.json'):
        old=json.loads(path.read_text(encoding='utf-8'))
        if old.get('report_id') and old['report_id'] not in active and old.get('state')=='open':
            # A publisher report may have moved back to center investigation.
            old.update(state='no_longer_in_publisher_inbox',updated_at=time.time())
            atomic_json(path,old)
    return len(reports)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True);p.add_argument('--inbox',required=True)
    p.add_argument('--batch',action='append');p.add_argument('--watch',action='store_true')
    a=p.parse_args()
    client=None
    while True:
        try:
            client=client or CenterClient(a.config)
            print('publisher_reports',receive(a.config,a.inbox,a.batch,client),flush=True)
        except Exception as exc:
            if not a.watch:raise
            print('报告领取失败，将重试：'+type(exc).__name__,flush=True)
        if not a.watch:break
        time.sleep(60)
