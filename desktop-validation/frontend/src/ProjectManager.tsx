import {useEffect, useRef, useState} from 'react'
import {X} from 'lucide-react'
import {api, type Project, type ProjectDetails, type WorkspaceReport} from './bridge'
import {configRecovery} from './present'
import {AccessPanel} from './AccessPanel'

const blank = () => ({
  project_id: '', name: '', workspace: '', allowed_roots: [] as string[], enabled: true, description: '',
  participants: [
    {id: 'developer', executor: 'codex', role: 'developer'},
    {id: 'reviewer', executor: 'cursor', role: 'reviewer'},
    {id: 'acceptor', executor: 'codex', role: 'acceptor'},
  ],
  verification: [],
  worktree: {enabled: false, base_ref: 'HEAD', root: '', path: ''},
  max_model_calls: 8, max_rounds: 6, max_repair_rounds: 1, max_run_seconds: 900, timeout_seconds: 120,
  concurrency: {max_write_tasks: 1, allow_shared_context: false},
  resource_locks: [],
})

export function ProjectManager({projects, initial, onClose, onChanged}: {projects: Project[]; initial: string; onClose: () => void; onChanged: (id: string) => void | Promise<void>}) {
  const [id, setId] = useState(initial)
  const [details, setDetails] = useState<ProjectDetails | null>(null)
  const [config, setConfig] = useState<Record<string, unknown>>(blank())
  const [advanced, setAdvanced] = useState('')
  const [preview, setPreview] = useState<WorkspaceReport | null>(null)
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [armRetire, setArmRetire] = useState(false)
  const epoch = useRef(0)
  const locked = useRef(false)
  const loadedId = useRef<string | null>(null)
  const listed = projects.find(item => item.id === id)

  useEffect(() => {
    if (loadedId.current === id) return
    const current = ++epoch.current
    setDetails(null)
    setPreview(null)
    setArmRetire(false)
    setNotice('')
    if (!id) {
      loadedId.current = ''
      const value = blank()
      setConfig(value)
      setAdvanced(JSON.stringify(value, null, 2))
      setBusy(false)
      return
    }
    setBusy(true)
    api.project(id).then(data => {
      if (epoch.current !== current) return
      loadedId.current = id
      setDetails(data)
      setConfig(data.config)
      setAdvanced(JSON.stringify(data.config, null, 2))
    }).catch(error => {
      if (epoch.current === current) setNotice(String(error))
    }).finally(() => {
      if (epoch.current === current) setBusy(false)
    })
    return () => { epoch.current++ }
  }, [id])

  const field = (key: string, value: unknown) => {
    const updated = {...config, [key]: value}
    setConfig(updated)
    setAdvanced(JSON.stringify(updated, null, 2))
    setPreview(null)
  }
  const act = async (action: () => Promise<ProjectDetails | {defaultProjectId: string} | {retired: boolean; projectId: string}>, message: string) => {
    if (locked.current) return
    locked.current = true
    setBusy(true)
    const current = epoch.current
    try {
      const data = await action()
      if (current !== epoch.current) return
      if ('retired' in data) {
        loadedId.current = ''
        setId('')
        setDetails(null)
        await onChanged('')
      } else if ('config' in data) {
        loadedId.current = String(data.config.project_id)
        setDetails(data)
        setConfig(data.config)
        setAdvanced(JSON.stringify(data.config, null, 2))
        setId(String(data.config.project_id))
        await onChanged(String(data.config.project_id))
      } else {
        await onChanged(data.defaultProjectId)
      }
      setArmRetire(false)
      setNotice(message)
    } catch (error) {
      if (current === epoch.current) setNotice(`${String(error)} ${configRecovery(String(error))}`)
    } finally {
      locked.current = false
      setBusy(false)
    }
  }
  const checkPath = async () => {
    setBusy(true)
    setPreview(null)
    try {
      const report = await api.inspectWorkspace(String(config.workspace || ''), id, !id)
      setPreview(report)
      setNotice(report.executionGranted ? '路径检查异常：后端不应授予执行权限' : report.accepted ? '路径可以登记。登记本身不授予执行权限。' : `路径检查未通过。 ${configRecovery(report.errors.join(' '))}`)
    } catch (error) {
      setNotice(`${String(error)} ${configRecovery(String(error))}`)
    } finally {
      setBusy(false)
    }
  }
  const budget = details?.readout?.budget
  const revision = details?.revision || listed?.revision || ''
  const participants = Array.isArray(config.participants) ? config.participants as {id: string; executor: string; role: string}[] : []
  const verification = Array.isArray(config.verification) ? config.verification as Record<string, unknown>[] : []
  const worktree = config.worktree && typeof config.worktree === 'object' ? config.worktree as Record<string, unknown> : {enabled: false, base_ref: 'HEAD', root: '', path: ''}
  const roleHelp: Record<string, string> = {developer: '编写代码。只有这一角色的修改会进入可信测试。', reviewer: '审查实现或失败证据。审查意见不是验收。', acceptor: '给出 accept 或 reject。accept 不能代替协调器验证。', diagnoser: '只读诊断，不作为验收。'}
  const setParticipants = (next: {id: string; executor: string; role: string}[]) => field('participants', next)
  const setWorktree = (key: string, value: unknown) => field('worktree', {...worktree, [key]: value})
  const setVerification = (index: number, patch: Record<string, unknown>) => field('verification', verification.map((item, itemIndex) => itemIndex === index ? {...item, ...patch} : item))
  const prepareSave = async () => {
    let parsed: Record<string, unknown>
    try { parsed = JSON.parse(advanced) as Record<string, unknown> }
    catch (error) { setNotice(`${String(error)} ${configRecovery(String(error))}`); return }
    const problems: string[] = []
    if (!Array.isArray(parsed.participants) || !parsed.participants.length) problems.push('至少需要一名参与者。')
    const tree = parsed.worktree as {enabled?: boolean; root?: string} | undefined
    if (tree?.enabled && !String(tree.root || '').trim()) problems.push('启用 Worktree 时必须填写根目录。Worktree 不是操作系统沙箱。')
    for (const key of ['max_model_calls', 'max_rounds', 'max_repair_rounds', 'max_run_seconds', 'timeout_seconds']) {
      const value = parsed[key]
      if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) problems.push(`${key} 需要是非负数字。`)
    }
    const workspace = String(parsed.workspace || '')
    if (!workspace) problems.push('需要工作区路径。')
    else {
      try {
        const report = await api.inspectWorkspace(workspace, String(parsed.project_id || ''), !id)
        setPreview(report)
        problems.push(...report.errors)
      } catch (error) { problems.push(String(error)) }
    }
    if (problems.length) { setNotice(`${problems.join(' ')} ${configRecovery(problems.join(' '))}`); return }
    await act(() => api.saveProject(parsed, !id, details?.revision), '项目配置已保存。这没有授予执行权限。')
  }

  return <div className="modal-backdrop"><div className="modal project-manager" role="dialog" aria-label="项目管理">
    <header><div><h2>项目管理</h2><p>登记与配置 · 执行权限由 Python 后端判断</p></div><button type="button" aria-label="关闭项目管理" onClick={onClose}><X size={18}/></button></header>
    <div className="project-form">
      <label>已登记项目<select aria-label="管理项目" value={id} disabled={busy} onChange={event => setId(event.target.value)}><option value="">添加项目</option>{projects.filter(item => item.registered).map(item => <option key={item.id} value={item.id}>{item.name}{item.enabled ? '' : ' · 已禁用'}{item.isDefault ? ' · 默认' : ''}{item.configError ? ' · 配置错误' : ''}</option>)}</select></label>
      <div className="task-settings-grid">
        {[['project_id', '项目 ID'], ['name', '项目名称'], ['workspace', '工作区路径']].map(([key, label]) => <label key={key}>{label}<input aria-label={label} value={String(config[key] || '')} disabled={busy || (key === 'project_id' && !!id)} onChange={event => field(key, event.target.value)}/></label>)}
        <label>允许根目录（每行一个）<textarea aria-label="允许根目录" value={(config.allowed_roots as string[] || []).join('\n')} disabled={busy} onChange={event => field('allowed_roots', event.target.value.split('\n').filter(Boolean))}/></label>
        <label>项目说明<input aria-label="项目说明" value={String(config.description || '')} disabled={busy} onChange={event => field('description', event.target.value)}/></label>
      </div>
      <div className="project-actions"><button className="secondary-button" type="button" disabled={busy || !String(config.workspace || '')} onClick={() => void checkPath()}>检查路径与 Git</button></div>
      {preview && <div className="project-state" data-path-preview="true"><p>检查结果：{preview.accepted ? '路径可用' : '不能登记'}</p><p className="preserve-text">规范化路径：{preview.normalized || '无'}</p><p>Git：{preview.git.summary || (preview.git.available ? preview.git.branch : '未检测到 Git 仓库')}{preview.git.branch ? ` · ${preview.git.branch}` : ''}</p>{preview.git.changes && <pre>{preview.git.changes}</pre>}{preview.errors.map(item => <p role="alert" key={item}>{item}</p>)}{preview.warnings.map(item => <p key={item}>{item}</p>)}<p>执行授权：{preview.executionGranted ? '异常' : '未授予'}</p></div>}
      {listed?.configError && <p role="alert">{listed.configError}</p>}
      {details && <div className="project-state" data-project-posture={details.posture?.states.join(' ') || ''}>
        <div className="posture-row">{(details.posture?.label || '').split(' · ').filter(Boolean).map(state => <span className="posture-chip" key={state}>{state}</span>)}</div>
        <p>状态：{details.posture?.label}</p>
        <p>下一步：{details.posture?.nextStep}</p>
        <p className="preserve-text">实际工作区：{String(details.config.workspace)}</p>
        <p>Git：{details.git.summary || (details.git.available ? details.git.branch : '未检测到 Git 仓库')}{details.git.error ? ` · ${details.git.error}` : ''}</p>
        {details.git.changes && <pre>{details.git.changes}</pre>}
        <p>运行许可：{details.execution.reason}</p>
        {details.readout && <div className="project-readout">
          <p>可信测试：{details.readout.verification.map(item => item.summary).join(' ') || '未配置'}</p>
          <p>Worktree：{String(details.readout.worktree.summary || '')}{details.readout.worktree.root ? ` 根 ${details.readout.worktree.root}` : ''}</p>
          {budget && <p>预算上限：模型调用 {budget.modelCalls} · 轮次 {budget.rounds} · 修复 {budget.repairs} · 运行 {budget.runSeconds} 秒 · 单步 {budget.stepSeconds} 秒</p>}
        </div>}
      </div>}
      {id && <AccessPanel projectId={id} enabled={!!details} focusMode="real"/>}
      <section className="project-setup" aria-label="协作配置">
        <h3>参与者与职责</h3>
        {participants.map((item, index) => <div className="task-settings-grid" key={`${item.id}-${index}`}>
          <label>参与者 ID<input aria-label={`参与者 ${index + 1} ID`} value={item.id} disabled={busy} onChange={event => setParticipants(participants.map((entry, entryIndex) => entryIndex === index ? {...entry, id: event.target.value} : entry))}/></label>
          <label>执行器<select aria-label={`参与者 ${index + 1} 执行器`} value={item.executor} disabled={busy} onChange={event => setParticipants(participants.map((entry, entryIndex) => entryIndex === index ? {...entry, executor: event.target.value} : entry))}><option value="mock">mock</option><option value="codex">codex</option><option value="cursor">cursor</option></select></label>
          <label>职责<select aria-label={`参与者 ${index + 1} 职责`} value={item.role} disabled={busy} onChange={event => setParticipants(participants.map((entry, entryIndex) => entryIndex === index ? {...entry, role: event.target.value} : entry))}><option value="developer">developer · 开发</option><option value="reviewer">reviewer · 审查</option><option value="acceptor">acceptor · 验收</option><option value="diagnoser">diagnoser · 诊断</option></select></label>
          <p className="microcopy">{roleHelp[item.role] || '未知职责。保存时由 Python 校验。'}</p>
        </div>)}
        <div className="project-actions">
          <button className="secondary-button" type="button" disabled={busy || participants.length >= 6} onClick={() => setParticipants([...participants, {id: `agent-${participants.length + 1}`, executor: 'mock', role: 'reviewer'}])}>添加参与者</button>
          <button className="secondary-button" type="button" disabled={busy || participants.length < 2} onClick={() => setParticipants(participants.slice(0, -1))}>移除最后一位</button>
        </div>
        <h3>可信测试与允许改动</h3>
        {verification.length === 0 && <p>未配置可信测试。开发任务没有独立验证时不能记为 verified。</p>}
        {verification.map((item, index) => <div className="project-state" key={index}>
          <p>{item.type === 'command' ? '可信命令' : item.type === 'allowed_changes' ? '允许改动' : String(item.type || '检查')}</p>
          {item.type === 'command' && <p className="preserve-text">命令：{Array.isArray(item.argv) ? item.argv.map(String).join(' ') : '未记录'} · 摘要 {String(item.sha256 || '未记录').slice(0, 12)}</p>}
          {item.type === 'allowed_changes' && <label>允许改动路径（每行一个）<textarea aria-label={`允许改动 ${index + 1}`} value={Array.isArray(item.allowed_paths) ? item.allowed_paths.map(String).join('\n') : ''} disabled={busy} onChange={event => setVerification(index, {allowed_paths: event.target.value.split('\n').filter(Boolean)})}/></label>}
        </div>)}
        <p className="microcopy">这里只展示和调整已有检查。保存不会运行可信命令，也不能改写执行白名单。</p>
        <h3>Worktree 与预算</h3>
        <label className="check-line"><input type="checkbox" aria-label="启用 Worktree" checked={Boolean(worktree.enabled)} disabled={busy} onChange={event => setWorktree('enabled', event.target.checked)}/>启用 Worktree 隔离</label>
        <div className="task-settings-grid">
          <label>基线<input aria-label="Worktree 基线" value={String(worktree.base_ref || '')} disabled={busy} onChange={event => setWorktree('base_ref', event.target.value)}/></label>
          <label>根目录<input aria-label="Worktree 根目录" value={String(worktree.root || '')} disabled={busy} onChange={event => setWorktree('root', event.target.value)}/></label>
          <label>路径<input aria-label="Worktree 路径" value={String(worktree.path || '')} disabled={busy} onChange={event => setWorktree('path', event.target.value)}/></label>
        </div>
        <p className="microcopy">Worktree 只是 Git 工作副本。它不是操作系统沙箱，也不会扩大允许写入的路径。</p>
        <div className="task-settings-grid">
          {[['max_model_calls', 'CLI 启动上限'], ['max_rounds', '最大轮次'], ['max_repair_rounds', '修复次数'], ['max_run_seconds', '总时限（秒）'], ['timeout_seconds', '单步时限（秒）']].map(([key, label]) => <label key={key}>{label}<input aria-label={label} type="number" value={Number(config[key] ?? 0)} disabled={busy} onChange={event => field(key, Number(event.target.value))}/></label>)}
        </div>
      </section>
      <details><summary>完整配置（Agent、可信测试、预算、Worktree、锁）</summary><textarea className="config-editor" aria-label="完整项目配置" disabled={busy} value={advanced} onChange={event => setAdvanced(event.target.value)}/><button className="secondary-button" disabled={busy} onClick={() => { try { const value = JSON.parse(advanced); if (!value || Array.isArray(value) || typeof value !== 'object') throw Error('根节点必须是对象'); setConfig(value); setNotice('配置已载入表单；保存时由后端完整验证') } catch (error) { setNotice(String(error)) } }}>载入配置到表单</button></details>
      <p className="microcopy">项目配置不授予执行权限。修改已批准的 Mock 预设会失去执行批准。界面不能改写白名单，也不会运行验证命令或创建 Worktree。移除登记只删除这一份 JSON。</p>
      <div className="project-actions">
        <button className="secondary-button" disabled={busy || (!!id && !details)} onClick={() => void prepareSave()}>{id ? '检查并保存项目配置' : '检查并添加项目'}</button>
        <button className="secondary-button" data-create-rehearsal="true" disabled={busy} onClick={() => void act(() => api.createRehearsal(false), '已准备安全演练。它只存在于应用样本目录。')}>创建安全演练</button>
        <button className="secondary-button" disabled={busy} onClick={() => void act(() => api.createRehearsal(true), '已按内置模板重置演练。其他仓库没有获得权限。')}>重置演练模板</button>
        {details && <>
          <button className="secondary-button" disabled={busy} onClick={() => void act(() => api.enableProject(id, !details.config.enabled, details.revision), details.config.enabled ? '项目已禁用。历史、源码和 Worktree 都还在。' : '项目已启用。这仍然不授予新的执行权限。')}>{details.config.enabled ? '禁用项目' : '启用项目'}</button>
          <button className="secondary-button" disabled={busy || !details.config.enabled} onClick={() => void act(() => api.defaultProject(id), '默认项目已保存')}>设为默认项目</button>
        </>}
        {revision && <button className="secondary-button" disabled={busy} onClick={() => setArmRetire(true)}>移除登记</button>}
        {armRetire && revision && <button className="secondary-button" disabled={busy} onClick={() => void act(() => api.retireProject(id, revision), '登记已移除。源码、历史和 Worktree 都没有删除。')}>确认只移除登记文件</button>}
      </div>
      <p role="status">{busy ? '正在处理…' : notice}</p>
    </div>
  </div></div>
}
