import {useEffect,useState} from 'react'
import type {Run} from './bridge'
import {activeStates,currentStage} from './workflow'
import {formatDuration} from './present'
export function RunStatus({run,cancel,onCancel}:{run:Run;cancel:boolean;onCancel?:()=>void}){
 const [now,setNow]=useState(Date.now())
 const active=activeStates.includes(run.task.state)
 useEffect(()=>{if(!active)return;const timer=setInterval(()=>setNow(Date.now()),1000);return()=>clearInterval(timer)},[active])
 const start=Date.parse(run.task.started)
 const seconds=active&&Number.isFinite(start)?Math.max(0,(now-start)/1000):run.resultSummary.duration_seconds
 return <div className="run-status" role="status"><span className={`status-word ${run.task.outcome.tone}`}><i/>{cancel?'正在取消':run.task.outcome.label}</span>{active&&<span>{currentStage(run)}</span>}<time>{formatDuration(seconds)||'耗时未知'}</time>{active&&onCancel&&<button className="secondary-button" disabled={cancel} onClick={onCancel}>{cancel?'取消中…':'停止任务'}</button>}</div>
}
