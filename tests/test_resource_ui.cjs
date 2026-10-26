const fs=require('fs'),vm=require('vm'),assert=require('assert');
class E{constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.dataset={};this.style={};this.attrs={};}append(...e){this.children.push(...e);}replaceChildren(){this.children=[];}addEventListener(){}setAttribute(k,v){this.attrs[k]=v;}}
let ready=false;const els={};const ctx={console,assert,URLSearchParams,setInterval(){},location:{pathname:'/',search:''},crypto:require('crypto').webcrypto,document:{querySelector(s){if(s==='#nodes'&&!ready)return null;if(s.startsWith('meta['))return{content:s==='meta[name=view]'?'tasks':'test'};return els[s]??=new E('div');},querySelectorAll(){return[];},createElement(t){return new E(t);}}};vm.createContext(ctx);
const source=fs.readFileSync(__dirname+'/../static/app.js','utf8');vm.runInContext(source,ctx);ready=true;
vm.runInContext(`
assert.equal(resourceLevel(59.9),'low');assert.equal(resourceLevel(60),'medium');assert.equal(resourceLevel(79.9),'medium');assert.equal(resourceLevel(80),'high');assert.equal(resourceLevel(NaN),'unknown');
const walk=e=>[e,...e.children.flatMap(walk)],text=e=>e.textContent+' '+e.children.map(text).join(' ');
const high=el('div');resourceMeter(high,'CPU 占用',92,'92%');assert(high.children.every(x=>x.className.includes('resource-high')));
const unknown=el('div');resourceMeter(unknown,'CPU 占用',NaN,'0%');assert(!walk(unknown).some(e=>e.tag==='progress'));assert(text(unknown).includes('暂无数据'));
const threads=el('div');renderThreadAllocation(threads,{allocated_cpu_threads:8,total_cpu_threads:8});assert(text(threads).includes('线程分配'));assert(walk(threads).filter(e=>e.tag==='progress').every(e=>e.className==='resource-high'));assert.equal(threads.children[1].value,8);
const telemetry={cpu_model:'Same CPU',cpu_percent:92,memory_total_gb:100,memory_available_gb:10,memory_percent:90,host_inventory:{at:1,home_storage:{total_gb:100,free_gb:20,path:'/private'}},storage_volumes:[{id:'volume',total_gb:100,free_gb:5}],gpus:[{index:0,name:'Same GPU',available:true,utilization:85,memory_total_gb:10,memory_free_gb:1}],simulations:[]};
const capacity={allocated_cpu_threads:8,total_cpu_threads:8,unresident_reserved_memory_gb:10,reserved_memory_gb:20,execution_limit_memory_gb:999,memory_gb:70,gpu_allocations:[]};
telemetry.resource_observation={at:Date.now()/1000};telemetry.host_inventory.at=Date.now()/1000;
const state={now:Date.now()/1000,jobs:[],batches:[],uploads:[],issues:[],nodes:['win1','win2','win3','win4','a800-ssh','dell-r940'].map(id=>({id,label:id,online:true,telemetry,capacity,active_unit_count:0}))};renderState(state);
const cards=document.querySelector('#nodes').children;
const metrics=cards.map(c=>walk(c).filter(x=>x.className?.startsWith('metric ')).map(text));for(const m of metrics)assert.deepEqual(m,metrics[0]);
for(const c of cards){const t=text(c);for(const bad of ['整机 CPU 采样','运行任务RAM上限合计','公共数据盘','本组服务器存储','专属目录','FP32'])assert(!t.includes(bad),bad);assert(walk(c).some(e=>e.tag==='progress'&&e.className==='resource-high'));}
state.nodes=[{...state.nodes[0],enabled:0,updating:true}];state.jobs=[{id:'transfer',node:state.nodes[0].id,status:'uploading'}];renderState(state);
const paused=text(document.querySelector('#nodes').children[0]);assert(paused.includes('暂停接单'));assert(!paused.includes('可分配内存'));assert(!paused.includes('可接新任务'));
state.nodes=[{...state.nodes[0],enabled:1,updating:false,telemetry:{...telemetry,resource_observation:{at:state.now-90},host_inventory:{at:state.now-90}}}];state.jobs=[];renderState(state);const stale=text(document.querySelector('#nodes').children[0]);assert(stale.includes('CPU 占用等待更新'));assert(stale.includes('GPU 占用等待更新'));assert(!stale.includes('92.0%'));assert(!stale.includes('可分配内存'));
`,ctx);
const css=fs.readFileSync(__dirname+'/../static/brand.css','utf8');assert(css.includes('.resource-high{--resource-color:#d12e3c}'));assert(css.includes('progress[class*="resource-"]::-webkit-progress-value'));console.log('PASS uniform six-node metrics, high/medium/low colors, missing values and removed diagnostics');
