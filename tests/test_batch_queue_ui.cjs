const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
class Element{constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.dataset={};this.style={};this.classList={toggle(){},add(){},remove(){}};}append(...x){this.children.push(...x);}replaceChildren(){this.children=[];}addEventListener(){}setAttribute(){}}
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
for(const view of ['tasks','operations']){
 const elements={};const context={assert,console,URLSearchParams,location:{search:'',pathname:'/'},setInterval(){},crypto:require('crypto').webcrypto,document:{querySelector(s){if(s==='#nodes')return null;if(s.startsWith('meta['))return{content:s==='meta[name=view]'?view:'fixture'};return elements[s]??=new Element('div');},querySelectorAll(){return[];},createElement:t=>new Element(t)}};
 vm.createContext(context);vm.runInContext(source,context);
 vm.runInContext(`
 const text=e=>e.textContent+' '+e.children.map(text).join(' ');
 const outer=e=>e.children.filter(c=>c.tag==='p').map(c=>c.textContent).join(' ');
 const job=(id,status,reason)=>({id,status,created:100,spec:{name:id,task_key:id},waiting_reason:reason,actions:[]});
 const batch=units=>({id:'batch',name:'混合批次',phase:'queued',group:'queued',units,attempts:units,total:units.length,completed:units.filter(j=>j.status==='succeeded').length,failed:0,cancelled:0,offline:0,nodes:[],created:100,actions:[]});
 const done=job('geometry','succeeded'),resource=job('current','queued','Dell：等待 CPU 容量释放；A800：等待可用内存');
 let card=batchCard(batch([done,resource]));assert(outer(card).includes('等待资源（1项）：Dell：等待 CPU 容量释放；A800：等待可用内存'));assert(!outer(card).includes('等待依赖'));
 const dep=job('dependent','queued','等待前序任务完成：geometry');
 card=batchCard(batch([dep,resource]));assert(outer(card).includes('等待依赖（1项）'));assert(outer(card).includes('等待资源（1项）'));
 card=batchCard(batch([dep,{...dep,id:'dependent2'}]));assert(outer(card).includes('等待依赖（2项）'));assert.equal(card.children.filter(c=>c.className?.includes('queue-dependency')).length,1);
 card=batchCard(batch([done,job('claim','queued','已匹配可用资源，等待服务器领取')]));assert(outer(card).includes('等待领取（1项）'));assert(!outer(card).includes('等待资源'));
 card=batchCard(batch([done,job('unknown','queued')]));assert(outer(card).includes('等待原因待同步（1项）'));assert(!outer(card).includes('等待资源'));
 card=batchCard(batch([job('geometry','running'),{...job('dependent','queued'),spec:{name:'dependent',depends_on:['geometry']}}]));assert(outer(card).includes('等待依赖（1项）：等待前序任务完成：geometry'));
 const retryDone={...done,id:'geometry-new',created:200};
 card=batchCard({...batch([retryDone,resource]),attempts:[job('geometry','failed'),retryDone,resource]});assert(!outer(card).includes('等待依赖'));
 // Current scheduler explanation wins over stale dependency metadata.
 const stale={...resource,spec:{...resource.spec,depends_on:['geometry'],dependency_wait:'等待前序任务完成：geometry'}};
 card=batchCard(batch([done,stale]));assert(outer(card).includes('等待资源'));assert(!outer(card).includes('等待依赖'));
 card=batchCard(batch([resource]));assert(text(card).includes('等待资源：Dell：等待 CPU 容量释放'));assert.equal(card.children.filter(c=>c.className?.includes('queue-resource')).length,1);
 const finished=batch([done,{...resource,status:'failed'}]);finished.phase='failed';finished.group='completed';card=batchCard(finished);assert(!outer(card).includes('等待资源'));assert(!outer(card).includes('等待依赖'));
 const unsafe=job('unsafe','queued','<img src=x onerror=alert(1)>');card=batchCard(batch([done,unsafe]));assert(outer(card).includes('<img src=x onerror=alert(1)>'));assert(!card.children.some(c=>c.tag==='img'));
 const many=Array.from({length:5},(_,i)=>job('wait'+i,'queued','Dell：等待资源 '+i));card=batchCard(batch(many));assert(outer(card).includes('另有2类原因，见计算明细'));assert(text(card).includes('等待资源 4'));
 `,context);
 console.log('PASS '+view+': batch/single reasons, dependency/resource/claim/unknown, retry/terminal precedence, grouping, bounded summary and safe text.');
}
