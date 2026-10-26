const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
class Element {constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.dataset={};this.listeners={};this.style={};this.classList={toggle(){},add(){},remove(){}};} append(...x){this.children.push(...x);} addEventListener(k,f){this.listeners[k]=f;} replaceChildren(){this.children=[];} setAttribute(){} }
const elements={};
const context={assert,console,URLSearchParams,location:{search:'',pathname:'/'},setInterval(){},crypto:require('crypto').webcrypto,
document:{querySelector(s){if(s==='#nodes')return null;if(s.startsWith('meta['))return {content:s==='meta[name=view]'?'tasks':'test'};return elements[s]??=new Element('div');},querySelectorAll(){return[];},createElement(t){return new Element(t);}}};
vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8'),context);
vm.runInContext(`
const content=e=>e.textContent+' '+e.children.map(content).join(' ');
const threads=el('div');renderThreadAllocation(threads,{allocated_cpu_threads:48,total_cpu_threads:252});
assert(content(threads).includes('48 / 252 线程 · 19%'));
assert.equal(threads.children[1].tag,'progress');assert.equal(threads.children[1].max,252);assert.equal(threads.children[1].value,48);
const idleThreads=el('div');renderThreadAllocation(idleThreads,{allocated_cpu_threads:0,total_cpu_threads:68});assert.equal(idleThreads.children[1].value,0);assert(content(idleThreads).includes('0%'));
const fullThreads=el('div');renderThreadAllocation(fullThreads,{allocated_cpu_threads:68,total_cpu_threads:68});assert.equal(fullThreads.children[1].value,68);assert(content(fullThreads).includes('100%'));
for(const capacity of [undefined,{allocated_cpu_threads:0,total_cpu_threads:0},{allocated_cpu_threads:-1,total_cpu_threads:8}]){const missingThreads=el('div');renderThreadAllocation(missingThreads,capacity);assert(content(missingThreads).includes('暂不可用'));assert(!missingThreads.children.some(e=>e.tag==='progress'));}
const overflowThreads=el('div');renderThreadAllocation(overflowThreads,{allocated_cpu_threads:9,total_cpu_threads:8});assert.equal(overflowThreads.children[1].value,8);
`,context);
console.log('PASS thread allocation bars: partial, idle, full, unavailable and bounded display.');
