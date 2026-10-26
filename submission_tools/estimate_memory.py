"""Static resource arithmetic only: never import or execute submitted task code."""
import argparse,json,math
from pathlib import Path
DTYPES={'bool':1,'int8':1,'uint8':1,'int16':2,'uint16':2,'float16':2,'int32':4,'uint32':4,'float32':4,'int64':8,'uint64':8,'float64':8,'complex64':8,'complex128':16}
GIB=2**30

def positive(value,label,zero=False):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<(0 if zero else 1):raise ValueError(label+' must be finite and nonnegative' if zero else label+' must be at least 1')
    return value

def allocation(item):
    name=item['name']
    if not isinstance(name,str) or not name:raise ValueError('Allocation requires a name')
    if 'bytes' in item:
        if 'shape' in item:raise ValueError(name+': specify bytes or shape, not both')
        size=positive(item['bytes'],name+'.bytes',True)
    else:
        size=DTYPES[item['dtype']]
        if not item.get('shape'):raise ValueError(name+': shape is empty')
        for dim in item['shape']:
            if isinstance(dim,bool) or not isinstance(dim,int) or dim<0:raise ValueError(name+': invalid shape')
            size*=dim
    copies=positive(item.get('copies',1),name+'.copies');processes=positive(item.get('processes',1),name+'.processes')
    shared=item.get('shared_across_processes',False)
    if not isinstance(shared,bool):raise ValueError(name+': shared_across_processes must be boolean')
    size*=copies*(1 if shared else processes)
    # Shared allocation is counted once, but is not presumed file backed.
    resident=item.get('resident_fraction',1);private=item.get('private_fraction',1)
    for label,value in [('resident_fraction',resident),('private_fraction',private)]:
        positive(value,name+'.'+label,True)
        if value>1:raise ValueError(name+': '+label+' must be <=1')
    return dict(name=name,ram_bytes=size*resident,commit_bytes=size*private,allocated_bytes=size)

def estimate(plan):
    if not isinstance(plan,dict) or plan.get('schema_version')!=1:raise ValueError('schema_version must be 1')
    stages=plan.get('stages',[])
    if not stages:raise ValueError('At least one stage is required')
    baseline=[allocation(x) for x in plan.get('persistent',[])];rows=[]
    for stage in stages:
        items=baseline+[allocation(x) for x in stage.get('allocations',[])]
        rows.append(dict(name=stage['name'],ram_gb=sum(x['ram_bytes'] for x in items)/GIB,commit_gb=sum(x['commit_bytes'] for x in items)/GIB,allocations=items))
    ram=max(x['ram_gb'] for x in rows);private=max(ram,max(x['commit_gb'] for x in rows))
    # Round decimal serialization only, never introduce a safety multiplier.
    request_ram=max(.25,math.ceil(ram*1e6)/1e6);request_commit=max(request_ram,math.ceil(private*1e6)/1e6)
    unknown=plan.get('unquantified',[])
    if not isinstance(unknown,list) or any(not isinstance(x,str) for x in unknown):raise ValueError('unquantified must be a list of descriptions')
    return dict(schema_version=1,status='provisional' if unknown else 'declared_components_only',
        resources=dict(mode='estimated',memory_basis='peak',memory_gb=request_ram,commit_gb=request_commit),
        predicted_peak_ram_gb=ram,predicted_peak_commit_gb=private,stages=rows,unquantified=unknown,
        safety_factor_applied=1,additive_headroom_gb=0,
        limitations='Arithmetic from agent declarations, not a measurement or guarantee. Include native solver workspace, libraries, child processes and output packing. Never derive final peaks from partial runs.')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('plan');parser.add_argument('--output');args=parser.parse_args()
    output=json.dumps(estimate(json.loads(Path(args.plan).read_text())),ensure_ascii=False,indent=2)+'\n'
    if args.output:Path(args.output).write_text(output)
    else:print(output,end='')
