import {useEffect, useRef, useState} from 'react'
import {ArrowUp, FolderOpen, Settings2, X} from 'lucide-react'
import {api, type Budget, type Defaults, type Preflight, type Project, type ProjectDetails, type Run, type Submission, type TaskRequest} from './bridge'
import {budgetLine, composeLead, executionChoices, gitBranch, gitStatusLabel, optionLabel, participantLine, verificationGap} from './composeModel'
import {ProjectInfoPanel} from './ProjectInfoPanel'
import {TaskSettingsPanel} from './TaskSettingsPanel'
import {activeStates} from './workflow'
import {CopyButton} from './records'

export function RunComposer({run,goal='',pending=false,onNew,enabled}:{run?:Run;goal?:string;pending?:boolean;onNew:()=>void;enabled:boolean}) {
  const active=pending||!!run&&activeStates.includes(run.task.state)
  const original=run?.task.goal||goal
  const [full,setFull]=useState(false)
  return <div className={`run-composer compose-editor ${full?'full-goal':''}`} data-run-composer>
    <div className="composer">
      <textarea aria-label="本次任务原始目标（只读）" readOnly value={original}/>
      <div className="composer-bottom"><span className="composer-readonly">{active?'任务运行中 · 只读目标':'本次任务已结束 · 原始目标'}</span><button className="inline-link" onClick={()=>setFull(!full)}>{full?'收起目标':'查看完整目标'}</button><CopyButton label="复制目标" text={original}/>{!active&&<button className="submit-button" disabled={!enabled} onClick={onNew}>在此项目新建任务<ArrowUp size={15}/></button>}</div>
    </div>
    <p className="composer-note muted">{active?'暂不支持运行中追加指令。可选择、复制完整目标。':'新任务会重新检查当前授权与剩余次数。历史使用过的签名不会自动复用。'}</p>
  </div>
}

type Pending = TaskRequest & {requestId: string}
function pending(): Pending | null { try { return JSON.parse(localStorage.getItem('v04-pending') || 'null') } catch { return null } }

