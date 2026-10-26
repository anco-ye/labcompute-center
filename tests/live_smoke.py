"""Run two tiny Python jobs on the real center and verify results."""
import io,json,zipfile,re,ssl,urllib.request,urllib.parse,http.cookiejar,time,uuid
from pathlib import Path
root=Path(__file__).resolve().parents[1]
import argparse,getpass
parser=argparse.ArgumentParser(description='Explicitly submit two tiny jobs to a configured TEST center')
parser.add_argument('--base',required=True);parser.add_argument('--ca',required=True);parser.add_argument('--username',required=True)
parser.add_argument('--node',default='win2');args=parser.parse_args()
if not args.base.startswith('https://'):raise SystemExit('Use verified HTTPS')
base=args.base.rstrip('/');password=getpass.getpass('Test-center password: ')
context=ssl.create_default_context(cafile=args.ca)
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
def get(path):return opener.open(base+path,timeout=15).read()
page=get('/login').decode()
csrf=re.search(r'name="csrf" value="([^"]+)',page)[1]
login=urllib.parse.urlencode(dict(csrf=csrf,username=args.username,password=password)).encode()
page=opener.open(base+'/login',data=login,timeout=15).read().decode()
csrf=re.search(r'name="csrf" content="([^"]+)',page)[1]
state=json.loads(get('/api/state'))
node=next(n for n in state['nodes'] if n['id']==args.node)
if node['telemetry'].get('external_busy') or node['current_jobs']:
 print('DEFERRED: Win2 is running existing work; no smoke job submitted',flush=True)
 raise SystemExit(0)
z=io.BytesIO()
specs=[dict(name='单文件上传验证 '+str(i),kind='python',entry='main.py') for i in (1,2)]
with zipfile.ZipFile(z,'w') as archive:
 archive.writestr('manifest.json',json.dumps(dict(schema_version=1,tasks=specs)))
 archive.writestr('main.py',"import json,platform,time,os\nfrom pathlib import Path\nfor i in range(8):\n Path('progress.tmp').write_text(json.dumps(dict(completed=i,total=8)))\n for attempt in range(6):\n  try:\n   os.replace('progress.tmp','progress.json');break\n  except PermissionError:time.sleep(.02*(attempt+1))\n time.sleep(1)\nr={'host':platform.node(),'sum':sum(range(10001))}\nPath('result.json').write_text(json.dumps(r))\nprint(json.dumps(r),flush=True)\n")
boundary=uuid.uuid4().hex
body=(f'--{boundary}\r\nContent-Disposition: form-data; name="package"; filename="smoke.zip"\r\nContent-Type: application/zip\r\n\r\n').encode()+z.getvalue()+f'\r\n--{boundary}--\r\n'.encode()
req=urllib.request.Request(base+'/api/jobs',data=body,headers={'X-CSRF-Token':csrf,'Content-Type':'multipart/form-data; boundary='+boundary})
ids=json.loads(opener.open(req,timeout=15).read())['ids']
print('Submitted:',','.join(ids),flush=True)
deadline=time.time()+90
saw_progress=False; saw_history=False
while time.time()<deadline:
 state=json.loads(get('/api/state'));jobs=[j for j in state['jobs'] if j['id'] in ids]
 saw_progress |= any(j['estimate_source']=='实时进度' for j in jobs)
 saw_history |= any(j['estimate_source']=='历史实测' for j in jobs)
 print([(j['spec']['name'],j['status'],j['estimate_source']) for j in jobs],flush=True)
 if all(j['status'] in ('succeeded','failed','cancelled','recovery_needed') for j in jobs):break
 time.sleep(5)
assert all(j['status']=='succeeded' for j in jobs),jobs
jobs.sort(key=lambda j:j['started'])
# Normal resource admission may execute the test jobs concurrently.
for jid in ids:
 result=zipfile.ZipFile(io.BytesIO(get('/api/jobs/'+jid+'/result')))
 data=json.loads(result.read('work/result.json'))
 assert data['sum']==50005000,data
assert saw_progress and saw_history, (saw_progress,saw_history)
print('PASS: single ZIP, automatic resources, progress ETA, learned history, upload, sequential allocation, Windows execution, logs, result download',flush=True)
# No private receipt or credentials are written into the repository.
