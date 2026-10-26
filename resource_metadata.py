"""Numeric scale metadata adapter for immutable, already-submitted packages."""
import hashlib,json,zipfile
from functools import lru_cache
from pathlib import Path
from resource_policy import workload_model


def program_digest(z):
    h=hashlib.sha256()
    for name in sorted(z.namelist()):
        if Path(name).suffix.lower() in ('.py','.pyc','.pyd','.so','.dylib','.m','.mexw64','.mexa64','.mexmaci64','.hoc','.mod','.dll','.exe','.r','.jl','.ps1','.bat','.cmd','.sh','.bash','.jar','.class','.js','.cjs','.mjs','.vbs','.wasm','.lib','.a') or '.so.' in Path(name).name or (not Path(name).suffix and not name.endswith('/')) or Path(name).name.startswith('requirements'):
            h.update(name.encode());h.update(z.read(name))
    return h.hexdigest()


@lru_cache(maxsize=256)
def immutable_package_digest(path, size, modified_ns):
    """Bind omitted dimensions to actual bytes, never a task-supplied label."""
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(4*1024**2),b''):h.update(block)
    stat=Path(path).stat()
    if (stat.st_size,stat.st_mtime_ns)!=(size,modified_ns):raise ValueError('Package changed during resource binding')
    return h.hexdigest()


@lru_cache(maxsize=256)
def package_table(path):
    try:
        with zipfile.ZipFile(path) as z:
            if not {'main.py','task_table.json','source_manifests.json'}<=set(z.namelist()):return None
            if z.getinfo('main.py').file_size>1024**2 or z.getinfo('task_table.json').file_size>4*1024**2:return None
            if not z.read('main.py').startswith(b'"""Self-contained H01 batch entry. All writes are confined to this task workdir."""'):return None
            table=json.loads(z.read('task_table.json'))
            if not isinstance(table,dict):return None
            main=z.read('main.py').decode('utf-8')
            candidates=[n for n in z.namelist() if n.endswith('/full_window_network_fixed_active_v3.py')]
            buffered=False
            if len(candidates)==1 and z.getinfo(candidates[0]).file_size<1024**2:
                source=z.read(candidates[0]).decode('utf-8')
                buffered=("'--no-savecurrents','--buffer-ms','1'" in main and all(t in source for t in (
                    'for vector in im+[t]+vv:vector.resize(0)',
                    'np.arange(args.buffer_ms,duration,args.buffer_ms)',
                    'raw.append(ima@coeff.T)',"volts[n:]=va",'max(100,int(duration*4))')))
            return program_digest(z),table,buffered
    except (OSError,ValueError,KeyError,zipfile.BadZipFile):return None


def legacy_model(package,spec):
    parsed=package_table(str(package))
    if not parsed:return None
    digest,table,buffered=parsed;task=table.get(spec.get('task_key'),{})
    if not task or task.get('id')!=spec.get('task_key'):return None
    stage=task.get('stage','');scale=task.get('input_scale')
    if not scale:
        scale=next((t.get('input_scale') for t in table.values() if t.get('environment')==task.get('environment') and t.get('stage')=='short_network'),None)
    if not scale:return None
    if not isinstance(scale,dict) or isinstance(scale.get('modeled_cells'),bool) or not isinstance(scale.get('modeled_cells'),(int,float)) or not 0<scale['modeled_cells']<=1e18:return None
    if stage in ('short_network','long_network'):family='h01-network:'+task.get('condition','strong_bursts')
    else:return None
    features=dict(cells=scale['modeled_cells'],segments=scale.get('recorded_segments'),duration_ms=task.get('duration_ms',5000 if 'long' in stage else 600))
    input_binding=None
    additional=0
    if buffered and stage in ('short_network','long_network'):
        duration=features.pop('duration_ms');features['buffer_ms']=1
        # Full time series of membrane currents are flushed every millisecond.
        # Retained reference/filter arrays and event metadata still grow in time;
        # add a deliberately conservative allowance instead of ignoring them.
        additional=((duration/.025+1)*512+duration*features['cells']*8192)/2**30
        # Input conditions change event times, not state/buffer dimensions.
        # The inspected generator bounds each input array by duration*4;
        # retained event allowance above covers every supported condition.
        family='h01-buffered-network-v3'
        if features['segments'] is None:
            # Unknown segment count is held constant by immutable input identity.
            # Do not invent a count or merge different environments/packages.
            try:
                stat=Path(package).stat()
                package_digest=immutable_package_digest(str(package),stat.st_size,stat.st_mtime_ns)
            except (OSError,ValueError):return None
            input_binding=hashlib.sha256(json.dumps([package_digest,task['environment']]).encode()).hexdigest()
            features.pop('segments')
            family='input-bound:'+input_binding
    identity=[digest,spec['kind'],spec['runtime'],spec['entry'],spec.get('requirements'),spec['cores']]
    try:
        model=workload_model(dict(family=family,features=features),identity)
        if additional:model.update(additional_memory_gb=additional,adapter_version=3)
        if input_binding:model.update(input_binding_sha256=input_binding,growth_bound='verified_retained_allowance_v1')
        return model
    except (ValueError,KeyError,TypeError):return None


@lru_cache(maxsize=512)
def _program_bundle(path,size,mtime):
    with zipfile.ZipFile(path) as z:return program_digest(z)


def current_model(package,spec):
    import resource_contracts
    audited=resource_contracts.model(package,spec,package_table(str(package))) or legacy_model(package,spec)
    if audited:return audited
    old=spec.get('resource_model')
    if not old:return None
    try:
        stat=Path(package).stat();program=_program_bundle(str(package),stat.st_size,stat.st_mtime_ns)
        return workload_model(dict(family=old['label'],features=old['features']),
            [program,spec['kind'],spec['runtime'],spec['entry'],spec.get('requirements'),spec['cores']])
    except (OSError,ValueError,KeyError,zipfile.BadZipFile):return None


def reservation_model(package,spec):
    """Use numeric scale metadata, or exact immutable-input identity for any app."""
    model=current_model(package,spec)
    if model:return model
    signature=spec.get('resource_signature')
    if signature and Path(package).is_file():
        return dict(family='identical-inputs:'+signature,features=dict(identical_inputs=1),label='identical-inputs',input_binding_sha256=signature)
    return None

