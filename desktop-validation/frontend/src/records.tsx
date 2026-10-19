import {useState} from 'react'

export function CopyButton({label, text}: {label: string; text: string}) {
  const [note, setNote] = useState('')
  const copy = async () => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text)
        setNote('已复制')
        return
      }
    } catch { /* Qt 默认关闭异步剪贴板。 */ }
    try {
      const area = document.createElement('textarea')
      area.value = text
      area.setAttribute('readonly', '')
      area.style.position = 'fixed'
      area.style.opacity = '0'
      document.body.appendChild(area)
      area.select()
      const ok = document.execCommand('copy')
      area.remove()
      setNote(ok ? '已复制' : '请选中文本后按 Ctrl+C')
    } catch {
      setNote('请选中文本后按 Ctrl+C')
    }
  }
  return <button type="button" className="inline-link" onClick={() => void copy()}>{note || label}</button>
}

export function RawRecord({title, value, opened = false}: {title: string; value: unknown; opened?: boolean}) {
  const [open, setOpen] = useState(opened)
  const text = open ? (typeof value === 'string' ? value : JSON.stringify(value, null, 2)) : ''
  return <details className="raw-record" open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{title}</summary>
    {open && <div className="raw-record-body"><pre>{text}</pre><CopyButton label="复制原始记录" text={text} /></div>}
  </details>
}
