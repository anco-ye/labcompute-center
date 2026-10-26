'use strict';
const $=s=>document.querySelector(s), csrf=$('meta[name=csrf]').content;
const labels={queued:'排队中',held:'队列已暂停',pause_requested:'正在暂停',paused:'已暂停',resume_requested:'正在继续',leased:'已分配',preparing:'准备计算',running:'运行中',installing:'安装依赖',uploading:'回传结果',cancel_requested:'正在取消',succeeded:'已完成',failed:'失败',cancelled:'已取消',recovery_needed:'正在恢复同步'};
const el=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
const date=t=>t?new Date(t*1000).toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';
const number=n=>Number(n||0).toFixed(1);
const storage=n=>n>=1024?(n/1024).toFixed(2)+' TiB':number(n)+' GiB';
function gpuModelLabel(gpu){return gpu.name||'GPU 型号未知';}

let pendingMutations=0, pageReloading=false;
const loadedUiVersion=$('meta[name=ui-version]')?.content;
const operationsView=$('meta[name=view]')?.content==='operations';
const administrator=$('meta[name=admin]')?.content==='true',priorityDrafts=new Map(),priorityFeedback=new Map();
const priorityName=n=>({'-10':'低',0:'普通',10:'高',20:'紧急'}[n]??String(n));
function taskMetadata(card,item,kind){
if(!administrator||!operationsView)return;
card.append(el('p','提交者：'+(item.submitted_by||'未知')+' · 上传：'+date(item.submitted_at||item.created),'hint task-metadata'));
const priorities=kind==='batches'?(item.priorities||[]):[item.priority||0];
if(priorities.length)card.append(el('p','优先级：'+priorities.map(priorityName).join(' / '),'hint'));
if(!item.can_set_priority)return;
const key=kind+'/'+item.id,box=el('div',undefined,'priority-control'),label=el('label','排队优先级'),select=el('select');select.setAttribute('aria-label',item.name||item.spec?.name||'任务优先级');
const values=[...new Set([-10,0,10,20,...priorities,...(priorityDrafts.has(key)?[Number(priorityDrafts.get(key))]:[])])].sort((a,b)=>a-b);
for(const value of values){const option=el('option',priorityName(value));option.value=value;select.append(option);}
select.value=priorityDrafts.get(key)??item.priority??0;select.addEventListener('change',()=>priorityDrafts.set(key,select.value));label.append(select);box.append(label);
const receipt=el('p',priorityFeedback.get(key)||'','hint priority-receipt');receipt.setAttribute('role','status');
box.append(button('应用优先级',async()=>{select.disabled=true;try{const result=await api('/api/'+key+'/priority',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({priority:Number(select.value)})});priorityDrafts.delete(key);const text='已将 '+result.ids.length+' 项未启动任务设为'+priorityName(result.priority)+'优先级；已启动任务保持原分配。';priorityFeedback.set(key,text);receipt.textContent=text;notify(text);}catch(e){const text='调整失败：'+e.message;priorityFeedback.set(key,text);receipt.textContent=text;throw e;}finally{select.disabled=false;}}));card.append(box,receipt);
}
// The task view describes the service outcome. Diagnostic evidence stays in operations.
function taskStatus(status){return {failed:'失败',recovery_needed:'同步恢复中',installing:'准备计算',leased:'准备计算',preparing:'准备计算',uploading:'整理结果',held:'已暂停排队'}[status]||labels[status]||'状态更新中';}
function taskShell(name,status){const card=el('article',undefined,'simulation-card'),head=el('div',undefined,'node-head');head.append(el('strong',name),el('span',status,'pill'));card.append(head);return card;}
const estimateDate=t=>new Date(t*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
function estimateText(e){
if(e?.state==='range'&&Number.isFinite(e.start)&&Number.isFinite(e.end))return '预计完成 '+estimateDate(e.start)+' — '+estimateDate(e.end)+' · '+(e.confidence||'初步范围');
return e?.reason||'完成时间估算中';
}
function combinedEstimate(items){
const ranges=items.map(j=>j.completion_estimate);
return items.length&&ranges.every(e=>e?.state==='range')?{state:'range',start:Math.max(...ranges.map(e=>e.start)),end:Math.max(...ranges.map(e=>e.end)),confidence:ranges.every(e=>e.confidence==='参考范围')?'参考范围':'初步范围'}:null;
}
function taskTime(status,end,finished,estimate){
if(status==='succeeded')return finished?'完成于 '+date(finished):'计算已完成';
if(status==='cancelled')return '已取消';
if(status==='failed')return '本次计算失败';
if(['paused','held','pause_requested'].includes(status))return '继续后更新预计完成时间';
if(status==='queued')return '排队中，启动后更新预计完成时间';
if(['uploading','cancel_requested','recovery_needed','resume_requested'].includes(status))return status==='uploading'?'计算已结束，结果准备中':status==='cancel_requested'?'正在取消':status==='resume_requested'?'正在继续':'连接恢复后更新预计完成时间';
return estimateText(estimate);
}
function taskFailureActions(card,j){
if(j.status!=='failed'&&!stoppedRecovery(j))return;
card.append(button('查看错误日志',async()=>{$('#log-title').textContent=j.spec.name+' · 错误日志';$('#log-text').textContent=(await api('/api/jobs/'+j.id+'/log')).text;$('#log-dialog').showModal();}));
card.append(button('处理状态与反馈',()=>openTaskFeedback(j.id)));
if(j.health?.owner==='publisher'){const link=el('a','上传修正版');link.href='/upload';card.append(link);}
}
function queueWaitInfo(job,context=[]){
if(job.status!=='queued')return null;
// Prefer the current scheduler response to dependency fields from older specs.
let reason=String(job.waiting_reason||'').trim();
if(!reason){
 reason=String(job.spec?.dependency_wait||'').trim();
 if(!reason){
  const latest=new Map();
  for(const prior of context){const key=prior.spec?.task_key;if(!key)continue;const old=latest.get(key);if(!old||(prior.created||0)>(old.created||0)||((prior.created||0)===(old.created||0)&&String(prior.id)>String(old.id)))latest.set(key,prior);}
  for(const key of job.spec?.depends_on||[]){
   const prior=latest.get(key);
   if(!prior){reason='前序任务状态尚未同步：'+key;break;}
   if(prior.status!=='succeeded'){reason=(['failed','cancelled'].includes(prior.status)?'前序任务失败或取消，请先处理前序任务：':'等待前序任务完成：')+key;break;}
  }
 }
}
if(!reason)return {kind:'unknown',label:'等待原因待同步',reason:'调度原因尚未返回，页面将自动更新'};
if(/^(等待前序任务|前序任务|等待依赖|依赖未完成|依赖失败)/.test(reason))return {kind:'dependency',label:'等待依赖',reason};
if(reason==='已匹配可用资源，等待服务器领取')return {kind:'claim',label:'等待领取',reason};
return {kind:'resource',label:'等待资源',reason};
}
function renderQueueReasons(card,jobs,context=jobs){
const groups=new Map();
for(const job of jobs){const wait=queueWaitInfo(job,context);if(!wait)continue;let group=groups.get(wait.kind);if(!group){group={label:wait.label,count:0,reasons:new Map()};groups.set(wait.kind,group);}group.count++;group.reasons.set(wait.reason,(group.reasons.get(wait.reason)||0)+1);}
for(const [kind,group]of groups){
 const reasons=[...group.reasons];const text=reasons.slice(0,3).map(([reason,count])=>(count>1?count+'项：':'')+reason).join('；');
 const count=jobs.length>1?'（'+group.count+'项）':'';
 card.append(el('p',group.label+count+'：'+text+(reasons.length>3?'；另有'+(reasons.length-3)+'类原因，见计算明细':''),'hint queue-reason queue-'+kind));
}
}
function renderProgress(card,j){
const progress=j.progress||{},activity=j.activity||{};
const running=j.status==='running'&&!j.execution_stopped;
const fresh=running&&j.node_online!==false&&j.progress_fresh!==false;
if(j.status==='succeeded'){meter(card,'整体进度',100,'已完成');return;}
if(['failed','cancelled'].includes(j.status))return;
if(!running){
 const stages={queued:'等待调度',held:'队列已暂停',leased:'资源已预留，等待启动确认',preparing:'准备计算',installing:'安装依赖',uploading:'计算已结束，结果准备中',paused:'已暂停',pause_requested:'等待暂停确认',resume_requested:'等待继续确认',cancel_requested:'等待取消确认',recovery_needed:'等待执行状态同步',failed:'本次计算失败',cancelled:'已取消'};
 card.append(el('p','当前阶段：'+(stoppedRecovery(j)?'执行已停止，已有输出保留':stages[j.status]||(j.execution_stopped?'执行已停止':'等待状态更新')),'hint'));
 if(j.status==='queued')renderQueueReasons(card,[j]);
 if(operationsView&&j.execution_stopped&&j.detail&&!stoppedRecovery(j))card.append(el('p',j.detail,'hint'));
 return;
}
if(Number.isFinite(j.progress_percent))meter(card,fresh?'整体进度':'上次整体进度',j.progress_percent,number(j.progress_percent)+'%');
else if(Number.isFinite(j.last_progress_percent))meter(card,'上次整体进度',j.last_progress_percent,number(j.last_progress_percent)+'%');
else card.append(el('p','整体进度：待更新','hint'));
const stage=activity.stage_label||(progress.stage&&!/^memory:/i.test(progress.stage)?progress.stage:null);
card.append(el('p',(fresh?'当前阶段：':'上次阶段：')+(stage||'阶段待更新'),'hint'));
if(Number.isFinite(j.stage_progress_percent))meter(card,fresh?'当前阶段进度':'上次阶段进度',j.stage_progress_percent,number(j.stage_progress_percent)+'%');
for(const metric of progress.metrics||[]){meter(card,(fresh?'':'上次')+metric.name,metric.completed/metric.total*100,number(metric.completed)+' / '+number(metric.total)+(metric.unit?' '+metric.unit:''));}
if(Number.isFinite(progress.completed)&&progress.scope!=='stage'&&progress.unit)card.append(el('p',(fresh?'':'上次：')+number(progress.completed)+' / '+number(progress.total)+' '+progress.unit,'hint'));
if(!operationsView)return;
const cpuFresh=activity.cpu_fresh&&j.node_online!==false;
const cpu=Number.isFinite(activity.cpu_cores)?number(activity.cpu_cores)+' 核':'暂无采样';
card.append(el('p',(cpuFresh?'CPU 用量：':'CPU 上次采样：')+cpu+(Number.isFinite(activity.allocated_cpu_threads)?' · 分配 '+activity.allocated_cpu_threads+' 逻辑 CPU':''),'hint'));
card.append(el('p','运行观测更新时间：'+date(activity.observed_at)+(cpuFresh?'':' · 等待新观测'),'hint'));
if(activity.progress_updated_at)card.append(el('p','进度来源更新时间：'+date(activity.progress_updated_at),'hint'));
}
function batchCounts(b){
const counts=new Map();
for(const j of b.units){const label=j.status==='running'&&!j.execution_stopped?'计算中':j.status==='uploading'?'整理结果':j.status==='queued'?'等待':j.status==='succeeded'?'已完成':j.status==='failed'?'失败':j.status==='cancelled'?'已取消':j.status==='paused'||j.status==='held'?'已暂停':j.execution_stopped?'已停止':taskStatus(j.status);counts.set(label,(counts.get(label)||0)+1);}
return '已完成 '+(counts.get('已完成')||0)+' / '+b.total+' 项'+[...counts].filter(([k])=>k!=='已完成').map(([k,v])=>' · '+k+' '+v+' 项').join('');
}
function retiredTaskLabel(j){if(j.status!=='cancelled')return '';const detail=String(j.detail||'');if(detail==='已由同一逻辑单元的纯计算修订替代')return '已被修订版替代';if(detail==='复核/汇总移至提交者Mac，移出中心计算图')return '旧阶段已停用';return '';}
function stoppedRecovery(j){return Boolean(j.execution_stopped)&&j.status==='recovery_needed';}
function jobStatus(j){
if(['succeeded','failed','cancelled'].includes(j.status))return retiredTaskLabel(j)||taskStatus(j.status);
if(stoppedRecovery(j))return '已停止，待处理';
if(j.status==='running'&&j.execution_stopped)return '计算已结束，状态同步中';
if(j.node_online===false&&jobGroup(j)==='running')return '状态待同步';
return taskStatus(j.status);
}
function batchStatus(b){if(b.historical_stage_only)return '历史阶段已停用';return b.units?.length&&b.units.every(j=>retiredTaskLabel(j))?'历史阶段已停用或替代':taskStatus(b.phase);}
function renderMemoryAllocation(card,j){
if(!operationsView)return;
const a=j.memory_allocation;if(!a)return;
const details=el('details',undefined,'node-details');details.dataset.key='memory-budget-'+j.id;details.append(el('summary','内存预留与上限'));
if(Number.isFinite(a.reservation_memory_gb))details.append(el('p','调度预留：RAM '+number(a.reservation_memory_gb)+' GiB · 提交 '+number(a.reservation_commit_gb)+' GiB','hint'));
details.append(el('p',(['succeeded','failed','cancelled'].includes(j.status)?'历史执行上限：RAM ':'执行上限：RAM ')+number(a.execution_limit_memory_gb)+' GiB · 提交预算 '+number(a.execution_limit_commit_gb)+' GiB','hint'));
if(a.initial_declaration)details.append(el('p','原始申报：RAM '+number(a.initial_declaration.memory_gb)+' GiB · 提交 '+number(a.initial_declaration.commit_gb)+' GiB','hint'));
if(Number.isFinite(a.reference_peak_gb))details.append(el('p','完整历史参考峰值 '+number(a.reference_peak_gb)+' GiB · '+a.reference_sample_ids.length+' 个可比执行','hint'));
if(a.peak_complete&&Number.isFinite(a.final_peak_gb))details.append(el('p','本次完整运行观测峰值 '+number(a.final_peak_gb)+' GiB','hint'));
else if(Number.isFinite(a.observed_peak_gb))details.append(el('p',(j.status==='running'&&!j.execution_stopped?'运行中累计观测峰值：RAM ':'本次停止前观测峰值：RAM ')+number(a.observed_peak_gb)+' GiB · 提交 '+number(a.observed_peak_private_gb)+' GiB；'+(j.status==='succeeded'?'观测记录未覆盖全程':'执行未完成')+'','hint'));
else if(j.status==='running')details.append(el('p','运行中，最终峰值待记录','hint'));
details.append(el('p',!Number.isFinite(a.reservation_memory_gb)?'执行已结束，调度预留已释放。':a.learned?'预留已按历史峰值校正':'预留依据：提交预算','hint'));
card.append(details);
}
function nodeResultLink(j){
if(!j.node||!(j.execution_stopped||j.status==='uploading'||j.has_result))return null;
const loc=globalThis.location;if(!loc?.hostname)return null;
const base=(!loc.port||loc.port==='443')?'https://'+loc.hostname+'/direct':'https://'+loc.hostname+':8445';
const link=el('a','下载结果');link.href=base+'/api/jobs/'+j.id+'/download';link.target='_blank';link.rel='noopener noreferrer';return link;
}
function taskResultLink(j){
let link;
if(j.has_result&&!j.spec?.result_transfer_error){link=el('a','下载结果');link.href='/api/jobs/'+j.id+'/result';}
else link=nodeResultLink(j);
if(link)link.className='result-download';return link;
}
function taskJob(j,controls=true,downloads=true){
const card=taskShell(j.spec.name,jobStatus(j));
taskMetadata(card,j,'jobs');
renderProgress(card,j);renderMemoryAllocation(card,j);
card.append(el('strong',(stoppedRecovery(j)?j.detail:'')||retiredTaskLabel(j)||taskTime(j.status,j.node_online===false?null:j.estimated_end,j.finished,j.node_online===false?null:j.completion_estimate),'remaining'));
if(j.status==='failed')bindTaskFeedback(card,j.id);
if(j.status==='running'&&j.completion_estimate?.state==='range')card.append(el('p',j.completion_estimate.source+' · 阶段变化后重新估算','hint'));
if(Number.isFinite(j.elapsed_seconds)&&j.status==='running'&&!j.execution_stopped)card.append(el('p','已运行 '+duration(j.elapsed_seconds),'hint'));
taskFailureActions(card,j);
if(j.status==='failed'&&/memory_gb|commit_gb/.test(j.health?.next_action||''))card.append(el('p',j.health.next_action,'hint'));
if(j.status==='failed'&&j.health?.owner==='publisher')card.append(el('p','任务输入或程序有误，请修正任务包后重新提交。','hint'));
for(const action of controls?(j.actions||[]).filter(a=>['cancel'].includes(a)):[]){card.append(button({pause:'暂停',resume:'继续',cancel:'取消'}[action],async()=>{if(action==='cancel'&&!confirm('取消“'+j.spec.name+'”？'))return;await api('/api/jobs/'+j.id+'/'+action,{method:'POST'});notify('操作已提交');}));}
const download=downloads?taskResultLink(j):null;if(download)card.append(download);
return card;
}
function taskUnitRow(j){
const row=el('div',undefined,'task-unit-row'),head=el('div',undefined,'node-head');
head.append(el('span',j.spec.name),el('span',jobStatus(j),'pill'));row.append(head);
if(j.finished)row.append(el('small','结束于 '+date(j.finished),'hint'));
if(!['succeeded','failed','cancelled'].includes(j.status))renderProgress(row,j);
if(j.status==='failed')row.append(button('查看错误',()=>openTaskFeedback(j.id)));
return row;
}
function batchResultAction(b){
if(b.units.some(j=>j.has_result&&!j.spec?.result_transfer_error)&&!b.units.some(j=>(!j.has_result||j.spec?.result_transfer_error)&&nodeResultLink(j))){
 const link=el('a','下载结果','result-download');link.href='/api/batches/'+b.id+'/result';return link;
}
if(b.units.some(j=>nodeResultLink(j))){const control=button('下载结果',()=>openBatchResults(b));control.className='result-download';return control;}
return null;
}
function openBatchResults(b){
const dialog=$('#results-dialog'),box=$('#results-list');if(!dialog||!box)return;
$('#results-title').textContent=b.name;box.replaceChildren();
for(const j of b.units){const link=taskResultLink(j);if(!link)continue;const row=el('div',undefined,'task-unit-row');row.append(el('span',j.spec.name),link);box.append(row);}
dialog.showModal();
}
function taskBatch(b){
const status=b.offline&&!['succeeded','failed','cancelled'].includes(b.phase)?'状态待同步':batchStatus(b),card=taskShell(b.name,status);
taskMetadata(card,b,'batches');
if(b.historical_stage_count)card.append(el('p','历史阶段 '+b.historical_stage_count+' 项，不计入计算数量','hint'));
if(b.historical_stage_only)return card;
const failedUnit=b.units.find(j=>j.status==='failed');if(failedUnit)bindTaskFeedback(card,failedUnit.id);
if(b.total===1)taskFailureActions(card,b.units[0]);
if(b.total===1){renderProgress(card,b.units[0]);renderMemoryAllocation(card,b.units[0]);}
else {
 card.append(el('p',batchCounts(b),'hint'));
 renderQueueReasons(card,b.units,[...(b.attempts||[]),...b.units]);
 for(const j of b.units.filter(j=>j.status==='running').slice(0,3)){card.append(el('p',j.spec.name,'hint'));renderProgress(card,j);}
}
const active=b.units.filter(j=>!['succeeded','failed','cancelled'].includes(j.status));
const estimates=active.map(j=>j.estimated_end);
const end=!b.offline&&active.length&&estimates.every(Number.isFinite)&&active.every(j=>j.status==='running')?Math.max(...estimates):null;
const batchEstimate=!b.offline&&active.every(j=>j.status==='running')?(active.length===1?active[0].completion_estimate:combinedEstimate(active)):null;
card.append(el('strong',taskTime(b.phase,end,Math.max(...b.units.map(j=>j.finished||0)),batchEstimate),'remaining'));
const running=active.filter(j=>j.status==='running'&&!j.execution_stopped&&j.node_online!==false);
const elapsed=running.map(j=>j.elapsed_seconds).filter(Number.isFinite);
if(elapsed.length)card.append(el('p',(running.length>1?'当前单元最长已运行 ':'已运行 ')+duration(Math.max(...elapsed)),'hint'));
if(!end&&b.phase==='running'&&!b.offline){
 const known=running.filter(j=>j.completion_estimate?.state==='range');
 if(known.length)card.append(el('p',(known.length===running.length?'当前运行单元预计结束范围：':'部分运行单元预计结束范围：')+estimateText(combinedEstimate(known)),'hint'));
}

for(const action of b.actions.filter(a=>['cancel'].includes(a))){card.append(button({pause:'暂停',resume:'继续',cancel:'取消'}[action],async()=>{if(action==='cancel'&&!confirm('取消“'+b.name+'”？已有结果保留。'))return;await api('/api/batches/'+b.id+'/'+action,{method:'POST'});notify('操作已提交');}));}
if(b.total===1){const download=taskResultLink(b.units[0]);if(download)card.append(download);}
else {const download=batchResultAction(b);if(download)card.append(download);const details=el('details');details.dataset.key='task-units-'+b.id;details.append(el('summary','计算明细'));for(const j of b.units)details.append(taskUnitRow(j));card.append(details);}
return card;
}
function taskSimulation(state,n,sim){
const status=sim.lifecycle||'unknown';
const names={running:'运行中',queued:'排队中',succeeded:'已完成',failed:'未完成',cancelled:'已取消',paused:'已暂停',unknown:'状态确认中'};
const live=['running','queued','paused','unknown'].includes(status),online=n.online||!live;
const card=taskShell(sim.name,online?(sim.progress_state==='finalizing'?'正在收尾':names[status]||'状态确认中'):'连接恢复中');
if(Number.isFinite(sim.percent))meter(card,'进度',sim.percent,number(sim.percent)+'%');
const fresh=!['stale','unreadable','finalizing'].includes(sim.progress_state);
const end=n.online&&fresh&&Number.isFinite(sim.remaining_seconds)?state.now+sim.remaining_seconds:null;
card.append(el('strong',taskTime(status,end,null,n.online&&fresh?sim.completion_estimate:null),'remaining'));
if(sim.command?.status==='pending')card.append(el('p','操作处理中','hint'));
for(const action of (sim.actions||[]).filter(a=>live&&['cancel'].includes(a))){const control=button({pause:'暂停',resume:'继续',cancel:'取消'}[action],async()=>{if(action==='cancel'&&!confirm('取消“'+sim.name+'”？已有结果保留。'))return;const response=await api('/api/nodes/'+n.id+'/simulations/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({simulation:sim.control_id,action})});pendingScienceCommand=response.id;notify('操作已提交');});control.disabled=!n.online||sim.command?.status==='pending';card.append(control);}
return card;
}
function visibleTasks(state){return allTasks(state);}
async function api(url,options={}){const mutating=!!options.method&&options.method!=='GET';if(mutating)pendingMutations++;try{options.cache='no-store';options.headers={...(options.headers||{}),'X-CSRF-Token':csrf};const r=await fetch(url,options);if(r.status===401){location.href='/login';throw Error('登录已过期');}const body=await r.json();if(!r.ok)throw Error(body.error||'请求失败');return body;}finally{if(mutating)pendingMutations--;}}
function notify(t){$('#notice').textContent=t;}
function button(text,fn){const b=el('button',text,'secondary small');b.addEventListener('click',async()=>{b.disabled=true;try{await fn();await refresh();}catch(e){notify(e.message);}finally{b.disabled=false;}});return b;}
function renderThreadAllocation(card,capacity){
const used=capacity?.allocated_cpu_threads,total=capacity?.total_cpu_threads;
if(!Number.isFinite(used)||used<0||!Number.isFinite(total)||total<=0){card.append(el('p','线程分配暂不可用','hint'));return;}
const percent=used/total*100,level=resourceLevel(percent),row=el('div',undefined,'metric resource-'+level);
row.append(el('span','线程分配'),el('strong',used+' / '+total+' 线程 · '+Math.round(percent)+'%'));card.append(row);
const bar=el('progress',undefined,'resource-'+level);bar.max=total;bar.value=Math.min(used,total);
bar.setAttribute('aria-label','已分配 '+used+' / '+total+' 个可调度线程');
bar.title='表示任务已分配的逻辑线程；实际繁忙程度见上方 CPU 占用。';card.append(bar);
}
function resourceLevel(value){return !Number.isFinite(value)?'unknown':value>=80?'high':value>=60?'medium':'low';}
function observationFresh(at,now){return Number.isFinite(at)&&Number.isFinite(now)&&now-at>=-5&&now-at<35;}
function meter(card,name,value,tail,resource=false){
const level=resourceLevel(value),row=el('div',undefined,'metric'+(resource?' resource-'+level:''));row.append(el('span',name),el('strong',Number.isFinite(value)?tail:'暂无数据'));card.append(row);
if(!Number.isFinite(value))return;
const p=el('progress',undefined,resource?'resource-'+level:undefined);p.max=100;p.value=Math.max(0,Math.min(100,value));p.setAttribute('aria-label',name+' '+tail);card.append(p);
}
function resourceMeter(card,name,value,tail){meter(card,name,value,tail,true);}
let selectedStatus='running', latestState=null;
const uploadRequests=new Map();
let pendingScienceCommand=null;
// Presentation belongs to the reader, not to a particular API response.
const expandedPanels=new Map();
const presentationStorageKey='compute-presentation:'+location.pathname;
let reloadPresentation=null;
try{const saved=JSON.parse(globalThis.sessionStorage?.getItem(presentationStorageKey)||'null');globalThis.sessionStorage?.removeItem(presentationStorageKey);if(saved&&Date.now()-saved.at<60000)reloadPresentation=saved;}catch{}
function panelKey(element){
const path=[];let current=element;
while(current){if(current.dataset?.key)path.unshift(current.dataset.key);current=current.parentElement?.closest?.('details[data-key]');}
return JSON.stringify([element.parentElement?.closest?.('[id]')?.id||'page',...path]);
}
function capturePresentation(){
for(const panel of document.querySelectorAll('details[data-key]'))expandedPanels.set(panelKey(panel),panel.open);
const scroll=[...document.querySelectorAll('[id]')].filter(e=>e.scrollTop||e.scrollLeft||e.scrollHeight>e.clientHeight||e.scrollWidth>e.clientWidth).map(e=>[e.id,e.scrollLeft,e.scrollTop]);
return {at:Date.now(),panels:[...expandedPanels],scroll,x:globalThis.scrollX||0,y:globalThis.scrollY||0};
}
function restorePresentation(saved){
for(const [key,open] of saved.panels)expandedPanels.set(key,open);
for(const panel of document.querySelectorAll('details[data-key]')){const key=panelKey(panel);if(expandedPanels.has(key))panel.open=expandedPanels.get(key);}
for(const [id,left,top] of saved.scroll){const e=document.getElementById?.(id);if(e){e.scrollLeft=left;e.scrollTop=top;}}
globalThis.scrollTo?.({left:saved.x,top:saved.y,behavior:'instant'});
}
function preservePresentation(render){
// Capture after the network wait: clicks made while a request is pending win.
const saved=reloadPresentation||capturePresentation();reloadPresentation=null;
render();restorePresentation(saved);
}
function refreshUiVersion(state){
if(!loadedUiVersion||!state.ui_version||state.ui_version===loadedUiVersion)return false;
if(pageReloading)return true;
// Protect user edits and uploads; read-only dialogs must not pin an obsolete UI.
if(pendingMutations||pendingScienceCommand||uploadRequests.size||$('#submit-button')?.disabled||$('#upload-dialog')?.open||priorityDrafts.size||[...document.querySelectorAll('input[type=file]')].some(e=>e.files?.length))return false;
try{globalThis.sessionStorage?.setItem(presentationStorageKey,JSON.stringify(capturePresentation()));}catch{}
pageReloading=true;const params=new URLSearchParams(location.search);if($('#nodes'))params.set('status',selectedStatus);
location.replace(location.pathname+(params.toString()?'?'+params.toString():''));return true;
}

function updateControlFeedback(state){if(!pendingScienceCommand)return;const sim=state.nodes.flatMap(n=>n.telemetry.simulations||[]).find(s=>s.command?.id===pendingScienceCommand);if(sim&&sim.command.status!=='pending'){notify(sim.command.status==='failed'?(operationsView?'操作失败：'+sim.command.detail:'操作未完成，中心核查中'):'操作已确认：'+(operationsView?sim.status:taskStatus(sim.lifecycle)));pendingScienceCommand=null;}}
const jobGroup=j=>['queued','held'].includes(j.status)?'queued':j.status==='cancelled'?'cancelled':['succeeded','failed'].includes(j.status)?'completed':'running';
const batchesOnNode=(state,id)=>(state.batches||[]).filter(b=>b.units.some(j=>j.node===id&&jobGroup(j)==='running'));
const simulationGroup=s=>s.lifecycle?({succeeded:'completed',failed:'completed',cancelled:'cancelled',queued:'queued',paused:'running',running:'running',unknown:'running'}[s.lifecycle]||'running'):['已取消','cancelled'].includes(s.status)?'cancelled':['已完成','计算完成','批次结束，含失败或未完成项','已中断','进程已结束，等待续跑'].includes(s.status)?'completed':['等待前序批次','等待启动','队列已暂停'].includes(s.status)?'queued':'running';
const groupNames={running:'进行中',queued:'队列',completed:'已结束',cancelled:'已取消'};
const duration=s=>{if(s===null||s===undefined)return '暂无预计时间';if(s<60)return '少于 1 分钟';if(s<3600)return Math.ceil(s/60)+' 分钟';if(s<86400)return (s/3600).toFixed(1)+' 小时';return (s/86400).toFixed(1)+' 天';};
function scienceMessage(n,sim){
if(['completed','cancelled'].includes(simulationGroup(sim)))return '';
if(!n.online)return '连接中断，当前运行状态无法确认。';
if(sim.status_detail)return sim.status_detail;
if(sim.health?.detail)return sim.health.detail;
if(sim.lifecycle==='paused'||sim.status==='已暂停')return '';
if(sim.progress_state==='unreadable'||sim.status==='状态文件暂不可读')return sim.process_state==='running'?'进程仍存活，但进度文件读取失败，无法确认最新进度。':'进度文件读取失败，运行状态尚未确认。';
if(sim.lifecycle==='unknown'||sim.status==='进程状态待确认')return '缺少有效的进程监测数据，运行状态尚未确认。';
if(sim.progress_state==='finalizing'||sim.status==='计算步已完成，结果尚未确认')return sim.result_written?'结果文件已写出，正在收尾。':'计算阶段结束，等待结果文件。';
if(sim.progress_state==='stale'||sim.status==='进程运行中，进度暂未更新')return '进程仍存活；'+(sim.kind==='batch'?'批次完成计数':'进度')+'已 '+duration(sim.source_age_seconds)+' 未更新，等待新进度。';
return '';
}
function simulationCard(state,n,sim,attention=false){
if(!operationsView)return taskSimulation(state,n,sim);
const ended=['completed','cancelled'].includes(simulationGroup(sim));
const card=el('article',undefined,'simulation-card');const head=el('div',undefined,'node-head');head.append(el('strong',n.label+' · '+sim.name),el('span',n.online||ended?sim.status:'连接中断 · 状态未确认','pill'));card.append(head);
if(sim.total!==undefined){const unit=sim.unit||'项';meter(card,'进度',sim.percent,number(sim.completed)+' / '+number(sim.total)+' '+unit+' · '+number(sim.percent)+'%');}
if(sim.queue_total)card.append(el('p','条件队列 '+sim.queue_completed+' / '+sim.queue_total+' 已完成','hint'));
if(sim.failure_detail)card.append(el('p',sim.failure_detail,'hint error'));
if(sim.health?.unresolved_failure&&sim.health.unresolved_failure!==sim.failure_detail)card.append(el('p','尚未解决：'+sim.health.unresolved_failure,'hint error'));
const message=scienceMessage(n,sim);if(message)card.append(el('p',message,'hint error'));
if(!ended&&!message){const text=sim.completion_estimate?estimateText(sim.completion_estimate):sim.status==='等待前序批次'?'等待前序批次完成':'完成时间估算中';if(text)card.append(el('strong',text,'remaining'));}
const details=el('details',undefined,'task-details');details.dataset.key=(attention?'attention-':'')+'science-'+n.id+'-'+sim.id;
const info=[];if(sim.elapsed_seconds!=null)info.push('已用 '+duration(sim.elapsed_seconds));if(sim.running?.length)info.push(sim.running.length+' 项并行');
details.append(el('summary',info.length?info.join(' · ')+' · 详情':'任务详情'));
if(!ended&&!message&&n.online&&sim.completion_estimate?.state==='range')details.append(el('p',estimateText(sim.completion_estimate)+' · '+sim.completion_estimate.source,'hint'));
if(!ended&&sim.source_age_seconds!=null)details.append(el('p','进度更新于 '+(sim.source_age_seconds<60?'刚刚':Math.floor(sim.source_age_seconds/60)+' 分钟前'),'hint'));
for(const name of sim.running||[])details.append(el('p',name,'hint'));
if(details.children.length>1)card.append(details);else if(info.length)card.append(el('p',info.join(' · '),'hint'));
if(sim.failed)card.append(el('p','失败 '+sim.failed+' 项','hint error'));
if(sim.command?.status==='pending')card.append(el('p','操作处理中：'+sim.command.detail,'hint'));
else if(sim.command?.status==='failed')card.append(el('p','操作失败：'+sim.command.detail,'hint'));
for(const action of ended&&sim.status!=='已中断'?[]:sim.actions||[]){const names={pause:'暂停',resume:'继续',cancel:'取消',start:'重新启动'};const control=button(names[action],async()=>{if(action==='cancel'&&!confirm('停止“'+sim.name+'”？已有结果保留。'))return;if(action==='start'&&!confirm('保留已有结果并重新启动“'+sim.name+'”？'))return;const response=await api('/api/nodes/'+n.id+'/simulations/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({simulation:sim.control_id,action})});pendingScienceCommand=response.id;notify('操作已提交，等待节点确认');});control.disabled=!n.online||sim.command?.status==='pending';card.append(control);}
return card;
}
function failureReason(j){const d=String(j.detail||'');return /^(结果回传失败|依赖 |项目依赖|基础环境|Python 独立|超过任务|超过最长|执行意外中断|环境准备超时|任务入口已退出)/.test(d)?d.slice(0,240):'执行失败，中心正在核查原因；已有输出和日志保留。';}
function jobCard(j,controls=true,downloads=true){
if(!operationsView)return taskJob(j,controls,downloads);
const card=el('article',undefined,'simulation-card'),head=el('div',undefined,'node-head');head.append(el('strong',j.spec.name),el('span',jobStatus(j),'pill'));card.append(head);
card.append(el('p','提交 '+date(j.created)+(j.node?' · '+j.node:''),'hint'));
taskMetadata(card,j,'jobs');
if(j.status==='failed'){card.append(el('p',j.health?.detail||failureReason(j),'hint'));if(j.health?.next_action)card.append(el('p',j.health.next_action,'hint'));}
if(j.node_online===false&&jobGroup(j)==='running')card.append(el('p','设备连接中断，等待重连确认','hint'));
renderProgress(card,j);renderMemoryAllocation(card,j);
if(j.status!=='queued')card.append(el('p',j.finished?'结束 '+date(j.finished):j.completion_estimate?.state==='range'?estimateText(j.completion_estimate):labels[j.status]||'状态未识别：'+j.status,'hint'));
if(j.events?.length){const timeline=el('details');timeline.dataset.key='events-'+j.id;timeline.append(el('summary','状态记录'));for(const e of j.events)timeline.append(el('p',date(e.at)+' · '+(labels[e.status]||e.status),'hint'));card.append(timeline);}
card.append(button('日志',async()=>{$('#log-title').textContent=j.spec.name;$('#log-text').textContent=(await api('/api/jobs/'+j.id+'/log')).text;$('#log-dialog').showModal();}));
for(const action of controls?(j.actions||[]):[]){const names={start:'恢复排队',pause:'暂停',resume:'继续',retry:j.status==='succeeded'?'再次运行':'重新排队',cancel:'取消'};card.append(button(names[action],async()=>{if(action==='cancel'&&!confirm('取消任务“'+j.spec.name+'”？'))return;const r=await api('/api/jobs/'+j.id+'/'+action,{method:'POST'});notify(action==='retry'?'已重新排队，原结果保留':'操作已提交');if(r.id)selectedStatus='queued';}));}
if(downloads){const link=taskResultLink(j);if(link)card.append(link);}return card;
}
function applicationStatus(t,app){
const matched=Object.values(t.runtimes||{}).filter(r=>String(r.label||'').toLowerCase()===String(app.name||'').toLowerCase());
if(!matched.length)return app.batch_ready?'支持上传计算':app.availability_detail||'已安装';
return matched.some(r=>r.status==='ready')?'支持上传计算':matched.some(r=>r.status==='unverified')?'后台计算尚未验证通过':'运行环境尚未发布';
}
function batchCard(b){
if(!operationsView)return taskBatch(b);
const card=el('article',undefined,'simulation-card'),head=el('div',undefined,'node-head');
const status=b.offline&&!['succeeded','failed','cancelled'].includes(b.phase)?'状态待同步':batchStatus(b);
head.append(el('strong',b.name),el('span',status,'pill'));card.append(head);
if(b.historical_stage_only){card.append(el('p','历史阶段 '+b.historical_stage_count+' 项，不计入计算数量','hint'));return card;}
if(b.historical_stage_count)card.append(el('p','历史阶段 '+b.historical_stage_count+' 项，不计入计算数量','hint'));
if(b.total===1)renderProgress(card,b.units[0]);
else meter(card,'完成单元',b.completed_units_percent??b.completed/b.total*100,b.completed+' / '+b.total+' 项已完成');
const phases=Object.entries(b.phases||{}).map(([key,count])=>(labels[key]||key)+' '+count);if(b.failed)phases.push('失败 '+b.failed);const retired=b.units.filter(j=>retiredTaskLabel(j)).length;if(b.cancelled>retired)phases.push('取消 '+(b.cancelled-retired));if(retired)phases.push('历史停用或替代 '+retired);if(phases.length)card.append(el('p',phases.join(' · '),'hint'));
if(b.failed){const failures=[...new Set(b.units.filter(j=>j.status==='failed').map(failureReason))];card.append(el('p',failures.slice(0,2).join('；'),'hint'));}
if(b.offline)card.append(el('p',b.phase==='cancel_requested'?'取消已记录，设备重连后自动停止；确认停止前保留资源。':'等待设备重连，自动恢复状态同步。','hint'));
card.append(el('p','提交 '+date(b.created)+(b.nodes.length?' · '+b.nodes.join('、'):''),'hint'));
taskMetadata(card,b,'batches');
if(b.total>1)renderQueueReasons(card,b.units,[...(b.attempts||[]),...b.units]);
const retryItems=b.units.filter(j=>['failed','cancelled'].includes(j.status)),transferRetry=retryItems.some(j=>j.spec.result_transfer_error);
for(const action of b.actions){const names={pause:'暂停批次',resume:'继续批次',cancel:'取消批次',retry:retryItems.length&&retryItems.every(j=>j.spec.result_transfer_error)?'重新回传结果':'重试未完成项'};card.append(button(names[action],async()=>{if(action==='cancel'&&!confirm('取消整个批次“'+b.name+'”？已有结果保留。'))return;await api('/api/batches/'+b.id+'/'+action,{method:'POST'});if(action==='retry'){selectedStatus=transferRetry?'running':'queued';$('#history-dialog')?.close();}notify(action==='retry'?(transferRetry?'已重试；已有计算结果将直接回传':'未完成项已重新排队，已完成结果保留'):'批次操作已提交');}));}
const download=batchResultAction(b);if(download)card.append(download);
const details=el('details');details.dataset.key='batch-'+b.id;details.append(el('summary','查看批次明细（'+b.total+' 项）'));for(const j of b.units)details.append(jobCard(j,false,false));
const current=new Set(b.units.map(j=>j.id)),prior=b.attempts.filter(j=>!current.has(j.id));if(prior.length){const history=el('details');history.dataset.key='attempts-'+b.id;history.append(el('summary','先前执行记录（'+prior.length+'）'));for(const j of prior)history.append(jobCard(j,false));details.append(history);}card.append(details);return card;
}
function uploadCard(u){
const card=el('article',undefined,'simulation-card'),head=el('div',undefined,'node-head');head.append(el('strong',u.name),el('span',u.status==='failed'?'上传失败':u.status==='cancelled'?'已取消':'上传中','pill'));card.append(head,el('p',u.detail||'接收任务包中','hint'));
if(administrator&&operationsView)card.append(el('p','提交者：'+(u.submitted_by||'未知')+' · 上传：'+date(u.created),'hint task-metadata'));
if(['receiving','processing'].includes(u.status))card.append(button('取消上传',async()=>{await api('/api/uploads/'+u.id+'/cancel',{method:'POST'});uploadRequests.get(u.id)?.abort();uploadRequests.delete(u.id);}));
else {const link=el('a','重新上传');link.href='/upload';card.append(link);}return card;
}
function allTasks(state){
return [...(state.batches||[]).map(b=>({type:'batch',item:b,group:b.group,created:b.group==='queued'?(b.queue_created??b.created):b.created,priority:b.priority||0})),...state.nodes.flatMap(n=>(n.telemetry.simulations||[]).map(sim=>({type:'simulation',item:sim,n,group:simulationGroup(sim),created:sim.enqueued_at||sim.created||0}))),...(state.uploads||[]).map(u=>({type:'upload',item:u,group:u.status==='cancelled'?'cancelled':u.status==='failed'?'completed':'queued',created:u.created}))];
}
function attentionTasks(state){
const items=allTasks(state).filter(x=>x.type==='batch'?x.item.failed>0:x.type==='upload'?x.item.status==='failed':x.item.lifecycle==='failed'||['已中断','批次结束，含失败或未完成项','进程已结束，等待续跑'].includes(x.item.status)||(!['completed','cancelled','queued'].includes(x.group)&&!!scienceMessage(x.n,x.item)));
const present=new Set(items.filter(x=>x.type==='simulation').map(x=>x.n.id+'|'+String(x.item.id).replaceAll('\\','/').toLowerCase()));
for(const item of items.filter(x=>x.type==='batch'))for(const j of item.item.units||[])present.add(j.node+'|job:'+j.id);
for(const issue of state.issues||[])if(!present.has(issue.node+'|'+issue.task))items.push({type:'issue',item:issue});
return items;
}
function issueCard(issue){const card=el('article',undefined,'simulation-card');card.append(el('strong',issue.node+' · '+issue.data.name),el('p',issue.data.detail,'hint error'));return card;}
function relatedIssues(state,item){
if(item.type==='issue')return [item.item];
const keys=item.type==='simulation'?[item.n.id+'|'+String(item.item.id).replaceAll('\\','/').toLowerCase()]:item.type==='batch'?(item.item.units||[]).flatMap(j=>[j.node+'|job:'+j.id]):[];
return (state.issues||[]).filter(i=>keys.includes(i.node+'|'+i.task));
}
function issueGuidance(issue){
if(issue.state==='resolved')return '已解决，原故障与处理记录保留。';
if(issue.data.owner==='publisher')return '需提交者修复。下载错误报告后，修正任务包并重新上传。';
if(issue.data.code==='progress_stale')return '中心正在核对进程活动与原始进度。';
if(issue.data.code==='progress_finalizing')return '中心核对进程退出和结果完整性，尚未确认任务完成。';
if(issue.data.code==='progress_unreadable')return '中心正在恢复进度读取。';
if(issue.data.code==='monitor_unavailable')return '中心正在恢复监测并核对真实状态。';
return '任务已停止，已有结果保留；点击查看原始错误。';
}
function appendIssueActions(card,issue){
card.append(el('p',(issue.data.owner==='publisher'?'提交者负责 · ':'中心负责 · ')+issueGuidance(issue),'issue-guidance'));
const id=issue.data.error_report?.report_id;if(!id)return;
card.append(button('错误详情',()=>openIssue(id)));
const link=el('a','下载错误报告','issue-report-link');link.href='/api/issues/'+id+'/report';link.download='';card.append(link);
}
let currentIssue=null,currentFeedbackJob=null,feedbackAttempt=null;
function bindTaskFeedback(card,id){const badge=card.children[0].children[1];badge.setAttribute('role','button');badge.setAttribute('tabindex','0');badge.setAttribute('title','查看任务详情');const open=()=>openTaskFeedback(id).catch(e=>notify(e.message));badge.addEventListener('click',open);badge.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open();}});}
let nativeErrorBlob=null;
function nativeTaskError(data,id){
let report=null;
const row=[...(data.feedback||[])].reverse().find(item=>item.request_id==='system:native-exit-v1'||item.request_id?.startsWith('system:native-exit-v1:'));try{report=row?JSON.parse(row.reply):null;}catch{report=null;}
if(!report)report=data.native_error;
return report&&report.schema_version===1&&report.source==='node_runner'&&report.job_id===id?report:null;
}
function showNativeTaskError(data,id){
const report=nativeTaskError(data,id),summary=$('#issue-summary'),link=$('#issue-report');
if(nativeErrorBlob){URL.revokeObjectURL(nativeErrorBlob);nativeErrorBlob=null;}
summary.style.whiteSpace='pre-wrap';link.hidden=!report;
if(!report){
const log=typeof data.raw_log==='string'?data.raw_log.trim():'';
const traceback=log.lastIndexOf('Traceback (most recent call last):');const excerpt=traceback>=0?log.slice(traceback).slice(-12000):log.split('\n').slice(-24).join('\n').slice(-6000);
summary.textContent=log?'原始报错\n'+excerpt:(data.detail||'节点原始错误记录尚未到达，平台正在同步。');
if(log){nativeErrorBlob=URL.createObjectURL(new Blob([JSON.stringify({schema_version:1,source:'node_log',job_id:id,log,ai_generated:false},null,2)],{type:'application/json'}));link.hidden=false;link.href=nativeErrorBlob;link.download='native-error-'+id+'.json';link.textContent='下载原始错误';}
return;
}
const evidence=Array.isArray(report.evidence)?report.evidence:[];
summary.textContent='节点原始报错（自动采集，未经 AI 改写）\n'+evidence.map(item=>item.source+'\n'+(item.context||item.message)).join('\n\n')+(evidence.length?'':'\n'+report.detail);
nativeErrorBlob=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));link.href=nativeErrorBlob;link.download='native-error-'+id+'.json';link.textContent='下载原始错误 JSON（供 Agent 读取）';
}
function hideFeedbackActions(){const form=$('#issue-feedback-form');if(form)form.hidden=true;const history=$('#issue-feedback-history')?.closest?.('details');if(history)history.hidden=true;}
async function openTaskFeedback(id){
const data=await api('/api/jobs/'+id+'/feedback');currentIssue=null;currentFeedbackJob=id;
if(!nativeTaskError(data,id)){try{data.raw_log=(await api('/api/jobs/'+id+'/log')).text;}catch{data.raw_log='';}data.detail=(latestState?.jobs||[]).find(j=>j.id===id)?.detail;}
$('#issue-title').textContent=data.name+' · 错误详情';$('#issue-owner').textContent=taskStatus(data.status);showNativeTaskError(data,id);$('#issue-next').textContent='';hideFeedbackActions();$('#issue-dialog').showModal();
}
async function openIssue(id){
const data=await api('/api/issues/'+id);currentIssue=data;currentFeedbackJob=null;feedbackAttempt=null;
$('#issue-title').textContent=data.issue.data.name||'问题详情';
$('#issue-owner').textContent=(data.issue.data.owner==='publisher'?'提交者负责':'中心负责')+' · '+(data.issue.state==='resolved'?'已解决':'尚未解决');
$('#issue-summary').textContent=data.issue.data.detail||'节点原始记录正在同步。';
$('#issue-next').textContent='';
$('#issue-report').href='/api/issues/'+id+'/report';$('#issue-report').hidden=!operationsView&&data.issue.data.owner!=='publisher';
hideFeedbackActions();$('#issue-dialog').showModal();
}
$('#issue-close')?.addEventListener('click',()=>$('#issue-dialog').close());
function renderAttention(state){
if(!operationsView)return;
const issues=attentionTasks(state),section=$('#attention'),box=$('#attention-tasks');if(!section||!box)return;section.hidden=!issues.length;box.replaceChildren();$('#attention-count').textContent=issues.length+' 项 · 按责任方处理';
for(const x of issues){const card=x.type==='simulation'?simulationCard(state,x.n,x.item,true):x.type==='batch'?batchCard(x.item):x.type==='issue'?issueCard(x.item):uploadCard(x.item);const related=relatedIssues(state,x);if(related.length>1){const group=el('details');group.dataset.key='issue-actions-'+x.item.id;group.append(el('summary','错误详情（'+related.length+' 项）'));for(const issue of related){const item=el('div');item.append(el('strong',issue.data.name));appendIssueActions(item,issue);group.append(item);}card.append(group);}else for(const issue of related)appendIssueActions(card,issue);if(!related.length&&x.type==='batch')card.append(el('p','处理方式：展开明细查看失败日志；需要继续时，重试未完成项。','issue-guidance'));box.append(card);}
}
function renderTasks(state){
const all=visibleTasks(state),items=all.filter(x=>x.group===selectedStatus).sort((a,b)=>selectedStatus==='queued'?(b.priority||0)-(a.priority||0)||a.created-b.created:b.created-a.created),box=$('#tasks');box.replaceChildren();
$('#queue-count').textContent=items.length+' 个任务';
for(const x of items){if(x.type==='batch')box.append(batchCard(x.item));else if(x.type==='simulation')box.append(simulationCard(state,x.n,x.item));else box.append(uploadCard(x.item));}
if(!items.length)box.append(el('p',(operationsView||state.account?.admin)?'暂无'+groupNames[selectedStatus]+'的任务':'当前账号暂无'+groupNames[selectedStatus]+'的任务；这里只显示你提交的任务。','hint'));
for(const tab of document.querySelectorAll('[data-status]')){const group=tab.dataset.status;tab.querySelector('span').textContent=all.filter(x=>x.group===group).length;tab.setAttribute('aria-selected',String(group===selectedStatus));tab.tabIndex=group===selectedStatus?0:-1;}
$('#task-results').setAttribute('aria-labelledby','tab-'+selectedStatus);
}
let refreshInFlight=false,refreshQueued=false,lastRenderedAt=null;
function showSyncFailure(){
const warning=$('#sync-warning');if(!warning)return;
warning.hidden=false;warning.textContent='页面状态未同步'+(lastRenderedAt?'；以下为 '+date(lastRenderedAt)+' 的记录':'，暂时无法确认任务状态')+'。请勿将旧状态视为当前运行状态。';
}
async function refresh(){if(!$('#nodes'))return;if(refreshInFlight){refreshQueued=true;return;}refreshInFlight=true;try{const previous=latestState;latestState=await api('/api/state'+(operationsView?'?scope=all':''));if(refreshUiVersion(latestState))return;renderState(latestState);updateControlFeedback(latestState);if(previous&&!uploadRequests.size){const changed=(latestState.batches||[]).filter(b=>{const old=(previous.batches||[]).find(x=>x.id===b.id);return old&&old.group!==b.group;});if(changed.length)notify(changed.map(b=>b.name+'：'+(b.group==='running'?(taskStatus(b.phase)+'，可在进行中查看'):b.group==='queued'?'已进入队列':(labels[b.phase]||'已结束')+(b.group==='completed'?'，可在已结束查看结果与记录':'，可在历史记录查看'))).join('；'));const priorScience=new Map(previous.nodes.flatMap(n=>(n.telemetry.simulations||[]).map(x=>[n.id+'|'+x.id,simulationGroup(x)])));const endedScience=latestState.nodes.flatMap(n=>(n.telemetry.simulations||[]).filter(x=>{const old=priorScience.get(n.id+'|'+x.id);return old&&old!==simulationGroup(x)&&['completed','cancelled'].includes(simulationGroup(x));}).map(x=>(operationsView?n.label+' · ':'')+x.name+'：'+(operationsView?x.status:taskStatus(x.lifecycle))+(operationsView&&x.failure_detail?'，'+x.failure_detail:'')+(simulationGroup(x)==='completed'?'；可在已结束查看':'；记录已移入历史记录')));if(endedScience.length)notify(endedScience.join('；'));}}catch(e){showSyncFailure();$('#updated').textContent=operationsView?'连接异常 · '+e.message:'状态同步中断';}finally{refreshInFlight=false;if(refreshQueued){refreshQueued=false;refresh();}}}
function renderState(state){return preservePresentation(()=>renderStateContent(state));}
function renderStateContent(state){
if($('#platform-workload')&&state.workload)$('#platform-workload').textContent='全平台：'+state.workload.active_units+' 个计算单元处理中 · '+state.workload.queued_units+' 个等待中';
if($('#service-status'))$('#service-status').textContent=state.service_paused?'平台暂未开放计算；已上传任务保留在队列，历史结果可下载。':'';$('#updated').textContent='更新于 '+new Date().toLocaleTimeString();$('#nodes').replaceChildren();
for(const n of state.nodes){const t=n.telemetry,card=el('article',undefined,'node'),head=el('div',undefined,'node-head');head.append(el('h3',n.label),el('span',n.online?'在线':'未连接','pill'+(n.online?' online':'')));card.append(head);
if(n.online){
const resourcesFresh=observationFresh(t.resource_observation?.at,state.now);
card.append(el('p',t.cpu_model||'CPU 型号读取中','hardware-model'));
if(resourcesFresh&&Number.isFinite(t.cpu_percent))resourceMeter(card,'CPU 占用',t.cpu_percent,number(t.cpu_percent)+'%');else card.append(el('p','CPU 占用等待更新','hint'));
renderThreadAllocation(card,n.capacity);
const memoryTotal=resourcesFresh?t.memory_total_gb:NaN,memoryFree=resourcesFresh?t.memory_available_gb:NaN;
if(Number.isFinite(memoryTotal)&&memoryTotal>0&&Number.isFinite(memoryFree)){
 const used=Math.max(0,memoryTotal-memoryFree);resourceMeter(card,'内存占用',used/memoryTotal*100,number(used)+' / '+number(memoryTotal)+' GiB');
}else card.append(el('p','内存信息暂不可用','hint'));
if(resourcesFresh&&n.enabled!==0&&!n.updating&&Number.isFinite(n.capacity?.memory_gb))card.append(el('p','可分配内存 '+number(n.capacity.memory_gb)+' GiB','hint'));
const storageFresh=observationFresh(t.storage_observation?.at??t.resource_observation?.at,state.now);
const disks=(storageFresh?(t.storage_volumes?.length?t.storage_volumes:t.disks||[]):[]).filter(d=>Number.isFinite(d.total_gb)&&d.total_gb>0&&Number.isFinite(d.free_gb));
if(disks.length){
 const total=disks.reduce((sum,d)=>sum+d.total_gb,0),free=disks.reduce((sum,d)=>sum+Math.max(0,Math.min(d.free_gb,d.total_gb)),0),used=total-free;
 resourceMeter(card,'存储占用',(used/total)*100,'已用 '+storage(used)+' / '+storage(total));
 card.append(el('p','可用 '+storage(free)+' · '+disks.length+' 个磁盘卷','hint'));
 const details=el('details',undefined,'node-details');details.dataset.key='storage-'+n.id;details.append(el('summary','查看各盘空间（'+disks.length+' 个）'));
 for(const disk of disks){const available=Math.max(0,Math.min(disk.free_gb,disk.total_gb)),occupied=disk.total_gb-available;resourceMeter(details,disk.id||disk.mount,occupied/disk.total_gb*100,'可用 '+storage(available)+' / '+storage(disk.total_gb));if(disk.schedulable===false)details.append(el('p','空间不足或不可写，暂停该盘新任务分配','hint'));}
 card.append(details);
}else card.append(el('p','存储信息暂不可用','hint'));
const gpuPanel=el('div',undefined,'gpu-resources'),gpus=t.gpus||[];
const gpuFresh=observationFresh(t.host_inventory?.at??t.resource_observation?.at,state.now);
let gpuList=gpuPanel;
if(gpus.length>1){
 const allocations=n.capacity?.gpu_allocations||[],allocated=gpus.filter(g=>allocations.some(a=>a.index===g.index&&a.allocated)).length,available=gpus.filter(g=>g.available&&!allocations.some(a=>a.index===g.index&&a.allocated)).length;
gpuPanel.append(el('p',gpus.length+' 张 GPU'+(gpuFresh?' · 可分配 '+available+' 张 · 已分配 '+allocated+' 张':' · 资源状态等待更新'),'hardware-model'));
 gpuPanel.append(el('p',[...new Set(gpus.map(gpuModelLabel))].join(' / '),'hint'));
if(gpuFresh&&gpus.every(g=>Number.isFinite(g.utilization))){const average=gpus.reduce((sum,g)=>sum+g.utilization,0)/gpus.length;resourceMeter(gpuPanel,'GPU 平均占用',average,number(average)+'%');}
if(gpuFresh&&gpus.every(g=>Number.isFinite(g.memory_total_gb)&&Number.isFinite(g.memory_free_gb))){const total=gpus.reduce((sum,g)=>sum+g.memory_total_gb,0),free=gpus.reduce((sum,g)=>sum+g.memory_free_gb,0);gpuPanel.append(el('p','显存合计：可用 '+number(free)+' / '+number(total)+' GiB','hint'));}
  gpuList=el('details',undefined,'node-details');gpuList.dataset.key='gpu-'+n.id;gpuList.append(el('summary','查看逐卡详情（'+gpus.length+' 张）'));gpuPanel.append(gpuList);
}
for(const gpu of t.gpus||[]){gpuList.append(el('p',(gpus.length>1?'GPU '+gpu.index+' · ':'')+gpuModelLabel(gpu),'hardware-model'));

const allocation=(n.capacity?.gpu_allocations||[]).find(x=>x.index===gpu.index);const gpuStatus=allocation?.allocated?'已分配':!gpuFresh?'状态等待更新':gpu.compute_occupied?'外部计算中':gpu.available?'空闲可分配':'暂不可分配';gpuList.append(el('p','GPU · '+gpuStatus+(allocation?.reserved_memory_gb?' · 显存预留 '+number(allocation.reserved_memory_gb)+' GiB':''),'hint'));
if(gpuFresh&&Number.isFinite(gpu.utilization))resourceMeter(gpuList,'GPU'+((t.gpus||[]).length>1?' '+gpu.index:'')+' 占用',gpu.utilization,number(gpu.utilization)+'%');else gpuList.append(el('p','GPU 占用等待更新','hint'));
if(gpuFresh&&Number.isFinite(gpu.memory_total_gb)&&Number.isFinite(gpu.memory_free_gb)&&gpu.memory_total_gb>0){
 const used=Math.max(0,gpu.memory_total_gb-gpu.memory_free_gb),total=gpu.memory_total_gb;
 resourceMeter(gpuList,'显存占用',used/total*100,number(used)+' / '+number(total)+' GiB');
}}
if(!gpus.length)gpuPanel.append(el('p','GPU 暂不可用','hint'));


card.append(gpuPanel);
const activeCount=n.active_unit_count;
const deliveries=(state.jobs||[]).filter(j=>j.node===n.id&&j.status==='uploading').length;
const current=batchesOnNode(state,n.id),science=(t.simulations||[]).filter(s=>simulationGroup(s)==='running');
card.append(el('p',n.updating?(operationsView?(deliveries?'暂停接单 · 等待已有结果回传后更新':'暂停接单 · 等待安全更新'):'暂停接单'):n.enabled===0?'暂停接单':(activeCount??(current.length+science.length))?'处理中 · '+(activeCount??(current.length+science.length))+' 个计算单元':t.external_busy?'计算中':'空闲','node-work'));
if(operationsView){
if(t.resource_observation?.state==='pressured')card.append(el('p','持续换页或内存压力 · 稳定后自动接单','hint error'));
else if(n.capacity?.commit_gb!=null&&n.capacity.commit_gb<.25)card.append(el('p','系统提交额度不足 · 暂缓新增计算','hint error'));
const details=el('details',undefined,'node-details');details.dataset.key='node-'+n.id;details.append(el('summary','应用与可用资源'));
if(resourcesFresh&&n.enabled!==0&&!n.updating&&n.capacity&&!n.capacity.pressure_reason)details.append(el('p','可接新任务：'+n.capacity.cpu_ids.length+' 线程 · '+number(n.capacity.memory_gb)+' GiB 内存','hint'));
if(resourcesFresh)details.append(el('p','可用内存 '+number(t.memory_available_gb)+' GiB · 磁盘可用 '+storage(t.disk_available_gb),'hint'));else details.append(el('p','资源观测等待更新','hint'));


for(const app of t.applications||[])details.append(el('p',app.name+(app.version?' '+app.version:'')+' · '+app.purpose+' · '+applicationStatus(t,app),'hint'));
card.append(details);}
}else card.append(el('p','连接恢复中','hint'));

$('#nodes').append(card);}
renderAttention(state);renderTasks(state);lastRenderedAt=state.now||Date.now()/1000;const warning=$('#sync-warning');if(warning)warning.hidden=true;if($('#history-dialog')?.open)loadHistory();
}

for(const tab of document.querySelectorAll('[data-status]')){tab.addEventListener('click',()=>{selectedStatus=tab.dataset.status;if(latestState)renderState(latestState);});tab.addEventListener('keydown',e=>{const tabs=[...document.querySelectorAll('[data-status]')];if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();let i=tabs.indexOf(tab);i=e.key==='Home'?0:e.key==='End'?tabs.length-1:(i+(e.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;tabs[i].click();tabs[i].focus();}});}
for(const link of document.querySelectorAll('a[href="/upload"]'))link.addEventListener('click',async e=>{e.preventDefault();pendingUploadOpen=true;if(await checkSubmissionDocument())openRequestedUpload();});
$('#close-upload')?.addEventListener('click',()=>$('#upload-dialog').close());
$('#submit')?.addEventListener('submit',async e=>{
e.preventDefault();if(!await checkSubmissionDocument())return;const form=e.target,data=new FormData(form),file=data.get('package');$('#submit-button').disabled=true;let uid;
const keyName='task-upload:'+csrf+':'+file.name+':'+file.size+':'+file.lastModified;
let uploadKey=sessionStorage.getItem(keyName);if(!uploadKey){uploadKey=crypto.randomUUID();sessionStorage.setItem(keyName,uploadKey);}
try{uid=(await api('/api/uploads',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:file.name})})).id;
selectedStatus='queued';$('#upload-dialog')?.close();await refresh();notify('上传中：'+file.name);
await new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();uploadRequests.set(uid,xhr);let lastProgress=0;xhr.onabort=()=>reject(Error('上传已取消'));xhr.open('POST','/api/uploads/'+uid+'/content');xhr.setRequestHeader('X-CSRF-Token',csrf);xhr.setRequestHeader('Idempotency-Key',uploadKey);xhr.upload.onprogress=e=>{if(e.lengthComputable){const percent=Math.floor(e.loaded/e.total*100);notify('上传中：'+file.name+' · '+percent+'%');if(Date.now()-lastProgress>2000||percent===100){lastProgress=Date.now();api('/api/uploads/'+uid+'/progress',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({percent})}).catch(()=>{});}}};xhr.onload=()=>{let body;try{body=JSON.parse(xhr.responseText);}catch{reject(Error('上传失败，请重试'));return;}if(xhr.status>=200&&xhr.status<300)resolve(body);else reject(Error(body.error||'上传失败'));};xhr.onerror=()=>reject(Error('上传连接中断，请重试'));xhr.send(data);});
sessionStorage.removeItem(keyName);notify('已上传，任务已进入队列');form.reset();if(!$('#nodes'))location.href='/?status=queued';else await refresh();
}catch(e){notify(e.message);if(uid)await api('/api/uploads/'+uid+'/failed',{method:'POST'}).catch(()=>{});await refresh();}finally{uploadRequests.delete(uid);$('#submit-button').disabled=false;}});
function renderHistory(state){return preservePresentation(()=>renderHistoryContent(state));}
function renderHistoryContent(state){const box=$('#history-tasks');box.replaceChildren();for(const task of allTasks(state).filter(t=>['completed','cancelled'].includes(t.group)).sort((a,b)=>b.created-a.created)){if(task.type==='batch')box.append(batchCard(task.item));else if(task.type==='simulation')box.append(simulationCard(state,task.n,task.item));else box.append(uploadCard(task.item));}if(!box.childElementCount)box.append(el('p','暂无历史记录','hint'));}
let historyInFlight=false;
async function loadHistory(){if(historyInFlight)return;historyInFlight=true;try{const state=await api('/api/state?include_archived=1'+(operationsView?'&scope=all':''));renderHistory(state);}catch(e){if(!$('#history-tasks').childElementCount)$('#history-tasks').textContent='历史记录暂时无法加载：'+e.message;}finally{historyInFlight=false;}}
$('#history-open')?.addEventListener('click',()=>{$('#history-dialog').showModal();loadHistory();});
$('#history-close')?.addEventListener('click',()=>$('#history-dialog').close());
$('#results-close')?.addEventListener('click',()=>$('#results-dialog').close());
$('#close-log')?.addEventListener('click',()=>$('#log-dialog').close());
const initialStatus=new URLSearchParams(location.search).get('status');if(['running','queued','completed'].includes(initialStatus))selectedStatus=initialStatus;
if($('#nodes')){refresh();setInterval(refresh,5000);document.addEventListener?.('visibilitychange',()=>{if(document.visibilityState==='visible')refresh();});globalThis.addEventListener?.('online',refresh);}


