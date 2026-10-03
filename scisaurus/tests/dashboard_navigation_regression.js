'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const root=process.argv[2]||process.cwd(),elements=new Map();
function element(id){
  if(!elements.has(id)){
    const attrs=new Map(),classes=new Set();
    elements.set(id,{hidden:false,open:false,close(){this.open=false;},dataset:{},attrs,classes,
      classList:{toggle(c,on){if(on)classes.add(c);else classes.delete(c);},remove(c){classes.delete(c);}},
      setAttribute(k,v){attrs.set(k,v);},getAttribute(k){return attrs.get(k);},removeAttribute(k){attrs.delete(k);}});
  }
  return elements.get(id);
}
const links=['calls','specialists','resources','inventory'].map(id=>{const e=element('link-'+id);e.setAttribute('href','#'+id);return e;});
const stages=['topic','survey'].map(id=>{const e=element('stage-'+id);e.dataset.stageId=id;return e;});
const location=new URL('http://localhost/?project=.&stage=survey#calls');
let scrolls=0;
const requests=[];
const context={URL,URLSearchParams,AbortController,console,fetch:(url,options)=>new Promise(resolve=>requests.push({url,options,resolve})),document:{querySelector:element,getElementById:id=>element('#'+id),querySelectorAll:selector=>selector.includes('stage-navigation')?stages:selector.includes('sidebar-project')?[]:links},
  window:{location,history:{pushState(_,__,url){location.href=new URL(url,location).href;}},scrollTo(){scrolls++;}}};
vm.createContext(context);
let app=fs.readFileSync(path.join(root,'scisaurus/dashboard/static/app.js'),'utf8');
const bootstrap=app.indexOf('\n  bindControls();\n  setView');assert(bootstrap>=0);
app=app.slice(0,bootstrap)+'\nwindow.testApi={renderProjectPage,navigateOperation,selectStage,renderHeader,setView,fetchSnapshot,state};\n})();';
vm.runInContext(app,context);
const api=context.window.testApi;
const panes=['mission-metrics','calls','activity','specialists','resources','structure','inventory'];
const visible=()=>panes.filter(id=>!element('#'+id).hidden);
const expected={calls:['mission-metrics','calls','activity'],specialists:['specialists'],resources:['resources'],inventory:['structure','inventory']};
let count=0;
for(const [route,sections] of Object.entries(expected)){
  api.navigateOperation(route);
  assert.deepEqual(visible(),sections);
  assert(element('#research-stage-view').hidden);
  assert(!element('#operations-view').hidden);
  assert.equal(links.filter(e=>e.classes.has('is-active')).length,1);
  assert.equal(stages.filter(e=>e.classes.has('is-active')).length,0);
  assert.equal(location.search,'?project=.&stage=survey');count++;
  api.renderProjectPage();assert.deepEqual(visible(),sections);count++;
}
api.selectStage('survey');
assert.deepEqual(visible(),[]);
assert(!element('#research-stage-view').hidden);
assert(element('#operations-view').hidden);
assert.equal(links.filter(e=>e.classes.has('is-active')).length,0);
assert.equal(stages.filter(e=>e.classes.has('is-active')).length,1);count++;
location.hash='resources';api.renderProjectPage();assert.deepEqual(visible(),['resources']);count++;
location.hash='stage-workspace';api.renderProjectPage();assert.deepEqual(visible(),[]);count++;
assert.equal(scrolls,5);
api.renderHeader({runtime:{processes:[{pid:123,owns_execution:true},{pid:456,owns_execution:false}]} });
assert.equal(element('#process-state').textContent,'PID 123');count++;
api.renderHeader({runtime:{processes:[{pid:456,owns_execution:false}]} });
assert.equal(element('#process-state').textContent,'not detected');count++;
api.setView('project','A');
assert(element('#sidebar-project-list').hidden);assert(element('#sidebar-project-label').hidden);count++;
api.setView('workspace');
assert(!element('#sidebar-project-list').hidden);assert(!element('#sidebar-project-label').hidden);count++;
(async()=>{
  api.state.view='project';api.state.projectRef='A';
  const first=api.fetchSnapshot();await api.fetchSnapshot();assert.equal(requests.length,1);count++;
  api.state.projectRef='B';const second=api.fetchSnapshot();
  assert.equal(requests.length,2);assert(requests[0].options.signal.aborted);count++;
  api.state.view='workspace';
  requests.forEach(request=>request.resolve({ok:true,json:async()=>({})}));await Promise.all([first,second]);
  assert.equal(api.state.snapshot,null);count++;
  console.log(JSON.stringify({status:'passed',cases:count,externalCalls:0}));
})().catch(error=>{console.error(error);process.exitCode=1;});
