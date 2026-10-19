import {useEffect, useState} from 'react'
import {Activity, ChevronRight, Eye, FileCode2, GitBranch, GitCompareArrows, Maximize2, Minimize2, ShieldCheck, Terminal, TriangleAlert} from 'lucide-react'
import type {Evidence, Outcome, Run, Task} from './bridge'
import {highlight} from './highlight'
import {commandName, eventLabel, formatClock, formatDuration, parseTestCounts, trustedVerification} from './present'
import {RawRecord} from './records'

function Status({task}: {task: Task}) {
  return <span className={`status ${task.outcome.tone}`}>{task.outcome.passed ? <ShieldCheck size={13} /> : task.outcome.kind === 'flow_complete' ? <Eye size={13} /> : task.state === 'queued' ? <Terminal size={13} /> : ['running', 'reviewing', 'verifying', 'repairing'].includes(task.state) ? <Activity size={13} /> : <TriangleAlert size={13} />}<span>{task.outcome.label}</span></span>
}
function isDiffPreamble(text: string) {
  const prefixes = ['diff --git ', 'index ', '--- ', '+++ ', 'old mode ', 'new mode ', 'deleted file mode', 'new file mode', 'similarity index', 'rename from ', 'rename to ', 'Binary files', '\\ No newline', '$ git ']
  return prefixes.some(prefix => text.startsWith(prefix))
}

function CodeLine({text, path, kind}: {text: string; path: string; kind: string}) {
  const tokens = kind === 'hunk' ? [{text: text || ' ', kind: 'plain' as const}] : highlight(text || ' ', path)
  return <code>{tokens.map((token, index) => <span key={index} className={token.kind === 'plain' ? undefined : `tok tok-${token.kind}`}>{token.text}</span>)}</code>
}

function VerificationView({records, outcome}: {records: Record<string, unknown>[]; outcome?: Outcome}) {
  const view = trustedVerification(records)
  return <div className="verify-view">
    <p className="verify-kicker">协调器 verification.json · Agent 自述不作为验收依据</p>
    <h3 className={view.state === 'pass' ? 'verify-pass' : view.state === 'fail' ? 'verify-fail' : ''}>{view.headline}</h3>
    <ul className="verify-points">
      {view.commands.map((command, index) => <li key={`${command.name}-${index}`}><b>{command.name}</b><span>exit {command.exitCode}{command.duration ? ` · ${command.duration}` : ''}</span>{command.tests && <span>{command.tests.run} 项，失败 {command.tests.failures}，错误 {command.tests.errors}，跳过 {command.tests.skipped}</span>}{command.error && <span>{command.error}</span>}{command.inconsistent && <span>输出中的失败数与通过标记不一致</span>}</li>)}
      {view.scope && <li><b>改动范围</b><span>{view.scope}</span></li>}
      {outcome && <li><b>验收结论</b><span>{outcome.label}{outcome.reason ? ` · ${outcome.reason}` : ''}</span></li>}
      {!view.commands.length && !view.scope && <li><span>{view.detail}</span></li>}
    </ul>
    {records.map((record, index) => {
      const tests = parseTestCounts(record.stdout)
      const stdout = typeof record.stdout === 'string' ? record.stdout : ''
      const stderr = typeof record.stderr === 'string' ? record.stderr : ''
      return <details className="verify-record" key={index}>
        <summary><span>{commandName(record)}</span><em>{record.superseded ? '已被替代' : record.evidence_invalidated ? '已失效' : record.ok === true ? '通过' : record.ok === false ? '未通过' : '未记录'}</em><ChevronRight size={13} /></summary>
        <dl className="msg-meta">
          <div><dt>退出码</dt><dd>{record.exit_code === undefined ? '不适用' : String(record.exit_code)}</dd></div>
          <div><dt>耗时</dt><dd>{formatDuration(record.duration_seconds) || '未记录'}</dd></div>
          {tests && <div><dt>测试</dt><dd>{tests.run} 项，失败 {tests.failures}</dd></div>}
        </dl>
        {stdout && <RawRecord title="stdout" value={stdout} />}
        {stderr && <RawRecord title="stderr" value={stderr} />}
        <RawRecord title="原始验证记录" value={record} />
      </details>
    })}
  </div>
}

