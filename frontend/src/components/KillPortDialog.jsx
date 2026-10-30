import { useEffect, useState } from 'react'
import { api } from '../api.js'

/**
 * Confirms killing whatever holds a port. Kill only — nothing is started in
 * its place, so the port is simply left free.
 */
export default function KillPortDialog({ port, onClose, onDone, onError, onNotice }) {
  const [occupant, setOccupant] = useState(null)
  const [loading, setLoading] = useState(true)
  const [working, setWorking] = useState(false)

  useEffect(() => {
    let active = true
    api
      .portOccupant(port)
      .then((info) => active && setOccupant(info))
      .catch((error) => active && onError(error.message))
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [port, onError])

  async function confirm() {
    setWorking(true)
    try {
      const result = await api.killPort(port)
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
            <h3>Kill whatever is on port {port}</h3>
            <p className="modal-sub">Frees the port. Nothing is started in its place.</p>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        <div className="modal-body">
          {loading && <p className="empty-hint pad">Checking what holds port {port}…</p>}

          {!loading && occupant && !occupant.occupied && (
            <p>Nothing is listening on port {port} any more.</p>
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
                  This process and everything it started will be stopped — politely first, forcibly
                  after 8 seconds. Port {port} is then free, and you can start an app on it whenever
                  you like.
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
            {working ? 'Killing…' : `Kill and free port ${port}`}
          </button>
        </footer>
      </div>
    </div>
  )
}
