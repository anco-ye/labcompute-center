"""Source-bound shape adapters, separate from generic resource fitting.

The two reviewed program bundles share the same allocation sites; their
preflight review routing differs. Unknown or edited code keeps normal budgets.
No owner, job ID, queue or host is used to select a contract.
"""
import hashlib
import ast
import io
import json
import math
from functools import lru_cache
from pathlib import Path
import zipfile

AUDITED_BUNDLES={
    'c40e53fd06bf8112f964e590e386f44b71c55b9bab265428c0c1f8085f0cecf2',
    '9f60701b3bfa348630f87a2d6461380d2b41c29a618dd10e672e290eeb195a66',
}
ROOT='analysis/H01_Allen_preexperiment_20261013'
# These engine revisions have identical allocation sites. Their only code
# difference removes independent review gates before computation.
ALLOCATION_ENGINE_FAMILIES={
    'b4ca9258344e25b0fc797fe1560586cbdaf4420e81d66476aa31e78d44fc34c1':'buffered-state-v1',
    '890268fb7e4e48ca826fa6477f3be6775d01e552a3fcbb4dbca91b06a6e4caf8':'buffered-state-v1',
}


def _count(x):
    if isinstance(x,bool) or not isinstance(x,int) or not 0<x<=10**8:raise ValueError('Invalid source shape')
    return x


def _json(z,name,limit=16*1024**2):
    if z.getinfo(name).file_size>limit:raise ValueError('Shape record too large')
    return json.loads(z.read(name))


@lru_cache(maxsize=256)
def allocation_group(path,size,mtime,environment):
    """Resource-metadata cache key survives changes to dispatch wrappers.

    Use the known allocation engine and the archive's data-entry inventory.
    This is a resource cache key, not scientific validation or a delivery hash.
    Unknown programs still cannot opt into the allocation contract.
    """
    with zipfile.ZipFile(path) as z:
        engine=hashlib.sha256(z.read(ROOT+'/scripts/full_window_network_fixed_active_v3.py')).hexdigest()
        data=sorted((x.filename,x.file_size,x.CRC) for x in z.infolist()
                    if x.filename.startswith(ROOT+'/') and '/scripts/' not in x.filename
                    and Path(x.filename).suffix.lower() not in ('.py','.pyc','.hoc','.mod','.dll') and not x.is_dir())
    engine=ALLOCATION_ENGINE_FAMILIES.get(engine,engine)
    return ('allocation-metadata-v1',hashlib.sha256(json.dumps([engine,data,environment]).encode()).hexdigest())


def _native_vertices(data):
    with zipfile.ZipFile(io.BytesIO(data)) as geometry:
        with geometry.open('vertices_um.npy') as stream:
            if stream.read(6)!=b'\x93NUMPY':raise ValueError('Invalid geometry array')
            major,minor=stream.read(2)
            if major not in (1,2,3):raise ValueError('Unsupported geometry array')
            width=2 if major==1 else 4;length=int.from_bytes(stream.read(width),'little')
            if length>65536:raise ValueError('Oversized geometry header')
            header=ast.literal_eval(stream.read(length).decode('utf-8'));shape=header['shape'];dtype=header['descr']
            if len(shape)!=2 or shape[1]!=3 or dtype not in ('<f4','<f8','>f4','>f8','|f4','|f8'):raise ValueError('Invalid vertex dimensions')
            count=_count(shape[0]);expected=8+width+length+count*3*int(dtype[-1])
            if geometry.getinfo('vertices_um.npy').file_size!=expected:raise ValueError('Truncated geometry array')
            return count


@lru_cache(maxsize=256)
def _network_inputs(path,size,mtime,environment):
    with zipfile.ZipFile(path) as z:
        matches=_json(z,ROOT+'/fixed_matching_'+environment+'_ready.json');vertices=0
        for match in matches:
            native=ROOT+'/bridge_repairs/'+environment+'/'+str(match['c3_id'])+'_bridged.npz'
            if z.getinfo(native).file_size>64*1024**2:raise ValueError('Oversized geometry input')
            vertices+=_native_vertices(z.read(native))
        inferred=_json(z,ROOT+'/inferred_local_connectivity_v1/'+environment+'.json')['selected']
        observed=_json(z,ROOT+'/observed_synapses_'+environment.removeprefix('H01_')+'.json')
        return len(matches),vertices,len(inferred)+len(observed)


def model(package,spec,parsed):
    if not parsed or parsed[0] not in AUDITED_BUNDLES:return None
    digest,table,buffered=parsed;task=table.get(spec.get('task_key'),{})
    if task.get('id')!=spec.get('task_key') or spec.get('kind')!='python':return None
    stage=task.get('stage');env=task.get('environment')
    # Pair-reference inspection is retired. Such workloads use ordinary
    # declared/observed resource budgets, never geometry-reference review.
    if stage not in ('short_network','long_network'):return None
    try:
        stat=Path(package).stat()
        family=hashlib.sha256(json.dumps(['audited-buffered-state-v1',spec['kind'],spec['runtime'],spec['entry'],spec['cores']]).encode()).hexdigest()
        common=dict(family=family,label='source-bound-allocation-v1',
                    audited_program_sha256=digest,adapter_version=4)
        if stage in ('short_network','long_network'):
            if not buffered:return None
            scale=task.get('input_scale') or next((t.get('input_scale') for t in table.values() if t.get('environment')==env and t.get('stage')=='short_network'),None)
            if not scale:return None
            cells=_count(scale['modeled_cells'])
            group=allocation_group(str(package),stat.st_size,stat.st_mtime_ns,env)
            segments=_count(scale.get('recorded_segments'))
            duration=task.get('duration_ms')
            if isinstance(duration,bool) or not isinstance(duration,(int,float)) or not 0<duration<=1e6:return None
            # State and live current buffers scale with segments/cells. Retained
            # reference/filter/event arrays are represented once as scale data.
            actual_cells,vertices,contacts=_network_inputs(str(package),stat.st_size,stat.st_mtime_ns,env)
            if actual_cells!=cells:return None
            retained=math.ceil(duration/.025+1)*16+math.ceil(duration*cells*4)+math.ceil(duration*contacts)
            state=group[1]
            return dict(common,allocation_contract='buffered-linear-v1',coverage_duration_ms=duration,
                        state_binding_sha256=state,retained_dimensions=['retained_values'],
                        features=dict(cells=cells,segments=segments,vertices=vertices,contacts=contacts,retained_values=retained))
    except (OSError,ValueError,KeyError,TypeError,SyntaxError,zipfile.BadZipFile):return None
