"""Platform-only 12-hour memory reservation and process-peak audit."""
import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

CENTER=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(CENTER))
from client_session import CenterClient


def positive(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>0


def inspect(state, previous=None, max_ratio=1.5):
    previous=previous or {};old={r['id']:r for r in previous.get('running',[])}
    nodes={n['id']:n for n in state['nodes']};running=[];completed=[]
    cutoff=previous.get('at',state['now']-48*3600)
    for job in state['jobs']:
        spec=job['spec'];status=job['status']
        if status in ('running','leased','preparing') and not job.get('execution_stopped'):
            hold=job.get('scheduling_reservation');node=nodes.get(job.get('node'),{})
            usage=node.get('telemetry',{}).get('usages',{}).get(job['id'],{})
            cap=(hold or {}).get('peak_headroom_cap')
            peak=usage.get('peak_gb');reference=cap.get('reference_peak_gb') if cap else None
            ratio=hold['memory_gb']/peak if hold and positive(peak) else None
            reference_ratio=hold['memory_gb']/reference if hold and positive(reference) else None
            # A live low-water RSS is never a complete-run peak.
            verdict=('confirmed_over_limit' if reference_ratio is not None and reference_ratio>max_ratio else
                     'within_completed_peak_limit' if reference_ratio is not None else
                     'provisional_over_ratio' if ratio is not None and ratio>max_ratio else
                     'awaiting_complete_peak')
            running.append(dict(id=job['id'],name=spec['name'],node=job.get('node'),status=status,
                execution_limit_memory_gb=spec['memory_gb'],execution_limit_commit_gb=spec.get('commit_gb'),
                reservation_memory_gb=(hold or {}).get('memory_gb'),reservation_commit_gb=(hold or {}).get('commit_gb'),
                peak_so_far_gb=peak,peak_private_so_far_gb=usage.get('peak_private_gb'),
                observed_ratio=ratio,completed_reference_ratio=reference_ratio,verdict=verdict,
                reference_sample_ids=(cap or {}).get('sample_ids',[])))
        elif status=='succeeded' and (job.get('finished') or 0)>=cutoff:
            ob=spec.get('resource_observation',{});peak=ob.get('peak_gb')
            if ob.get('cleanup_verified') is not True or not positive(peak):continue
            if ob.get('peak_complete') is False or (job.get('memory_allocation') is not None and not job['memory_allocation'].get('peak_complete')):
                completed.append(dict(id=job['id'],name=spec['name'],finished=job['finished'],sampled_peak_gb=peak,peak_complete=False,execution_limit_memory_gb=spec['memory_gb'],verdict='awaiting_complete_peak'))
                continue
            prior=old.get(job['id'],{}).get('reservation_memory_gb')
            completed.append(dict(id=job['id'],name=spec['name'],finished=job['finished'],peak_gb=peak,
                peak_private_gb=ob.get('peak_private_gb'),execution_limit_memory_gb=spec['memory_gb'],
                execution_limit_to_final_peak=spec['memory_gb']/peak,
                last_observed_reservation_gb=prior,
                last_observed_reservation_to_final_peak=prior/peak if positive(prior) else None,
                reservation_history='last_12h_sample_only' if positive(prior) else 'not_recorded',
                verdict='observed_reservation_over_limit' if positive(prior) and prior/peak>max_ratio else
                        'historical_execution_limit_only' if spec['memory_gb']/peak>max_ratio else 'within_limit'))
    issues=[dict(id=r['id'],kind=r['verdict']) for r in running+completed
            if r['verdict'] in ('confirmed_over_limit','observed_reservation_over_limit','provisional_over_ratio')]
    key=hashlib.sha256(json.dumps(sorted((x['id'],x['kind']) for x in issues)).encode()).hexdigest() if issues else None
    return dict(at=state['now'],max_ratio=max_ratio,running=running,completed=completed,issues=issues,
                notification_key=key,notify=bool(key and key!=previous.get('notified_key')),
                science_review='disabled',scope='Scheduling holds and OS process-tree peaks only; execution limits remain unchanged.')


def run():
    parser=argparse.ArgumentParser();parser.add_argument('--if-due',action='store_true');args=parser.parse_args()
    out=CENTER/'.private/memory-reservation-12h';out.mkdir(exist_ok=True)
    path=out/'latest.json';previous=json.loads(path.read_text()) if path.exists() else {}
    if args.if_due and time.time()-previous.get('at',0)<12*3600:
        print(json.dumps(dict(skipped='not_due',next_due=previous['at']+12*3600)));return
    guard=json.loads((CENTER/'.private/short-review-compat-20261017/in-progress.json').read_text())
    client=CenterClient(dict(server='https://100.100.0.6:8443',ca_file=str(CENTER/'access/LabCompute-CA.cer'),username='lab',
        password=json.loads((CENTER/'.private/secrets.json').read_text())['password']),cache_dir=CENTER/'.private/client-sessions')
    state=client.state();result=inspect(state,previous);result['front_repair_active']=guard.get('active') is True
    result['notified_key']=previous.get('notified_key');result['next_due']=result['at']+12*3600
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    (out/('audit-'+str(int(result['at']))+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(dict(at=result['at'],max_ratio=result['max_ratio'],running=len(result['running']),completed=len(result['completed']),
        issues=result['issues'],notification_key=result['notification_key'],notify=result['notify'],next_due=result['next_due']),ensure_ascii=False))


if __name__=='__main__':run()
