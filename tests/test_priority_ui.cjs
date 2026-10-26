const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
class Element{constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.dataset={};this.listeners={};}append(...items){this.children.push(...items);}replaceChildren(){this.children=[];}addEventListener(k,f){this.listeners[k]=f;}setAttribute(){}}
function fixture(admin){const elements={},calls=[];const context={console,assert,URLSearchParams,crypto:require('crypto').webcrypto,location:{search:'',pathname:'/'},setInterval(){},document:{querySelector(s){if(s==='#nodes'&&!context.hasNodes)return null;if(s.startsWith('meta['))return{content:s==='meta[name=admin]'?String(admin):s==='meta[name=view]'?(admin?'operations':'tasks'):'fixture'};return elements[s]??=new Element('div');},querySelectorAll(){return[];},createElement:t=>new Element(t)},fetch:async(url,options)=>{calls.push({url,options});return{status:200,ok:true,json:async()=>({ids:['j'],priority:20})}},calls};vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8'),context);return context;}
(async()=>{
 const admin=fixture(true);
 await vm.runInContext(`(async()=>{
 const content=e=>e.textContent+' '+e.children.map(content).join(' '),find=(e,tag)=>e.tag===tag?e:e.children.map(x=>find(x,tag)).find(Boolean);
 const j={id:'j',status:'queued',created:100,submitted_at:50,submitted_by:'Alice',priority:0,can_set_priority:true,spec:{name:'Image tiles'},actions:[]};
 let card=taskJob(j);assert(content(card).includes('Alice'));assert(content(card).includes('上传：'));assert(content(card).includes('普通'));
 let select=find(card,'select');select.value='20';select.listeners.change();card=taskJob(j);assert.equal(find(card,'select').value,'20');
 await find(card,'button').listeners.click();assert.equal(calls[0].url,'/api/jobs/j/priority');assert.equal(JSON.parse(calls[0].options.body).priority,20);assert($('#notice').textContent.includes('1 项未启动任务'));assert(!priorityDrafts.has('jobs/j'));
 assert(!find(taskJob({...j,status:'running',can_set_priority:false}),'select'));
 const b={id:'b',name:'batch',submitted_at:50,submitted_by:'Bob',created:100,priority:10,priorities:[0,10],can_set_priority:true,units:[j],actions:[],phase:'queued',total:1,completed:0,progress_percent:0};
 const batch=taskBatch(b);assert(content(batch).includes('Bob'));assert(content(batch).includes('普通 / 高'));assert(find(batch,'select'));
 })()`,admin);
 const user=fixture(false);vm.runInContext(`const j={id:'j',status:'queued',submitted_by:'Alice',can_set_priority:false,spec:{name:'own'},actions:[]};const c=taskJob(j);assert(!c.children.some(e=>e.tag==='select'||e.textContent.includes('Alice')));`,user);
 const polling=fixture(false),responses=[];polling.hasNodes=true;polling.fetch=()=>new Promise(resolve=>responses.push(resolve));
 vm.runInContext('renderState=()=>{};updateControlFeedback=()=>{};refresh();refresh();refresh();',polling);assert.equal(responses.length,1);
 const response={ok:true,status:200,json:async()=>({ui_version:'fixture',nodes:[],batches:[]})};responses[0](response);await new Promise(setImmediate);assert.equal(responses.length,2);responses[1](response);await new Promise(setImmediate);assert.equal(responses.length,2);
 console.log('PASS state polling: one in flight, repeated refreshes coalesce into one follow-up.');
 console.log('PASS admin priority controls, upload metadata, draft retention, mutation receipt, and ordinary-user view.');
})().catch(e=>{console.error(e);process.exitCode=1;});
