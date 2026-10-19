const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const Module = require('node:module')
const ts = require('../../../design-prototype/node_modules/typescript')

function load(name) {
  const source = path.join(__dirname, '../src', name)
  const compiled = new Module(source, module)
  compiled._compile(ts.transpileModule(fs.readFileSync(source, 'utf8'), {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText, source)
  return compiled.exports
}
const present = load('present.ts')
const highlight = load('highlight.ts')

const model = '{"summary":"已实现 Utf8Clip 的 UTF-8 字节计数和码点边界截断。","details":"按码点遍历，完整保留或舍弃代理对。","decision":"continue","files_touched":["src/main/java/studio/util/Utf8Clip.java"],"risks":[]}'

test('model JSON becomes natural language and does not keep the raw object', () => {
  const narrative = present.parseAgentContent(model)
  assert.equal(narrative.summary, '已实现 Utf8Clip 的 UTF-8 字节计数和码点边界截断。')
  assert.match(narrative.details, /按码点遍历/)
  assert.equal(narrative.body.includes('{'), false)
  assert.deepEqual(narrative.files, ['src/main/java/studio/util/Utf8Clip.java'])
})

test('prose before JSON is kept and markup stays text', () => {
  const narrative = present.parseAgentContent('说明在前 {"summary":"结论","details":"细节","risks":["<img src=x onerror=alert(1)>"]}')
  assert.equal(narrative.summary, '结论')
  assert.match(narrative.details, /说明在前/)
  assert.match(narrative.details, /细节/)
  assert.equal(narrative.risks[0], '<img src=x onerror=alert(1)>')
})

test('unparseable text is shown as itself', () => {
  const narrative = present.parseAgentContent('<script>alert(1)</script> 普通说明')
  assert.equal(narrative.summary, '<script>alert(1)</script> 普通说明')
  assert.equal(narrative.risks.length, 0)
})

test('trusted test counts come only from coordinator stdout', () => {
  const stdout = 'Tests run: 1, Failures: 1, Errors: 0, Skipped: 0\nTests run: 5, Failures: 0, Errors: 0, Skipped: 0\n'
  const view = present.trustedVerification([
    {type: 'allowed_changes', ok: true, changed: ['src/main/java/studio/util/Utf8Clip.java'], illegal: [], missing_required_changes: []},
    {type: 'command', ok: true, exit_code: 0, duration_seconds: 3.665, trusted_script: 'E:/trusted/verify_utf8_clip.py', stdout, stderr: ''},
  ])
  assert.equal(view.state, 'pass')
  assert.equal(view.headline, '验证通过 · 5 项')
  assert.equal(view.commands[0].name, 'verify_utf8_clip.py')
  assert.deepEqual(view.commands[0].tests, {run: 5, failures: 0, errors: 0, skipped: 0})
  assert.match(view.scope, /改动范围通过/)
  const claimed = present.trustedVerification([{type: 'command', ok: false, exit_code: 1, stdout: 'agent says 5 tests passed but Tests run: 5, Failures: 2, Errors: 1, Skipped: 0'}])
  assert.equal(claimed.state, 'fail')
  assert.equal(claimed.commands[0].tests.failures, 2)
  assert.equal(present.parseTestCounts('Maven/JUnit 5 项全绿'), null)
})

test('new-task allowance stays separate from a saved run', () => {
  assert.equal(present.projectAllowance(undefined, false, true), '只读历史，不能发起新任务')
  assert.equal(present.projectAllowance({mock: {allowed: false}, offline_cli: {allowed: false}, real: {allowed: false}}, true, true), '当前不能发起新任务')
  assert.equal(present.projectAllowance({mock: {allowed: true}, real: {allowed: false}}, true, true), '可发起 Mock')
  assert.equal(present.isHistoricalTask('verified'), true)
  assert.equal(present.isHistoricalTask('failed'), true)
  assert.equal(present.isHistoricalTask('cancelled'), true)
  assert.equal(present.isHistoricalTask('running'), false)
  assert.equal(present.isHistoricalTask('reviewing'), false)
  assert.equal(present.projectAllowance({real: {allowed: false}}, true, true).includes('失败'), false)
})

test('project posture names come from backend states', () => {
  assert.equal(present.postureSummary(['registered', 'read_only']), '已登记 · 只读可用')
  assert.equal(present.postureSummary(['registered', 'mock_executable']), '已登记 · Mock 可执行')
  assert.equal(present.postureSummary(['registered', 'real_authorized']), '已登记 · 真实 CLI 已授权')
  assert.equal(present.postureSummary(undefined), '')
})

test('duration and source labels stay factual', () => {
  assert.equal(present.formatDuration(152.555), '2 分 33 秒')
  assert.equal(present.formatDuration(73.961), '1 分 14 秒')
  assert.equal(present.formatDuration('152'), '')
  assert.equal(present.sourceLabel({executionMode: 'real', callKind: 'model'}), '真实 CLI · 独立签名授权')
  assert.equal(present.sourceLabel({executionMode: 'mock', callKind: 'mock'}), 'Mock · 真实模型调用 0')
})

test('highlight returns text tokens and does not emit markup', () => {
  const tokens = highlight.highlight('public class Utf8Clip { int n = 1; // 注释', 'Utf8Clip.java')
  assert.equal(tokens.map(token => token.text).join(''), 'public class Utf8Clip { int n = 1; // 注释')
  assert.equal(tokens.some(token => token.kind === 'keyword' && token.text === 'public'), true)
  assert.equal(tokens.some(token => token.text.includes('<span')), false)
})

test('repair evidence follows saved coordinator order and does not trust the model', () => {
  const view = present.collaborationView({
    events: [
      {time: '1', event: 'status', phase: 'verifying'},
      {time: '2', event: 'baseline_finished', ok: false},
      {time: '3', event: 'baseline_entered_repair', skipped_developers: 1},
      {time: '4', event: 'step_started', round: 1, agent: 'cursor', role: 'reviewer'},
      {time: '5', event: 'status', phase: 'repairing'},
      {time: '6', event: 'step_started', round: 2, agent: 'codex', role: 'developer'},
      {time: '7', event: 'status', phase: 'verifying'},
      {time: '8', event: 'step_started', round: 3, agent: 'cursor-accept', role: 'acceptor'},
      {time: '9', event: 'run_finished', stop_reason: 'verified'},
    ],
    messages: [
      {round_index: 1, role: 'reviewer', executor: 'cursor', content: '{"summary":"1001 克应得 14","decision":"continue"}'},
      {round_index: 3, role: 'acceptor', executor: 'cursor', content: '{"summary":"测试已经通过","decision":"accept"}'},
    ],
    verification: [
      {type: 'command', ok: false, exit_code: 1, error: '验证退出码为 1，预定为 0', stderr: 'quote(1001) expected 14 but got 10', baseline: true, attempt: 1, diff_path: 'baseline/changes.diff', superseded: true},
      {type: 'allowed_changes', ok: true, changed: ['src/Postage.java'], illegal: []},
      {type: 'command', ok: true, exit_code: 0, attempt: 2, diff_path: 'rounds/002/changes.diff', stdout: 'ok'},
    ],
    repairsUsed: 1,
    stopReason: 'verified',
    passed: true,
    outcomeLabel: '已验收',
    taskId: 'postage-preset-repair',
    diffSources: ['baseline/changes.diff', 'rounds/002/changes.diff'],
  })
  assert.deepEqual(view.phases.map(phase => phase.label), ['第一次可信测试', '可信测试失败', '自动修复触发', 'Cursor 审查', 'Codex 修复', '第二次可信测试', 'Cursor 最终验收', '协调器最终状态'])
  assert.equal(view.triggered, true)
  assert.equal(view.verified, true)
  assert.equal(view.exitCode, '1')
  assert.match(view.cases, /quote\(1001\)/)
  assert.match(view.feedback, /不受信任/)
  assert.match(view.feedback, /1001 克应得 14/)
  assert.deepEqual(view.files, ['src/Postage.java'])
  assert.equal(view.beforeDiff, 'baseline/changes.diff')
  assert.equal(view.afterDiff, 'rounds/002/changes.diff')
  assert.match(view.retest, /通过/)
  assert.equal(view.headline.includes('修复成功'), false)
})

test('a run without repair is not presented as repaired', () => {
  const view = present.collaborationView({
    events: [
      {time: '1', event: 'step_started', round: 1, agent: 'codex', role: 'developer'},
      {time: '2', event: 'status', phase: 'verifying'},
      {time: '3', event: 'run_finished', stop_reason: 'flow_complete'},
    ],
    messages: [{round_index: 1, role: 'developer', executor: 'codex', content: '{"summary":"我已修复并通过测试","decision":"accept"}'}],
    verification: [{type: 'command', ok: true, exit_code: 0, attempt: 1, stdout: 'ok'}],
    repairsUsed: 0,
    stopReason: 'flow_complete',
    passed: false,
    outcomeLabel: '流程结束',
  })
  assert.equal(view.triggered, false)
  assert.equal(view.verified, false)
  assert.equal(view.headline, '本次未触发自动修复')
  assert.equal(view.phases.map(phase => phase.label).includes('Codex 初次开发'), true)
  assert.equal(view.phases.map(phase => phase.label).includes('自动修复触发'), false)
})

test('model claims and missing diffs cannot create verified', () => {
  const claimed = present.collaborationView({
    events: [{time: '1', event: 'run_finished', stop_reason: 'verified'}],
    messages: [{role: 'acceptor', executor: 'cursor', content: '测试已经通过'}],
    verification: [{type: 'command', ok: false, exit_code: 1, error: '失败', attempt: 1}],
    repairsUsed: 0,
    stopReason: 'verified',
    passed: true,
    taskId: 'current',
  })
  assert.equal(claimed.verified, false)
  assert.equal(claimed.inconsistencies.length > 0, true)
  const foreign = present.collaborationView({verification: [{type: 'command', ok: true, task_id: 'other'}], taskId: 'current', passed: true, stopReason: 'verified'})
  assert.equal(foreign.verified, false)
  assert.match(foreign.headline, /不属于当前任务/)
  const missing = present.collaborationView({
    verification: [{type: 'command', ok: true, exit_code: 0, attempt: 2, superseded: false, diff_path: 'rounds/009/changes.diff'}, {type: 'command', ok: false, exit_code: 1, attempt: 1, superseded: true, diff_path: 'baseline/changes.diff'}],
    repairsUsed: 1, stopReason: 'verified', passed: true, diffSources: ['rounds/001/changes.diff'],
  })
  assert.equal(missing.verified, false)
  assert.equal(missing.inconsistencies.some(item => item.includes('不在本次任务中')), true)
})

test('config errors name a recovery step', () => {
  assert.match(present.configRecovery('PROJECT_DUPLICATE：项目 ID 已存在'), /不会授予执行权限/)
  assert.match(present.configRecovery('CONFIG_CONFLICT'), /重新选择该项目/)
})

test('ui sources do not inject model text as HTML', () => {
  for (const name of ['Timeline.tsx', 'Inspector.tsx', 'ResultSummary.tsx', 'present.ts', 'highlight.ts', 'ProjectManager.tsx', 'AccessPanel.tsx']) {
    const source = fs.readFileSync(path.join(__dirname, '../src', name), 'utf8')
    assert.equal(source.includes('dangerouslySetInnerHTML'), false, name)
  }
})