export function Inspector({task, run, tab, setTab, kind, setKind, evidence, loading, error, load, maximized, onToggleMaximize}: {task: Task | undefined; run: Run | null; tab: string; setTab: (s: string) => void; kind: string; setKind: (s: string) => void; evidence: Evidence | null; loading: boolean; error: string; load: (k: string, f?: string, o?: number) => void; maximized: boolean; onToggleMaximize: () => void}) {
  const [wrap, setWrap] = useState(false)
  const [selected, setSelected] = useState('')
  const [rawLogs, setRawLogs] = useState(false)
  const [rawResult, setRawResult] = useState(false)
  useEffect(() => setSelected(''), [task?.id])
  const files = evidence?.files || []
  const file = files.find(item => item.id === selected) || files[0]
  const verification = Array.isArray(evidence?.data) ? evidence.data as Record<string, unknown>[] : []
  const logs = Array.isArray(evidence?.data) ? evidence.data as Record<string, unknown>[] : []
  return <aside className="inspector" aria-label="真实代码和验证上下文">
    <div className="inspector-header">
      <div className="inspector-tabs" role="tablist">
        {[{id: 'diff', name: '改动', Icon: GitCompareArrows}, {id: 'evidence', name: '证据', Icon: ShieldCheck}, {id: 'session', name: '会话', Icon: Activity}].map(item => <button type="button" role="tab" data-tab={item.id} aria-selected={tab === item.id} className={tab === item.id ? 'selected' : ''} key={item.id} onClick={() => setTab(item.id)}><item.Icon size={14} />{item.name}</button>)}
      </div>
      <button type="button" className="icon-button inspector-max-toggle" aria-label={maximized ? '恢复分栏' : '最大化查看'} aria-pressed={maximized} title={maximized ? '恢复分栏' : '最大化查看'} onClick={onToggleMaximize}>{maximized ? <><Minimize2 size={15} /><span>恢复分栏</span></> : <Maximize2 size={15} />}</button>
    </div>
    {tab === 'evidence' && <div className="sub-tabs" role="tablist">{[{id: 'verification', name: '可信验证'}, {id: 'logs', name: '执行日志'}, {id: 'result', name: '最终验收'}].map(item => <button type="button" role="tab" aria-selected={kind === item.id} className={kind === item.id ? 'selected' : ''} key={item.id} onClick={() => setKind(item.id)}>{item.name}</button>)}</div>}
    {error ? <div className="soft-empty" role="alert">没有对应证据：{error}</div> : !run ? <div className="soft-empty">选择任务后查看对应证据</div> : loading && !evidence ? <div className="soft-empty inspector-pending" aria-busy="true">正在读取当前任务证据…</div> : tab === 'diff' ? <>
      <div className="diff-summary"><div><strong>{files.length ? `${files.length} 个文件` : '没有历史 Diff'}</strong><span><b className="add-text">+{files.reduce((sum, item) => sum + item.added, 0)}</b><b className="remove-text">−{files.reduce((sum, item) => sum + item.removed, 0)}</b></span></div><p><GitBranch size={12} />{evidence?.source || 'Git Diff'}</p></div>
      {files.length ? <>
        <div className="file-list">{files.map(item => <button type="button" key={item.id} className={item.id === file?.id ? 'selected' : ''} onClick={() => { setSelected(item.id); load('diff', item.id) }}><FileCode2 size={14} /><span title={`${item.source || ''} ${item.path}`}>{item.path}{item.source ? ` · ${item.source}` : ''}</span><b className="add-text">+{item.added}</b><b className="remove-text">−{item.removed}</b></button>)}</div>
        <div className="diff-toolbar"><span title={file?.path}>{file?.path}</span><button type="button" aria-label="切换 Diff 换行" aria-pressed={wrap} onClick={() => setWrap(!wrap)}>{wrap ? '已换行' : '横向滚动'}</button></div>
        <div className={`code-diff ${wrap ? 'wrapped' : ''}`} aria-label="真实历史 Diff">{evidence?.lines?.map((line, index) => {
          if (line.kind !== 'hunk' && isDiffPreamble(line.text)) return null
          if (line.kind === 'hunk') return <div className="code-line hunk" key={index}><code>{line.text}</code></div>
          return <div className={`code-line ${line.kind}`} key={index}><span className="line-num">{line.old || ''}</span><span className="line-num">{line.next || ''}</span><span className="line-sign">{line.kind === 'add' ? '+' : line.kind === 'remove' ? '−' : ''}</span><CodeLine text={line.text || ' '} path={file?.path || ''} kind={line.kind} /></div>
        })}</div>
        {(evidence?.total || 0) > (evidence?.lines?.length || 0) && <div className="pagination"><button type="button" disabled={!evidence?.offset} onClick={() => load('diff', file?.id, Math.max(0, (evidence?.offset || 0) - 300))}>上一页</button><span>{(evidence?.offset || 0) + 1}–{(evidence?.offset || 0) + (evidence?.lines?.length || 0)} / {evidence?.total}</span><button type="button" disabled={(evidence?.offset || 0) + (evidence?.lines?.length || 0) >= (evidence?.total || 0)} onClick={() => load('diff', file?.id, (evidence?.offset || 0) + (evidence?.lines?.length || 0))}>下一页</button></div>}
      </> : <div className="context-empty"><h3>{evidence?.snapshot ? '没有代码改动' : '没有对应证据'}</h3><p>{evidence?.snapshot ? '保存的差异快照不包含 Git patch。' : '此任务未保存 Git Diff。'}</p><button type="button" className="secondary-button" onClick={() => { setTab('evidence'); setKind('result') }}>查看验收结果</button></div>}
    </> : tab === 'evidence' ? <div className="evidence-content">{kind === 'verification' ? <VerificationView records={verification} outcome={task?.outcome} /> : kind === 'logs' ? <>
      <p className="verify-kicker">events.jsonl · {evidence?.total || 0} 项</p>
      {evidence?.present === false && <p className="muted-copy">没有对应证据：未保存执行日志</p>}
      {Boolean(evidence?.invalidRecords) && <p className="muted-copy">{evidence?.invalidRecords} 条损坏或未完整记录已排除</p>}
      <div className="log-list">{logs.map((item, index) => <div className="log-row" key={index}><time>{formatClock(item.time).slice(11) || '—'}</time><span>{eventLabel(String(item.event || ''))}</span><code>{String(item.agent || item.phase || item.stop_reason || '')}</code></div>)}</div>
      <button type="button" className="inline-link" onClick={() => setRawLogs(value => !value)}>{rawLogs ? '收起原始日志' : '查看原始日志'}</button>
      {rawLogs && <pre className="event-log" aria-label="真实执行日志">{JSON.stringify(logs, null, 2)}</pre>}
      {(evidence?.total || 0) > logs.length && <div className="pagination"><button type="button" disabled={!evidence?.offset} onClick={() => load('logs', '', Math.max(0, (evidence?.offset || 0) - 300))}>上一页</button><span>{(evidence?.offset || 0) + 1}–{(evidence?.offset || 0) + logs.length} / {evidence?.total}</span><button type="button" disabled={(evidence?.offset || 0) + logs.length >= (evidence?.total || 0)} onClick={() => load('logs', '', (evidence?.offset || 0) + logs.length)}>下一页</button></div>}
    </> : <>
      <p className="verify-kicker">协调器 result.json</p>
      {task && <Status task={task} />}
      <div className="acceptance-list"><div><span>验收</span><strong>{task?.outcome.label || '未记录'}</strong></div><div><span>停止原因</span><code>{task?.outcome.reason || '未记录'}</code></div><div><span>说明</span><span>{String(run?.resultSummary.stop_reason_zh || run?.resultSummary.error_message || '没有附加说明')}</span></div></div>
      {task?.outcome.kind === 'flow_complete' && <p className="result-note">流程结束不等于功能验收。</p>}
      {evidence?.present === false && <p className="muted-copy">没有对应证据：未保存 result.json</p>}
      <button type="button" className="inline-link" onClick={() => setRawResult(value => !value)}>{rawResult ? '收起原始结果' : '查看原始结果'}</button>
      {rawResult && <pre className="terminal-block result-json" aria-label="真实最终结果">{JSON.stringify(evidence?.data || {}, null, 2)}</pre>}
    </>}</div> : <div className="session-content">
      <p className="verify-kicker">本任务用量 · {task?.callKind}</p>
      <div className="usage-grid"><div><strong>{run?.usageReport?.totals.input_tokens?.known ?? '未知'}</strong><span>已知输入 Token（含缓存）</span></div><div><strong>{run?.usageReport?.totals.output_tokens?.known ?? '未知'}</strong><span>已知输出 Token</span></div></div>
      <div className="key-value"><span>Codex</span><span>{run?.usage.codex_calls || 0}</span></div>
      <div className="key-value"><span>Cursor</span><span>{run?.usage.cursor_calls || 0}</span></div>
      <div className="key-value"><span>Mock</span><span>{run?.usage.mock_calls || 0}</span></div>
      <div className="key-value"><span>离线 CLI</span><span>{run?.usage.offline_cli_calls || 0}</span></div>
      <div className="key-value"><span>真实 CLI 启动</span><strong>{run?.usage.real_model_calls || 0}</strong></div>
      <div className="key-value"><span>CLI 内部模型请求 / 实际费用</span><span>未知 / 未对账</span></div>
      <RawRecord title="来源可追溯的用量" value={run?.usageReport || {status:'UNKNOWN'}} />
      <RawRecord title="已保存的权限和预算" value={run?.permission || {}} />
      <p className="result-note">历史 allow_real 配置不会启用执行器。真实 CLI 仍由 Python 签名门控决定。</p>
    </div>}
    <div className="inspector-footer"><span className="tiny-dot" />{evidence?.source || '已保存的执行上下文'}<span>只读</span></div>
  </aside>
}
