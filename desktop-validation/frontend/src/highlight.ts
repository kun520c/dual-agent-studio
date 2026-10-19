export interface Token { text: string; kind: 'plain' | 'keyword' | 'string' | 'comment' | 'number' | 'type' }

const KEYWORDS = new Set(['if', 'else', 'for', 'while', 'return', 'new', 'class', 'public', 'private', 'protected', 'static', 'final', 'void', 'int', 'long', 'boolean', 'byte', 'char', 'float', 'double', 'null', 'true', 'false', 'throw', 'throws', 'package', 'import', 'try', 'catch', 'finally', 'this', 'super', 'extends', 'implements', 'interface', 'enum', 'switch', 'case', 'break', 'continue', 'default', 'instanceof', 'var', 'let', 'const', 'function', 'def', 'from', 'async', 'await', 'export', 'type', 'elif', 'pass', 'raise', 'with', 'as', 'in', 'not', 'and', 'or', 'lambda', 'None', 'True', 'False'])

function language(path: string) {
  const ext = path.split(/[/\\.]/).pop()?.toLowerCase() || ''
  if (ext === 'py') return 'python'
  if (ext === 'json') return 'json'
  if (['java', 'ts', 'tsx', 'js', 'jsx'].includes(ext)) return 'c-like'
  return ''
}

export function highlight(code: string, path: string): Token[] {
  const lang = language(path)
  if (!lang || !code) return [{text: code || ' ', kind: 'plain'}]
  const tokens: Token[] = []
  const push = (text: string, kind: Token['kind']) => {
    if (!text) return
    const last = tokens[tokens.length - 1]
    if (last && last.kind === kind) last.text += text
    else tokens.push({text, kind})
  }
  let i = 0
  while (i < code.length) {
    const rest = code.slice(i)
    if (lang !== 'json' && rest.startsWith('//')) {
      const end = code.indexOf('\n', i)
      const stop = end < 0 ? code.length : end
      push(code.slice(i, stop), 'comment'); i = stop; continue
    }
    if (lang === 'python' && code[i] === '#') {
      const end = code.indexOf('\n', i)
      const stop = end < 0 ? code.length : end
      push(code.slice(i, stop), 'comment'); i = stop; continue
    }
    if (lang === 'c-like' && rest.startsWith('/*')) {
      const end = code.indexOf('*/', i + 2)
      const stop = end < 0 ? code.length : end + 2
      push(code.slice(i, stop), 'comment'); i = stop; continue
    }
    const quote = code[i]
    if (quote === '"' || quote === "'" || (lang === 'python' && quote === '`')) {
      let j = i + 1
      while (j < code.length) {
        if (code[j] === '\\') { j += 2; continue }
        if (code[j] === quote) { j++; break }
        if (code[j] === '\n' && quote !== '`') break
        j++
      }
      push(code.slice(i, j), 'string'); i = j; continue
    }
    if (lang === 'c-like' && quote === '@') {
      let j = i + 1
      while (j < code.length && /[\w$]/.test(code[j])) j++
      push(code.slice(i, j), 'keyword'); i = j; continue
    }
    if (/[0-9]/.test(quote) && (i === 0 || !/[\w$]/.test(code[i - 1]))) {
      let j = i + 1
      while (j < code.length && /[0-9a-fA-FxX._]/.test(code[j])) j++
      push(code.slice(i, j), 'number'); i = j; continue
    }
    if (/[\w$]/.test(quote)) {
      let j = i + 1
      while (j < code.length && /[\w$]/.test(code[j])) j++
      const word = code.slice(i, j)
      const kind = KEYWORDS.has(word) ? 'keyword' : lang === 'c-like' && /^[A-Z]/.test(word) ? 'type' : 'plain'
      push(word, kind); i = j; continue
    }
    push(quote, 'plain'); i++
  }
  return tokens.length ? tokens : [{text: code, kind: 'plain'}]
}
