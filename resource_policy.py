"""Deterministic estimated-memory admission and bounded clean-restart recovery."""
import math
import json
from functools import lru_cache


def padded(value):
    return math.ceil((value*1.3+.5)*4)/4


def configure(spec, resources, restart):
    mode=resources.get('mode','fixed')
    if mode not in ('fixed','estimated'):raise ValueError('resources.mode 必须为 fixed 或 estimated')
    if restart not in ('never','from_start'):raise ValueError('restart_policy 必须为 never 或 from_start')
    basis=resources.get('memory_basis','peak' if mode=='estimated' else 'budget')
    if basis not in ('peak','budget'):raise ValueError('resources.memory_basis 必须为 peak 或 budget')
    if mode=='fixed' and basis!='budget':raise ValueError('fixed 模式的 memory_basis 必须为 budget')
    prior=spec.get('resource_budget',{})
    # Read the raw declaration, never feed a previously padded limit back in.
    raw={k:resources.get(k,prior.get('declared',{}).get(k,spec[k])) for k in ('memory_gb','commit_gb')}
    if (prior.get('mode')==mode and prior.get('basis')==basis and prior.get('declared')==raw
            and spec.get('restart_policy')==restart):return spec
    spec.update(resource_mode=mode,restart_policy=restart)
    add_margin=mode=='estimated' and basis=='peak'
    spec['memory_gb']=padded(raw['memory_gb']) if add_margin else raw['memory_gb']
    spec['commit_gb']=max(spec['memory_gb'],padded(raw['commit_gb']) if add_margin else raw['commit_gb'])
    if mode=='estimated':spec['resource_estimate']=dict(raw)
    else:spec.pop('resource_estimate',None)
    spec['resource_budget']=dict(mode=mode,basis=basis,declared=raw,
        factor=1.3 if add_margin else 1,additive_gb=.5 if add_margin else 0,
        rounding_gb=.25 if add_margin else 0,
        initial_limits={k:spec[k] for k in ('memory_gb','commit_gb')},
        policy='initial_headroom_once_v1')
    return spec


def telemetry_write_failure(report):
    """Recognize an old supervisor's exact platform telemetry replacement error."""
    import re
    from pathlib import PureWindowsPath
    if report.get('exit_code')!=-1 or report.get('cleanup_verified') is not True:return False
    match=re.fullmatch(r"\[WinError (?:5|32|33)\][^\r\n]*?: '(?P<src>[^'\r\n]+)' -> '(?P<dst>[^'\r\n]+)'",str(report.get('detail','')))
    if not match:return False
    src=PureWindowsPath(match['src']);dst=PureWindowsPath(match['dst'])
    return bool(dst.name in ('usage.json','process_tree.json') and re.fullmatch(re.escape(dst.name)+r'\.[a-f0-9]{32}\.tmp',src.name) and re.fullmatch('[a-f0-9]{32}',src.parent.name) and src.with_name(dst.name)==dst)


