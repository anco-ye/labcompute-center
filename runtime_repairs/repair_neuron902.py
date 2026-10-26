"""Install the verified adapter for future imports; never inject into live solvers."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

MARKER = '# LabCompute NEURON 9.0.2 array ownership repair v1'
ADAPTER_SHA = '3641f36cd50db16ed6f249267108839d19638ed138033fcf2730a3728dca1ab4'

PROBE = r'''
import gc,json,sys
sys.path.append('C:/LabComputeCenter/venv/Lib/site-packages')
import psutil,numpy as np,neuron
from neuron import h
assert neuron.__version__=='9.0.2',neuron.__version__
v=h.Vector([1.,2.,3.]);a=v.as_numpy();a[0]=9;assert v[0]==9
v[1]=8;assert a[1]==8;assert h.Vector().as_numpy().shape==(0,)
h.load_file('stdrun.hoc');s=h.Section();s.insert('pas');stim=h.IClamp(s(.5));stim.delay=.3;stim.dur=2;stim.amp=.01
rec=h.Vector().record(s(.5)._ref_v,.025);clock=h.Vector().record(h._ref_t,.025)
h.dt=.025;h.finitialize(-65);h.continuerun(5)
trace=np.array(rec.as_numpy(),copy=True)
assert np.array_equal(trace,np.array(rec))
vectors=[h.Vector(range(40)) for _ in range(1000)]
p=psutil.Process();gc.collect();before=p.memory_info().private
for _ in range(250):
 for v in vectors:np.array(v.as_numpy(),copy=True)
gc.collect();growth=p.memory_info().private-before
print(json.dumps(dict(version=neuron.__version__,growth_bytes=growth,trace=trace.tolist(),adapter=h.Vector.__dict__.get('as_numpy').__module__ if h.Vector.__dict__.get('as_numpy') else 'native')))
'''

def install(home, python, adapter, backup_root):
    home=Path(home);adapter=Path(adapter);target=home/'lib/python/neuron/__init__.py'
    payload=adapter.read_bytes()
    if hashlib.sha256(payload).hexdigest()!=ADAPTER_SHA:raise ValueError('Unvalidated adapter')
    original=target.read_bytes();module=target.with_name('_lab_numpy_compat.py')
    env=os.environ.copy();env.update(NEURONHOME=str(home),PYTHONPATH=str(home/'lib/python'))
    env['PATH']=os.pathsep.join([str(home/'bin'),str(home/'mingw/usr/bin'),env.get('PATH','')])
    def probe():
        p=subprocess.run([str(python),'-c',PROBE],env=env,capture_output=True,text=True,timeout=55)
        if p.returncode:raise RuntimeError(p.stderr[-2000:])
        return json.loads(p.stdout.strip().splitlines()[-1])
    before=probe()
    if MARKER.encode() in original:
        if not module.exists() or module.read_bytes()!=payload or before['growth_bytes']>8*2**20:raise RuntimeError('Installed repair is inconsistent')
        return dict(status='already_repaired',probe=before)
    backup=Path(backup_root)/('neuron902-'+time.strftime('%Y%m%d-%H%M%S'));backup.mkdir(parents=True)
    shutil.copy2(target,backup/'__init__.py');old_module=module.read_bytes() if module.exists() else None
    if old_module is not None:(backup/'_lab_numpy_compat.py').write_bytes(old_module)
    try:
        module.write_bytes(payload)
        new=original+('\n'+MARKER+'\nfrom neuron import _lab_numpy_compat\n_lab_numpy_compat.install()\n').encode()
        compile(new,str(target),'exec');tmp=target.with_suffix('.py.next');tmp.write_bytes(new);os.replace(tmp,target)
        after=probe()
        if before['trace']!=after['trace'] or after['growth_bytes']>8*2**20:raise RuntimeError('Runtime repair regression')
    except Exception:
        target.write_bytes(original)
        if old_module is None:module.unlink(missing_ok=True)
        else:module.write_bytes(old_module)
        raise
    report=dict(status='repaired',at=time.time(),backup=str(backup),adapter_sha256=ADAPTER_SHA,
                original_sha256=hashlib.sha256(original).hexdigest(),installed_sha256=hashlib.sha256(new).hexdigest(),
                before=before,after=after,scope='future processes only; scientific inputs unchanged')
    (backup/'receipt.json').write_text(json.dumps(report,indent=2));return report

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--home',required=True);p.add_argument('--python',required=True)
    p.add_argument('--adapter',required=True);p.add_argument('--backup-root',required=True);a=p.parse_args()
    print(json.dumps(install(a.home,a.python,a.adapter,a.backup_root)))
