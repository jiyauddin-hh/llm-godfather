import { useEffect, useState } from 'react'
import { api } from '../api.js'

/**
 * Confirms killing whatever holds an app's port. This is destructive, so the
 * dialog shows the exact process — pid, user and full command — before asking.
 */
export default function TakeoverDialog({ app, onClose, onDone, onError, onNotice }) {
  const [occupant, setOccupant] = useState(null)
  const [loading, setLoading] = useState(true)
  const [working, setWorking] = useState(false)

  useEffect(() => {
    let active = true
    api
      .occupant(app.id)
      .then((info) => active && setOccupant(info))
      .catch((error) => active && onError(error.message))
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [app.id, onError])

  async function confirm() {
    setWorking(true)
    try {
      const result = await api.takeover(app.id)
      onNotice(result.message)
      onDone()
    } catch (error) {
      onError(error.message)
      setWorking(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <header className="modal-header">
          <div>
            <h3>Take over port {app.port}</h3>
            <p className="modal-sub">Stop the process using it, then start {app.name} here.</p>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        <div className="modal-body">
          {loading && <p className="empty-hint pad">Checking what holds the port…</p>}

          {!loading && occupant && !occupant.occupied && (
            <p>Nothing holds port {app.port} any more. You can just press Start.</p>
          )}

          {!loading && occupant?.occupied && (
            <>
              <div className="kill-card">
                <div className="kill-row">
                  <span className="kill-key">Process</span>
                  <code>{occupant.process ?? 'unknown'}</code>
                </div>
                <div className="kill-row">
                  <span className="kill-key">PID</span>
                  <code>{occupant.pid ?? '—'}</code>
                </div>
                <div className="kill-row">
                  <span className="kill-key">User</span>
                  <code>{occupant.user ?? 'unknown'}</code>
                </div>
                {occupant.command && (
                  <div className="kill-row wide">
                    <span className="kill-key">Command</span>
                    <code>{occupant.command}</code>
                  </div>
                )}
              </div>

              {occupant.killable ? (
                <p>
                  This process and everything it started will be stopped — first politely, then
                  forcibly if it does not exit within 8 seconds. Then <strong>{app.name}</strong> starts
                  under God Father, with its logs captured from the first line.
                </p>
              ) : (
                <div className="banner warn">{occupant.reason}</div>
              )}
            </>
          )}
        </div>

        <footer className="modal-footer">
          <button type="button" className="btn ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn danger"
            disabled={working || loading || !occupant?.killable}
            onClick={confirm}
          >
            {working ? 'Taking over…' : 'Stop it and start here'}
          </button>
        </footer>
      </div>
    </div>
  )
}
