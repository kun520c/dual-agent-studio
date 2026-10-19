import type {Run} from './bridge'

export const activeStates = ['queued','running','reviewing','verifying','repairing']
export function workflow(run:Run) {
  const starts=new Map<number,Record<string,unknown>>()
  for(const event of run.events)if(event.event==='step_started')starts.set(Number(event.round),event)
  const messages=new Map<number,Record<string,unknown>>()
  for(const message of run.messages)messages.set(Number(message.round_index),message)
  const rounds=[...new Set([...starts.keys(),...messages.keys()])].sort((a,b)=>a-b)
  let developers=0
  const rows: {key:string;type:'agent'|'verification';round:number;role:string;repair:boolean;message?:Record<string,unknown>;start?:Record<string,unknown>;activity:Record<string,unknown>[];checks:Record<string,unknown>[]}[]=[]
  const usedChecks=new Set<number>()
  const baseline=run.verificationSummary.filter((check,index)=>{if(check.baseline===true||String(check.diff_path||'').startsWith('baseline/')){usedChecks.add(index);return true}return false})
  if(baseline.length)rows.push({key:'verification-baseline',type:'verification',round:0,role:'coordinator',repair:false,activity:[],checks:baseline})
  const baselineRepair=run.events.some(e=>e.event==='baseline_entered_repair')
  const repairRounds=new Set<number>();let phase=''
  for(const event of run.events){
    if(event.event==='status')phase=String(event.phase||'')
    if(event.event==='step_started'&&event.role==='developer'&&phase==='repairing')repairRounds.add(Number(event.round))
  }
  for(const round of rounds){
    const message=messages.get(round),start=starts.get(round)
    const role=String(message?.role||start?.role||'')
    if(role==='developer')developers++
    const seen=new Set<string>()
    const activity=run.events.filter(e=>e.event==='cli_activity'&&Number(e.round)===round).filter(e=>{const key=JSON.stringify([e.cli_event_id,e.kind,e.state,e.detail]);if(seen.has(key))return false;seen.add(key);return true})
    const failedBefore=run.verificationSummary.some(c=>c.ok===false&&c.superseded===true&&Number(String(c.diff_path||'').match(/rounds\/(\d+)\//)?.[1]||0)<round)
    const retest=run.verificationSummary.some(c=>Number(c.attempt)>1&&Number(String(c.diff_path||'').match(/rounds\/(\d+)\//)?.[1])===round)
    const repair=role==='developer'&&(repairRounds.has(round)||(baselineRepair&&developers===1)||(developers>1&&failedBefore&&(retest||run.usage.repairs_used>0)))
    rows.push({key:`agent-${round}`,type:'agent',round,role,repair,message,start,activity,checks:[]})
    const checks=run.verificationSummary.filter((check,index)=>{
      const match=String(check.diff_path||'').match(/rounds\/(\d+)\//)
      const belongs=match?Number(match[1])===round:role==='developer'&&Number(check.attempt||1)===developers
      if(belongs&&!usedChecks.has(index)){usedChecks.add(index);return true}return false
    })
    if(checks.length)rows.push({key:`verification-${round}`,type:'verification',round,role:'coordinator',repair,activity:[],checks})
  }
  const remaining=run.verificationSummary.filter((_,index)=>!usedChecks.has(index))
  if(remaining.length)rows.push({key:'verification-saved',type:'verification',round:0,role:'coordinator',repair:false,activity:[],checks:remaining})
  return rows
}

export function currentStage(run:Run){
  if(!activeStates.includes(run.task.state))return run.task.outcome.label
  if(run.task.state==='verifying')return '协调器 · 可信测试'
  const last=[...run.events].reverse().find(e=>e.event==='step_started')
  if(!last)return run.task.state==='queued'?'等待调度':'协调器 · 正在准备工作区'
  const participant=(run.permission.participants as Record<string,unknown>[]|undefined)?.find(p=>p.id===last.agent)
  const name=String(participant?.executor||last.agent||'Agent')
  return `${name==='codex'?'Codex':name==='cursor'?'Cursor':name} · ${{developer:'开发',reviewer:'审查',acceptor:'验收',diagnoser:'诊断'}[String(last.role)]||'执行'}${run.task.state==='repairing'?' · 自动修复':''}`
}
