/** Display rules for the new-task page. Permission flags come only from the backend. */

export interface ExecutionChoice {
  id: string
  name: string
  allowed: boolean
  reason: string
}

const ORDER = ['mock', 'offline_cli', 'real'] as const
const NAMES: Record<string, string> = {mock: 'Mock', offline_cli: '离线模拟 CLI', real: '真实 CLI'}
const ROLES: Record<string, string> = {developer: '开发者', reviewer: '审查者', diagnoser: '诊断者', acceptor: '验收者'}

export function executionChoices(
  modes: Record<string, {allowed?: boolean; reason?: string}> | undefined,
  fallback?: {allowed?: boolean; reason?: string},
): ExecutionChoice[] {
  if (!modes || !Object.keys(modes).length) {
    if (!fallback) return []
    const allowed = fallback.allowed === true
    return [{id: 'mock', name: NAMES.mock, allowed, reason: allowed ? '' : (fallback.reason || '执行权限尚未返回')}]
  }
  return ORDER.filter(id => Object.prototype.hasOwnProperty.call(modes, id)).map(id => {
    const item = modes[id] || {}
    const allowed = item.allowed === true
    return {id, name: NAMES[id] || id, allowed, reason: allowed ? '' : (item.reason || '后端没有允许这个来源')}
  })
}

export function optionLabel(choice: ExecutionChoice): string {
  if (choice.allowed) return choice.name
  const reason = choice.reason.replace(/\s+/g, ' ').trim()
  const short = reason.length > 48 ? `${reason.slice(0, 48)}…` : reason
  return `${choice.name} · 不可用：${short || '未授权'}`
}

export function composeLead(description: unknown): string {
  const fallback = '用自然语言描述这次要完成的工作。'
  if (typeof description !== 'string') return fallback
  const text = description.trim()
  if (!text || text.length > 120 || /[A-Za-z]:[\\/]/.test(text)) return fallback
  return text
}

export function gitBranch(raw?: string): string {
  let line = (raw || '').replace(/^##\s*/, '').split('...')[0].trim()
  const fresh = /^No commits yet on\s+(.+)$/i.exec(line)
  if (fresh) line = fresh[1].trim()
  if (!line || /^(HEAD|No commits|Initial commit)/i.test(line)) return ''
  return line.slice(0, 32)
}

export function gitStatusLabel(summary?: string): {text: string; tone: 'ok' | 'warn' | 'muted'; risk: string} {
  if (!summary) return {text: '正在读取', tone: 'muted', risk: ''}
  if (summary.startsWith('工作区干净')) return {text: '工作区干净', tone: 'ok', risk: ''}
  if (summary === '有未提交改动') return {text: '有未提交改动', tone: 'warn', risk: 'Git 工作区有未提交改动。提交前确认这次任务不会带上无关改动。'}
  if (summary === '未检测到 Git 仓库') return {text: '未检测到仓库', tone: 'warn', risk: '当前目录不是可检查的 Git 仓库。执行前需要仓库。'}
  if (summary === 'Git 状态没有读到') return {text: '状态未读到', tone: 'warn', risk: 'Git 状态没有读到，提交前不能确认工作区是否安全。'}
  return {text: summary, tone: 'warn', risk: summary}
}

export function verificationGap(checks: {type?: string}[] | undefined): string {
  if (!checks) return ''
  if (!checks.length) return '没有可信测试配置。开始协作前会被预检阻断。'
  const command = checks.some(item => item.type === 'command')
  const scope = checks.some(item => item.type === 'allowed_changes')
  if (!command || !scope) return '可信测试或允许改动范围没有配齐。开始协作前会被预检阻断。'
  return ''
}

export function participantLine(person: {executor?: string; role?: string}): string {
  const executor = person.executor === 'codex' ? 'Codex' : person.executor === 'cursor' ? 'Cursor' : person.executor === 'mock' ? 'Mock' : (person.executor || '未命名')
  const role = ROLES[person.role || ''] || person.role || '未登记职责'
  return `${executor} · ${role}`
}

export function budgetLine(budget: {modelCalls: number; rounds: number; repairs: number; runSeconds: number; stepSeconds: number} | undefined, execution: string): string {
  if (!budget) return '预算尚未读取'
  const calls = execution === 'mock' ? '模拟调用' : execution === 'offline_cli' ? '离线调用' : 'CLI 调用'
  return `${calls} ${budget.modelCalls} · 轮数 ${budget.rounds} · 修复 ${budget.repairs} · 总时限 ${budget.runSeconds} 秒 · 单步 ${budget.stepSeconds} 秒`
}

export function worktreeSummary(worktree: Record<string, unknown> | undefined, readout?: string): string {
  if (readout) return readout
  if (worktree?.enabled === true) return '已启用 Worktree。路径由后端在执行时再次检查。这不是操作系统沙箱。'
  return '未启用 Worktree。任务使用登记的工作区。这不是操作系统沙箱。'
}
