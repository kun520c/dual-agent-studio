const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const Module=require('node:module');
const ts=require('../../../design-prototype/node_modules/typescript');
const source=path.join(__dirname,'../src/historyPaging.ts');
const compiled=new Module(source,module);
compiled._compile(ts.transpileModule(fs.readFileSync(source,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,source);
const {readTimelinePage,restoreSelection,shouldPollRun}=compiled.exports;

test('an active historical selection outside first 100 continues polling until terminal',()=>{
 const page=Array.from({length:100},(_,i)=>({id:'new-'+i,state:'failed'}));
 assert.equal(shouldPollRun('old-150',{task:{id:'old-150',state:'running'}},page),true);
 assert.equal(shouldPollRun('old-150',{task:{id:'old-150',state:'verified'}},page),false);
 assert.equal(shouldPollRun('old-150',{task:{id:'other',state:'running'}},page),false);
 assert.equal(shouldPollRun('old-150',{task:{id:'old-150',state:'running'}},page,true,true),false);
 assert.equal(shouldPollRun('old-150',{task:{id:'old-150',state:'running'}},page,false),true);
});

test('F6 restores a selection beyond the first 100 without scanning pages',async()=>{
  const items=Array.from({length:100},(_,i)=>({id:'new-'+i,outcome:{passed:true}}));let calls=0;
  const result=await restoreSelection(items,'old-150',async id=>{calls++;return {taskId:id,task:{id,title:'原先选择'}};});
  assert.equal(result.id,'old-150');assert.equal(result.items.at(-1).title,'原先选择');assert.equal(calls,1);assert.equal(result.error,'');
});
test('F6 missing or corrupt selected history remains selected with explicit feedback',async()=>{
  for(const load of [async()=>{throw Error('记录已损坏');},async()=>({taskId:'other',task:{id:'other'}})]){
    const result=await restoreSelection([{id:'other',outcome:{passed:true}}],'old-150',load);
    assert.equal(result.id,'old-150');assert.match(result.error,/无法恢复/);assert.equal(result.items.length,1);
  }
});

function sample(){
  const messages=Array.from({length:121},(_,index)=>({id:'m-'+index}));
  const events=Array.from({length:321},(_,index)=>({index}));
  const calls=[];
  const load=async(id,m,e)=>{calls.push([id,m,e]);return {taskId:id,task:{id},messageTotal:messages.length,eventTotal:events.length,messageOffset:m,eventOffset:e,messages:messages.slice(m,m+100),events:events.slice(e,e+200)};};
  return {messages,events,calls,load};
}
test('tail follows appended records across page boundaries with bounded reads',async()=>{
  const fixture=sample();let page=await readTimelinePage(fixture.load,'run-a',0,0,true);
  assert.equal(page.messages.at(-1).id,'m-120');assert.equal(page.events.at(-1).index,320);
  assert.equal(fixture.calls.length,2);
  fixture.messages.push(...Array.from({length:101},(_,i)=>({id:'m-'+(121+i)})));
  fixture.events.push(...Array.from({length:121},(_,i)=>({index:321+i})));
  page=await readTimelinePage(fixture.load,'run-a',page.messageOffset,page.eventOffset,true);
  assert.equal(page.messageOffset,200);assert.equal(page.eventOffset,400);
  assert.equal(page.messages.at(-1).id,'m-221');assert.equal(page.events.at(-1).index,441);
  assert.equal(new Set(page.messages.map(m=>m.id)).size,page.messages.length);
  assert.equal(new Set(page.events.map(e=>e.index)).size,page.events.length);
});
test('manual pages pause follow; append preserves position; returning latest resumes',async()=>{
  const fixture=sample();let page=await readTimelinePage(fixture.load,'run-a',0,0,false);
  fixture.messages.push({id:'m-121'});fixture.events.push({index:321});
  page=await readTimelinePage(fixture.load,'run-a',page.messageOffset,page.eventOffset,false);
  assert.equal(page.messageOffset,0);assert.equal(page.eventOffset,0);
  assert.equal(page.messages.at(-1).id,'m-99');assert.equal(page.events.at(-1).index,199);
  const messageIds=[],eventIndices=[];
  for(let m=0;m<fixture.messages.length;m+=100){const p=await fixture.load('run-a',m,0);messageIds.push(...p.messages.map(x=>x.id));}
  for(let e=0;e<fixture.events.length;e+=200){const p=await fixture.load('run-a',0,e);eventIndices.push(...p.events.map(x=>x.index));}
  assert.deepEqual(messageIds,fixture.messages.map(x=>x.id));assert.deepEqual(eventIndices,fixture.events.map(x=>x.index));
  page=await readTimelinePage(fixture.load,'run-a',0,0,true);
  assert.equal(page.messages.at(-1).id,'m-121');assert.equal(page.events.at(-1).index,321);
});
test('selection or paging generation change suppresses a stale tail follow-up',async()=>{
  const fixture=sample();let current=true;
  const load=async(...args)=>{const page=await fixture.load(...args);current=false;return page;};
  await readTimelinePage(load,'old-run',0,0,true,()=>current);
  assert.deepEqual(fixture.calls,[['old-run',0,0]]);
});
