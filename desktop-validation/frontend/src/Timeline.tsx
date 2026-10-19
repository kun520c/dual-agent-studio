import {memo, useState, type ReactNode} from 'react'
import {Code2, ScanLine, ShieldCheck, FileCode2, ChevronDown, LoaderCircle, CircleCheck, CircleX, Ban} from 'lucide-react'
import type {Evidence, Run} from './bridge'
import {CopyButton, RawRecord} from './records'
import {decisionLabel, formatClock, formatDuration, parseAgentContent, type ReadingMode} from './present'
import {activeStates, workflow} from './workflow'
import {UsageView} from './UsageView'
export type {ReadingMode}
function Avatar({name}:{name:string}){return <span className={`agent-avatar ${name==='cursor'?'cursor':name==='codex'?'codex':'coordinator'}`}>{name==='cursor'?<ScanLine size={16}/>:name==='codex'?<Code2 size={16}/>:<ShieldCheck size={16}/>}</span>}
const AgentMessage=memo(function AgentMessage({message,mode}:{message:Record<string,unknown>;mode:ReadingMode}){
 const narrative=parseAgentContent(message.content),[expanded,setExpanded]=useState(false)
 const details=narrative.details,limit=mode==='concise'?400:1800
 const files=Array.isArray(message.claimed_files)?message.claimed_files:[]
 return <><div className="agent-prose">{details&&<p className="preserve-text message-details">{mode==='detailed'||expanded||details.length<=limit?details:details.slice(0,limit)+'…'}</p>}{mode!=='detailed'&&details.length>limit&&<button className="inline-link" onClick={()=>setExpanded(!expanded)}>{expanded?'收起':'展开完整说明'}</button>}</div>
 {mode!=='concise'&&files.length>0&&<div className="workflow-files">{files.map(file=><span key={String(file)}><FileCode2 size={13}/><code>{String(file)}</code></span>)}</div>}
 {narrative.risks.length>0&&<details className="workflow-details" open={mode==='detailed'?true:undefined}><summary>风险与注意事项 · {narrative.risks.length}</summary><ul>{narrative.risks.map(risk=><li key={risk}>{risk}</li>)}</ul></details>}
 {message.error_kind&&message.error_kind!=='ok'?<p className="workflow-failure">{String(message.error_message||message.error_kind)}</p>:null}
 <div className="message-actions"><CopyButton label="复制正文" text={narrative.body||narrative.summary}/>{mode!=='concise'&&<RawRecord title="原始消息" value={message}/>}</div></>
})
function Step({id,name,stage,summary,state,time,open,onToggle,children}:{id:string;name:string;stage:string;summary:string;state:string;time?:string;open:boolean;onToggle:()=>void;children:ReactNode}){
 return <article className={`workflow-step step-${state}`} data-step-key={id} data-current-step={state==='running'?'true':undefined}>
 <div className="step-header" role="button" tabIndex={0} aria-expanded={open} onClick={()=>{if(!window.getSelection()?.toString())onToggle()}} onKeyDown={event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();onToggle()}}} title={`${name} · ${stage} · ${open?'收起':'展开'}详情`}>
 <Avatar name={name.toLowerCase()}/><span className="step-identity"><strong>{name}</strong><small>{stage}</small></span><span className="step-summary preserve-text">{summary}</span><span className="step-state">{state==='running'?<LoaderCircle className="step-spinner" size={17}/>:state==='success'?<CircleCheck size={17}/>:state==='cancelled'?<Ban size={17}/>:<CircleX size={17}/>}<small>{state==='running'?'正在执行':state==='success'?'已完成':state==='cancelled'?'已取消':'已失败'}</small>{time&&<time>{time}</time>}</span><ChevronDown size={14} className={open?'step-chevron open':'step-chevron'}/>
 </div><div className="step-content" hidden={!open}>{children}</div></article>
}
export function Timeline({run,mode,diff,diffError,onEvidence,onPage,followLatest,onFollowLatest}: {run:Run;mode:ReadingMode;diff:Evidence|null;diffError:string;verification:Evidence|null;verificationState:'loading'|'ready'|'error';onEvidence:(kind:string)=>void;onPage:(m:number,e:number)=>void;followLatest:boolean;onFollowLatest:()=>void}){
 const active=activeStates.includes(run.task.state),rows=workflow(run)
 // Explicit user choices persist; automatic defaults respond to actual stage and mode.
 const [choices,setChoices]=useState<Record<string,boolean>>({})
 const opened=(key:string,current=false)=>choices[key]??(mode==='detailed'||current)
 const toggle=(key:string,current=false)=>setChoices(old=>({...old,[key]:!opened(key,current)}))
 const last=rows.filter(row=>row.type==='agent').at(-1)
 const verifying=active&&run.task.state==='verifying'&&run.events.some(e=>e.event==='verification_started')
 return <div className={`timeline workflow reading-${mode}`} data-timeline-task={run.taskId}>
 <div className="workflow-goal"><span>任务目标</span><span title={run.task.goal}>{(run.task.goal||'目标未保存').split('\n')[0].slice(0,160)}</span><small>完整目标见底部</small></div>
 {run.integrityWarnings.length>0&&<p className="workflow-failure" role="alert">{run.integrityWarnings.join('；')}</p>}
 {rows.map(row=>{
  if(row.type==='verification'){
   const failed=row.checks.some(check=>check.ok===false)
   const summary=row.checks.map(check=>{const tests=check.tests as {run:number;failures:number;errors:number}|undefined;return tests?`${check.toolchain||'可信测试'} ${tests.run-tests.failures-tests.errors}/${tests.run} 通过`:check.type==='allowed_changes'?`改动范围${check.ok?'通过':'未通过'}`:`可信验证${check.ok?'通过':'未通过'}`}).join(' · ')
   return <div key={row.key} data-workflow-stage="verification"><Step id={row.key} name="协调器" stage={`可信${row.repair?'复测':'测试'}`} summary={summary} state={failed?'failed':'success'} open={opened(row.key)} onToggle={()=>toggle(row.key)}>{row.checks.map((check,index)=>{const tests=check.tests as {run:number;failures:number;errors:number}|undefined;return <div className={`workflow-test ${check.ok===false?'failed':''}`} key={index}><span className="test-mark">{check.ok===true?'✓':check.ok===false?'!':'·'}</span><span>{check.type==='allowed_changes'?'改动范围检查':check.type==='command'?String(check.toolchain||'可信命令')+'验证':String(check.type||'独立验证')}<small>{tests?`${tests.run} 项 · 失败 ${tests.failures} · 错误 ${tests.errors}`:check.exit_code!==undefined?`退出码 ${String(check.exit_code)}`:check.ok===true?'通过':check.ok===false?'未通过':'结果未知'}{check.superseded?' · 修复前证据，已替代':''}{check.evidence_invalidated?' · 已失效':''}</small></span></div>})}<button className="inline-link" onClick={()=>onEvidence('verification')}>查看验证证据</button></Step></div>
  }
  const m=row.message,participants=run.permission.participants as Record<string,unknown>[]|undefined
  const p=participants?.find(p=>p.id===row.start?.agent),executor=String(m?.executor||p?.executor||row.start?.agent||'Agent')
  const name=executor==='codex'?'Codex':executor==='cursor'?'Cursor':executor
  const stage=row.repair?'自动修复':({developer:'开发',reviewer:'审查',acceptor:'验收',diagnoser:'诊断'}[row.role]||'执行')
  const current=active&&!m&&row===last&&!verifying
  const ok=!!m&&m.invoked!==false&&(!m.error_kind||m.error_kind==='ok')&&(m.exit_code===undefined||m.exit_code===0)
  const state=current?'running':m?(ok?'success':'failed'):run.task.state==='cancelled'?'cancelled':'failed'
  const narrative=parseAgentContent(m?.content)
  const summary=narrative.summary||String(m?.error_message||(current?'CLI 正在工作，等待本阶段结果。':state==='cancelled'?'此阶段已取消':'输出未保存'))
  return <div key={row.key} data-workflow-stage={row.repair?'repair':row.role} data-message-id={String(m?.id||'')}><Step id={row.key} name={name} stage={stage} summary={summary+(m?.decision==='accept'?' · accept':'')} state={state} time={formatDuration(m?.duration_seconds)||formatClock(row.start?.time).slice(11)} open={opened(row.key,current)} onToggle={()=>toggle(row.key,current)}>
  {row.repair&&<p className="repair-cause">根据上一轮可信失败证据与审查反馈修复，再使用同一可信命令复测。</p>}
  {m?<AgentMessage message={m} mode={mode}/>:<p className="agent-waiting">{summary}</p>}
  {mode!=='concise'&&row.activity.length>0&&<details className="workflow-details" open={mode==='detailed'?true:undefined} data-cli-activity><summary>工具与执行记录 · {row.activity.length}</summary>{row.activity.map((event,index)=><div className="activity-row" key={index}><span>{event.kind==='agent_message'?'说明':event.kind==='file_change'?'文件改动':event.kind==='command_execution'?'命令':'工具'}</span><p className="preserve-text">{String(event.detail||'')}</p><small>{event.state==='completed'?'完成':'开始'}</small></div>)}</details>}
  {typeof m?.decision==='string'&&<span className="workflow-decision">{decisionLabel(m.decision)}{m.decision==='accept'?' · accept':''}</span>}
  </Step></div>
 })}
 {verifying&&<Step id="verification-current" name="协调器" stage="可信测试" summary="正在执行固定可信命令" state="running" open={opened('verification-current',true)} onToggle={()=>toggle('verification-current',true)}><p className="agent-waiting">等待协调器保存验证结果。</p></Step>}
 {!rows.length&&!verifying&&<p className="agent-waiting">{active?'正在准备工作区，尚未启动 Agent。':'此历史没有可读取的阶段记录。'}</p>}
 {!active&&run.hasResult&&<article className="workflow-finish" data-workflow-stage="result"><ShieldCheck size={19}/><div><strong>{run.task.outcome.label}{run.task.outcome.passed?' · verified':''}</strong><p>{String(run.resultSummary.error_message||run.resultSummary.stop_reason_zh||run.task.outcome.reason)}</p><small>{run.usageReport?.cli_launches??run.task.modelCalls??'未知'} 次 CLI 启动 · {run.usage.repairs_used??'未知'} 次修复{!rows.some(row=>row.repair)?' · 本次未触发自动修复':''}</small><button className="inline-link" onClick={()=>onEvidence('result')}>查看最终验收记录</button></div></article>}
 {mode!=='concise'&&diff&&<div className="workflow-change"><FileCode2 size={15}/><span>{diff.files?.length?`${new Set(diff.files.map(f=>f.path)).size} 个文件有改动`:diff.snapshot?'没有代码改动':'未保存改动快照'}</span><button className="inline-link" onClick={()=>onEvidence('diff')}>查看 Diff</button></div>}{mode!=='concise'&&diffError&&<p className="muted-copy">Diff：{diffError}</p>}
 {mode!=='concise'&&<UsageView run={run} expanded={mode==='detailed'}/>}
 {(run.messageTotal>run.messages.length||run.eventTotal>run.events.length)&&<div className="pagination"><button disabled={run.messageOffset===0&&run.eventOffset===0} onClick={()=>onPage(Math.max(0,run.messageOffset-100),Math.max(0,run.eventOffset-200))}>较早记录</button><span>已显示 {run.messages.length} / {run.messageTotal} 条消息</span><button disabled={run.messageOffset+run.messages.length>=run.messageTotal&&run.eventOffset+run.events.length>=run.eventTotal} onClick={()=>onPage(run.messageOffset+run.messages.length,run.eventOffset+run.events.length)}>较新记录</button></div>}
 {!followLatest&&<div className="follow-latest"><button onClick={onFollowLatest}>↓ 返回当前步骤</button></div>}
 {mode!=='concise'&&<PublicEvents run={run} mode={mode}/>}
 </div>
}
function PublicEvents({run,mode}:{run:Run;mode:ReadingMode}){
 const [open,setOpen]=useState(false),shown=mode==='detailed'||open
 return <details className="workflow-details raw-events" open={shown} onToggle={e=>{if(mode!=='detailed')setOpen(e.currentTarget.open)}}><summary>完整执行事件 · {run.eventTotal} · 分页读取</summary>{shown&&run.events.map((event,index)=><RawRecord title={`${formatClock(event.time)} · ${String(event.event)}`} value={event} key={index}/>)}</details>
}
