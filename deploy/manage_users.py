"""Administrator-local account provisioning. Never print passwords."""
import argparse,getpass,json,secrets,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from access_control import accounts,registry_lock,registry_path
from common import atomic_json
from werkzeug.security import generate_password_hash

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('username');p.add_argument('--role',choices=['user','admin'],default='user');p.add_argument('--disable',action='store_true');a=p.parse_args()
 config=Path(a.config);cfg=json.loads(config.read_text(encoding='utf-8-sig'))
 with registry_lock(config):
  rows=accounts(config,cfg);row=next((r for r in rows if r['username']==a.username),None)
  if a.disable:
   if not row:raise SystemExit('Account not found')
   row['enabled']=False
  else:
   password=getpass.getpass('New password: ')
   if not password or password!=getpass.getpass('Repeat password: '):raise SystemExit('Password empty or mismatch')
   if row is None:row={'id':secrets.token_hex(16),'username':a.username};rows.append(row)
   row.update(password_hash=generate_password_hash(password),role=a.role,enabled=True)
  if not any(r.get('enabled',True) and r['role']=='admin' for r in rows):raise SystemExit('At least one enabled administrator is required')
  atomic_json(registry_path(config,cfg),rows);print('Account updated:',a.username)
