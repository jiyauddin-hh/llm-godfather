import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import KillPortDialog from './KillPortDialog.jsx'

export default function PortsView({ onSelectApp, onError, onNotice }) {
  const [ports, setPorts] = useState([])
  const [loading, setLoading] = useState(true)
  const [onlyMine, setOnlyMine] = useState(false)
  const [killing, setKilling] = useState(null)

  const load = useCallback(async () => {
    try {
      setPorts(await api.ports())
    } catch (error) {
      onError(error.message)
    } finally {
      setLoading(false)
    }
  }, [onError])

  useEffect(() => {
    load()
    const timer = setInterval(load, 5000)
    return () => clearInterval(timer)
  }, [load])

  const rows = onlyMine ? ports.filter((port) => port.managed_app_id) : ports

  return (
    <section className="detail">
      <header className="detail-header">
        <div className="detail-title">
          <div>
            <h2>Port map</h2>
            <p className="detail-sub">Every TCP port listening on this machine, refreshed every 5s.</p>
          </div>
        </div>
        <div className="detail-actions">
          <label className="checkbox">
            <input
              type="checkbox"
              checked={onlyMine}
              onChange={(event) => setOnlyMine(event.target.checked)}
            />
            Only registered apps
          </label>
          <button className="btn ghost" onClick={load}>
            ⟳ Refresh
          </button>
        </div>
      </header>

      <div className="table-wrap">
        {loading ? (
          <p className="empty-hint pad">Scanning ports…</p>
        ) : rows.length === 0 ? (
          <p className="empty-hint pad">
            Nothing listening. On some systems process details need elevated permissions.
          </p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Port</th>
                <th>State</th>
                <th>Process</th>
                <th>PID</th>
                <th>Address</th>
                <th>Registered app</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const listening = Boolean(row.pid || row.address)
                return (
                  <tr key={row.port}>
                    <td className="mono strong">{row.port}</td>
                    <td>
                      <span className={`pill ${listening ? 'running' : 'stopped'}`}>
                        {listening ? 'listening' : 'free'}
                      </span>
                    </td>
                    <td className="mono">{row.process ?? '—'}</td>
                    <td className="mono">{row.pid ?? '—'}</td>
                    <td className="mono muted">{row.address || '—'}</td>
                    <td>
                      {row.managed_app_id ? (
                        <button className="link" onClick={() => onSelectApp(row.managed_app_id)}>
                          {row.managed_app_name}
                        </button>
                      ) : (
                        <span className="muted">unmanaged</span>
                      )}
                    </td>
                    <td className="row-action">
                      {listening && (
                        <button
                          className="btn ghost small danger"
                          onClick={() => setKilling(row.port)}
                          title={`Stop whatever is using port ${row.port}`}
                        >
                          Kill
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      {killing !== null && (
        <KillPortDialog
          port={killing}
          onClose={() => setKilling(null)}
          onError={onError}
          onNotice={onNotice}
          onDone={() => {
            setKilling(null)
            load()
          }}
        />
      )}
    </section>
  )
}