def recovery(spec, report, policy=None):
    """Called only after final result receipt and verified process-tree cleanup."""
    policy=policy or {}
    code=report.get('failure_code');evidence=report.get('failure_evidence',{})
    safe_setup=code=='isolation_setup' or (code=='environment_setup' and evidence.get('phase')=='preparing')
    safe_restart=(code=='runner_interrupted' or telemetry_write_failure(report)) and spec.get('restart_policy')=='from_start'
    if (safe_setup or safe_restart) and report.get('cleanup_verified') is True:
        count=spec.get('execution_retries',0)
        if count>=policy.get('max_execution_retries',2):return None
        revised=dict(spec,execution_retries=count+1,retry_origin='automatic')
        for key in ('cpu_ids','gpu_ids','deadline','backfill_deadline','dependency_jobs','work_volume'):revised.pop(key,None)
        return revised
    if spec.get('resource_mode')!='estimated' or spec.get('restart_policy')!='from_start':return None
    evidence=report.get('failure_evidence',{})
    if (report.get('failure_code')!='resource_limit' or evidence.get('phase')!='running'
            or evidence.get('cleanup_verified') is not True
            or evidence.get('limit') not in ('超过任务内存上限','超过任务提交内存上限')):return None
    count=spec.get('resource_retries',0)
    if count>=policy.get('max_retries',2):return None
    peaks=[evidence.get('peak_gb'),evidence.get('peak_private_gb')]
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 for v in peaks):return None
    s=dict(spec)
    ram=max(s['memory_gb'],padded(peaks[0]));commit=max(s['commit_gb'],padded(peaks[1]),ram)
    kernel_peak=evidence.get('kernel_peak_commit_gb')
    if isinstance(kernel_peak,(int,float)) and not isinstance(kernel_peak,bool) and math.isfinite(kernel_peak) and kernel_peak>0:
        ram=max(ram,padded(kernel_peak));commit=max(commit,ram)
    learned=s.get('resource_calibration',{}).get('method') in ('completed_scale_envelope','completed_identical_inputs')
    if learned:
        if evidence['limit']=='超过任务内存上限':ram=max(ram,s['memory_gb']*1.5)
        else:commit=max(commit,s['commit_gb']*1.5)
    elif not (count==0 and s.get('resource_calibration')):
        if evidence['limit']=='超过任务内存上限':ram=max(ram,s['memory_gb']*2)
        else:commit=max(commit,s['commit_gb']*2)
    if count==0 and s.get('resource_calibration') and not learned:
        ram=max(ram,s['resource_calibration']['original_memory_gb']);commit=max(commit,s['resource_calibration']['original_commit_gb'])
    commit=max(commit,ram)
    if ram>policy.get('max_memory_gb',64) or commit>policy.get('max_commit_gb',128):return None
    s.update(memory_gb=ram,commit_gb=commit,resource_retries=count+1,retry_origin='automatic')
    for key in ('cpu_ids','gpu_ids','deadline','backfill_deadline','dependency_jobs','work_volume'):s.pop(key,None)
    return s


def final_observation(spec, result):
    """Full successful-run resource evidence, read from native exit metadata only."""
    from pathlib import Path
    if spec.get('execution_validation',{}).get('failure_code'):return None
    try:
        path=Path(result);stat=path.stat()
        return json.loads(_final_observation_cached(str(path),stat.st_size,stat.st_mtime_ns,not str(spec.get('runtime','')).endswith('-linux')))
    except (OSError,ValueError,TypeError):return None


def observed_memory(result):
    """Display-only native peaks, including stopped/failed runs; never a learning sample."""
    from pathlib import Path
    try:
        path=Path(result);stat=path.stat()
        return json.loads(_observed_memory_cached(str(path),stat.st_size,stat.st_mtime_ns))
    except (OSError,ValueError,TypeError):return {}


@lru_cache(maxsize=2048)
def _observed_memory_cached(path,size,modified_ns):
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            if z.getinfo('exit.json').file_size>16384:return '{}'
            report=json.loads(z.read('exit.json'))
        return json.dumps({key:value for key in ('peak_gb','peak_private_gb')
                           if isinstance(value:=report.get(key),(int,float)) and not isinstance(value,bool)
                           and math.isfinite(value) and value>=0})
    except (OSError,ValueError,KeyError,zipfile.BadZipFile):return '{}'


@lru_cache(maxsize=2048)
def _final_observation_cached(path, size, modified_ns, legacy_windows):
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            if z.getinfo('exit.json').file_size>16384:return 'null'
            report=json.loads(z.read('exit.json'))
        peaks=[report.get(k) for k in ('peak_gb','peak_private_gb')]
        if (type(report.get('exit_code')) is not int or report.get('exit_code')!=0 or report.get('cleanup_verified') is not True
                or report.get('peak_complete') is False
                or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0 for v in peaks)):return 'null'
        # Older Windows supervisors omitted peak_complete, but recorded the
        # completed process-tree peaks and lifecycle timestamps at normal exit.
        # This remains an observed full-run peak, not a mathematical maximum.
        kernel=report.get('kernel_memory')
        lifetime=[report.get(k) for k in ('started','finished','elapsed_seconds')]
        native_legacy=bool(legacy_windows and report.get('status')=='succeeded'
            and all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and v>0 for v in lifetime)
            and lifetime[1]>lifetime[0] and lifetime[2]<=lifetime[1]-lifetime[0])
        if report.get('peak_complete') is not True and not native_legacy and (not isinstance(kernel,dict) or kernel.get('verified') is not True):return 'null'
        return json.dumps(dict(cleanup_verified=True,peak_gb=peaks[0],peak_private_gb=peaks[1],peak_complete=True,
            memory_measurement=report.get('memory_measurement') or 'native_final_run_process_tree_observation'))
    except (OSError,ValueError,KeyError,zipfile.BadZipFile):return 'null'


