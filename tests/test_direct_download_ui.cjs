const fs=require('fs'),vm=require('vm'),assert=require('assert');
const src=fs.readFileSync(__dirname+'/../static/app.js','utf8');const fn=src.slice(src.indexOf('function nodeResultLink('),src.indexOf('function taskJob('));
const ctx={location:{hostname:'192.0.2.30',port:'8443'},el:(tag,text)=>({tag,text})};vm.createContext(ctx);vm.runInContext(fn,ctx);
assert.equal(ctx.nodeResultLink({node:'win3',status:'running'}),null);
let a=ctx.nodeResultLink({id:'abc',node:'win3',status:'uploading'});assert.equal(a.href,'https://192.0.2.30:8445/api/jobs/abc/download');assert.equal(a.rel,'noopener noreferrer');
ctx.location={hostname:'compute.example.org',port:''};a=ctx.nodeResultLink({id:'abc',node:'dell-r940',has_result:true});assert.equal(a.href,'https://compute.example.org/direct/api/jobs/abc/download');
assert.equal(ctx.taskResultLink({id:'done',has_result:true}).href,'/api/jobs/done/result');assert.equal(ctx.taskResultLink({id:'node',node:'win3',status:'uploading'}).text,'下载结果');assert(!src.includes('下载中心已保存副本'));console.log('direct download UI passed');
