const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')

const root = path.join(__dirname, '../src')
const styles = fs.readFileSync(path.join(root, 'styles.css'), 'utf8')
const integration = fs.readFileSync(path.join(root, 'integration.css'), 'utf8')
const main = fs.readFileSync(path.join(root, 'main.tsx'), 'utf8')
const inspector = fs.readFileSync(path.join(root, 'Inspector.tsx'), 'utf8')

function block(source, selector) {
  const start = source.indexOf(selector)
  assert.notEqual(start, -1, selector)
  return source.slice(start, source.indexOf('}', start))
}

function token(blockText, name) {
  const found = blockText.match(new RegExp(`${name}:([^;]+)`))
  assert.ok(found, name)
  return found[1].trim()
}

test('light and dark large surfaces share one neutral background token', () => {
  const light = block(styles, ":root, :root[data-theme='light']")
  const dark = block(styles, ":root[data-theme='dark']")
  assert.equal(token(light, '--canvas'), token(light, '--panel'))
  assert.equal(token(dark, '--canvas'), token(dark, '--panel'))
  assert.equal(token(light, '--raised'), '#FFFFFF')
  assert.notEqual(token(light, '--canvas'), '#FFFFFF')
  assert.notEqual(token(light, '--canvas'), '#FAFAF9')
  assert.notEqual(token(light, '--sidebar'), token(light, '--canvas'))
  assert.equal(token(light, '--text'), '#202020')
  assert.equal(token(dark, '--text'), '#F4F4F5')
  assert.match(block(styles, '.modal {'), /background:var\(--raised\)/)
})

test('composer stays compact and inspector maximize stays inside the workspace grid', () => {
  const composer = block(integration, '.compose-editor .composer {')
  const field = block(integration, '.compose-editor .composer textarea {')
  assert.match(composer, /height:\s*158px/)
  assert.match(field, /overflow-y:\s*auto/)
  assert.match(field, /min-height:\s*0/)
  assert.doesNotMatch(block(integration, '.app.inspector-max .inspector {'), /position:\s*fixed|inset:\s*0/)
  assert.match(block(integration, '.app.inspector-max .inspector {'), /grid-column:\s*2\s*\/\s*-1/)
  assert.match(main, /inspectorMaximized/)
  assert.doesNotMatch(main, /persist\('inspectorMaximized'/)
  assert.match(inspector, /最大化查看/)
  assert.match(inspector, /Maximize2/)
  assert.match(inspector, /Minimize2/)
  assert.match(inspector, /恢复分栏/)
})