function Composer({project, overview, enabled, draft, setDraft, ime, recordIme, onIme, onSubmit, onOpenManager, onRehearse}: {
  project: Project | undefined
  overview: ProjectDetails | null
  enabled: boolean
  draft: string
  setDraft: (value: string) => void
  ime: boolean
  recordIme: (name: string, data: string) => void
  onIme: (value: boolean) => void
  onSubmit: (value: Submission) => void
  onOpenManager: () => void
  onRehearse: () => void
}) {
  const [execution, setExecution] = useState('mock')
  const [defaults, setDefaults] = useState<Defaults | null>(null)
  const [details, setDetails] = useState<ProjectDetails | null>(null)
  const [title, setTitle] = useState('')
  const [template, setTemplate] = useState('verified')
  const [mode, setMode] = useState('controlled_write')
  const [budget, setBudget] = useState<Budget | null>(null)
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [validated, setValidated] = useState(false)
  const [report, setReport] = useState<Preflight | null>(null)
  const [confirmation, setConfirmation] = useState<TaskRequest | null>(null)
  const [panel, setPanel] = useState<'settings' | 'info' | null>(null)
  const inFlight = useRef(false)
  const retained = useRef<Pending | null>(pending())
  const epoch = useRef(0)
  const autoMode = useRef('')
  const userLocked = useRef(false)
  const loadedProject = useRef('')
  const seenPreflight = useRef(false)
  const gearRef = useRef<HTMLButtonElement>(null)
  const infoRef = useRef<HTMLButtonElement>(null)
  const fieldRef = useRef<HTMLTextAreaElement>(null)
  const draftRef = useRef(draft)
  const titleRef = useRef(title)
  draftRef.current = draft
  titleRef.current = title

  const choices = executionChoices(details?.execution.modes || overview?.execution.modes, details?.execution || overview?.execution)
  const choice = choices.find(item => item.id === execution)
  const git = gitStatusLabel(overview?.git?.summary)
  const branch = gitBranch(overview?.git?.branch)
  const checks = defaults?.verification || overview?.readout?.verification
  const gap = choice?.allowed ? verificationGap(checks) : ''
  const participants = defaults?.participants?.length
    ? defaults.participants
    : (Array.isArray(details?.config?.participants) ? details.config.participants as Defaults['participants'] : [])

  useEffect(() => {
    const current = ++epoch.current
    const projectChanged = loadedProject.current !== (project?.id || '')
    if (projectChanged) {
      loadedProject.current = project?.id || ''
      userLocked.current = false
      autoMode.current = ''
      seenPreflight.current = false
      setTitle('')
      setTemplate('verified')
      setMode('controlled_write')
      setBudget(null)
      setReport(null)
      setDetails(null)
      setPanel(null)
    }
    setDefaults(null)
    setValidated(false)
    setConfirmation(null)
    if (!(enabled && project?.registered)) {
      setDetails(null)
      setNotice(project?.configError || (project ? '此入口只开放历史读取' : ''))
      return () => { epoch.current++ }
    }
    api.project(project.id).then(async data => {
      if (current !== epoch.current) return
      setDetails(data)
      const available = executionChoices(data.execution.modes, data.execution)
      const selected = available.find(item => item.id === execution)
      if (selected?.allowed !== true) {
        if (!userLocked.current) {
          const next = available.find(item => item.allowed)
          if (next && next.id !== execution && autoMode.current !== `${project.id}:${next.id}`) {
            autoMode.current = `${project.id}:${next.id}`
            setExecution(next.id)
            return
          }
        }
        setBudget(null)
        setNotice(selected?.reason || data.execution.reason || '当前执行来源不可执行')
        return
      }
      const preset = await api.defaults(project.id, execution)
      if (current !== epoch.current) return
      setDefaults(preset)
      setBudget(retained.current?.projectId === project.id && retained.current.budget ? retained.current.budget : preset.budget)
      setNotice('')
    }).catch(error => { if (current === epoch.current) setNotice(String(error)) })
    return () => { epoch.current++ }
  }, [project?.id, project?.enabled, enabled, execution])

  const restorePending = () => {
    const saved = retained.current
    if (!saved || saved.projectId !== project?.id) return
    setTitle(saved.title)
    setTemplate(saved.templateId)
    setMode(saved.mode || 'controlled_write')
    userLocked.current = true
    setExecution(saved.executionMode || 'mock')
    if (saved.budget) setBudget(saved.budget)
    setDraft(saved.goal)
    setNotice('未确认内容已恢复。页面恢复不会自动提交；重试仍使用原来的请求 ID')
  }
  useEffect(restorePending, [project?.id])
  useEffect(() => { setValidated(false); setConfirmation(null); setReport(null) }, [draft, title, template, mode, project?.id, execution])

  const request = (): TaskRequest => ({
    projectId: project?.id || '',
    title: titleRef.current.trim() || draftRef.current.trim().split('\n')[0].slice(0, 80),
    goal: draftRef.current.trim(),
    templateId: execution === 'mock' ? template : 'collaborate',
    mode,
    executionMode: execution,
    ...(budget ? {budget} : {}),
  })

  useEffect(() => {
    if (!seenPreflight.current || !defaults || !draftRef.current.trim()) return
    const current = epoch.current
    let alive = true
    api.preflight(request()).then(checked => {
      if (!alive || current !== epoch.current) return
      setReport(checked)
      setValidated(!checked.items.some(item => item.status === 'BLOCKED'))
    }).catch(error => { if (alive && current === epoch.current) setNotice(String(error)) })
    return () => { alive = false }
  }, [execution, mode, template, budget, defaults])

  const validate = async (confirm = false) => {
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    const current = epoch.current
    try {
      const value = request()
      seenPreflight.current = true
      const checked = await api.preflight(value)
      if (current !== epoch.current) return
      setReport(checked)
      const blocked = checked.items.filter(item => item.status === 'BLOCKED')
      if (blocked.length) {
        setValidated(false)
        setNotice('')
        return
      }
      await api.validate(value)
      if (current !== epoch.current) return
      setValidated(true)
      setNotice('')
      if (confirm) {
        setPanel(null)
        setConfirmation(value)
      }
    } catch (error) {
      if (current === epoch.current) setNotice(String(error))
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }

  const submit = async () => {
    if (inFlight.current || ime || !defaults || !confirmation) return
    inFlight.current = true
    setBusy(true)
    try {
      const value = confirmation
      let saved = retained.current
      if (saved) {
        const {requestId: _requestId, ...payload} = saved
        const normalized = {projectId: payload.projectId, title: payload.title, goal: payload.goal, templateId: payload.templateId, mode: payload.mode || 'controlled_write', executionMode: payload.executionMode || 'mock', budget: payload.budget || defaults.budget}
        if (JSON.stringify(value) !== JSON.stringify(normalized)) throw new Error('未确认提交仍待恢复。请使用“恢复未确认内容”后重试，避免重复任务。')
      }
      if (!saved) {
        saved = {...value, requestId: crypto.randomUUID()}
        retained.current = saved
        localStorage.setItem('v04-pending', JSON.stringify(saved))
      }
      const result = await api.submit(saved)
      retained.current = null
      localStorage.removeItem('v04-pending')
      setConfirmation(null)
      setNotice('')
      onSubmit(result)
    } catch (error) {
      setNotice(String(error))
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }

  const submitReason = !project ? '先从左侧选择项目'
    : project.configError ? project.configError
    : !enabled ? '当前只读，不能发起新任务'
    : ime ? '正在组合输入，暂不能提交'
    : busy ? '正在处理…'
    : !draft.trim() ? '先写下任务目标'
    : !choices.length ? '正在读取执行权限'
    : choice?.allowed !== true ? (choice?.reason || '当前执行来源不可执行')
    : !defaults ? (notice || '正在读取执行配置')
    : ''
  const notes: {tone: 'bad' | 'warn' | 'ok'; text: string}[] = []
  const pushNote = (tone: 'bad' | 'warn' | 'ok', text: string) => {
    const clean = text.replace(/\s+/g, ' ').trim()
    if (!clean || notes.some(item => item.text === clean)) return
    notes.push({tone, text: clean.length > 240 ? `${clean.slice(0, 240)}…` : clean})
  }
  if (project?.configError) pushNote('bad', project.configError)
  else if (!enabled && project) pushNote('bad', '当前只读，不能发起新任务')
  if (choice && !choice.allowed) pushNote('bad', choice.reason)
  if (git.risk) pushNote('warn', git.risk)
  if (gap) pushNote('warn', gap)
  report?.items.filter(item => item.status === 'BLOCKED').forEach(item => pushNote('bad', `${item.title}：${item.detail}`))
  if (notice) pushNote(notice.includes('已恢复') || notice.includes('校验通过') ? 'ok' : 'bad', notice)
  if (validated && report && !report.items.some(item => item.status === 'BLOCKED')) pushNote('ok', '配置校验通过。预检没有启动模型。')

  const selectSource = (value: string) => {
    userLocked.current = true
    setExecution(value)
    setMode('controlled_write')
    setTemplate(current => current === 'review' ? 'verified' : current)
  }

  return <>
    <div className="compose-status" aria-label="项目状态">
      <span className={`compose-chip ${git.tone}`} title={overview?.git?.summary || ''}><i/>Git{branch ? ` · ${branch}` : ''} · {git.text}</span>
      <span className={`compose-chip ${choice ? (choice.allowed ? 'ok' : 'bad') : 'muted'}`} title={choice?.reason || ''}><i/>{choice ? `${choice.name} · ${choice.allowed ? '允许执行' : '不可执行'}` : '执行来源 · 正在读取'}</span>
    </div>
    <div className="compose-editor">
      <div className="composer">
        <textarea ref={fieldRef} aria-label="自然语言任务" maxLength={2000} value={draft} disabled={busy} placeholder="写下要完成的目标。提交前会再确认一次。" onChange={event => setDraft(event.target.value)} onCompositionStart={event => { onIme(true); recordIme('compositionstart', event.data) }} onCompositionUpdate={event => recordIme('compositionupdate', event.data)} onCompositionEnd={event => { onIme(false); recordIme('compositionend', event.data) }}/>
        <div className="composer-bottom">
          <div className="composer-tools">
            <div className="source-slot">
              <select className="composer-source" aria-label="执行来源" value={choices.some(item => item.id === execution) ? execution : ''} disabled={busy || !choices.length} onChange={event => selectSource(event.target.value)}>
                {!choices.length && <option value="">正在读取</option>}
                {choices.map(item => <option key={item.id} value={item.id} disabled={!item.allowed}>{optionLabel(item)}</option>)}
              </select>
            </div>
            <button type="button" ref={gearRef} className="tool-button" aria-label="任务设置" aria-expanded={panel === 'settings'} aria-controls="task-settings-panel" onClick={() => setPanel(panel === 'settings' ? null : 'settings')}><Settings2 size={16}/></button>
            <button type="button" ref={infoRef} className="tool-button labeled" aria-label="项目信息" aria-expanded={panel === 'info'} aria-controls="project-info-panel" onClick={() => setPanel(panel === 'info' ? null : 'info')}><FolderOpen size={15}/><span>项目</span></button>
          </div>
          <button type="button" className="submit-button" disabled={!!submitReason} title={submitReason || undefined} aria-describedby={notes.length ? 'composer-notes' : undefined} onClick={() => void validate(true)}><span>{busy ? '正在处理…' : '开始协作'}</span><ArrowUp size={16}/></button>
        </div>
      </div>
      {notes.length > 0 && <div className="composer-notes" id="composer-notes">{notes.slice(0, 3).map(item => <p key={item.text} className={`composer-note ${item.tone}`} role="status">{item.text}</p>)}</div>}
    </div>
    <TaskSettingsPanel open={panel === 'settings'} anchorRef={gearRef} onClose={() => setPanel(null)} execution={execution} title={title} setTitle={setTitle} mode={mode} setMode={setMode} template={template} setTemplate={setTemplate} defaults={defaults} budget={budget} setBudget={value => { setBudget(value); setReport(null) }} readout={details?.readout || overview?.readout} participants={participants} busy={busy} validated={validated} report={report} canValidate={!!project && !!draft.trim()} onValidate={() => void validate(false)} onRestore={restorePending} canRestore={retained.current?.projectId === project?.id}/>
    <ProjectInfoPanel open={panel === 'info'} anchorRef={infoRef} onClose={() => setPanel(null)} project={project} overview={details || overview} enabled={enabled} onOpenManager={() => { setPanel(null); onOpenManager() }} onRehearse={() => { setPanel(null); onRehearse() }}/>
    {confirmation && <div className="modal-backdrop"><div className="modal" role="dialog" aria-label={execution === 'mock' ? '确认模拟任务' : execution === 'offline_cli' ? '确认离线模拟 CLI 任务' : '确认真实 CLI 任务'}><header><h2>确认提交{execution === 'mock' ? '模拟' : execution === 'offline_cli' ? '离线模拟 CLI' : '真实 CLI'}任务</h2><button type="button" aria-label="关闭提交确认" disabled={busy} onClick={() => setConfirmation(null)}><X size={18}/></button></header><div className="project-form"><strong>{confirmation.title}</strong><p>{confirmation.goal}</p><p>{execution !== 'mock' ? 'Codex 开发后立即做可信测试；失败时 Cursor 先审查，再由 Codex 修复并复测，最后 Cursor 验收。审查意见本身不是 verified。' : mode === 'read_only' ? '只读审查，不做功能验收。' : '固定 Mock 流程会在隔离样本里完成开发、审查、可信测试和验收。'}</p><p>预算：{budgetLine(confirmation.budget, execution)}</p><p>项目：{project?.name} · 执行来源：{choice?.name || execution}</p><p>Agent：{participants.map(participantLine).join(' → ') || '未配置'}</p><p>可信验证：{defaults?.verification.map(item => item.label).join(' / ') || '未读取'}</p>{git.risk && <p>{git.risk}</p>}<p>{execution === 'mock' ? '真实 Codex/Cursor 调用已由 Python 阻断。' : execution === 'offline_cli' ? '受控替身验证协议；真实模型调用为 0。' : '提交与每次 CLI 启动均重新校验签名、路径、程序摘要和预算。'}</p><p>预检未启动模型。真实模型调用 0。</p>{notice && <div role="status">{notice}</div>}<button type="button" className="secondary-button" disabled={busy || ime} onClick={() => void submit()}>{busy ? '正在提交…' : '确认提交'}</button></div></div></div>}
  </>
}

export function ComposePage({project, overview, enabled, draft, setDraft, ime, recordIme, onIme, onSubmit, onOpenManager, onRehearse}: {
  project: Project | undefined
  overview: ProjectDetails | null
  enabled: boolean
  draft: string
  setDraft: (value: string) => void
  ime: boolean
  recordIme: (name: string, data: string) => void
  onIme: (value: boolean) => void
  onSubmit: (value: Submission) => void
  onOpenManager: () => void
  onRehearse: () => void
}) {
  return <section className="compose-page" data-compose-page="true">
    <div className="compose-column">
      <header className="compose-intro">
        <h1>{project?.name || '选择已登记项目'}</h1>
        <p>{composeLead(overview?.config?.description)}</p>
      </header>
      <div className="compose-dock">
        <Composer project={project} overview={overview} enabled={enabled} draft={draft} setDraft={setDraft} ime={ime} recordIme={recordIme} onIme={onIme} onSubmit={onSubmit} onOpenManager={onOpenManager} onRehearse={onRehearse}/>
      </div>
    </div>
  </section>
}
