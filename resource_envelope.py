"""Conservative nonnegative affine envelopes for audited linear allocation contracts.

Only the center's source adapters can create contracts. Task workload labels do
not opt into these bounds. Every training execution must have fully completed.
"""
import math
from functools import lru_cache


def finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>=0


@lru_cache(maxsize=128)
def _fit(points):
    # Normalize dimensions, then fit an intercept and nonnegative coefficients.
    # Lift the intercept by the largest residual so no observed peak is missed.
    dimensions=len(points[0][0]);scales=[max(p[0][i] for p in points) or 1 for i in range(dimensions)]
    x=[[1]+[v/scales[i] for i,v in enumerate(p[0])] for p in points];y=[p[1] for p in points]
    weights=[min(y)]+[0]*dimensions
    for _ in range(256):
        for k in range(dimensions+1):
            residual=[v-sum(w*r[j] for j,w in enumerate(weights) if j!=k) for r,v in zip(x,y)]
            denominator=sum(r[k]**2 for r in x)
            weights[k]=max(0,sum(r[k]*v for r,v in zip(x,residual))/denominator) if denominator else 0
    weights[0]+=max(0,max(v-sum(w*z for w,z in zip(weights,r)) for r,v in zip(x,y)))
    return tuple(weights),tuple(scales)


def predict(points, target):
    weights,scales=_fit(tuple(sorted(points)))
    return weights[0]+sum(w*v/s for w,v,s in zip(weights[1:],target,scales))


def completed_envelope(model, samples):
    """A fit covers all samples, with held-out checks and bounded extrapolation."""
    if model.get('allocation_contract')!='buffered-linear-v1':return None
    keys=sorted(model.get('features',{}));target=tuple(model['features'][k] for k in keys)
    if not keys or not all(finite(x) for x in target):return None
    rows=[];anchors=[];covered=[]
    for row in samples:
        other=row.get('model',{});ob=row.get('observation',{})
        if (other.get('family')!=model.get('family') or other.get('allocation_contract')!=model['allocation_contract']
                or set(other.get('features',{}))!=set(keys)
                or ob.get('peak_complete') is False or ob.get('cleanup_verified') is not True):continue
        shape=tuple(other['features'][k] for k in keys);peaks=tuple(ob.get(k) for k in ('peak_gb','peak_private_gb'))
        if not all(finite(v) for v in shape+peaks) or not all(peaks):continue
        if (model.get('state_binding_sha256') and other.get('state_binding_sha256')==model['state_binding_sha256']
                and all(other['features'][k]==model['features'][k] for k in keys if k not in model.get('retained_dimensions',[]))):
            anchors.append((shape,peaks,row.get('id')))
        if other.get('coverage_duration_ms',0)>=model.get('coverage_duration_ms',0):covered.append(shape)
        rows.append((shape,peaks,row.get('id')))
    if len(set(covered))<3:return None
    # Audited allocation sites grow linearly; keep a finite scale range anyway.
    maxima=[max(r[0][i] for r in rows) for i in range(len(keys))]
    if any(v>m*4 for v,m in zip(target,maxima)) and not anchors:return None
    predicted=[]
    for column in (0,1):
        points=tuple((shape,peaks[column]) for shape,peaks,_ in rows)
        # Duplicate shapes cannot stand in for independent scale coverage.
        for shape in {r[0] for r in rows}:
            train=[p for p in points if p[0]!=shape];held=[p[1] for p in points if p[0]==shape]
            if len({p[0] for p in train})<2:return None
            if predict(train,shape)*1.6+.75<max(held):return None
        value=predict(points,target)
        # A complete run of the identical immutable state measures its actual
        # setup/buffer cost better than cross-shape extrapolation. Only the
        # nonnegative learned increment for retained dimensions is added.
        if anchors:value=max(p[column]+max(0,value-predict(points,shape)) for shape,p,_ in anchors)
        predicted.append(value*1.6+.75)
    return dict(memory_gb=math.ceil(predicted[0]*4)/4,
                commit_gb=math.ceil(max(predicted)*4)/4,
                samples=len(rows),sample_ids=[r[2] for r in rows],
                anchor_ids=[r[2] for r in anchors],
                method='completed_affine_envelope',features=dict(zip(keys,target)),
                safety_factor=1.6,additive_gb=.75)