def calibrate(spec, observations):
    """Lower repeat estimates only from complete identical-input executions.

    Caller binds owner, package/unit and runtime revision. One complete native
    observation is sufficient; no duplicate execution is needed. A limit hit restores
    the original declaration before further expansion; no running job is shrunk.
    """
    if spec.get('resource_mode')!='estimated' or spec.get('restart_policy')!='from_start' or spec.get('depends_on') or spec.get('resource_retries',0):return spec
    valid=[]
    for row in observations:
        values=[row.get('peak_gb'),row.get('peak_private_gb')]
        if row.get('peak_complete') is not False and row.get('cleanup_verified') is True and all(isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x) and x>0 for x in values):valid.append(values)
    if not valid:return spec
    ram=max(.25,max(x[0] for x in valid)*1.5)
    commit=max(ram,max(x[1] for x in valid)*1.5)
    revised=dict(spec)
    if ram<spec['memory_gb']*.75:revised['memory_gb']=ram
    if commit<spec['commit_gb']*.75:revised['commit_gb']=max(revised['memory_gb'],commit)
    if revised!=spec:revised['resource_calibration']=dict(method='completed_identical_inputs',samples=len(valid),original_memory_gb=spec['memory_gb'],original_commit_gb=spec['commit_gb'],safety_factor=1.5,additive_gb=0)
    return revised


def workload_model(value, code_family):
    """Strict numeric metadata, never executable estimation instructions."""
    import hashlib,json,re
    if value is None:return None
    if not isinstance(value,dict) or set(value)-{'family','features'}:raise ValueError('resources.workload 仅支持 family、features')
    family=value.get('family');features=value.get('features')
    if not isinstance(family,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}',family):raise ValueError('workload.family 格式无效')
    if not isinstance(features,dict) or not 1<=len(features)<=8:raise ValueError('workload.features 需要1–8个规模计数')
    for key,n in features.items():
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_]{1,40}',key) or isinstance(n,bool) or not isinstance(n,(int,float)) or not math.isfinite(n) or n<0 or n>1e18:raise ValueError('workload.features 必须为非负有限规模计数')
    return dict(family=hashlib.sha256(json.dumps([code_family,family,sorted(features)]).encode()).hexdigest(),features=features,label=family)


def fit_completed(spec, samples, safety_factor=1.6, additive_gb=.75):
    # Cache immutable model inputs, never live telemetry or execution decisions.
    return json.loads(_fit_cached(json.dumps(spec,sort_keys=True),json.dumps(samples,sort_keys=True),safety_factor,additive_gb))


@lru_cache(maxsize=256)
def _fit_cached(spec_json,samples_json,safety_factor,additive_gb):
    return json.dumps(_fit_completed(json.loads(spec_json),json.loads(samples_json),safety_factor,additive_gb))


