const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(require('path').join(__dirname,'../static/app.js'),'utf8');
const elements=Object.fromEntries(['issue-summary','issue-report'].map(id=>[id,{style:{}}]));
const sandbox={$:selector=>elements[selector.slice(1)],URL:{createObjectURL:()=> 'blob:raw-error',revokeObjectURL:()=>{}},Blob};
vm.createContext(sandbox);vm.runInContext(source.slice(source.indexOf('let nativeErrorBlob='),source.indexOf('async function openTaskFeedback')),sandbox);
const report={schema_version:1,source:'node_runner',job_id:'job',evidence:[{source:'work/log',context:'<script>unsafe()</script>\nFailed to find consistent initial values.'}]};
sandbox.showNativeTaskError({native_error:report},'job');
assert(elements['issue-summary'].textContent.includes('<script>unsafe()</script>'));
assert.equal(elements['issue-report'].download,'native-error-job.json');
assert.equal(elements['issue-report'].hidden,false);
assert.equal(sandbox.nativeTaskError({native_error:report},'other'),null);
sandbox.showNativeTaskError({feedback:[]},'job');assert.equal(elements['issue-report'].hidden,true);
assert.equal(sandbox.nativeTaskError({feedback:[{request_id:'system:native-exit-v1',reply:JSON.stringify(report)}]},'job').source,'node_runner');
console.log('native error UI passed');

const latest={...report,detail:'new execution failed',evidence:[]};
sandbox.showNativeTaskError({native_error:report,feedback:[{request_id:'system:native-exit-v1:next',reply:JSON.stringify(latest)}]},'job');
assert(elements['issue-summary'].textContent.includes('new execution failed'));
