const fs=require('fs'),assert=require('assert'),path=require('path');
const {chromium}=require('playwright');
(async()=>{
const browser=await chromium.launch({...(process.env.CHROME_EXECUTABLE?{executablePath:process.env.CHROME_EXECUTABLE}:{}),headless:true});
try{
const page=await browser.newPage({viewport:{width:1100,height:700}});
await page.route('http://presentation.test/**',route=>route.fulfill({contentType:'text/html',body:'<meta name="csrf" content="test"><meta name="ui-version" content="test"><main></main>'}));
await page.goto('http://presentation.test/');
const errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.addScriptTag({content:fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8')});
await page.evaluate(()=>{
 document.body.innerHTML='<div style="height:900px"></div><div id="nodes"></div><div id="tasks"></div><dialog id="history-dialog" style="height:200px;overflow:auto"><div id="history-tasks"></div></dialog><div style="height:2000px"></div>';
 globalThis.fixture=()=>'<details data-key="batch"><summary>batch</summary><details data-key="child"><summary>child</summary><div style="height:800px">content</div></details></details>';
 document.querySelector('#tasks').innerHTML=fixture();document.querySelector('#history-tasks').innerHTML=fixture();
 renderStateContent=()=>{document.querySelector('#tasks').innerHTML=fixture();};
 renderHistoryContent=()=>{document.querySelector('#history-tasks').innerHTML=fixture();};
 updateControlFeedback=()=>{};
 api=()=>new Promise(resolve=>globalThis.resolveState=resolve);
});
// A click while HTTP is pending must survive, including nested panels and scroll.
await page.evaluate(()=>{refresh();document.querySelectorAll('#tasks details').forEach(d=>d.open=true);window.scrollTo(0,1000);});
await page.evaluate(()=>resolveState({ui_version:'test',nodes:[],batches:[]}));await page.waitForTimeout(40);
assert.deepEqual(await page.locator('#tasks details').evaluateAll(ds=>ds.map(d=>d.open)),[true,true]);
assert.equal(await page.evaluate(()=>scrollY),1000);
// Closing while pending wins as well; duplicate keys in another container stay independent.
await page.evaluate(()=>{document.querySelector('#history-dialog').showModal();document.querySelectorAll('#history-tasks details').forEach(d=>d.open=true);document.querySelector('#history-dialog').scrollTop=250;refresh();document.querySelector('#tasks details').open=false;});
const dialogY=await page.locator('#history-dialog').evaluate(d=>d.scrollTop);
await page.evaluate(()=>resolveState({ui_version:'test',nodes:[],batches:[]}));await page.waitForTimeout(40);
assert.equal(await page.locator('#tasks details').first().evaluate(d=>d.open),false);
assert.deepEqual(await page.locator('#history-tasks details').evaluateAll(ds=>ds.map(d=>d.open)),[true,true]);
await page.evaluate(()=>renderHistory({}));
assert.deepEqual(await page.locator('#history-tasks details').evaluateAll(ds=>ds.map(d=>d.open)),[true,true]);
assert.equal(await page.locator('#history-dialog').evaluate(d=>d.scrollTop),dialogY);
// Hidden tab panels retain their state when they return.
await page.evaluate(()=>{preservePresentation(()=>document.querySelector('#tasks').replaceChildren());preservePresentation(()=>document.querySelector('#tasks').innerHTML=fixture());});
assert.deepEqual(await page.locator('#tasks details').evaluateAll(ds=>ds.map(d=>d.open)),[false,true]);
// Version reload uses a short-lived presentation snapshot, consumed on next load.
await page.evaluate(()=>{sessionStorage.setItem(presentationStorageKey,JSON.stringify(capturePresentation()));});
await page.reload();await page.addScriptTag({content:fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8')});
await page.evaluate(()=>{document.body.innerHTML='<div style="height:900px"></div><div id="tasks"><details data-key="batch"><summary>batch</summary><details data-key="child"><summary>child</summary>content</details></details></div><div style="height:2000px"></div>';preservePresentation(()=>{});});
assert.deepEqual(await page.locator('#tasks details').evaluateAll(ds=>ds.map(d=>d.open)),[false,true]);
assert.equal(await page.evaluate(()=>sessionStorage.getItem(presentationStorageKey)),null);
assert.deepEqual(errors,[]);
console.log('PASS real Chromium: pending open/close, nested and scoped panels, page/dialog scroll, tab return, reload snapshot.');
}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
