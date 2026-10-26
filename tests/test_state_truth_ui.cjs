const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
class Element{constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.dataset={};}append(...items){this.children.push(...items);}addEventListener(){}setAttribute(){}replaceChildren(){this.children=[];}}
const elements={};const context={Element,assert,console,URLSearchParams,location:{search:'',pathname:'/'},setInterval(){},crypto:require('crypto').webcrypto,
document:{querySelector(s){if(s==='#nodes')return null;if(s.startsWith('meta['))return {content:s==='meta[name=view]'?'operations':'test'};return elements[s]??=new Element('div');},querySelectorAll(){return[];},createElement(t){return new Element(t);}}};
vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8'),context);
vm.runInContext(`
const text=e=>e.textContent+' '+e.children.map(text).join(' ');
const j={id:'x',status:'running',spec:{name:'x'},actions:[],progress_percent:69,stage_progress_percent:22,progress_fresh:true,progress:{stage:'矩阵分解'},activity:{stage_label:'求解器计算中',cpu_cores:1.7,cpu_fresh:true,observed_at:100},execution_stopped:0};
for(const status of ['uploading','queued','leased','held','paused','failed','cancelled','recovery_needed']){
 const c=new Element('div');renderProgress(c,{...j,status});const t=text(c);assert(!t.includes('求解器计算中'));assert(!t.includes('CPU'));assert(!t.includes('69.0%'));}
let c=new Element('div');renderProgress(c,j);assert(text(c).includes('CPU 用量'));assert(text(c).includes('当前阶段'));
c=new Element('div');renderProgress(c,{...j,node_online:false,progress_fresh:false});assert(text(c).includes('上次整体进度'));assert(text(c).includes('上次阶段'));assert(!text(c).includes('CPU 用量'));
c=new Element('div');renderProgress(c,{...j,execution_stopped:1});assert(!text(c).includes('CPU'));
assert(batchCounts({total:2,units:[{...j,status:'uploading'},{...j,status:'queued'}]}).includes('整理结果 1 项 · 等待 1 项'));
assert(!taskTime('failed').includes('可下载'));
c=new Element('div');renderProgress(c,{...j,status:'queued',waiting_reason:'Win3：服务器未开放接单；Dell：操作系统不兼容'});assert(text(c).includes('等待资源：Win3：服务器未开放接单；Dell：操作系统不兼容'));
c=new Element('div');renderProgress(c,{...j,progress_percent:95});assert(!c.children.some(e=>(e.className||'').includes('resource-high')));
c=new Element('div');renderProgress(c,{...j,status:'succeeded'});assert(!c.children.some(e=>(e.className||'').includes('resource-high')));
assert.equal(taskStatus('failed'),'失败');assert.equal(groupNames.completed,'已结束');assert.equal(groupNames.running,'进行中');
`,context);
console.log('PASS state precedence, stale metrics, stopped execution and batch counts');

if(process.env.STATE_SNAPSHOT){
 context.snapshot=JSON.parse(fs.readFileSync(process.env.STATE_SNAPSHOT,'utf8'));
 vm.runInContext(`for(const j of snapshot.jobs){const c=new Element('div');renderProgress(c,j);if(j.status!=='running'||j.execution_stopped)assert(!text(c).includes('CPU 用量'));}for(const b of snapshot.batches)assert(batchCounts(b).includes(' / '+b.total+' 项'));`,context);
 console.log('PASS live snapshot: '+context.snapshot.jobs.length+' jobs and '+context.snapshot.batches.length+' batches');
}
vm.runInContext(`
for(const status of ['queued','held','leased','preparing','installing','running','paused','pause_requested','resume_requested','uploading','cancel_requested','recovery_needed','succeeded','failed','cancelled']){
 const record={...j,status,stage:'installing',node_online:true,has_result:false};
 for(const make of [taskJob,jobCard])assert.equal(make(record).children[0].children[1].textContent,jobStatus(record));
 if(['succeeded','failed','cancelled'].includes(status))assert.equal(jobStatus({...record,node_online:false}),taskStatus(status));
}
const endedUnits=[{...j,id:'one',status:'succeeded',has_result:true},{...j,id:'two',status:'succeeded',has_result:true}];
const endedBatch={id:'done',name:'complete',phase:'succeeded',group:'completed',total:2,completed:2,failed:0,cancelled:0,units:endedUnits,attempts:endedUnits,actions:[],nodes:[],phases:{succeeded:2},offline:true};
const walk=e=>[e,...e.children.flatMap(walk)];
for(const make of [taskBatch,batchCard]){const c=make(endedBatch);assert.equal(c.children[0].children[1].textContent,'已完成');assert.equal(walk(c).filter(x=>x.className==='result-download').length,1);}
`,context);
console.log('PASS 15 lifecycle states in both views, terminal precedence and expanded batch single download');
