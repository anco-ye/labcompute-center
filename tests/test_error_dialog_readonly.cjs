const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
const template=fs.readFileSync(path.join(__dirname,'../templates/index.html'),'utf8');
assert(!template.includes('issue-feedback-form'));assert(!template.includes('提交反馈'));assert(!template.includes('feedback-list'));
const elements={};const node=()=>({style:{},showModal(){this.open=true;},closest(){return null;}});
const calls=[];let legacyLog='download wheel\nTraceback (most recent call last):\n  <script>unsafe()</script>\nTimeoutError: The read operation timed out';
const sandbox={$:id=>elements[id]??=node(),URL:{createObjectURL:()=> 'blob:error',revokeObjectURL(){}},Blob,latestState:null,taskStatus:x=>x,
 api:async url=>{calls.push(url);return url.endsWith('/log')?{text:legacyLog}:{name:'p3m-c-16',status:'failed',feedback:[]};}};
vm.createContext(sandbox);vm.runInContext(source.slice(source.indexOf('let currentIssue='),source.indexOf('async function openIssue')),sandbox);
(async()=>{
 await sandbox.openTaskFeedback('job');
 assert(elements['#issue-dialog'].open);assert.equal(elements['#issue-feedback-form'].hidden,true);
 assert(elements['#issue-summary'].textContent.includes('TimeoutError'));assert(elements['#issue-summary'].textContent.includes('<script>unsafe()</script>'));
 assert(!elements['#issue-summary'].textContent.includes('download wheel'));assert(!elements['#issue-summary'].innerHTML);
 assert.equal(elements['#issue-report'].hidden,false);assert(calls.includes('/api/jobs/job/log'));
 legacyLog='';await sandbox.openTaskFeedback('job');assert(elements['#issue-summary'].textContent.includes('正在同步'));assert(!elements['#issue-summary'].textContent.includes('反馈'));
 sandbox.api=async()=>({name:'task',status:'failed',native_error:{schema_version:1,source:'node_runner',job_id:'job',detail:'native failure'}});
 await sandbox.openTaskFeedback('job');assert(elements['#issue-summary'].textContent.includes('native failure'));
 console.log('PASS readonly errors: legacy raw log fallback, automatic native report, no feedback action, XSS text, missing receipt');
})().catch(e=>{console.error(e);process.exitCode=1;});
