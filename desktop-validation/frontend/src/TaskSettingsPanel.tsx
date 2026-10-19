import type {RefObject} from 'react'
import {X} from 'lucide-react'
import type {Budget, Defaults, Preflight, ProjectReadout} from './bridge'
import {participantLine, worktreeSummary} from './composeModel'
import {FloatingPanel} from './FloatingPanel'

const labels: Record<keyof Budget, string> = {modelCalls: '模拟调用次数', rounds: '最大轮数', repairs: '修复次数', runSeconds: '总时限（秒）', stepSeconds: '单步时限（秒）'}

export function TaskSettingsPanel({open, anchorRef, onClose, execution, title, setTitle, mode, setMode, template, setTemplate, defaults, budget, setBudget, readout, participants, busy, validated, report, canValidate, onValidate, onRestore, canRestore}: {
  open: boolean
  anchorRef: RefObject<HTMLButtonElement | null>
  onClose: () => void
  execution: string
  title: string
  setTitle: (value: string) => void
  mode: string
  setMode: (value: string) => void
  template: string
  setTemplate: (value: string) => void
  defaults: Defaults | null
  budget: Budget | null
  setBudget: (value: Budget) => void
  readout?: ProjectReadout
  participants: {id?: string; executor?: string; role?: string}[]
  busy: boolean
  validated: boolean
  report: Preflight | null
  canValidate: boolean
  onValidate: () => void
  onRestore: () => void
  canRestore: boolean
}) {
  const scope = readout?.verification?.find(item => item.type === 'allowed_changes')?.summary || '允许改动路径未在项目摘要中展开。'
  const tests = defaults?.verification?.length
    ? defaults.verification.map(item => item.label).join(' · ')
    : (readout?.verification?.map(item => item.summary).join(' · ') || '未读取可信测试摘要。')
  const templates = defaults?.templates.filter(item => (item.id === 'review') === (mode === 'read_only')) || []
  return <FloatingPanel open={open} anchorRef={anchorRef} label="任务设置" onClose={onClose}>
    <header className="floating-head">
      <h2>任务设置</h2>
      <button type="button" aria-label="关闭任务设置" onClick={onClose}><X size={16}/></button>
    </header>
    <div className="floating-body">
    <label>任务标题<input data-autofocus aria-label="任务标题" maxLength={120} value={title} disabled={busy} placeholder="留空时使用目标首行" onChange={event => setTitle(event.target.value)}/></label>
    <label>执行模式<select aria-label="执行模式" value={mode} disabled={busy || execution !== 'mock'} onChange={event => {setMode(event.target.value); setTemplate(event.target.value === 'read_only' ? 'review' : 'verified')}}>
      <option value="controlled_write">受控写入</option>
      <option value="read_only">只读审查</option>
    </select></label>
    {execution === 'mock' && <label>模拟模板<select aria-label="模拟执行模板" value={template} disabled={!defaults || busy} onChange={event => setTemplate(event.target.value)}>
      {templates.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
    </select></label>}
    <p className="microcopy">{execution !== 'mock' ? 'Codex 在授权 Worktree 开发和修复，Cursor 以 Ask 模式审查和验收。可信命令由协调器执行。' : mode === 'read_only' ? '只读模式只运行审查者，不修改工作区。流程结束不代表验收通过。' : '受控写入只在批准的隔离工作区运行固定样本，不执行自然语言里的任意代码。'}</p>
    <details>
      <summary>预算与超时</summary>
      {budget && defaults ? <div className="settings-grid">{(Object.keys(labels) as (keyof Budget)[]).map(key => <label key={key}>{key === 'modelCalls' && execution !== 'mock' ? 'CLI 调用次数' : labels[key]}<input aria-label={labels[key]} type="number" step={key.endsWith('Seconds') ? '0.2' : '1'} min={key === 'repairs' ? 0 : key.endsWith('Seconds') ? 0.2 : 1} max={defaults.budget[key]} value={budget[key]} disabled={busy} onChange={event => setBudget({...budget, [key]: Number(event.target.value)})}/></label>)}</div> : <p className="microcopy">当前来源不可执行，不能编辑预算。</p>}
      <p className="microcopy">不能超过项目预设。真实模式的预算硬限制仍由 Python 执行门控决定。</p>
    </details>
    <details>
      <summary>参与者与职责</summary>
      {participants.length ? <ul className="agent-list">{participants.map(person => <li key={person.id || participantLine(person)}>{participantLine(person)}</li>)}</ul> : <p className="microcopy">未配置参与者。</p>}
      {defaults?.assignment && <p className="microcopy">{defaults.assignment}</p>}
    </details>
    <details>
      <summary>可信测试与允许改动</summary>
      <p className="microcopy">{tests}</p>
      <p className="microcopy">{scope}</p>
    </details>
    <details>
      <summary>Worktree</summary>
      <p className="microcopy">{worktreeSummary(defaults?.worktree, readout?.worktree?.summary)}</p>
    </details>
    {report && <div className="preflight-list" aria-label="联调预检">{report.items.map(item => <div className={`preflight-item ${item.status}`} key={item.code}><b>{item.status === 'PASS' ? '通过' : item.status === 'BLOCKED' ? '阻断' : '未检查'}</b><span>{item.title}：{item.detail}</span></div>)}<span>真实模型调用 {report.realModelCalls}</span></div>}
    <div className="panel-actions">
      <button type="button" className="secondary-button" disabled={busy || !canValidate} onClick={onValidate}>{busy ? '正在校验…' : validated ? '重新校验并预检' : '校验并预检'}</button>
      {canRestore && <button type="button" className="secondary-button" disabled={busy} onClick={onRestore}>恢复未确认内容</button>}
    </div>
    </div>
  </FloatingPanel>
}
