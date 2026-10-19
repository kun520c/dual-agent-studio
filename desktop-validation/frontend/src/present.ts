/** Display helpers. Untrusted model text stays a string; it is never treated as HTML. */

export type ReadingMode = 'concise' | 'standard' | 'detailed'

export interface Narrative {
  summary: string
  details: string
  risks: string[]
  decision: string
  files: string[]
  body: string
}

export interface TestCounts {
  run: number
  failures: number
  errors: number
  skipped: number
}

export interface CommandView {
  name: string
  ok: boolean | null
  exitCode: string
  duration: string
  tests: TestCounts | null
  error: string
  inconsistent: boolean
}

export interface TrustedView {
  state: 'pass' | 'fail' | 'missing' | 'stale'
  headline: string
  detail: string
  commands: CommandView[]
  scope: string
}

const ROLES: Record<string, string> = {developer: '开发者', reviewer: '审查者', diagnoser: '诊断者', acceptor: '验收者'}
const PHASES: Record<string, string> = {queued: '排队', running: 'Codex 开发', verifying: '可信测试', reviewing: 'Cursor 审查或验收', repairing: 'Codex 修复', verified: '已验收', failed: '失败', cancelled: '已取消'}
const DECISIONS: Record<string, string> = {continue: '继续', accept: '接受', repair: '修复', reject: '退回'}
const EVENTS: Record<string, string> = {run_started: '运行开始', step_started: '轮次开始', step_finished: '轮次结束', run_finished: '运行结束', status: '状态记录', verification_started: '协调器验证开始', verification_finished: '协调器验证结束', verification: '协调器验证记录'}

export function roleLabel(role: string) { return ROLES[role] || role || '历史消息' }
export function phaseLabel(state?: string) { return PHASES[state || ''] || state || '未知阶段' }
export function decisionLabel(value: string) { return DECISIONS[value] || value }
export function eventLabel(name: string) { return EVENTS[name] || name || '事件记录' }

export function sourceLabel(task?: {executionMode?: string; callKind?: string; mode?: string}) {
  const mode = task?.executionMode || ''
  if (mode === 'offline_cli' || task?.callKind === 'offline_cli') return '离线模拟 CLI · 真实模型调用 0'
  if (mode === 'real') return '真实 CLI · 独立签名授权'
  if (mode === 'mock' || task?.callKind === 'mock' || task?.mode === '模拟') return 'Mock · 真实模型调用 0'
  if (task?.callKind === 'model') return '真实 CLI · 历史模型记录'
  return task?.mode || '执行来源未记录'
}

