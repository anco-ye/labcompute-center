const fs=require('fs'),path=require('path'),cp=require('child_process');
for(const n of fs.readdirSync(path.join(__dirname,'../tests')).filter(x=>x.startsWith('test_')&&x.endsWith('.cjs')).sort()){const r=cp.spawnSync(process.execPath,[path.join(__dirname,'../tests',n)],{stdio:'inherit'});if(r.status!==0)process.exit(r.status||1);}