def _fit_completed(spec, samples, safety_factor=1.6, additive_gb=.75):
    """Conservative scale envelope from complete successful execution trees."""
    if spec.get('resource_mode')!='estimated' or spec.get('restart_policy')!='from_start' or spec.get('resource_retries',0):return spec
    model=spec.get('resource_model');valid=[]
    if not model:return spec
    cap=completed_peak_cap(model,samples,dict(rss_gb=0,private_gb=0,peak_gb=0,peak_private_gb=0))
    if cap:
        original=spec.get('resource_calibration',{})
        ram=original.get('original_memory_gb',spec['memory_gb'])
        commit=original.get('original_commit_gb',spec.get('commit_gb',spec['memory_gb']))
        memory=min(ram,cap['memory_gb']);private=min(commit,max(memory,cap['commit_gb']))
        if memory>=ram and private>=commit:return spec
        return dict(spec,memory_gb=memory,commit_gb=private,resource_calibration=dict(
            method='completed_peak_cap',original_memory_gb=ram,original_commit_gb=commit,
            safety_factor=1.5,additive_gb=0,samples=len(cap['sample_ids']),sample_ids=cap['sample_ids'],
            reference_peak_gb=cap['reference_peak_gb'],reference_peak_private_gb=cap['reference_peak_private_gb']))
    if model.get('allocation_contract') in ('buffered-linear-v1','sequential-fixed-workspace-v1'):
        import resource_envelope
        if model['allocation_contract']=='buffered-linear-v1':
            fitted=resource_envelope.completed_envelope(model,samples)
        elif spec.get('kernel_memory_required'):
            fitted=dict(memory_gb=model.get('static_memory_gb'),commit_gb=model.get('static_commit_gb'),
                        method='verified_workspace_bound',samples=0,sample_ids=[],features=model['features'],
                        safety_factor=None,additive_gb=None)
        else:return spec
        if not fitted:return spec
        if any(not resource_envelope.finite(fitted[k]) or fitted[k]<=0 for k in ('memory_gb','commit_gb')):return spec
        original=spec.get('resource_calibration',{})
        ram=original.get('original_memory_gb',spec['memory_gb']);commit=original.get('original_commit_gb',spec.get('commit_gb',spec['memory_gb']))
        memory=min(ram,fitted['memory_gb']);private=min(commit,max(memory,fitted['commit_gb']))
        if memory>=ram*.85 and private>=commit*.85:return spec
        return dict(spec,memory_gb=memory,commit_gb=private,resource_calibration=dict(fitted,
                    original_memory_gb=ram,original_commit_gb=commit,shape_binding_sha256=model.get('shape_binding_sha256')))
    for row in samples:
        other=row.get('model',{});o=row.get('observation',{})
        if other.get('family')!=model['family'] or set(other.get('features',{}))!=set(model['features']):continue
        if model.get('input_binding_sha256') and (other.get('input_binding_sha256')!=model['input_binding_sha256']
                or other.get('growth_bound')!=model.get('growth_bound')
                or other.get('features')!=model['features']):continue
        values=[o.get('peak_gb'),o.get('peak_private_gb')]
        if o.get('peak_complete') is False or o.get('cleanup_verified') is not True or any(isinstance(x,bool) or not isinstance(x,(float,int)) or not math.isfinite(x) or x<=0 for x in values):continue
        ratios=[]
        for key,target in model['features'].items():
            source=other['features'][key]
            if source==0 and target>0:break
            ratios.append(target/source if source else 1)
        else:
            factor=max(ratios)
            # Do not extrapolate far beyond a single completed case.
            if factor<=2:valid.append((max(1,factor),values,row.get('id')))
    if not valid:return spec
    overhead=model.get('additional_memory_gb',0)
    if isinstance(overhead,bool) or not isinstance(overhead,(int,float)) or not math.isfinite(overhead) or overhead<0:return spec
    peaks=[max(factor*v[i] for factor,v,_ in valid) for i in (0,1)]
    ram=max(1,math.ceil((peaks[0]*safety_factor+additive_gb+overhead)*4)/4)
    commit=max(ram,math.ceil((peaks[1]*safety_factor+additive_gb+overhead)*4)/4)
    calibration=spec.get('resource_calibration',{})
    original_ram=calibration.get('original_memory_gb',spec['memory_gb'])
    original_commit=calibration.get('original_commit_gb',spec.get('commit_gb',spec['memory_gb']))
    ram=min(original_ram,ram);commit=min(original_commit,max(ram,commit))
    if ram>=original_ram*.85 and commit>=original_commit*.85:return spec
    return dict(spec,memory_gb=ram,commit_gb=commit,resource_calibration=dict(
        method='completed_scale_envelope',samples=len(valid),sample_ids=[x[2] for x in valid],
        original_memory_gb=original_ram,original_commit_gb=original_commit,
        features=model['features'],safety_factor=safety_factor,additive_gb=additive_gb,retained_allowance_gb=overhead))


def completed_peak_cap(model, samples, usage, ratio=1.5):
    """Cap a scheduling hold only when a complete matching workspace exists.

    An early live RSS sample or a shorter execution cannot establish its final
    peak. Execution limits are deliberately outside this policy.
    """
    state=model.get('state_binding_sha256');binding=model.get('input_binding_sha256')
    if not state and not binding:return None
    matched=[]
    for row in samples:
        other=row.get('model',{});ob=row.get('observation',{})
        if other.get('family')!=model.get('family') or other.get('features')!=model.get('features'):continue
        if state:
            if (other.get('state_binding_sha256')!=state or
                    other.get('coverage_duration_ms',0)<model.get('coverage_duration_ms',0)):continue
        elif (other.get('input_binding_sha256')!=binding or
              other.get('additional_memory_gb',0)<model.get('additional_memory_gb',0)):continue
        peaks=[ob.get(k) for k in ('peak_gb','peak_private_gb')]
        if ob.get('peak_complete') is False or ob.get('cleanup_verified') is not True or any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or x<=0 for x in peaks):continue
        matched.append((row.get('id'),*peaks))
    if not matched:return None
    ram=max(max(x[1] for x in matched),usage['rss_gb'],usage['peak_gb'])
    private=max(ram,max(x[2] for x in matched),usage['private_gb'],usage['peak_private_gb'])
    return dict(ratio=ratio,memory_gb=ram*ratio,commit_gb=private*ratio,
                reference_peak_gb=ram,reference_peak_private_gb=private,
                sample_ids=[x[0] for x in matched],basis='completed_matching_workspace_and_observed_peak')


