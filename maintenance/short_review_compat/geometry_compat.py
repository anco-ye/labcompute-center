"""Narrow cross-version comparison of frozen source geometry; fail closed."""
import copy,hashlib,math
from pathlib import Path

LENGTH_ATOL_UM=1e-7
ROOT_FIELDS=('source_soma_root','effective_soma_root','soma_reference_override')

def compare_geometry(reference,current,native_dir,bindings,binding_prefix):
    import numpy as np
    audit=dict(passed=False,length_atol_um=LENGTH_ATOL_UM,length_rtol=0,root_checks=[],rounded_lengths=[],differences=[])
    def fail(path,reason):audit['differences'].append(dict(path=path,reason=reason))
    def normalize(rows,side):
        rows=copy.deepcopy(rows)
        if not isinstance(rows,list):fail(side,'geometry must be a list');return rows
        seen=set()
        for i,row in enumerate(rows):
            path=f'{side}/{i}'
            if not isinstance(row,dict) or not isinstance(row.get('geometry'),dict):fail(path,'missing geometry');continue
            soma=row.get('soma_id');cid=row.get('c3_id');g=row['geometry']
            if not isinstance(soma,str) or soma in seen:fail(path,'invalid or duplicate soma identity');continue
            seen.add(soma)
            if not isinstance(cid,str) or not cid.isdigit():fail(path,'invalid native source identity');continue
            native=Path(native_dir)/(cid+'_bridged.npz');key=binding_prefix+'/'+native.name
            try:
                digest=hashlib.sha256(native.read_bytes()).hexdigest()
                if not bindings.get(key) or digest!=bindings[key] or g.get('source_sha256')!=digest:raise ValueError('native source hash mismatch')
                with np.load(native,allow_pickle=False) as data:
                    value=data['soma_root']
                    if value.size!=1 or value.dtype.kind not in 'iu':raise ValueError('invalid native soma root')
                    root=int(value.item())
                present=[k in g for k in ROOT_FIELDS]
                if any(present) and not all(present):raise ValueError('partial soma reference metadata')
                if all(present) and (g['soma_reference_override'] is not None or any(type(g[k]) is not int or g[k]!=root for k in ROOT_FIELDS[:2])):raise ValueError('soma reference differs from frozen native root')
                audit['root_checks'].append(dict(side=side,soma_id=soma,c3_id=cid,native_root=root,native_sha256=digest,legacy_defaults=not any(present),passed=True))
                # Absence means the native root only after the frozen NPZ proves it.
                g.update(source_soma_root=root,effective_soma_root=root,soma_reference_override=None)
                g.pop('inferred_partition_path',None) # Existing machine-local path exclusion.
            except (OSError,ValueError,KeyError,TypeError) as exc:fail(path,str(exc))
        return rows
    left=normalize(reference,'reference');right=normalize(current,'current')
    def equal(a,b,path=()):
        if isinstance(a,dict) and isinstance(b,dict):
            if set(a)!=set(b):fail('/'.join(path),'field set differs');return
            for key in sorted(a):equal(a[key],b[key],path+(key,))
        elif isinstance(a,list) and isinstance(b,list):
            if len(a)!=len(b):fail('/'.join(path),'list length differs');return
            for i,(x,y) in enumerate(zip(a,b)):equal(x,y,path+(str(i),))
        elif len(path)==6 and path[1:4]==('active_model','nodal_candidate_audit','gaps') and path[4].isdigit() and path[-1]=='native_cable_extent_um':
            valid=all(type(x) in (int,float) and math.isfinite(x) and x>=0 for x in (a,b))
            if not valid or abs(a-b)>LENGTH_ATOL_UM:fail('/'.join(path),'derived cable length exceeds absolute tolerance')
            elif a!=b:audit['rounded_lengths'].append(dict(path='/'.join(path),reference=a,current=b,absolute_difference_um=abs(a-b)))
        elif type(a)!=type(b) or a!=b:fail('/'.join(path),'exact value differs')
    equal(left,right)
    audit['passed']=not audit['differences'] and len(audit['root_checks'])==len(reference)+len(current) and bool(reference)
    return audit
