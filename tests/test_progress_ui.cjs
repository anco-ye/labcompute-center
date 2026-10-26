const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
class Element{constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.dataset={};}append(...items){this.children.push(...items);}addEventListener(){}setAttribute(){}replaceChildren(){this.children=[];}}
const elements={};const context={assert,console,URLSearchParams,location:{search:'',pathname:'/'},setInterval(){},crypto:require('crypto').webcrypto,
document:{querySelector(s){if(s==='#nodes')return null;if(s.startsWith('meta['))return {content:s==='meta[name=view]'?'tasks':'test'};return elements[s]??=new Element('div');},querySelectorAll(){return[];},createElement(t){return new Element(t);}}};
vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8'),context);
vm.runInContext(`
const text=e=>e.textContent+' '+e.children.map(text).join(' ');
const job={id:'one',status:'running',spec:{name:'COMSOL'},actions:[],elapsed_seconds:100,progress:{scope:'stage',stage:'矩阵分解'},stage_progress_percent:69,progress_fresh:true};
let card=taskJob(job);assert(text(card).includes('当前阶段'));assert(text(card).includes('69.0%'));assert(text(card).includes('整体进度：待更新'));assert(!text(card).includes('整体进度 69'));
card=taskJob({...job,progress:{},stage_progress_percent:undefined});assert(!text(card).includes('0.0%'));assert(text(card).includes('已运行'));
card=taskJob({...job,progress:{completed:8,total:10,unit:'轮',metrics:[{name:'循环目标',completed:800,total:1000,unit:'次'}]},stage_progress_percent:undefined,progress_percent:80});assert(text(card).includes('整体进度'));assert(text(card).includes('循环目标'));assert(text(card).includes('轮'));
const batch={id:'b',name:'计算',total:2,completed:0,running:1,queued:1,phase:'running',units:[job,{...job,id:'two',status:'queued'}],actions:[]};
card=taskBatch(batch);assert(text(card).includes('计算中 1 项'));assert(text(card).includes('等待 1 项'));assert(text(card).includes('69.0%'));assert(!text(card).includes('0.0%'));
card=taskJob({...job,progress_fresh:false});assert(text(card).includes('上次阶段'));
`,context);
console.log('PASS truthful overall/stage/goal counters, unknown percentage, batch counts and stale progress.');
