$ErrorActionPreference='Stop'
$env:PIP_DISABLE_PIP_VERSION_CHECK='1'
$r='C:\LabComputeCenter'
New-Item -ItemType Directory -Force "$r\runtimes","$r\cache","$r\wheels" | Out-Null
$account="$env:COMPUTERNAME\LabCompute"
& icacls "$r\cache" /grant "${account}:(OI)(CI)M" | Out-Null
& "$r\venv\Scripts\python.exe" -m venv "$r\runtimes\python311-v1"
if($LASTEXITCODE -ne 0){throw 'base environment failed'}
$packages=@('numpy==2.2.6','scipy==1.15.3','pandas==2.2.3','matplotlib==3.10.3','h5py==3.13.0','scikit-learn==1.6.1','openpyxl==3.1.5')
& "$r\runtimes\python311-v1\Scripts\python.exe" -m pip download --only-binary=:all: --dest "$r\wheels" @packages > "$r\logs\runtime-download.log" 2>&1
if($LASTEXITCODE -ne 0){throw 'wheel download failed: runtime-download.log'}
& "$r\venv\Scripts\python.exe" -m venv "$r\runtimes\python311-science-v1"
& "$r\runtimes\python311-science-v1\Scripts\python.exe" -m pip install --no-index --find-links "$r\wheels" @packages > "$r\logs\runtime-install.log" 2>&1
if($LASTEXITCODE -ne 0){throw 'scientific environment install failed'}
& "$r\runtimes\python311-science-v1\Scripts\python.exe" -c "import numpy as n,scipy,pandas,matplotlib,h5py,sklearn,openpyxl; assert n.allclose(n.linalg.solve([[2.,0],[0,4.]],[6.,8.]),[3,2]); print('SCIENTIFIC_RUNTIME_OK',n.__version__)"
if($LASTEXITCODE -ne 0){throw 'runtime validation failed'}
& "$r\runtimes\python311-science-v1\Scripts\python.exe" -m pip freeze > "$r\runtimes\science-v1-lock.txt"
'RUNTIME_READY'
