// Exercise task actions and feedback without connecting to a live controller.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
class Element{
 constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.dataset={};this.listeners={};this.style={};this.open=false;}
 append(...items){this.children.push(...items);}
 addEventListener(event,fn){this.listeners[event]=fn;}
 replaceChildren(){this.children=[];}
 setAttribute(){}
 close(){this.open=false;this.closed=true;}
 showModal(){this.open=true;}
}
const elements={},calls=[];
const document={querySelector(s){if(s==='#nodes')return null;if(s==='meta[name=view]')return {content:'operations'};if(s==='meta[name=csrf]')return {content:'csrf'};if(s==='meta[name=ui-version]')return {content:'old-ui'};if(s==='dialog[open]')return null;return elements[s]??=new Element('div');},querySelectorAll(){return [];},createElement(tag){return new Element(tag);}};
const context={document,URLSearchParams,crypto:require('crypto').webcrypto,location:{search:'',pathname:'/',replace(url){calls.push({reload:url});}},console,setInterval(){},confirm(){return true;},assert,calls,fetch:async(url,options)=>{calls.push({url,options});return {status:200,ok:true,json:async()=>({ok:true,ids:['new-attempt']})};}};
vm.createContext(context);
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
vm.runInContext(source,context);
(async()=>{
 await vm.runInContext(`(async()=>{
 const j={id:'unit',created:1,status:'failed',spec:{name:'自动补库'},detail:'依赖 missing_lib 自动安装失败；请查看日志。',has_result:true,actions:['retry']};
 const b={id:'batch',name:'同一实验',group:'completed',phase:'failed',phases:{},total:1,completed:0,failed:1,cancelled:0,queued:0,running:0,progress_percent:0,nodes:[],units:[j],attempts:[j],actions:['retry']};
 const card=batchCard(b);
 const content=e=>e.textContent+' '+e.children.map(content).join(' ');
 assert(content(card).includes('missing_lib 自动安装失败'));
 $('#history-dialog').open=true;
 await card.children.find(e=>e.tag==='button'&&e.textContent==='重试未完成项').listeners.click();
 assert.equal(selectedStatus,'queued');assert.equal($('#history-dialog').open,false);
 assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/batches/batch/retry');
 const upload=uploadCard({id:'upload',name:'包.zip',status:'failed',detail:'连接中断'});
 assert.equal(upload.children.find(e=>e.tag==='a'&&e.textContent==='重新上传').href,'/upload');assert.equal(calls.length,1);
 j.spec.result_transfer_error=true;const transfer=batchCard(b);
 assert(!content(transfer).includes('下载回传错误说明'));
 await transfer.children.find(e=>e.tag==='button'&&e.textContent==='重新回传结果').listeners.click();
 assert.equal(selectedStatus,'running');assert.equal(calls.length,2);
 const ended=simulationCard({now:1},{id:'win2',label:'Win2',online:true},{name:'finished',status:'已完成',elapsed_seconds:null,source_age_seconds:500,actions:['start','cancel'],control_detail:'internal path',command:{status:'succeeded',detail:'正在备份'}});
 assert(!ended.children.some(e=>e.tag==='button'));assert(!content(ended).includes('正在备份'));assert(!content(ended).includes('internal path'));
 assert(!content(ended).includes('已用'));assert(!content(ended).includes('进度更新于'));
 const compact=simulationCard({now:1},{id:'win3',label:'Win3',online:true},{id:'stable',name:'batch',status:'运行中',total:10,completed:3,percent:30,remaining_seconds:100,elapsed_seconds:60,source_age_seconds:12,running:['work-unit'],actions:['pause','cancel']});
 const fold=compact.children.find(e=>e.tag==='details');assert(fold&&!fold.open);assert.equal(fold.dataset.key,'science-win3-stable');assert(content(fold).includes('work-unit'));
 assert(!compact.children.some(e=>e.textContent==='work-unit'));assert.equal(compact.children.filter(e=>e.tag==='button').length,2);
 const interrupted={name:'memory guard',status:'已中断',failure_detail:'可用物理内存降至 2.1 GiB，触发内存保护',source_age_seconds:900,actions:[]};
 assert.equal(simulationGroup(interrupted),'completed');
 const stoppedCard=simulationCard({now:1},{id:'win2',label:'Win2',online:true},interrupted);
 assert(content(stoppedCard).includes('2.1 GiB'));assert(!content(stoppedCard).includes('暂不可操作'));assert(!content(stoppedCard).includes('等待新进度'));
 const stale={id:'stale',name:'stale task',kind:'batch',status:'进程运行中，进度暂未更新',lifecycle:'running',progress_state:'stale',source_age_seconds:600,remaining_seconds:1234,actions:[]};
 const liveNode={id:'win1',label:'Win1',online:true,telemetry:{simulations:[stale,interrupted]}};
 const staleCard=simulationCard({now:1},liveNode,stale);
 assert(content(staleCard).includes('10 分钟'));assert(content(staleCard).includes('进程仍存活'));assert(!content(staleCard).includes('约剩'));assert(!content(staleCard).includes('预计结束'));
 const offlineCard=simulationCard({now:1},{...liveNode,online:false},stale);assert(content(offlineCard).includes('连接中断'));assert(!content(offlineCard).includes('约剩'));
 const offlineDone=simulationCard({now:1},{...liveNode,online:false},{name:'done',status:'计算完成',lifecycle:'succeeded'});assert(content(offlineDone).includes('计算完成'));assert(!content(offlineDone).includes('连接中断'));
 const attentionState={nodes:[liveNode],batches:[],uploads:[]};renderAttention(attentionState);
 assert.equal(attentionTasks(attentionState).length,2);assert.equal($('#attention').hidden,false);assert(content($('#attention-tasks')).includes('2.1 GiB'));
 liveNode.telemetry.simulations=[{...stale,status:'运行中',progress_state:'fresh',source_age_seconds:1}];renderAttention(attentionState);assert.equal($('#attention').hidden,true);assert.equal($('#attention-tasks').children.length,0);
 const unknownAfterFailure=simulationCard({now:1},{id:'win2',label:'Win2',online:true},{id:'prior',name:'prior failure',status:'进程状态待确认',lifecycle:'unknown',health:{detail:'监测待确认',unresolved_failure:'输入缺失仍未修复'}},true);
 assert(content(unknownAfterFailure).includes('监测待确认'));assert(content(unknownAfterFailure).includes('尚未解决：输入缺失仍未修复'));
 pendingScienceCommand='ack';updateControlFeedback({nodes:[{telemetry:{simulations:[{status:'已暂停',command:{id:'ack',status:'succeeded'}}]}}]});
 assert.equal(pendingScienceCommand,null);assert.equal($('#notice').textContent,'操作已确认：已暂停');
 const state={batches:[b],nodes:[],uploads:[{id:'uploading',status:'receiving',created:2}]};
 assert.equal(allTasks(state).filter(t=>t.group==='queued').length,1);
 assert.equal(simulationGroup({status:'进程已结束，等待续跑'}),'completed');
 assert.equal(simulationGroup({status:'运行中',lifecycle:'failed'}),'completed');
 const historyState={batches:[],nodes:[{id:'win2',label:'Win2',online:true,telemetry:{simulations:[interrupted]}}]};
 renderHistory(historyState);assert(content($('#history-tasks')).includes('2.1 GiB'));
 historyState.nodes[0].telemetry.simulations=[];renderHistory(historyState);assert(!content($('#history-tasks')).includes('2.1 GiB'));
 const distributed={batches:[{units:[{node:'win3',status:'succeeded'},{node:'win4',status:'running'}]}]};
 assert.equal(batchesOnNode(distributed,'win3').length,0);
 assert.equal(batchesOnNode(distributed,'win4').length,1);
 distributed.batches[0].units[1].status='paused';assert.equal(batchesOnNode(distributed,'win4').length,1);
 distributed.batches[0].units[1].status='cancelled';assert.equal(batchesOnNode(distributed,'win4').length,0);
 assert(!${JSON.stringify(source.includes("'/api/nodes/'+n.id+'/enabled'"))});
 assert.equal(applicationStatus({runtimes:{'python-neuron':{label:'NEURON',status:'ready'}}},{name:'NEURON',batch_ready:false}),'支持上传计算');
 assert.equal(applicationStatus({runtimes:{matlab:{label:'MATLAB',status:'unverified'}}},{name:'MATLAB',batch_ready:true}),'后台计算尚未验证通过');
 assert.equal(applicationStatus({runtimes:{}},{name:'CAD',batch_ready:false}),'已安装');
 const terminal={...j,status:'succeeded',stage:'installing',node_online:false,execution_stopped:1};
 assert.equal(jobCard(terminal).children[0].children[1].textContent,'已完成');
 assert.equal(jobStatus({...terminal,status:'failed'}),'失败');
 assert.equal(jobStatus({...terminal,status:'running'}),'计算已结束，状态同步中');
 const issue={node:'win4',task:'old',state:'open',first_seen:123,data:{name:'interrupted task',owner:'center',code:'execution_failed',detail:'original process stopped',error_report:{report_id:'a'.repeat(24)}}};
 const issueActions=issueCard(issue);appendIssueActions(issueActions,issue);
 assert(content(issueActions).includes('中心负责'));assert(content(issueActions).includes('错误详情'));
 assert(issueActions.children.some(x=>x.href==='/api/issues/'+issue.data.error_report.report_id+'/report'));
 const originalFetch=fetch,submitted=[];
 fetch=async(url,options)=>{if(options?.method==='POST')submitted.push(options);return {status:200,ok:true,json:async()=>({report_id:issue.data.error_report.report_id,issue,feedback:[]})};};
 await openIssue(issue.data.error_report.report_id);assert($('#issue-dialog').open);assert.equal($('#issue-owner').textContent,'中心负责 · 尚未解决');
 assert.equal($('#issue-summary').textContent,'original process stopped');assert.equal($('#issue-feedback-form').hidden,true);assert.equal(submitted.length,0);
 $('#issue-dialog').close();fetch=originalFetch;
 assert.equal(refreshUiVersion({ui_version:'old-ui'}),false);
 assert.equal(refreshUiVersion({}),false);
 pendingMutations=1;assert.equal(refreshUiVersion({ui_version:'new-ui'}),false);pendingMutations=0;
 pendingScienceCommand='not-acknowledged';assert.equal(refreshUiVersion({ui_version:'new-ui'}),false);pendingScienceCommand=null;
 uploadRequests.set('active',{});assert.equal(refreshUiVersion({ui_version:'new-ui'}),false);uploadRequests.clear();
 $('#submit-button').disabled=true;assert.equal(refreshUiVersion({ui_version:'new-ui'}),false);$('#submit-button').disabled=false;
 const originalQuery=document.querySelector,originalAll=document.querySelectorAll;
 $('#upload-dialog').open=true;assert.equal(refreshUiVersion({ui_version:'new-ui'}),false);$('#upload-dialog').close();
 document.querySelectorAll=s=>s==='input[type=file]'?[{files:[{}]}]:[];assert.equal(refreshUiVersion({ui_version:'new-ui'}),false);document.querySelectorAll=originalAll;
 document.querySelector=s=>s==='#nodes'?{}:originalQuery(s);selectedStatus='queued';
 $('#history-dialog').open=true;$('#log-dialog').open=true;assert.equal(refreshUiVersion({ui_version:'new-ui'}),true);assert.equal(calls[calls.length-1].reload,'/?status=queued');
 const reloadCount=calls.length;assert.equal(refreshUiVersion({ui_version:'new-ui'}),true);assert.equal(calls.length,reloadCount);
 document.querySelector=originalQuery;
 console.log('PASS task UI: clear dependency error, retry returns to queue, re-upload opens form, one status classification, no node-level pause control.');
 })()`,context);
})().catch(e=>{console.error(e);process.exitCode=1;});