def running_reservation(spec, model, samples, usage, now):
    """Scheduling request learned from completed profiles; execution limits stay intact.

    Numeric scale metadata works for any application. Without scale metadata,
    the controller only groups byte-identical submission inputs. Retries, stale
    samples and tasks without kernel limits retain their original requests.
    """
    if (not spec.get('kernel_memory_required') or spec.get('resource_mode')!='estimated'
            or spec.get('restart_policy')!='from_start' or spec.get('resource_retries',0)
            or not model
            or usage.get('phase')!='running' or not 0<=now-usage.get('at',0)<15
            or usage.get('elapsed_seconds',0)<60):return None
    candidate=dict(spec,resource_model=model);candidate.pop('resource_calibration',None)
    # A complete run with the same immutable inputs can cover dimensions that
    # were omitted from numeric metadata, provided code has a verified bound on
    # the remaining growth. This is not cross-input scale extrapolation.
    import re
    binding=model.get('input_binding_sha256')
    exact=bool(isinstance(binding,str) and re.fullmatch('[a-f0-9]{64}',binding)
        and model.get('growth_bound')=='verified_retained_allowance_v1')
    if exact:
        samples=[row for row in samples if row.get('model',{}).get('input_binding_sha256')==binding
                 and row['model'].get('growth_bound')==model['growth_bound']
                 and row['model'].get('features')==model['features']]
    proposed=fit_completed(candidate,samples,safety_factor=2 if exact else 1.6,additive_gb=1 if exact else .75)
    calibration=proposed.get('resource_calibration',{})
    method=calibration.get('method')
    values=[usage.get(k) for k in ('rss_gb','private_gb','peak_gb','peak_private_gb')]
    if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or x<0 for x in values):return None
    cap=completed_peak_cap(model,samples,usage)
    if method not in ('completed_scale_envelope','completed_affine_envelope','verified_workspace_bound'):
        if not cap:return None
        proposed=cap
        calibration=dict(method='completed_peak_cap',sample_ids=cap['sample_ids'],samples=len(cap['sample_ids']),safety_factor=1.5)
        method='completed_peak_cap'
    if not cap and method!='verified_workspace_bound' and calibration.get('samples',0)<(1 if exact else 3):return None
    ram=max(proposed['memory_gb'],math.ceil((max(values[0],values[2])*1.6+.75)*4)/4)
    commit=max(ram,proposed['commit_gb'],math.ceil((max(values[1],values[3])*1.6+.75)*4)/4)
    ram=min(spec['memory_gb'],ram);commit=min(spec.get('commit_gb',spec['memory_gb']),commit)
    if cap:
        ram=min(ram,cap['memory_gb']);commit=max(ram,min(commit,cap['commit_gb']))
    if not cap and ram>=spec['memory_gb']*.85 and commit>=spec.get('commit_gb',spec['memory_gb'])*.85:return None
    return dict(memory_gb=ram,commit_gb=max(ram,commit),at=now,
                method='verified_workspace_reservation' if method=='verified_workspace_bound' else 'completed_profile_reservation',sample_ids=calibration['sample_ids'],
                samples=calibration['samples'],family=model['family'],
                anchor_ids=calibration.get('anchor_ids',[]),state_binding_sha256=model.get('state_binding_sha256'),
                evidence_scope='source_bound_workspace' if method=='verified_workspace_bound' else 'bounded_same_inputs' if exact else 'completed_scale_profiles',
                safety_factor=calibration['safety_factor'],input_binding_sha256=binding if exact else None,
                peak_headroom_cap=cap)
