import type {Evidence, Run} from './bridge'
import {callBudget, diffStats, formatDuration, phaseLabel, traceLine, trustedVerification, type ReadingMode} from './present'

function recordsOf(evidence: Evidence | null) {
  return Array.isArray(evidence?.data) ? evidence.data as Record<string, unknown>[] : null
}

export function ResultSummary({run, diff, verification, verificationState, mode}: {run: Run; verification: Evidence | null; verificationState: 'loading' | 'ready' | 'error'; diff: Evidence | null; mode: ReadingMode}) {
  const task = run.task
  const active = ['queued', 'running', 'reviewing', 'verifying', 'repairing'].includes(task.state)
  const budget = callBudget(task.callKind, run.usage, run.permission.max_model_calls)
  const repairCap = typeof run.permission.max_repair_rounds === 'number' ? run.permission.max_repair_rounds : null
  const duration = formatDuration(run.resultSummary.duration_seconds)
  const records = recordsOf(verification)
  const trusted = records ? trustedVerification(records) : null
  const files = diffStats(diff?.files)
  const fileText = !diff ? '读取改动…' : files && files.count ? `${files.count} 个文件 · +${files.added} −${files.removed}` : diff.snapshot ? '没有代码改动' : '未保存 Git Diff'
  const verifyText = verificationState === 'loading' ? '读取验证…' : verificationState === 'error' ? '验证记录读取失败' : trusted?.headline || '未保存可信验证'
  const last = [...run.messages].reverse().find(message => message.executor || message.role)
  const current = last ? `${String(last.executor || 'Agent')} · ${phaseLabel(task.state)}` : phaseLabel(task.state)
  const timing = active ? current : duration || '耗时未记录'
  const compact = [timing, `Codex ${run.usage.codex_calls || 0}`, `Cursor ${run.usage.cursor_calls || 0}`, `${budget.label} ${budget.used}${budget.limit !== null ? `/${budget.limit}` : ''}`, verifyText, fileText].join(' · ')
  return <section className={`result-summary ${mode === 'detailed' ? 'detailed' : 'compact'}`} data-result-summary aria-label="执行结果">
    <div className="result-summary-head">
      <span className={`status-word ${task.outcome.tone}`}><i />{task.outcome.label}</span>
      <span className="result-compact">{mode === 'concise' ? timing : compact}</span>
    </div>
    {mode === 'detailed' && <dl className="result-metrics">
      <div><dt>Codex</dt><dd>{run.usage.codex_calls || 0} 次</dd></div>
      <div><dt>Cursor</dt><dd>{run.usage.cursor_calls || 0} 次</dd></div>
      <div><dt>{budget.label}</dt><dd>{budget.used}{budget.limit !== null ? ` / ${budget.limit}` : ''}{task.callKind !== 'model' ? ` · 真实 ${budget.real}` : ''}</dd></div>
      <div><dt>自动修复</dt><dd>{run.usage.repairs_used || 0}{repairCap !== null ? ` / ${repairCap}` : ''}</dd></div>
      <div><dt>可信验证</dt><dd>{verifyText}</dd></div>
      <div><dt>修改</dt><dd>{fileText}</dd></div>
    </dl>}
    {mode === 'detailed' && <p className="result-note">{trusted?.detail || (verificationState === 'ready' ? '没有可展示的协调器验证明细。Agent 自述不计入可信验证。' : '可信验证只展示协调器保存的记录。')}</p>}
    {mode === 'detailed' && run.messages.length > 0 && <p className="result-trace">{traceLine(run.messages)}</p>}
    {task.outcome.kind === 'flow_complete' && mode !== 'concise' && <p className="result-note">流程结束不等于功能验收。</p>}
  </section>
}
