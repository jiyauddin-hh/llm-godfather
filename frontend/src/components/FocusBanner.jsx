import { useState } from 'react'
import { api } from '../api.js'

const PHASE = {
  starting: 'Switching to Focus Mode…',
  active: 'Focus Mode',
  restoring: 'Exiting Focus Mode…',
  ended: 'Focus Mode ended',
}

// Shown on every page while Focus Mode is on, while it is switching, and once
// after it ends so the result of the restore is not missed.
export default function FocusBanner({ focus, onChanged, onError, onDismiss }) {
  const [open, setOpen] = useState(false)
  const [exiting, setExiting] = useState(false)

  const working = focus.busy || focus.phase === 'starting' || focus.phase === 'restoring'
  const log = focus.log ?? []
  const last = log[log.length - 1]
  const failed = focus.failed ?? []

  async function exit() {
    setExiting(true)
    try {
      await api.focusExit()
      onChanged()
    } catch (error) {
      onError(error.message)
    } finally {
      setExiting(false)
    }
  }

  const summary =
    focus.phase === 'ended'
      ? `${focus.restored?.length ?? 0} app(s) restored${failed.length ? `, ${failed.length} failed` : ''}`
      : `${focus.target?.name} has the machine · ${focus.stopped?.length ?? 0} app(s) stopped` +
        (focus.unloaded?.length ? ` · ${focus.unloaded.length} model(s) unloaded` : '')

  return (
    <div className={`focus-banner ${focus.phase} ${failed.length ? 'has-failed' : ''}`}>
      <div className="focus-banner-row">
        <span className={`focus-badge ${working ? 'working' : ''}`}>◎</span>
        <div className="focus-banner-text">
          <strong>{PHASE[focus.phase] ?? 'Focus Mode'}</strong>
          <span>{working && last ? last.text : summary}</span>
        </div>
        <button className="btn ghost small" onClick={() => setOpen((v) => !v)}>
          {open ? 'Hide steps' : 'Show steps'}
        </button>
        {focus.active && focus.phase === 'active' && (
          <button className="btn warn small" disabled={exiting || working} onClick={exit}>
            {exiting ? 'Exiting…' : 'Exit Focus Mode'}
          </button>
        )}
        {focus.phase === 'ended' && (
          <button className="btn ghost small" onClick={onDismiss} aria-label="Dismiss">
            ✕
          </button>
        )}
      </div>
      {open && (
        <ol className="focus-log">
          {log.map((entry, index) => (
            <li key={index} className={entry.level}>
              <span className="mono muted">{new Date(entry.ts).toLocaleTimeString()}</span> {entry.text}
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}
