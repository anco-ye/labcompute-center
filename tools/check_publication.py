"""Check the public distribution and nested ZIPs without printing secret values."""
import argparse,io,re,subprocess,zipfile
from pathlib import Path

BLOCKED_DIRS={'.private','.local','access','config','data','jobs','logs','tls','backups','node_modules','.git','__pycache__'}
PATTERNS={
 'private_key':re.compile(rb'-----BEGIN [A-Z ]*PRIVATE KEY-----\s+[A-Za-z0-9+/=\s]{40,}'),
 'github_credential':re.compile(rb'(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})'),
 'private_account_path':re.compile(rb'/Users/[A-Za-z][^/\s]+/(?:Documents|\.ssh|\.codex)/'),
}

def inspect_bytes(name,data,findings,depth=0):
 for kind,pattern in PATTERNS.items():
  if pattern.search(data):findings.append((name,kind))
 if name.endswith('.zip'):
  if depth>=3:raise ValueError('Unexpected nested archive depth')
  with zipfile.ZipFile(io.BytesIO(data))as archive:
   total=0
   for item in archive.infolist():
    total+=item.file_size
    if total>64*1024**2:raise ValueError('Public support archive unexpectedly large')
    path=Path(item.filename)
    if path.is_absolute()or'..'in path.parts:raise ValueError('Archive member escapes root')
    if set(path.parts)&BLOCKED_DIRS or path.suffix.lower()in {'.pem','.key','.p12','.pfx','.cer','.crt'}:findings.append((name+'!'+item.filename,'private_or_instance_material'))
    if not item.is_dir():inspect_bytes(name+'!'+item.filename,archive.read(item),findings,depth+1)

def check(root):
 root=Path(root).resolve();findings=[];count=0
 for path in root.rglob('*'):
  relative=path.relative_to(root)
  if set(relative.parts)&BLOCKED_DIRS:continue
  if not path.is_file():continue
  if path.is_symlink():findings.append((relative.as_posix(),'symlink'));continue
  count+=1
  if path.suffix.lower()in {'.pem','.key','.p12','.pfx','.cer','.crt','.sqlite','.sqlite3'}:findings.append((relative.as_posix(),'private_or_instance_material'))
  inspect_bytes(relative.as_posix(),path.read_bytes(),findings)
 return count,findings

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('root',nargs='?',default=str(Path(__file__).resolve().parents[1]));args=parser.parse_args()
 count,findings=check(args.root)
 print('Scanned public files:',count)
 for name,kind in findings:print(kind+': '+name)
 raise SystemExit(1 if findings else 0)
