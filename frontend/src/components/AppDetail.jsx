import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { formatDateTime, formatUptime, STATUS_LABEL } from '../utils.js'
import LogViewer from './LogViewer.jsx'
import SharePanel from './SharePanel.jsx'
import TakeoverDialog from './TakeoverDialog.jsx'

export default function AppDetail({
  app,
  apps = [],
  network,
  onChanged,
  onEdit,
  onDelete,
  onFocus,
  focusActive,
  onError,
  onNotice,
}) {
  const [tab, setTab] = useState('logs')
  const [busy, setBusy] = useState(false)
  const [takingOver, setTakingOver] = useState(false)
  const [events, setEvents] = useState([])

  useEffect(() => setTab('logs'), [app.id])

  useEffect(() => {
    if (tab !== 'events') return
    api.events(app.id).then(setEvents).catch((error) => onError(error.message))
  }, [tab, app.id, app.runtime.status, onError])

  async function act(action, label) {
    setBusy(true)
    try {
      const result = await action(app.id)
      onNotice(result?.message ?? `${app.name} ${label}`)
      onChanged()
    } catch (error) {
      onError(error.message)
    } finally {
      setBusy(false)
    }
  }

  const { runtime } = app
  const running = runtime.status === 'running'

  return (
    <section className="detail">
      <header className="detail-header">
        <div className="detail-title">
          <span className={`dot large ${runtime.status}`} />
          <div>
            <h2>
              {app.name}
              {app.protected && (
                <span className="lock" title="Protected — Focus Mode never stops this app">
                  🔒
                </span>
              )}
            </h2>
            <p className="detail-sub">
              {app.description || <span className="muted">No description</span>}
            </p>
          </div>
        </div>

        <div className="detail-actions">
          {running ? (
            <button className="btn danger" disabled={busy} onClick={() => act(api.stop, 'stopped')}>
              ■ Stop
            </button>
          ) : runtime.status === 'external' ? (
            <button className="btn warn" onClick={() => setTakingOver(true)}>
              ⚡ Take over
            </button>
          ) : (
            <button className="btn success" disabled={busy} onClick={() => act(api.start, 'started')}>
              ▶ Start
            </button>
          )}
          <button className="btn" disabled={busy} onClick={() => act(api.restart, 'restarted')}>
            ⟳ Restart
          </button>
          <button
            className="btn focus"
            disabled={focusActive}
            title={
              focusActive
                ? 'Focus Mode is already on — exit it first'
                : 'Stop everything this app does not need, and free the GPU for it'
            }
            onClick={() => onFocus(app)}
          >
            ◎ Focus on this app
          </button>
          <button className="btn ghost" onClick={() => onEdit(app)}>
            Edit
          </button>
          <button className="btn ghost danger" onClick={() => onDelete(app)}>
            Delete
          </button>
        </div>
      </header>

      <div className="stat-row">
        <Stat label="Status" value={STATUS_LABEL[runtime.status]} tone={runtime.status} />
        <Stat label="Port" value={app.port ? String(app.port) : '—'} tone={runtime.port_open ? 'running' : ''} />
        <Stat
          label="Network"
          value={app.share_lan ? 'Shared on LAN' : 'Local only'}
          tone={app.share_lan ? 'external' : ''}
        />
        <Stat label="PID" value={runtime.pid ?? '—'} />
        <Stat label="Uptime" value={formatUptime(runtime.uptime_seconds)} />
        <Stat label="CPU" value={runtime.cpu_percent != null ? `${runtime.cpu_percent}%` : '—'} />
        <Stat label="Memory" value={runtime.memory_mb != null ? `${runtime.memory_mb} MB` : '—'} />
        <Stat label="Restarts" value={runtime.restart_count} />
      </div>

      {runtime.last_error && <div className="banner error">{runtime.last_error}</div>}
      {runtime.status === 'crashed' && (
        <div className="banner warn">
          Exited with code {runtime.exit_code}. Check the log below for the cause.
        </div>
      )}
      {runtime.status === 'external' && (
        <div className="banner info">
          <strong>Running outside God Father.</strong> Port {app.port} is being served
          {runtime.pid ? ` by pid ${runtime.pid}` : ''}, but this process was started somewhere else —
          its output goes to whichever terminal launched it, so <strong>no logs are captured here</strong>.
          To read its logs: stop it where it is running now, then press Start — or let God Father do
          both for you.
          <button className="btn warn small banner-action" onClick={() => setTakingOver(true)}>
            ⚡ Take over port {app.port}
          </button>
        </div>
      )}

      <div className="tabs">
        {['logs', 'overview', 'network', 'events'].map((name) => (
          <button
            key={name}
            className={`tab ${tab === name ? 'active' : ''}`}
            onClick={() => setTab(name)}
          >
            {name[0].toUpperCase() + name.slice(1)}
          </button>
        ))}
      </div>

      <div className="tab-body">
        {tab === 'logs' && <LogViewer appId={app.id} onError={onError} />}

        {tab === 'overview' && (
          <dl className="fields">
            <Field label="Command"><code>{app.command}</code></Field>
            <Field label="Working directory"><code>{app.working_dir}</code></Field>
            <Field label="Port">{app.port ?? '—'}</Field>
            <Field label="Auto-restart">{app.auto_restart ? 'Enabled' : 'Disabled'}</Field>
            <Field label="Start on boot">
              {app.start_on_boot ? 'Yes — starts with God Father' : 'No'}
            </Field>
            <Field label="Needs (Focus Mode)">
              {app.needs.length === 0 ? (
                <span className="muted">Nothing</span>
              ) : (
                app.needs.map((id) => apps.find((other) => other.id === id)?.name ?? `#${id}`).join(', ')
              )}
            </Field>
            <Field label="Uses AI models">{app.uses_models || <span className="muted">None set</span>}</Field>
            <Field label="Protected">
              {app.protected ? 'Yes — Focus Mode never stops it' : 'No'}
            </Field>
            <Field label="Started at">{formatDateTime(runtime.started_at)}</Field>
            <Field label="Last exit code">{runtime.exit_code ?? '—'}</Field>
            <Field label="Created">{formatDateTime(app.created_at)}</Field>
            <Field label="Updated">{formatDateTime(app.updated_at)}</Field>
            <Field label="Environment">
              {Object.keys(app.env).length === 0 ? (
                <span className="muted">None</span>
              ) : (
                <ul className="env-list">
                  {Object.entries(app.env).map(([key, value]) => (
                    <li key={key}>
                      <code>{key}</code>=<code>{value}</code>
                    </li>
                  ))}
                </ul>
              )}
            </Field>
          </dl>
        )}

        {tab === 'network' && (
          <SharePanel
            app={app}
            network={network}
            onChanged={onChanged}
            onError={onError}
            onNotice={onNotice}
          />
        )}

        {tab === 'events' && (
          <div className="events">
            {events.length === 0 ? (
              <p className="empty-hint pad">No events recorded yet.</p>
            ) : (
              events.map((event) => (
                <div key={event.id} className="event-row">
                  <span className={`event-type ${event.type}`}>{event.type}</span>
                  <span className="event-msg">{event.message}</span>
                  <span className="event-time">{formatDateTime(event.created_at)}</span>
                </div>
              ))
            )}
          </div>
        )}
      </div>

      {takingOver && (
        <TakeoverDialog
          app={app}
          onClose={() => setTakingOver(false)}
          onError={onError}
          onNotice={onNotice}
          onDone={() => {
            setTakingOver(false)
            onChanged()
          }}
        />
      )}
    </section>
  )
}

function Stat({ label, value, tone = '' }) {
  return (
    <div className={`stat ${tone}`}>
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
    </div>
  )
}

function Field({ label, children }) {
  return (
    <div className="field">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  )
}
