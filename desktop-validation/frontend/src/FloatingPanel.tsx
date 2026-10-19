import {useEffect, useRef, type ReactNode, type RefObject} from 'react'
import {createPortal} from 'react-dom'

export function FloatingPanel({open, anchorRef, label, onClose, children}: {
  open: boolean
  anchorRef: RefObject<HTMLElement | null>
  label: string
  onClose: () => void
  children: ReactNode
}) {
  const panelRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef(onClose)
  closeRef.current = onClose

  useEffect(() => {
    if (!open) return
    const scroller = document.querySelector<HTMLElement>('.compose-page')
    const scrollTop = scroller?.scrollTop || 0
    const panel = panelRef.current
    const focusable = () => panel
      ? [...panel.querySelectorAll<HTMLElement>('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')].filter(element => !element.hasAttribute('disabled'))
      : []
    const preferred = panel?.querySelector<HTMLElement>('[data-autofocus]')
    ;(preferred || focusable()[0])?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        closeRef.current()
        return
      }
      if (event.key !== 'Tab') return
      const items = focusable()
      if (!items.length) return
      const first = items[0]
      const last = items[items.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      if (scroller) scroller.scrollTop = scrollTop
      if (!document.querySelector('.modal')) anchorRef.current?.focus()
    }
  }, [open, anchorRef])

  if (!open) return null
  return createPortal(
    <>
      <div className="floating-dismiss dialog" onClick={() => closeRef.current()} />
      <div
        ref={panelRef}
        className="floating-panel dialog"
        role="dialog"
        aria-modal="true"
        aria-label={label}
        id={label === '任务设置' ? 'task-settings-panel' : 'project-info-panel'}
      >
        {children}
      </div>
    </>,
    document.body,
  )
}
