'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const root=process.argv[2]||process.cwd();
const appPath=process.argv[3]||path.join(root,'scisaurus/dashboard/static/app.js');
function harness(){
  const elements=new Map(),pending=[];
  const element=(id)=>{if(!elements.has(id)){const attrs=new Map(),classes=new Set();elements.set(id,{textContent:'',innerHTML:'',hidden:false,attrs,classes,classList:{add:c=>classes.add(c),remove:c=>classes.delete(c),toggle(c,on){if(on)classes.add(c);else classes.delete(c);}},setAttribute:(k,v)=>attrs.set(k,String(v)),getAttribute:k=>attrs.get(k)??null,removeAttribute:k=>attrs.delete(k),showModal(){this.open=true;},close(){this.open=false;}});}return elements.get(id);};
  const context={URLSearchParams,window:{location:{search:'?project=test'}},document:{querySelector:element},fetch:url=>new Promise((resolve,reject)=>pending.push({url,resolve,reject})),console};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root,'scisaurus/dashboard/static/output-view.js'),'utf8'),context);
  let app=fs.readFileSync(appPath,'utf8');
  const bootstrap=app.indexOf('\n  bindControls();\n  setView');assert(bootstrap>=0,'Native bootstrap boundary absent');
  app=app.slice(0,bootstrap)+'\nwindow.testApi={openInspector,openLiteratureDetail,openAgentInspector,state};\n})();';
  vm.runInContext(app,context);
  return {api:context.window.testApi,pending,element};
}
const file=name=>({is_text:true,name:name+'.json',text:JSON.stringify({summary:name}),root_key:'topic',path:name+'.json',size:20});
const resolve=(request,value)=>request.resolve({ok:true,json:async()=>value});
(async()=>{
  let count=0;
  // A slow file request cannot overwrite a newer paper selection.
  {
    const h=harness(),older=h.api.openInspector('topic::A.json','A'),newer=h.api.openLiteratureDetail('W_B');
    resolve(h.pending[1],{item:{title:'B',abstract:'CURRENT B'}});await newer;
    resolve(h.pending[0],file('A'));await older;
    assert.equal(h.element('#inspector-title').textContent,'B');
    assert(h.element('#inspector-readable').innerHTML.includes('CURRENT B'));
    assert(!h.element('#inspector-readable').innerHTML.includes('>A<'));
    assert.equal(h.element('#inspector-raw').getAttribute('href'),null);count++;
  }
  // Reverse entry-point race and stale failure completion.
  for(const reject of [false,true]){
    const h=harness(),older=h.api.openLiteratureDetail('W_A'),newer=h.api.openInspector('topic::B.json','B');
    resolve(h.pending[1],file('B'));await newer;
    if(reject)h.pending[0].reject(new Error('STALE ERROR'));else resolve(h.pending[0],{item:{title:'A'}});
    await older;assert.equal(h.element('#inspector-title').textContent,'B');
    assert(!h.element('#inspector-readable').innerHTML.includes('STALE ERROR'));count++;
  }
  // Agent inspector shares the same token as file/paper entry points.
  {
    const h=harness();h.api.state.snapshot={artifacts:[{author:'reviewer',logical_id:'reviewer',file_ref:'topic::agent.json'}]};
    const older=h.api.openAgentInspector('reviewer'),newer=h.api.openInspector('topic::B.json','B');
    resolve(h.pending[1],file('B'));await newer;resolve(h.pending[0],file('AGENT A'));await older;
    assert.equal(h.element('#inspector-title').textContent,'B');assert(!h.element('#inspector-readable').innerHTML.includes('AGENT A'));count++;
  }
  // A previously enabled link must lose both navigation and keyboard focus
  // immediately when a different inspector begins loading.
  {
    const h=harness(),first=h.api.openInspector('topic::A.json','A');resolve(h.pending[0],file('A'));await first;
    const raw=h.element('#inspector-raw');assert(raw.getAttribute('href').includes('A.json'));
    assert.equal(raw.getAttribute('aria-disabled'),'false');assert.equal(raw.getAttribute('tabindex'),null);
    const next=h.api.openLiteratureDetail('W_B');
    assert.equal(raw.getAttribute('href'),null);assert.equal(raw.getAttribute('tabindex'),'-1');assert.equal(raw.getAttribute('aria-disabled'),'true');
    resolve(h.pending[1],{item:{title:'B'}});await next;assert.equal(raw.getAttribute('href'),null);count++;
  }
  console.log(JSON.stringify({status:'passed',nativeAppPath:appPath,cases:count,externalCalls:0}));
})().catch(error=>{console.error(error);process.exitCode=1;});