if(!$('#nodes')&&loadedUiVersion)setInterval(()=>api('/api/ui-version').then(refreshUiVersion).catch(()=>{}),5000);

// Document updates apply to each account, across devices.
const documentDialog=document.getElementById?.('document-update-dialog');
let documentDownloadBusy=false,documentCheckBusy=false,pendingUploadOpen=false;
function openRequestedUpload(){pendingUploadOpen=false;if($('#upload-dialog'))$('#upload-dialog').showModal();else location.href='/upload';}
async function checkSubmissionDocument(){
 if(documentDownloadBusy||documentCheckBusy)return false;
 documentCheckBusy=true;
 try{const info=await api('/api/submission-document');
  if(info.required){if(documentDialog&&!documentDialog.open)documentDialog.showModal();else if(!documentDialog)notify('请先下载最新提交文档，再上传任务包。');return false;}
  if(documentDialog?.open)documentDialog.close();return true;
 }catch(e){notify('暂时无法读取文档版本，请稍后重试。');return false;}
 finally{documentCheckBusy=false;}
}
if(documentDialog){
 documentDialog.addEventListener('cancel',e=>e.preventDefault());
 $('#document-download').addEventListener('click',async()=>{
  if(documentDownloadBusy)return;documentDownloadBusy=true;$('#document-download').disabled=true;
  $('#document-download-status').textContent='正在下载…';
  try{const response=await fetch('/api/submission-document/download',{credentials:'same-origin',cache:'no-store'});
   if(!response.ok)throw new Error('下载失败，请重试');
   const blob=await response.blob(),url=URL.createObjectURL(blob),link=document.createElement('a');
   link.href=url;link.download='任务上传文档.md';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);
   $('#document-download-status').textContent='下载已发起，请将新版文档交给 Agent。';
  }catch(e){$('#document-download-status').textContent=e.message;}
  finally{documentDownloadBusy=false;$('#document-download').disabled=false;if(await checkSubmissionDocument()){if(pendingUploadOpen)openRequestedUpload();}}
 });

}
// Retire controls from cached templates too; running services need no restart.
hideFeedbackActions();
const legacyFeedbackSection=$('#feedback-list')?.closest?.('section');if(legacyFeedbackSection)legacyFeedbackSection.hidden=true;
