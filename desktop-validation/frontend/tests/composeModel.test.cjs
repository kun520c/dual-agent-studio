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

const model = load('composeModel.ts')

test('execution choices come only from backend permission flags', () => {
  assert.deepEqual(model.executionChoices(undefined, undefined), [])
  const fallback = model.executionChoices(undefined, {allowed: false, reason: '只读'})
  assert.equal(fallback.length, 1)
  assert.equal(fallback[0].allowed, false)
  assert.equal(fallback[0].reason, '只读')
  const modes = model.executionChoices({
    mock: {allowed: true, reason: '已登记隔离 Mock'},
    offline_cli: {allowed: false, reason: '没有离线授权'},
    real: {allowed: false, reason: '真实 CLI 未授权：部署未安装人工授权公钥'},
    invented: {allowed: true, reason: '前端不能采用未知名来源'},
  })
  assert.deepEqual(modes.map(item => item.id), ['mock', 'offline_cli', 'real'])
  assert.equal(modes[0].allowed, true)
  assert.equal(modes[1].allowed, false)
  assert.equal(modes[2].allowed, false)
  assert.match(model.optionLabel(modes[2]), /不可用：真实 CLI 未授权/)
  assert.equal(model.optionLabel(modes[0]), 'Mock')
  const missingFlag = model.executionChoices({mock: {reason: '字段缺失'}})
  assert.equal(missingFlag[0].allowed, false)
})

test('project lead and git risk stay short and do not surface a windows path', () => {
  assert.equal(model.composeLead('内置安全演练。只在样本目录运行。'), '内置安全演练。只在样本目录运行。')
  assert.equal(model.composeLead('E:\\repo\\workspace'), '用自然语言描述这次要完成的工作。')
  assert.equal(model.composeLead(''), '用自然语言描述这次要完成的工作。')
  assert.equal(model.gitStatusLabel('工作区干净（未列出未跟踪文件）').risk, '')
  assert.match(model.gitStatusLabel('有未提交改动').risk, /未提交改动/)
  assert.match(model.gitStatusLabel('未检测到 Git 仓库').risk, /Git 仓库/)
  assert.equal(model.gitBranch('## main...origin/main'), 'main')
  assert.equal(model.gitBranch('## No commits yet on master'), 'master')
})

test('verification gap and participant summary avoid raw command lines', () => {
  assert.equal(model.verificationGap(undefined), '')
  assert.match(model.verificationGap([]), /没有可信测试/)
  assert.match(model.verificationGap([{type: 'command'}]), /没有配齐/)
  assert.equal(model.verificationGap([{type: 'command'}, {type: 'allowed_changes'}]), '')
  assert.equal(model.participantLine({executor: 'mock', role: 'developer'}), 'Mock · 开发者')
  assert.equal(model.worktreeSummary({enabled: false}), '未启用 Worktree。任务使用登记的工作区。这不是操作系统沙箱。')
})