export function formatDuration(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return ''
  const total = Math.round(value)
  if (total < 60) return value < 10 ? `${value.toFixed(1)} 秒` : `${total} 秒`
  const minutes = Math.floor(total / 60)
  const seconds = total % 60
  return seconds ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分`
}

export function formatClock(value: unknown) {
  return String(value || '').replace('T', ' ').slice(0, 19)
}

function textField(value: unknown) {
  return typeof value === 'string' ? value.replace(/\r\n/g, '\n').trim() : ''
}

function stringList(value: unknown) {
  if (!Array.isArray(value)) return []
  return value.filter((item): item is string => typeof item === 'string' && item.trim() !== '').map(item => item.trim())
}

function tryObject(raw: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
    return parsed as Record<string, unknown>
  } catch {
    return null
  }
}

function parsePayload(raw: string): {value: Record<string, unknown>; prefix: string} | null {
  const trimmed = raw.trim()
  if (!trimmed) return null
  const direct = tryObject(trimmed)
  if (direct) return {value: direct, prefix: ''}
  if (trimmed.length > 200000) return null
  const end = trimmed.lastIndexOf('}')
  if (end < 1) return null
  let attempts = 0
  for (let start = trimmed.lastIndexOf('{', end); start >= 0 && attempts < 12; start = trimmed.lastIndexOf('{', start - 1), attempts++) {
    const parsed = tryObject(trimmed.slice(start, end + 1))
    if (parsed && (textField(parsed.summary) || textField(parsed.details) || textField(parsed.message))) return {value: parsed, prefix: trimmed.slice(0, start).trim()}
    if (start === 0) break
  }
  return null
}

export function parseAgentContent(raw: unknown): Narrative {
  const text = typeof raw === 'string' ? raw : ''
  const parsed = parsePayload(text)
  if (!parsed) {
    const summary = text.trim()
    return {summary, details: '', risks: [], decision: '', files: [], body: summary}
  }
  const summary = textField(parsed.value.summary) || textField(parsed.value.message)
  const details = textField(parsed.value.details) || (summary ? '' : textField(parsed.value.content))
  const risks = stringList(parsed.value.risks)
  const decision = textField(parsed.value.decision)
  const files = stringList(parsed.value.files_touched)
  const lead = parsed.prefix
  const shownSummary = summary || lead
  const shownDetails = [summary && lead ? lead : '', details].filter(Boolean).join('\n\n')
  const riskLine = risks.length ? `风险：${risks.join('；')}` : ''
  const body = [shownSummary, shownDetails, riskLine].filter(Boolean).join('\n\n')
  return {summary: shownSummary, details: shownDetails, risks, decision, files, body}
}

export function parseTestCounts(stdout: unknown): TestCounts | null {
  if (typeof stdout !== 'string' || !stdout.includes('Tests run:')) return null
  const pattern = /Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)/g
  let found: TestCounts | null = null
  let match: RegExpExecArray | null
  while ((match = pattern.exec(stdout))) {
    found = {run: Number(match[1]), failures: Number(match[2]), errors: Number(match[3]), skipped: Number(match[4])}
  }
  return found
}

function baseName(value: string) {
  const parts = value.split(/[/\\]/).filter(Boolean)
  return parts[parts.length - 1] || ''
}

export function commandName(record: Record<string, unknown>) {
  if (record.type === 'allowed_changes') return '改动范围'
  const script = baseName(textField(record.trusted_script))
  if (script) return script
  const argv = Array.isArray(record.argv) ? record.argv : []
  for (const item of argv) {
    const name = baseName(typeof item === 'string' ? item : '')
    if (/\.(py|sh|bat|cmd|ps1)$/i.test(name)) return name
  }
  return record.type === 'command' ? '可信命令' : textField(record.type) || '验证'
}

function currentRecords(records: Record<string, unknown>[]) {
  return records.filter(record => record.superseded !== true && record.evidence_invalidated !== true)
}

function scopeText(record: Record<string, unknown> | undefined) {
  if (!record) return ''
  const changed = Array.isArray(record.changed) ? record.changed.length : 0
  const illegal = Array.isArray(record.illegal) ? record.illegal.length : 0
  const missing = Array.isArray(record.missing_required_changes) ? record.missing_required_changes.length : 0
  if (record.ok === true && illegal === 0 && missing === 0) return `改动范围通过 · ${changed} 个文件`
  if (illegal > 0) return `超出允许范围 · ${illegal} 个文件`
  if (missing > 0) return `缺少要求的改动 · ${missing} 个文件`
  return record.ok === false ? '改动范围未通过' : '改动范围未记录'
}

export function trustedVerification(records: Record<string, unknown>[]): TrustedView {
  if (!records.length) return {state: 'missing', headline: '未保存可信验证', detail: '没有 verification.json 记录。Agent 自述不能代替这项结果。', commands: [], scope: ''}
  const current = currentRecords(records)
  if (!current.length) return {state: 'stale', headline: '验证记录已失效', detail: '保存的检查均已替代或失效。', commands: [], scope: ''}
  const commands = current.filter(record => record.type === 'command').map(record => {
    const tests = parseTestCounts(record.stdout)
    const ok = record.ok === true ? true : record.ok === false ? false : null
    return {
      name: commandName(record),
      ok,
      exitCode: record.exit_code === undefined || record.exit_code === null ? '未记录' : String(record.exit_code),
      duration: formatDuration(record.duration_seconds),
      tests,
      error: textField(record.error),
      inconsistent: ok === true && !!tests && (tests.failures > 0 || tests.errors > 0),
    }
  })
  const scope = scopeText([...current].reverse().find(record => record.type === 'allowed_changes'))
  const failed = current.some(record => record.ok !== true)
  const state = failed ? 'fail' : 'pass'
  const latest = commands[commands.length - 1]
  const tests = latest?.tests
  const testText = tests ? `${tests.run} 项测试，失败 ${tests.failures}，错误 ${tests.errors}` : ''
  const headline = state === 'pass'
    ? `验证通过${testText ? ` · ${tests?.run} 项` : ''}`
    : `验证未通过${latest ? ` · exit ${latest.exitCode}` : ''}`
  const detail = [latest ? `${latest.name} · exit ${latest.exitCode}` : '', latest?.duration, testText, scope, latest?.inconsistent ? '输出里的失败数与通过记录不一致' : ''].filter(Boolean).join(' · ')
  return {state, headline, detail, commands, scope}
}

export function diffStats(files: {added: number; removed: number}[] | null | undefined) {
  if (!files) return null
  return {
    count: files.length,
    added: files.reduce((sum, file) => sum + (Number(file.added) || 0), 0),
    removed: files.reduce((sum, file) => sum + (Number(file.removed) || 0), 0),
  }
}

export function traceLine(messages: Record<string, unknown>[]) {
  return messages.map((message, index) => {
    const role = roleLabel(String(message.role || '步骤'))
    const decision = textField(message.decision)
    const problem = textField(message.error_kind)
    return `第 ${String(message.round_index ?? index + 1)} 轮 ${role}${decision ? ` · ${decisionLabel(decision)}` : ''}${problem && problem !== 'ok' ? ` · ${problem}` : ''}`
  }).join('  ·  ')
}

const ACTIVE_STATES = new Set(['queued', 'running', 'reviewing', 'verifying', 'repairing'])
const ALLOWANCE_NAMES: Record<string, string> = {mock: 'Mock', offline_cli: '离线模拟 CLI', real: '真实 CLI'}

export function isHistoricalTask(state?: string) {
  return !ACTIVE_STATES.has(state || '')
}

const POSTURE_LABELS: Record<string, string> = {
  registered: '已登记',
  disabled: '已禁用',
  read_only: '只读可用',
  mock_executable: 'Mock 可执行',
  real_authorized: '真实 CLI 已授权',
  offline_authorized: '离线 CLI 已授权',
}

export function postureSummary(states: string[] | undefined) {
  return (states || []).map(state => POSTURE_LABELS[state] || state).join(' · ')
}

export function projectAllowance(modes: Record<string, {allowed?: boolean}> | undefined, writable: boolean, registered: boolean) {
  if (!registered || !writable) return '只读历史，不能发起新任务'
  const allowed = ['mock', 'offline_cli', 'real'].filter(mode => modes?.[mode]?.allowed)
  if (!allowed.length) return '当前不能发起新任务'
  return `可发起 ${allowed.map(mode => ALLOWANCE_NAMES[mode] || mode).join('、')}`
}

export function callBudget(kind: string | undefined, usage: Record<string, number>, cap: unknown) {
  const real = usage.real_model_calls || 0
  const used = kind === 'offline_cli' ? usage.offline_cli_calls || 0 : kind === 'mock' ? usage.mock_calls || 0 : real
  const label = kind === 'offline_cli' ? '离线 CLI 启动' : kind === 'mock' ? 'Mock 调用' : '真实 CLI 启动'
  const limit = typeof cap === 'number' && Number.isFinite(cap) ? cap : null
  return {label, used, limit, real}
}

export interface CollabPhase {
  label: string
  detail: string
  tone: 'ok' | 'bad' | 'neutral'
}

export interface RepairStory {
  triggered: boolean | null
  headline: string
  failureReason: string
  exitCode: string
  cases: string
  feedback: string
  files: string[]
  beforeDiff: string
  afterDiff: string
  retest: string
  verified: boolean
  inconsistencies: string[]
  phases: CollabPhase[]
}

const STOP_TEXT: Record<string, string> = {
  verified: '协调器判定 verified',
  unverified: '可信验证未通过',
  max_repair: '达到最大修复次数，未验收',
  flow_complete: '流程结束，不是验收通过',
  rejected: '验收者未接受',
  cancelled: '已取消',
  budget: '预算耗尽',
  repeated_failure: '修复后失败与上一轮相同',
  baseline_clean: '预置测试已通过，未调用模型，也不是 verified',
}

function agentName(executor: string) {
  if (executor === 'codex') return 'Codex'
  if (executor === 'cursor') return 'Cursor'
  if (executor === 'mock') return 'Mock'
  return ''
}

function executorOf(message: Record<string, unknown> | undefined, agent: string) {
  const fromMessage = agentName(textField(message?.executor))
  if (fromMessage) return fromMessage
  return agentName(agent)
}

function messageFor(messages: Record<string, unknown>[], round: unknown, role: string) {
  return messages.find(item => item.role === role && String(item.round_index ?? '') === String(round ?? ''))
}

function commandRecords(records: Record<string, unknown>[]) {
  return records.filter(item => item && item.type === 'command')
}

function sameAttempt(left: Record<string, unknown>, right: Record<string, unknown>) {
  return left.baseline === true ? right.baseline === true : right.baseline !== true && String(left.attempt ?? '') === String(right.attempt ?? '')
}

function takeGroup(commands: Record<string, unknown>[], cursor: number) {
  if (cursor >= commands.length) return {batch: [] as Record<string, unknown>[], cursor}
  const first = commands[cursor]
  let next = cursor + 1
  while (next < commands.length && sameAttempt(first, commands[next])) next += 1
  return {batch: commands.slice(cursor, next), cursor: next}
}

function testLabel(batch: Record<string, unknown>[]) {
  const first = batch[0] || {}
  const attempt = Number(first.attempt)
  if (first.baseline === true || attempt === 1) return '第一次可信测试'
  if (attempt === 2) return '第二次可信测试'
  if (Number.isFinite(attempt) && attempt > 2) return `第 ${attempt} 次可信测试`
  return '可信测试'
}

function commandDetail(batch: Record<string, unknown>[]) {
  return batch.map(item => {
    const tests = parseTestCounts(item.stdout)
    const testsText = tests ? `${tests.run} 项，失败 ${tests.failures}，错误 ${tests.errors}` : ''
    const exitCode = item.exit_code === undefined || item.exit_code === null ? '未记录' : String(item.exit_code)
    return [commandName(item), `exit ${exitCode}`, testsText, textField(item.error)].filter(Boolean).join(' · ')
  }).join('；')
}

function failureCase(record: Record<string, unknown> | undefined) {
  if (!record) return '未保存失败用例输出'
  const tests = parseTestCounts(record.stdout) || parseTestCounts(record.stderr)
  if (tests && (tests.failures > 0 || tests.errors > 0)) return `${tests.run} 项，失败 ${tests.failures}，错误 ${tests.errors}，跳过 ${tests.skipped}`
  const line = (textField(record.stderr) || textField(record.error) || textField(record.stdout)).split('\n').map(item => item.trim()).find(Boolean) || ''
  return line.slice(0, 240) || '未保存失败用例输出'
}

function pushTest(phases: CollabPhase[], batch: Record<string, unknown>[]) {
  if (!batch.length) return
  const failed = batch.some(item => item.ok !== true)
  phases.push({label: testLabel(batch), detail: commandDetail(batch), tone: failed ? 'bad' : 'ok'})
  if (failed) phases.push({label: '可信测试失败', detail: commandDetail(batch.filter(item => item.ok !== true)), tone: 'bad'})
}

export function collaborationView(input: {
  events?: Record<string, unknown>[]
  messages?: Record<string, unknown>[]
  verification?: Record<string, unknown>[] | null
  repairsUsed?: number
  stopReason?: string
  passed?: boolean
  outcomeLabel?: string
  taskId?: string
  diffSources?: string[] | null
  diffError?: string
  pending?: boolean
  unreadable?: boolean
}): RepairStory {
  const events = input.events || []
  const messages = input.messages || []
  const records = input.verification || []
  const commands = commandRecords(records)
  const phases: CollabPhase[] = []
  const inconsistencies: string[] = []
  if (input.unreadable) {
    inconsistencies.push('验证证据没有读到，不能判断自动修复，也不能显示为验收通过')
    return emptyStory('验证记录读取失败，不能判断是否发生自动修复', phases, inconsistencies)
  }
  if (input.pending) {
    return emptyStory('正在读取自动修复证据', phases, inconsistencies, true)
  }
  if (records.some(item => input.taskId && item.task_id && item.task_id !== input.taskId)) {
    inconsistencies.push('验证记录的任务 ID 与当前任务不一致，已停止展示成功')
    return emptyStory('证据不属于当前任务', phases, inconsistencies)
  }
  let cursor = 0
  let repairArmed = false
  const ordered = events.map((event, index) => ({event, index})).sort((a, b) => String(a.event.time || '').localeCompare(String(b.event.time || '')) || a.index - b.index)
  const consumeUpcoming = (baseline: boolean) => {
    if (cursor >= commands.length) return [] as Record<string, unknown>[]
    const next = commands[cursor]
    const isBaseline = next.baseline === true
    if (isBaseline !== baseline) return []
    const taken = takeGroup(commands, cursor)
    cursor = taken.cursor
    return taken.batch
  }
  if (!ordered.length && messages.length) {
    phases.push({label: '阶段来源', detail: '没有执行事件。下面的顺序来自已保存的消息和验证记录。', tone: 'neutral'})
    messages.forEach(message => {
      const role = textField(message.role)
      const name = executorOf(message, '') || 'Agent'
      if (role === 'developer') {
        phases.push({label: repairArmed ? `${name} 修复` : `${name} 初次开发`, detail: `第 ${String(message.round_index ?? '—')} 轮`, tone: 'neutral'})
      } else if (role === 'reviewer') phases.push({label: `${name} 审查`, detail: `第 ${String(message.round_index ?? '—')} 轮`, tone: 'neutral'})
      else if (role === 'acceptor') phases.push({label: repairArmed ? `${name} 最终验收` : `${name} 验收`, detail: `第 ${String(message.round_index ?? '—')} 轮`, tone: 'neutral'})
    })
  }
  for (const item of ordered) {
    const event = item.event
    const name = textField(event.event)
    if (name === 'baseline_finished') {
      pushTest(phases, consumeUpcoming(true))
    } else if (name === 'baseline_entered_repair' || (name === 'status' && event.phase === 'repairing' && !repairArmed)) {
      repairArmed = true
      const skipped = event.skipped_developers
      phases.push({label: '自动修复触发', detail: name === 'baseline_entered_repair' ? `预置测试失败后进入修复${skipped === undefined ? '' : `，跳过开发者 ${String(skipped)} 次`}` : '协调器在可信测试失败后进入修复', tone: 'neutral'})
    } else if (name === 'step_started') {
      const role = textField(event.role)
      const message = messageFor(messages, event.round, role)
      const who = executorOf(message, textField(event.agent)) || (role === 'developer' ? '开发者' : role === 'reviewer' ? '审查者' : role === 'acceptor' ? '验收者' : 'Agent')
      if (role === 'developer') {
        phases.push({label: repairArmed ? `${who} 修复` : `${who} 初次开发`, detail: `第 ${String(event.round ?? '—')} 轮`, tone: 'neutral'})
      } else if (role === 'reviewer' || role === 'diagnoser') {
        phases.push({label: role === 'diagnoser' ? `${who} 诊断` : `${who} 审查`, detail: `第 ${String(event.round ?? '—')} 轮`, tone: 'neutral'})
      } else if (role === 'acceptor') {
        phases.push({label: repairArmed ? `${who} 最终验收` : `${who} 验收`, detail: `第 ${String(event.round ?? '—')} 轮 · 模型 accept 不能单独构成 verified`, tone: 'neutral'})
      }
    } else if (name === 'status' && event.phase === 'verifying') {
      if (commands[cursor]?.baseline === true) continue
      pushTest(phases, consumeUpcoming(false))
    } else if (name === 'run_finished') {
      phases.push(finalPhase(input, inconsistencies))
    }
  }
  if (cursor < commands.length) {
    if (ordered.length) inconsistencies.push('有验证记录没有对应的协调器阶段，已按保存顺序附在末尾')
    while (cursor < commands.length) {
      const taken = takeGroup(commands, cursor)
      cursor = taken.cursor
      pushTest(phases, taken.batch)
    }
  }
  if (!phases.some(phase => phase.label === '协调器最终状态')) phases.push(finalPhase(input, inconsistencies))
  const current = commands.filter(item => item.superseded !== true && item.evidence_invalidated !== true)
  const superseded = commands.filter(item => item.superseded === true)
  const repairsUsed = input.repairsUsed || 0
  const repairPhase = phases.some(phase => phase.label === '自动修复触发' || phase.label.endsWith('修复'))
  const triggered = repairsUsed > 0 || repairPhase || superseded.length > 0
  if (repairsUsed > 0 && !commands.some(item => item.ok === false)) inconsistencies.push('结果记录了修复次数，但没有失败的可信命令')
  if (superseded.length && !current.length) inconsistencies.push('失败验证已被标记替代，但没有留下当前复测记录')
  if (input.passed && input.stopReason === 'verified' && (!current.length || current.some(item => item.ok !== true))) inconsistencies.push('结果标记为 verified，但当前可信命令并没有全部通过')
  if (input.passed && input.stopReason && input.stopReason !== 'verified') inconsistencies.push('通过标记与停止原因不一致')
  const failed = commands.find(item => item.ok === false)
  const retest = [...commands].reverse().find(item => item !== failed && item.superseded !== true)
  if (input.diffError) inconsistencies.push(`Diff 读取失败：${input.diffError}`)
  if (input.diffSources) {
    for (const item of commands) {
      const diffPath = textField(item.diff_path)
      if (diffPath && !input.diffSources.includes(diffPath)) inconsistencies.push(`验证引用的 Diff 不在本次任务中：${diffPath}`)
    }
  }
  const matched = records.filter(item => item.type === 'allowed_changes' && Array.isArray(item.changed))
  const uniqueFiles: string[] = []
  for (const item of matched) {
    for (const entry of item.changed as unknown[]) {
      if (typeof entry === 'string' && !uniqueFiles.includes(entry)) uniqueFiles.push(entry)
    }
  }
  const evidenceOk = current.length > 0 && current.every(item => item.ok === true)
  const verified = Boolean(input.passed && input.stopReason === 'verified' && evidenceOk && !inconsistencies.length)
  const repairRound = Number([...messages].reverse().find(item => item.role === 'developer' && repairArmed)?.round_index || commands.find(item => Number(item.attempt) > 1)?.attempt || 0)
  const reviewer = [...messages].reverse().find(item => (item.role === 'reviewer' || item.role === 'diagnoser') && (!repairRound || Number(item.round_index) < repairRound))
  const feedback = reviewer ? `审查者第 ${String(reviewer.round_index ?? '—')} 轮写入的反馈（不受信任，不能代替验证）：${(parseAgentContent(reviewer.content).summary || '没有可展示正文').slice(0, 280)}` : '未保存审查者消息。失败事实只采用协调器验证记录。'
  let headline = '本次未触发自动修复'
  if (triggered && verified) headline = '发生过自动修复。当前可信命令通过，协调器状态为 verified'
  else if (triggered) headline = '发生过自动修复。当前没有进入 verified'
  if (inconsistencies.length && triggered) headline = '自动修复证据不完整，不能显示为修复成功'
  if (inconsistencies.length && !triggered && input.passed) headline = '本次未触发自动修复。验收证据不一致，不能显示为成功'
  const finalIndex = phases.map(phase => phase.label).lastIndexOf('协调器最终状态')
  if (finalIndex >= 0) phases[finalIndex] = finalPhase(input, inconsistencies)
  return {
    triggered,
    headline,
    failureReason: failed ? textField(failed.error) || '可信命令未通过' : '',
    exitCode: failed ? (failed.exit_code === undefined || failed.exit_code === null ? '未记录' : String(failed.exit_code)) : '',
    cases: failed ? failureCase(failed) : '',
    feedback: triggered ? feedback : '',
    files: uniqueFiles,
    beforeDiff: textField(failed?.diff_path),
    afterDiff: textField(retest?.diff_path),
    retest: retest ? `${retest.ok === true ? '通过' : retest.ok === false ? '未通过' : '结果未保存'} · exit ${retest.exit_code === undefined ? '未记录' : String(retest.exit_code)}` : (triggered ? '未保存复测记录' : ''),
    verified,
    inconsistencies,
    phases,
  }
}

function finalPhase(input: {stopReason?: string; outcomeLabel?: string; passed?: boolean}, inconsistencies: string[]): CollabPhase {
  const reason = input.stopReason || ''
  const detail = [input.outcomeLabel || STOP_TEXT[reason] || reason || '最终状态未记录', inconsistencies.length ? '证据不一致，不能把模型自述当成验收' : ''].filter(Boolean).join(' · ')
  const tone = input.passed && reason === 'verified' && !inconsistencies.length ? 'ok' : reason === 'flow_complete' || reason === 'cancelled' ? 'neutral' : 'bad'
  return {label: '协调器最终状态', detail, tone}
}

function emptyStory(headline: string, phases: CollabPhase[], inconsistencies: string[], pending = false): RepairStory {
  return {triggered: pending ? null : false, headline, failureReason: '', exitCode: '', cases: '', feedback: '', files: [], beforeDiff: '', afterDiff: '', retest: '', verified: false, inconsistencies, phases}
}

export function configRecovery(message: string) {
  if (/PROJECT_DUPLICATE|已存在|已登记/.test(message)) return '恢复：打开已登记项目再编辑，或更换项目 ID 与工作区。登记不会授予执行权限。'
  if (/CONFIG_CONFLICT|配置已改变/.test(message)) return '恢复：重新选择该项目，载入最新配置后再保存。'
  if (/PROJECT_BUSY|运行中或排队/.test(message)) return '恢复：等任务结束或取消队列后再改配置。历史记录会保留。'
  if (/PATH_DENIED|不存在|不是目录|allowed_roots|允许根/.test(message)) return '恢复：修正工作区和允许根目录，先检查路径与 Git。工作区必须位于允许根目录内。'
  if (/Git|不是 Git/.test(message)) return '恢复：在该目录初始化 Git 后再做协作。当前仍可以登记，但执行前需要仓库。'
  if (/JSON|无法识别的字段|participants|verification/.test(message)) return '恢复：用下方表单修改参与者、预算和 Worktree。保存时仍由 Python 整份校验。'
  return '恢复：按上面的原因修改后重试。失败不会授予执行权限，也不会删除历史。'
}
