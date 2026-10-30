import { useEffect, useState } from 'react'
import { api } from '../api.js'

const GROUPS = [
  {
    kind: 'project',
    title: 'Your projects',
    blurb: 'Applications running from your home folder. These are the ones whose logs you want.',
  },
  {
    kind: 'tool',
    title: 'Developer tools',
    blurb: 'Tunnels, model servers and similar helpers.',
  },
  {
    kind: 'service',
    title: 'System services',
    blurb: 'Run by systemd as another user. Import them for the port map — starting them here will not work.',
  },
]

export default function DiscoverModal({ onClose, onImported, onError }) {
  const [loading, setLoading] = useState(true)
  const [rows, setRows] = useState([])
  const [importing, setImporting] = useState(false)

  useEffect(() => {
    let active = true
    api
      .discover()
      .then((found) => {
        if (!active) return
        setRows(
          found.map((item, index) => ({
            key: `${item.pid}-${index}`,
            ...item,
            // Projects are pre-selected; tools and services are opt-in.
            selected: item.kind === 'project' && item.importable && !item.already_registered,
            name: item.suggested_name,
          }))
        )
      })
      .catch((error) => onError(error.message))
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [onError])

  function patch(key, changes) {
    setRows((previous) => previous.map((row) => (row.key === key ? { ...row, ...changes } : row)))
  }

  function toggleGroup(kind, selected) {
    setRows((previous) =>
      previous.map((row) =>
        row.kind === kind && row.importable && !row.already_registered ? { ...row, selected } : row
      )
    )
  }

  const chosen = rows.filter((row) => row.selected)

  async function handleImport() {
    if (!chosen.length) return
    setImporting(true)
    try {
      const result = await api.importApps(
        chosen.map((row) => ({
          name: row.name.trim(),
          description: row.description,
          command: row.command,
          working_dir: row.working_dir,
          port: row.port,
        }))
      )
      onImported(result)
    } catch (error) {
      onError(error.message)
    } finally {
      setImporting(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal wide" onClick={(event) => event.stopPropagation()}>
        <header className="modal-header">
          <div>
            <h3>Applications found on this machine</h3>
            <p className="modal-sub">
              Edit anything before importing. Names and commands are guesses from the running processes.
            </p>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        <div className="modal-body">
          {loading && <p className="empty-hint pad">Scanning processes and ports…</p>}

          {!loading && rows.length === 0 && (
            <p className="empty-hint pad">
              Nothing found beyond God Father itself. Start your apps, then scan again.
            </p>
          )}

          {!loading &&
            GROUPS.map(({ kind, title, blurb }) => {
              const group = rows.filter((row) => row.kind === kind)
              if (!group.length) return null
              const allOn = group.every((row) => row.selected || row.already_registered || !row.importable)

              return (
                <section key={kind} className="discover-group">
                  <div className="discover-group-head">
                    <div>
                      <h4>
                        {title} <span className="muted">({group.length})</span>
                      </h4>
                      <p className="discover-blurb">{blurb}</p>
                    </div>
                    <button className="btn ghost small" onClick={() => toggleGroup(kind, !allOn)}>
                      {allOn ? 'Deselect all' : 'Select all'}
                    </button>
                  </div>

                  {group.map((row) => {
                    const locked = row.already_registered || !row.importable
                    return (
                      <div key={row.key} className={`discover-row ${row.selected ? 'on' : ''}`}>
                        <input
                          type="checkbox"
                          checked={row.selected}
                          disabled={locked}
                          onChange={(event) => patch(row.key, { selected: event.target.checked })}
                        />

                        <div className="discover-fields">
                          <div className="discover-line">
                            <input
                              className="discover-name"
                              value={row.name}
                              disabled={locked}
                              onChange={(event) => patch(row.key, { name: event.target.value })}
                            />
                            <span className="pill port">:{row.port ?? '—'}</span>
                            {row.extra_ports.map((port) => (
                              <span key={port} className="pill port dim">
                                :{port}
                              </span>
                            ))}
                            {row.pid && <span className="discover-pid">pid {row.pid}</span>}
                            {row.already_registered && (
                              <span className="pill added">already added as {row.registered_as}</span>
                            )}
                          </div>

                          <input
                            className="discover-command mono"
                            value={row.command}
                            disabled={locked}
                            onChange={(event) => patch(row.key, { command: event.target.value })}
                          />

                          <div className="discover-dir mono">{row.working_dir}</div>
                          {row.note && <div className="discover-note">{row.note}</div>}
                        </div>
                      </div>
                    )
                  })}
                </section>
              )
            })}
        </div>

        <footer className="modal-footer">
          <span className="footer-note">
            Importing only registers them. To capture logs, stop an app where it runs now and start it
            here.
          </span>
          <button type="button" className="btn ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={!chosen.length || importing}
            onClick={handleImport}
          >
            {importing ? 'Importing…' : `Import ${chosen.length} selected`}
          </button>
        </footer>
      </div>
    </div>
  )
}
