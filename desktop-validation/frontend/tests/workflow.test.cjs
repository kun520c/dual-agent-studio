const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),Module=require('node:module')
const ts=require('../../../design-prototype/node_modules/typescript')
const npm=path.resolve(__dirname,'../../../design-prototype/node_modules')
for(const ext of ['.ts','.tsx'])require.extensions[ext]=(m,file)=>{m.paths.unshift(npm);m._compile(ts.transpileModule(fs.readFileSync(file,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2020,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText,file)}
const React=require(path.join(npm,'react')),server=require(path.join(npm,'react-dom/server'))
const {workflow,currentStage}=require('../src/workflow.ts'),{Timeline}=require('../src/Timeline.tsx')
function run(messages,checks=[],events=[]){return {taskId:'ui',task:{id:'ui',goal:'任务',state:'verified',outcome:{label:'可信验证通过',tone:'success'},started:'',callKind:'offline_cli'},permission:{participants:[]},messages,events,messageTotal:messages.length,eventTotal:events.length,verificationSummary:checks,verificationTotal:checks.length,integrityWarnings:[],usage:{},resultSummary:{},hasResult:true}}
function message(round,role,content='{"summary":"自然语言","details":"说明","decision":"continue","risks":[]}'){return {id:'m'+round,round_index:round,role,executor:role==='developer'?'codex':'cursor',content,exit_code:0}}
function render(r,mode='standard'){return server.renderToStaticMarkup(React.createElement(Timeline,{run:r,mode,diff:null,diffError:'',verification:null,verificationState:'ready',onEvidence(){},onPage(){},followLatest:true,onFollowLatest(){}}))}
test('first-pass real history never invents repair stages',()=>{const r=run([message(1,'developer'),message(2,'reviewer'),message(3,'acceptor')]);assert.equal(workflow(r).filter(x=>x.repair).length,0);const html=render(r);assert.match(html,/本次未触发自动修复/);assert.doesNotMatch(html,/data-workflow-stage="repair"/);assert.equal((html.match(/自然语言/g)||[]).length,3)})
test('offline repair is failure, review, repair, retest, acceptance in saved order',()=>{const r=run([message(1,'developer'),message(2,'reviewer'),message(3,'developer'),message(4,'acceptor')],[{type:'command',ok:false,attempt:1,diff_path:'rounds/001/changes.diff',superseded:true},{type:'command',ok:true,attempt:2,diff_path:'rounds/003/changes.diff'}]);assert.deepEqual(workflow(r).map(x=>x.type==='verification'?(x.checks[0].ok?'pass':'fail'):x.repair?'repair':x.role),['developer','fail','reviewer','repair','pass','acceptor']);const html=render(r);assert.match(html,/可信复测/);assert.match(html,/修复前证据，已替代/)})
test('a live start names the actual Agent without premature stages or result',()=>{const r=run([],[],[{event:'step_started',round:1,role:'developer',agent:'codex-dev'}]);r.task.state='running';r.hasResult=false;assert.match(currentStage(r),/codex-dev/);const html=render(r);assert.match(html,/正在执行/);assert.doesNotMatch(html,/data-workflow-stage="acceptor"|data-workflow-stage="result"/)})
test('raw JSON is collapsed and untrusted HTML is escaped',()=>{const html=render(run([message(1,'developer','<img src=x onerror=alert(1)>')]));assert.doesNotMatch(html,/<img/);assert.match(html,/&lt;img/);assert.doesNotMatch(html,/content_sha256|raw-record-body/)})
test('repeated saved CLI events are deduplicated',()=>{const e={event:'cli_activity',round:1,kind:'command_execution',state:'completed',detail:'read file',cli_event_id:'x'};const r=run([message(1,'developer')],[],[e,e]);assert.equal(workflow(r)[0].activity.length,1)})
test('a baseline failure leads directly to an evidence-backed repair',()=>{const r=run([message(1,'reviewer'),message(2,'developer'),message(3,'acceptor')],[{type:'command',ok:false,baseline:true,attempt:1,diff_path:'baseline/changes.diff',superseded:true},{type:'command',ok:true,attempt:2,diff_path:'rounds/002/changes.diff'}],[{event:'baseline_entered_repair'}]);assert.deepEqual(workflow(r).map(x=>x.type==='verification'?(x.checks[0].ok?'pass':'fail'):x.repair?'repair':x.role),['fail','reviewer','repair','pass','acceptor']);assert.match(render(r),/data-workflow-stage="repair"/)})
test('multiple planned developer messages cannot invent automatic repair',()=>{const r=run([message(1,'developer'),message(2,'developer'),message(3,'acceptor')]);assert.equal(workflow(r).some(x=>x.repair),false);assert.doesNotMatch(render(r),/data-workflow-stage="repair"/)})

test('reading modes change actual content and expansion, including short messages',()=>{
 const r=run([message(1,'developer')],[],[{event:'cli_activity',round:1,kind:'command_execution',detail:'bounded tool output'}])
 const concise=render(r,'concise'),standard=render(r),detailed=render(r,'detailed')
 assert.doesNotMatch(concise,/raw-events|data-cli-activity|usage-view|原始消息/)
 assert.match(standard,/class="step-content" hidden=""/)
 assert.match(standard,/工具与执行记录/)
 assert.doesNotMatch(detailed,/class="step-content" hidden/)
 assert.match(detailed,/raw-events" open=""/)
})
test('only actual unfinished stages spin, and terminal states stop immediately',()=>{
 const r=run([],[],[{event:'step_started',round:1,role:'developer',agent:'codex-dev'}]);r.task.state='running';r.hasResult=false
 assert.match(render(r),/step-spinner/)
 r.task.state='cancelled';assert.doesNotMatch(render(r),/step-spinner/);assert.match(render(r),/已取消/)
 r.task.state='failed';assert.doesNotMatch(render(r),/step-spinner/)
})
test('detailed mode retains a complete long Agent explanation',()=>{
 const detail='公开说明'.repeat(1800),r=run([message(1,'developer',JSON.stringify({summary:'短摘要',details:detail}))])
 assert.ok(render(r,'detailed').includes(detail));assert.ok(!render(r).includes(detail))
})
