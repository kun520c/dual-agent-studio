import {useEffect, useState} from 'react'
import {api, type AccessReport} from './bridge'

const names: Record<string, string> = {mock: 'Mock', offline_cli: '离线 CLI', real: '真实 CLI'}
const order = ['mock', 'offline_cli', 'real'] as const

export function AccessPanel({projectId, enabled, focusMode = '', embedded = false}: {projectId: string; enabled: boolean; focusMode?: string; embedded?: boolean}) {
  const [report, setReport] = useState<AccessReport | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState('')
  const [open, setOpen] = useState(false)
  useEffect(() => {
    let alive = true
    setReport(null); setError(''); setCopied(''); setOpen(false)
    if (!enabled || !projectId) return
    api.inspect(projectId).then(value => { if (alive) setReport(value) }).catch(reason => { if (alive) setError(String(reason)) })
    return () => { alive = false }
  }, [projectId, enabled])
  if (!enabled || !projectId) return null
  const detail = (value: AccessReport) => <>
    {order.map(mode => {
      const item = value.modes[mode]
      return <p key={mode}><b className={item?.allowed ? 'allowed' : 'blocked'}>{names[mode]} {item?.allowed ? '允许' : '阻断'}</b><span>{item?.reason || '没有返回原因'}</span></p>
    })}
    <p>生产公钥：{value.signerInstalled ? '已安装。签名是否有效只由 Python 门控决定，界面不能代签或安装新的信任根。' : '未安装。不能在界面安装公钥或代签。'}</p>
    <p>授权文件：{value.authorization.detail}{value.authorization.signer ? ` 签名者 ${value.authorization.signer}。` : ''}</p>
    <p>Codex：{value.cli.codex.detail} 版本 {value.cli.codex.version || 'NOT CHECKED'}。Cursor：{value.cli.cursor.detail} 版本 {value.cli.cursor.version || 'NOT CHECKED'}。</p>
    <p>账号：{value.account.detail}</p>
    {value.checklist && <div className="preflight-list" aria-label="授权预检">{value.checklist.map(item => <div className={`preflight-item ${item.status}`} key={item.code}><b>{item.status === 'PASS' ? '通过' : item.status === 'BLOCKED' ? '阻断' : '未检查'}</b><span>{item.title}：{item.detail}{item.action ? ` 下一步：${item.action}` : ''}</span></div>)}</div>}
    {value.nextSteps.map(step => <p key={step}>{step}</p>)}
    <p>真实模型调用 {value.realModelCalls}。预检和导出都不启动模型。</p>
  </>
  const exportRequest = async () => {
    setBusy(true)
    try {
      const value = await api.inspect(projectId, true)
      setReport(value)
      setCopied(value.export ? `已写入 ${value.export.relative}。文件没有签名。` : '')
    } catch (reason) { setError(String(reason)) }
    finally { setBusy(false) }
  }
  const blocked = report ? [focusMode, 'real', 'offline_cli', 'mock'].filter((mode, index, list) => list.indexOf(mode) === index).find(mode => report.modes[mode] && !report.modes[mode].allowed) : ''
  const reason = blocked && report ? `${names[blocked] || blocked}：${report.modes[blocked]?.reason || '没有返回原因'}` : ''
  const actions = report && <>
    <button className="secondary-button" type="button" disabled={busy} onClick={() => void exportRequest()}>{busy ? '正在导出…' : '导出待签名参数'}</button>
    {copied && <p role="status">{copied}</p>}
    {report.export && <textarea className="access-export" readOnly aria-label="待签名参数" value={JSON.stringify(report.unsigned, null, 2)} />}
  </>
  if (embedded) return <div className="access-embedded" aria-label="项目执行权限">
    {!report && !error && <p>正在读取后端授权…</p>}
    {error && <p role="alert">{error}</p>}
    {report && <div className="access-detail">{detail(report)}{actions}</div>}
  </div>
  return <section className={`access-strip ${open ? 'open' : ''}`} aria-label="项目执行权限">
    <button type="button" className="access-toggle" aria-expanded={open} onClick={() => setOpen(value => !value)}>
      <span className="access-title">执行权限</span>
      {!report && !error && <span className="access-reason neutral">正在读取后端授权…</span>}
      {error && <span className="access-reason">{error}</span>}
      {report && (open ? order : order.filter(mode => mode === focusMode)).map(mode => {
        const item = report.modes[mode]
        const allowed = !!item?.allowed
        return <span key={mode} className={`access-chip ${allowed ? 'allowed' : 'blocked'}`}>{names[mode]} {allowed ? '允许' : '阻断'}</span>
      })}
      {reason && !open && <span className="access-reason" title={reason}>{reason}</span>}
      <span className="access-toggle-label">{open ? '收起' : '详情'}</span>
    </button>
    {open && report && <div className="access-detail">{detail(report)}{actions}</div>}
    {open && error && !report && <div className="access-detail"><p>{error}</p></div>}
  </section>
}
