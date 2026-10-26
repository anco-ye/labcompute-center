const fs=require('fs'),vm=require('vm'),assert=require('assert');
class E{constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.dataset={};this.style={};this.attrs={};}append(...x){this.children.push(...x);}replaceChildren(){this.children=[];}showModal(){this.open=true;}addEventListener(){}setAttribute(k,v){this.attrs[k]=v;}}
let ready=false;const els={};const ctx={assert,console,URLSearchParams,setInterval(){},location:{pathname:'/',search:'',hostname:'192.0.2.30',port:'8443'},crypto:require('crypto').webcrypto,document:{querySelector(s){if(s==='#nodes'&&!ready)return null;if(s.startsWith('meta['))return {content:s==='meta[name=view]'?'tasks':s==='meta[name=admin]'?'true':'test'};return els[s]??=new E('div');},querySelectorAll(){return[];},createElement(t){return new E(t);}}};vm.createContext(ctx);vm.runInContext(fs.readFileSync(__dirname+'/../static/app.js','utf8'),ctx);ready=true;
const fixtureJobs=[0,1].map(i=>({id:'unit'+i,status:'succeeded',node:'win3',has_result:true,created:100,execution_stopped:1,spec:{name:'Finished'},actions:[]}));
const fixture={now:200,nodes:[],jobs:fixtureJobs,uploads:[],issues:[],is_admin:true,batches:fixtureJobs.map((j,i)=>({id:'batch'+i,name:i?'救援产品三视图':'水空救援90秒',group:'completed',phase:'succeeded',created:100,total:2,completed:2,failed:0,cancelled:0,running:0,queued:0,units:[j,{...j,id:j.id+'-two'}],attempts:[j,{...j,id:j.id+'-two'}],nodes:[],actions:[],phases:{succeeded:1}}))};
ctx.snapshot=process.env.STATE_SNAPSHOT?JSON.parse(fs.readFileSync(process.env.STATE_SNAPSHOT,'utf8')):fixture;
vm.runInContext(`
const walk=e=>[e,...e.children.flatMap(walk)],text=e=>e.textContent+' '+e.children.map(text).join(' ');
const rendering=snapshot.batches.filter(b=>b.name.includes('水空救援90秒')||b.name.includes('救援产品三视图'));assert.equal(rendering.length,2);
for(const b of rendering){assert.equal(b.phase,'succeeded');assert.equal(b.running,0);assert.equal(b.completed,b.total);assert(!visibleTasks(snapshot).filter(t=>t.group==='running').some(t=>t.item.id===b.id));const c=taskBatch(b);assert(text(c).includes('已完成'));const primary=walk(c).filter(x=>x.tag==='a'&&x.textContent==='下载结果');assert.equal(primary.length,1);assert.equal(walk(c).filter(x=>x.tag==='progress').length,0);assert.equal(primary[0].href,'/api/batches/'+b.id+'/result');for(const bad of ['中心已保存副本','节点领取','内存预留与上限','CPU 上次采样','运行观测更新时间'])assert(!text(c).includes(bad),bad);}
const reservationCard=taskJob({id:'not-received',status:'leased',spec:{name:'Reserved'},actions:[]});assert(text(reservationCard).includes('资源已预留，等待启动确认'));assert(!text(reservationCard).includes('已领取'));
const readyJob={id:'done',status:'succeeded',node:'win3',has_result:true,spec:{name:'Finished'},memory_allocation:{execution_limit_memory_gb:80},actions:[],execution_stopped:1,detail:'exit=0'};let c=taskJob(readyJob);assert.equal(c.children.filter(x=>x.tag==='a').length,1);assert(!text(c).includes('exit=0'));
c=taskJob({...readyJob,id:'pending-copy',has_result:false,status:'uploading'});assert(text(c).includes('整理结果'));assert(!text(c).includes('回传'));assert.equal(c.children.filter(x=>x.tag==='a').length,1);assert(c.children.find(x=>x.tag==='a').href.endsWith('/pending-copy/download'));
lastRenderedAt=snapshot.now;showSyncFailure();assert(!document.querySelector('#sync-warning').hidden);assert(text(document.querySelector('#sync-warning')).includes('页面状态未同步'));renderState(snapshot);assert(document.querySelector('#sync-warning').hidden);
`,ctx);
console.log('PASS delivered rendering batches, single download entry, simple user cards and explicit stale-state warning');

vm.runInContext(`
const nodeUnits=[{...readyJob,id:'node-one',has_result:false,status:'uploading'},{...readyJob,id:'node-two',has_result:false,status:'uploading'}];
const nodeBatch={id:'node-batch',name:'Node results',units:nodeUnits,total:2,phase:'uploading',actions:[]};
let action=batchResultAction(nodeBatch);assert.equal(action.tag,'button');assert.equal(action.textContent,'下载结果');
assert.equal(walk(taskBatch(nodeBatch)).filter(e=>e.className==='result-download').length,1);
openBatchResults(nodeBatch);assert(document.querySelector('#results-dialog').open);assert.equal(walk(document.querySelector('#results-list')).filter(e=>e.tag==='a').length,2);
const mixed={...nodeBatch,units:[readyJob,nodeUnits[1]]};assert.equal(batchResultAction(mixed).tag,'button');openBatchResults(mixed);assert.equal(walk(document.querySelector('#results-list')).filter(e=>e.tag==='a').length,2);
assert.equal(batchResultAction({...nodeBatch,units:nodeUnits.map(j=>({...j,status:'running',execution_stopped:0}))}),null);
`,ctx);
console.log('PASS node-only and mixed result selection, no false download before execution ends');
