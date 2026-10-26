const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
class Element {constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.dataset={};this.listeners={};this.style={};this.classList={toggle(){},add(){},remove(){}};} append(...x){this.children.push(...x);} addEventListener(k,f){this.listeners[k]=f;} replaceChildren(){this.children=[];} setAttribute(){} }
const elements={};
const context={assert,console,URLSearchParams,location:{search:'',pathname:'/'},setInterval(){},crypto:require('crypto').webcrypto,
document:{querySelector(s){if(s==='#nodes')return null;if(s.startsWith('meta['))return {content:s==='meta[name=view]'?'tasks':'test'};return elements[s]??=new Element('div');},querySelectorAll(){return[];},createElement(t){return new Element(t);}}};
vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8'),context);
vm.runInContext(`
const content=e=>e.textContent+' '+e.children.map(content).join(' ');
const failed={id:'old',name:'我的计算',lifecycle:'failed',status:'已中断',failure_detail:'旧守护终止 PID7476，53.073GiB',health:{owner:'center'},actions:['start'],percent:68};
const state={now:1000,nodes:[{id:'win3',online:true,telemetry:{simulations:[failed]}}],batches:[],issues:[{node:'win3',task:'old',state:'open',data:{owner:'center'}}]};
const card=simulationCard(state,state.nodes[0],failed),text=content(card);
assert(text.includes('未完成'));assert(!text.includes('可下载'));
for(const forbidden of ['守护','7476','53.073','反馈','重新启动','科学','复核'])assert(!text.includes(forbidden));
assert.equal(visibleTasks(state)[0].group,'completed');
const j={id:'j',spec:{name:'结果'},status:'succeeded',finished:100,has_result:true,result_review:{verdict:'failed',summary:'科学结果不符合预期'},actions:['retry']};
assert(content(jobCard({...j,status:'running',actions:['pause','resume','cancel']})).includes('取消'));assert(!content(jobCard({...j,status:'running',actions:['pause','resume','cancel']})).includes('暂停'));assert(content(jobCard(j)).includes('已完成'));assert(content(jobCard(j)).includes('下载结果'));assert(!content(jobCard(j)).includes('科学'));
const replaced={...j,status:'cancelled',detail:'已由同一逻辑单元的纯计算修订替代'};
assert(content(jobCard(replaced)).includes('已被修订版替代'));assert(!content(jobCard(replaced)).includes('已取消'));
assert.equal(jobStatus({...j,status:'queued',detail:'等待最高优先级任务实际领取资源'}),'排队中');
assert.equal(jobStatus({...j,status:'cancelled',detail:'用户取消'}),'已取消');
const b={id:'b',name:'任务',phase:'running',group:'running',progress_percent:50,total:2,completed:0,units:[{...j,status:'running',estimated_end:2000},{...j,status:'queued',estimated_end:null}],actions:[],attempts:[]};
assert.equal(batchCard(b).children.find(e=>e.tag==='strong').textContent,'完成时间估算中');
const errored={...j,status:'failed',health:{owner:'publisher'},actions:[]};
const failedCard=jobCard(errored);assert(content(failedCard).includes('查看错误日志'));assert(content(failedCard).includes('下载结果'));assert(content(failedCard).includes('上传修正版'));assert(!content(failedCard).includes('取消'));
const finishedState={nodes:[],uploads:[],batches:[{...b,id:'done',group:'completed',phase:'succeeded',units:[j],total:1},{...b,id:'error',group:'completed',phase:'failed',units:[errored],total:1},b]};
selectedStatus='completed';renderTasks(finishedState);assert.equal($('#tasks').children.length,2);assert(content($('#tasks')).includes('下载结果'));assert(content($('#tasks')).includes('查看错误日志'));
const partlyQueued={...b,units:[{...j,status:'running',estimated_end:2000,completion_estimate:{state:'range',start:1800,end:2400,confidence:'初步范围'},elapsed_seconds:90},{...j,status:'queued'}]};
const earlyCard=batchCard(partlyQueued);assert(content(earlyCard).includes('已运行 2 分钟'));assert(content(earlyCard).includes('当前运行单元预计结束'));assert.equal(earlyCard.children.find(e=>e.tag==='strong').textContent,'完成时间估算中');
assert(content(earlyCard).includes('初步范围'));
assert(!content(jobCard({...j,status:'running',estimated_end:2000})).includes('预计完成'));
const historicalOnly=batchCard({...b,units:[],historical_stage_only:true,historical_stage_count:25,total:0});
assert(content(historicalOnly).includes('不计入计算数量'));
assert(!content(historicalOnly).includes('已完成 0 / 0'));
const offlineEstimate=batchCard({...partlyQueued,offline:1});assert(!content(offlineEstimate).includes('当前运行单元预计结束'));
const live={...failed,lifecycle:'running',status:'运行中',progress_state:'fresh',remaining_seconds:100,completion_estimate:{state:'range',start:1800,end:2400,confidence:'初步范围'},actions:[]};
assert(content(simulationCard(state,state.nodes[0],live)).includes('预计完成'));
assert(!content(simulationCard(state,{...state.nodes[0],online:false},live)).includes('预计完成'));
assert(!content(simulationCard(state,state.nodes[0],{...live,progress_state:'stale'})).includes('预计完成'));
`,context);
console.log('PASS customer view: interrupted work stays visible, no operations/science leakage, truthful completion and estimates.');
vm.runInContext(`
const stageJob={id:'stage',status:'running',spec:{name:'求解',cores:8},stage_progress_percent:68,progress:{scope:'stage',stage:'Memory: 3000'},activity:{stage_label:'求解器计算中（未报告具体阶段）',cpu_cores:7.8,allocated_cpu_threads:8,observed_at:1000,progress_updated_at:990,cpu_fresh:true,state:'cpu_active'}};
const stageCard=el('div');renderProgress(stageCard,stageJob);const stageText=content(stageCard);
for(const text of ['整体进度：待更新','当前阶段进度','68.0%','当前阶段：'])assert(stageText.includes(text),text);
assert(!stageText.includes('Memory:'));
const staleCard=el('div');renderProgress(staleCard,{...stageJob,activity:{...stageJob.activity,cpu_fresh:false,state:'stale'}});
assert(!content(staleCard).includes('CPU 上次采样：'));assert(!content(staleCard).includes('CPU 计算中'));
const doneCard=el('div');renderProgress(doneCard,{...stageJob,status:'succeeded'});assert(!content(doneCard).includes('CPU 用量：'));
`,context);
console.log('PASS stage/overall separation and observed CPU timestamps.');
vm.runInContext(`
const memoryCard=el('div');renderMemoryAllocation(memoryCard,{id:'memory',status:'running',memory_allocation:{reservation_memory_gb:3,reservation_commit_gb:4.5,execution_limit_memory_gb:28,execution_limit_commit_gb:37,learned:true,reference_peak_gb:2,reference_sample_ids:['native'],peak_complete:false}});
assert.equal(memoryCard.children.length,0);
`,context);
console.log('PASS memory reservation and limit are distinct.');

vm.runInContext(`
const priorQuery=document.querySelector;
document.querySelector=s=>s==='#nodes'?(elementsForNodes):priorQuery(s);
const publicNode={id:'a800-ssh',label:'a800',online:true,telemetry:{cpu_model:'AMD EPYC 7742',cpu_percent:4,memory_total_gb:2000,memory_available_gb:1900,gpus:[],simulations:[]}};
selectedStatus='completed';
renderState({now:1000,nodes:[publicNode],jobs:[j],batches:[{...b,id:'done',group:'completed',phase:'succeeded',units:[j],total:1}],uploads:[],issues:[],workload:{active_units:1,queued_units:0}});
assert.equal(elementsForNodes.children.length,1);
assert(content($('#tasks')).includes('下载结果'));
document.querySelector=priorQuery;
`,Object.assign(context,{elementsForNodes:new Element('div')}));
console.log('PASS non-admin A800 payload renders resources and tasks without internal host inventory.');
