"""Stage a pinned CUDA wheel set; publish only after a real device calculation."""
import json, subprocess, time, traceback
from pathlib import Path
ROOT=Path('C:/LabComputeCenter')
INDEX='https://download.pytorch.org/whl/cu126'
PACKAGES=['torch==2.7.1+cu126','numpy==2.2.6']
def main():
    target=ROOT/'runtimes/python311-torch271-cu126';target.mkdir(parents=True,exist_ok=True)
    result=target/'validation.json'
    try:
        base=ROOT/'runtimes/python311-v1/Scripts/python.exe'
        python=target/'Scripts/python.exe'
        if not python.is_file():subprocess.run([str(base),'-m','venv',str(target)],check=True)
        wheels=ROOT/'wheels';wheels.mkdir(exist_ok=True)
        subprocess.run([str(python),'-m','pip','download','--disable-pip-version-check','--only-binary=:all:','--dest',str(wheels),'--index-url',INDEX,*PACKAGES],check=True,timeout=1800)
        subprocess.run([str(python),'-m','pip','install','--no-index','--find-links',str(wheels),*PACKAGES],check=True,timeout=900)
        probe="import torch,json; assert torch.cuda.is_available(); a=torch.arange(4096,dtype=torch.float32,device='cuda').reshape(64,64); b=a@a.T; torch.cuda.synchronize(); assert torch.allclose(b.cpu(),a.cpu()@a.cpu().T,rtol=1e-4,atol=1); print(json.dumps(dict(torch=torch.__version__,cuda=torch.version.cuda,gpu=torch.cuda.get_device_name(0),capability=torch.cuda.get_device_capability(0),sum=float(b.sum()))))"
        p=subprocess.run([str(python),'-c',probe],check=True,capture_output=True,text=True,timeout=120)
        data=dict(ok=True,at=time.time(),probe=json.loads(p.stdout),packages=PACKAGES,index=INDEX)
    except Exception:
        data=dict(ok=False,at=time.time(),error=traceback.format_exc())
    result.write_text(json.dumps(data,indent=2));print(json.dumps(data),flush=True)
if __name__=='__main__':main()
