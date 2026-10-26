"""Read-only operational invariants. Findings never authorize task mutations."""
PRODUCTION_NODES=('win1','win2','win3','win4','a800-ssh','dell-r940')


def evaluate(snapshot, previous=None):
    findings=[]
    def add(key,kind,severity,**evidence):
        findings.append(dict(key=key,kind=kind,severity=severity,evidence=evidence))
    at=snapshot['at']
    if previous and at-previous['at']>90*60:
        add('inspection:coverage-gap','inspection_record_gap','warning',gap_seconds=at-previous['at'])
    nodes={n['id']:n for n in snapshot.get('nodes',[])}
    for nid in PRODUCTION_NODES:
        n=nodes.get(nid)
        if n is None:
            add(nid+':missing','inspection_coverage_missing','warning',node=nid)
            continue
        if n.get('query_error'):
            add(nid+':query','inspection_query_failed','warning',node=nid,error=n['query_error'])
            continue
        age=n.get('oldest_unacknowledged_seconds',0);count=n.get('unacknowledged_leases',0)
        # A missing acknowledgement is a diagnostic trigger, never permission
        # to revoke a lease: ledger/native identity must be checked separately.
        if count and age>=600:
            add(nid+':offer-backlog','lease_delivery_gap','critical'if age>=3600 else'warning',node=nid,count=count,oldest_seconds=age,native_verification_required=True)
        if (count and age>=300 and n.get('reserved_cpu_fraction',0)>=.75
                and n.get('cpu_percent',100)<20):
            add(nid+':reservation-utilization','reservation_execution_gap','warning',node=nid,count=count,reserved_fraction=n['reserved_cpu_fraction'],cpu_percent=n['cpu_percent'])
        if n.get('layout_pending',0)and n.get('oldest_layout_seconds',0)>=600:
            add(nid+':layout-backlog','storage_preparation_gap','warning',node=nid,count=n['layout_pending'],oldest_seconds=n['oldest_layout_seconds'])
        if n.get('heartbeat_age_seconds',float('inf'))<30 and n.get('sample_age_seconds',float('inf'))>=20:
            add(nid+':sample-freshness','live_agent_stale_resource_sample','warning',node=nid,sample_age_seconds=n.get('sample_age_seconds'))
        if n.get('native_check_error'):
            add(nid+':native-coverage','inspection_native_check_incomplete','warning',node=nid,error=n['native_check_error'])
        native=snapshot.get('native',{}).get(nid,{})
        for unit in native.get('units',[]):
            limit=unit.get('RuntimeMaxUSec')
            if limit not in (None,'infinity','0'):
                add(nid+':runtime:'+unit['unit'],'scientific_wall_time_limit_present','critical',node=nid,unit=unit)
        reused=[p for p in native.get('task_processes',[])if p.get('same_identity')is False]
        if reused:
            add(nid+':identity','task_receipt_process_identity_mismatch','warning',node=nid,receipts=reused)
    http=snapshot.get('http',{})
    if http.get('error')or http.get('status')!=200:
        add('web:health','control_plane_health_failed','critical',**http)
    elif http.get('seconds',0)>5:
        add('web:latency','control_plane_latency','warning',**http)
    for change in snapshot.get('pending_changes',[]):
        if change.get('age_seconds',0)>=3600:
            add('deployment:'+change['id'],'verified_fix_pending_activation','warning',change=change)
    # Transfer state must not hide a calculation failure whose authenticated
    # runner receipt is already stored. Do not wait for the archive to arrive.
    for receipt in snapshot.get('failed_native_receipts',[]):
        add('native-failure:'+receipt['job'],'execution_failed_while_delivery_pending',
            'critical',receipt=receipt)
    # A ready archive delivers evidence; it does not turn failed execution into success.
    existing_keys={f['key']for f in findings}
    for job in snapshot.get('recent_failed_jobs',[]):
        key='native-failure:'+job['id']
        if key not in existing_keys:
            add(key,'recent_execution_failure','critical',job=job,native_verification_required=True)
            existing_keys.add(key)
    prior={f['key']:f for f in (previous or{}).get('findings',[])}
    current={f['key']:f for f in findings}
    # A single fresh snapshot must not close an intermittent fault and cause
    # the next slow response to be announced as a completely new incident.
    healthy={}
    for key,old in prior.items():
        if key in current:continue
        healthy[key]=(previous or{}).get('healthy_observations',{}).get(key,0)+1
        if healthy[key]<2:
            pending=dict(old,confirmation_pending=True)
            findings.append(pending);current[key]=pending
    new=[f for f in findings if f['key']not in prior]
    worsened=[]
    for f in findings:
        old=prior.get(f['key'])
        if not old:continue
        severity=f['severity']=='critical'and old['severity']!='critical'
        old_count=old.get('evidence',{}).get('count',0);count=f['evidence'].get('count',0)
        if severity or(old_count and count>=2*old_count):worsened.append(f)
    return dict(at=at,findings=findings,new_findings=new,worsened_findings=worsened,
                resolved_keys=sorted(set(prior)-set(current)),
                healthy_observations=healthy,
                observation_only=True,all_expected_nodes_present=all(n in nodes for n in PRODUCTION_NODES))
