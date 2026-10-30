import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { formatUptime, STATUS_LABEL } from '../utils.js'

/**
 * Traffic, errors and load per application — the Analytics tab of the wall.
 *
 * Request counts come from the access lines each app already prints into its
 * captured log, so an app whose server prints none is listed as *not counted*
 * rather than shown as idle. Response time only exists where the server logs
 * a duration; everywhere else it says "not logged" instead of a number.
 */

const WINDOWS = [
  { minutes: 60, label: '1h' },
  { minutes: 360, label: '6h' },
  { minutes: 1440, label: '24h' },
  { minutes: 10080, label: '7d' },
]

const CODE_TONE = { s2xx: 'good', s3xx: 'info', s4xx: 'warn', s5xx: 'bad' }
const CODE_LABEL = { s2xx: '2xx', s3xx: '3xx', s4xx: '4xx', s5xx: '5xx' }
const SOURCE_LABEL = { loopback: 'This machine', lan: 'Local network', external: 'Internet' }
const SOURCE_TONE = { loopback: 'info', lan: 'good', external: 'warn' }

function count(value) {
  if (value == null) return '—'
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 10_000) return `${(value / 1000).toFixed(1)}k`
  return value.toLocaleString()
}

function perMinute(value) {
  if (!value) return '0'
  return value >= 10 ? Math.round(value).toLocaleString() : value.toFixed(1)
}

function ms(value) {
  if (value == null) return null
  return value >= 1000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value)} ms`
}

function memory(mb) {
  if (mb == null) return '—'
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`
}

function stamp(iso, minutes) {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return minutes > 1440
    ? date.toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false })
    : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
}

function ago(iso) {
  if (!iso) return 'never'
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000
  if (seconds < 90) return 'just now'
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`
  return `${Math.round(seconds / 86400)} d ago`
}

function windowLabel(minutes) {
  return WINDOWS.find((choice) => choice.minutes === minutes)?.label ?? `${minutes}m`
}

export default function AnalyticsView({ onSelectApp, onError }) {
  const [minutes, setMinutes] = useState(60)
  const [data, setData] = useState(null)
  const [failed, setFailed] = useState(null)
  const [scanning, setScanning] = useState(false)
  // The window asked for most recently. A slow answer for the previous window
  // must not land on top of the one just picked.
  const wanted = useRef(minutes)

  const load = useCallback(async () => {
    wanted.current = minutes
    try {
      const next = await api.traffic(minutes)
      if (wanted.current !== minutes) return
      setData(next)
      setFailed(null)
    } catch (error) {
      if (wanted.current === minutes) setFailed(error.message)
    }
  }, [minutes])

  useEffect(() => {
    load()
    const timer = setInterval(load, 10000)
    return () => clearInterval(timer)
  }, [load])

  async function scanNow() {
    setScanning(true)
    try {
      const next = await api.trafficScan(minutes)
      if (wanted.current === minutes) setData(next)
    } catch (error) {
      onError(error.message)
    } finally {
      setScanning(false)
    }
  }

  if (!data && failed) {
    return (
      <div className="an">
        <div className="banner error">
          <strong>The backend does not have Analytics yet.</strong> Restart God Father, then reload
          this page:
        </div>
        <pre className="cf-config">systemctl --user restart godfather</pre>
        <p className="cf-hint">{failed}</p>
      </div>
    )
  }

  if (!data) return <p className="empty-hint pad">Reading application logs…</p>

  const totals = data.totals
  const counted = data.apps.filter((app) => app.counted)
  const uncounted = data.apps.filter((app) => !app.counted)
  const busiest = counted.find((app) => app.requests > 0)
  const slowest = [...counted].filter((app) => app.p95_ms != null).sort((a, b) => b.p95_ms - a.p95_ms)[0]
  const mostErrors = [...counted]
    .filter((app) => app.errors > 0)
    .sort((a, b) => b.error_rate - a.error_rate)[0]
  const names = Object.fromEntries(data.apps.map((app) => [app.app_id, app.name]))
  // From the data, not the picker: until the new window arrives, the numbers
  // on screen still belong to the old one and must be labelled as such.
  const shown = data.window_minutes
  const label = windowLabel(shown)

  return (
    <div className="an">
      <div className="tdash-head">
        <h3>Traffic across every application</h3>
        <span className="tdash-meta">
          {data.monitoring ? (
            `logs read every ${Math.round(data.sample_seconds)}s` +
            (data.last_scan ? ` · last ${stamp(data.last_scan, 0)}` : '')
          ) : (
            <span className="tdash-stale">log reader not running</span>
          )}
        </span>
        <div className="seg">
          {WINDOWS.map((choice) => (
            <button
              key={choice.minutes}
              className={`seg-btn ${minutes === choice.minutes ? 'active' : ''}`}
              onClick={() => setMinutes(choice.minutes)}
            >
              {choice.label}
            </button>
          ))}
        </div>
        <button className="btn ghost small" onClick={scanNow} disabled={scanning} title="Read new log lines now">
          {scanning ? 'Reading…' : '⟳ Scan now'}
        </button>
      </div>

      {data.last_error && <div className="banner warn">Log reader: {data.last_error}</div>}

      <div className="tdash-kpis">
        <Kpi label={`Requests · ${label}`} value={count(totals.requests)} sub={`across ${totals.apps_active} of ${totals.apps_counted} counted apps`} />
        <Kpi label="Right now" value={`${perMinute(totals.rpm_now)}/min`} sub="last 5 minutes" />
        <Kpi label="Average" value={`${perMinute(totals.rpm)}/min`} sub={`over ${label}`} />
        <Kpi
          label="Error rate"
          value={`${totals.error_rate}%`}
          sub={`${count(totals.s4xx)} 4xx · ${count(totals.s5xx)} 5xx`}
          tone={totals.error_rate >= 5 ? 'bad' : ''}
        />
        <Kpi
          label="Server errors"
          value={count(totals.s5xx)}
          sub={totals.requests ? `${totals.server_error_rate}% of requests` : 'no requests'}
          tone={totals.s5xx ? 'bad' : ''}
        />
        <Kpi
          label="Response p95"
          value={ms(totals.p95_ms) ?? 'not logged'}
          sub={totals.timed ? `avg ${ms(totals.avg_ms)} · ${count(totals.timed)} timed` : 'no server prints a duration'}
          dim={totals.p95_ms == null}
        />
        <Kpi
          label="Apps"
          value={`${totals.running} up`}
          sub={
            `${totals.stopped} stopped · ${totals.crashed} crashed` +
            (totals.crashes ? ` · ${totals.crashes} crashes in ${label}` : '')
          }
          tone={totals.crashed ? 'bad' : ''}
        />
        <Kpi label="CPU · memory" value={`${totals.cpu_percent}%`} sub={`${memory(totals.memory_mb)} in managed apps`} />
      </div>

      <div className="an-highlights">
        <Highlight
          title="Busiest"
          app={busiest}
          value={busiest ? `${busiest.share}% of traffic` : null}
          sub={busiest ? `${count(busiest.requests)} requests` : 'no traffic in this window'}
          onSelect={onSelectApp}
        />
        <Highlight
          title="Most errors"
          app={mostErrors}
          value={mostErrors ? `${mostErrors.error_rate}% failing` : null}
          sub={mostErrors ? `${count(mostErrors.errors)} of ${count(mostErrors.requests)}` : 'no failed requests'}
          onSelect={onSelectApp}
          tone={mostErrors ? 'bad' : 'good'}
        />
        <Highlight
          title="Slowest"
          app={slowest}
          value={slowest ? `p95 ${ms(slowest.p95_ms)}` : null}
          sub={slowest ? `avg ${ms(slowest.avg_ms)}` : 'no app logs its response time'}
          onSelect={onSelectApp}
        />
      </div>

      <section className="an-panel">
        <div className="an-panel-head">
          <strong>Requests over time</strong>
          <span className="muted">all counted apps · {data.bucket_minutes >= 1 ? `${Math.round(data.bucket_minutes)} min` : '1 min'} buckets</span>
        </div>
        <TrafficChart points={data.points} minutes={shown} bucket={data.bucket_minutes} />
      </section>

      <div className="an-split">
        <section className="an-panel">
          <div className="an-panel-head">
            <strong>Response codes</strong>
          </div>
          <SplitBar
            parts={['s2xx', 's3xx', 's4xx', 's5xx'].map((key) => ({
              key,
              label: CODE_LABEL[key],
              tone: CODE_TONE[key],
              value: totals[key],
            }))}
          />
          <div className="an-panel-head spaced">
            <strong>Where requests come from</strong>
          </div>
          <SplitBar
            parts={['loopback', 'lan', 'external'].map((key) => ({
              key,
              label: SOURCE_LABEL[key],
              tone: SOURCE_TONE[key],
              value: totals[key],
            }))}
          />
        </section>

        <section className="an-panel">
          <div className="an-panel-head">
            <strong>Share of load</strong>
            <span className="muted">requests · {label}</span>
          </div>
          <ShareList apps={counted.filter((app) => app.requests > 0)} onSelect={onSelectApp} />
        </section>
      </div>

      <div className="an-split">
        {shown >= 360 && (
          <section className="an-panel">
            <div className="an-panel-head">
              <strong>Busiest hours</strong>
              <span className="muted">requests by hour of day · {label}</span>
            </div>
            <HourBars hours={data.by_hour} />
          </section>
        )}

        <section className="an-panel">
          <div className="an-panel-head">
            <strong>Top endpoints</strong>
            <span className="muted">IDs in paths grouped as :id</span>
          </div>
          {data.top_paths.length === 0 ? (
            <p className="tdash-empty">No requests in the last {label}.</p>
          ) : (
            <table className="an-table">
              <tbody>
                {data.top_paths.map((row) => (
                  <tr key={`${row.app_id}-${row.method}-${row.path}`}>
                    <td className="mono an-method">{row.method}</td>
                    <td className="mono an-path" title={row.path}>{row.path}</td>
                    <td className="muted an-app">{names[row.app_id] ?? '—'}</td>
                    <td className="num">{count(row.requests)}</td>
                    <td className={`num ${row.errors ? 'bad' : 'muted'}`}>
                      {row.errors ? `${count(row.errors)} err` : ''}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>

      <div className="an-panel-head spaced">
        <strong>Per application</strong>
        <span className="muted">{counted.length} counted · busiest first</span>
      </div>

      {counted.length === 0 ? (
        <p className="tdash-empty">
          No application has printed a request line yet. Start a web app from God Father and use it — its
          access log is what gets counted.
        </p>
      ) : (
        <div className="tdash-routes">
          {counted.map((app) => (
            <AppCard key={app.app_id} app={app} minutes={shown} onSelect={onSelectApp} />
          ))}
        </div>
      )}

      {uncounted.length > 0 && (
        <details className="an-uncounted">
          <summary>
            {uncounted.length} not counted — these apps have printed no request lines, so their traffic is
            unknown rather than zero
          </summary>
          <ul>
            {uncounted.map((app) => (
              <li key={app.app_id}>
                <span className={`dot ${app.status}`} />
                <button className="tile-name" onClick={() => onSelectApp(app.app_id)}>
                  {app.name}
                </button>
                <span className="muted">{app.note}</span>
              </li>
            ))}
          </ul>
        </details>
      )}

      <p className="tdash-note">
        Counted from the access log each application prints, as captured by God Father — so only apps
        started here, and only servers that log requests (uvicorn, Flask, gunicorn, Express/morgan, gin…).
        Response time appears only where the server prints one; uvicorn does not. The wall's own
        reachability checks are left out. History is kept for {data.retention_days} days.
      </p>
    </div>
  )
}

function Kpi({ label, value, sub, tone = '', dim = false }) {
  return (
    <div className={`tdash-kpi ${tone} ${dim ? 'dim' : ''}`}>
      <span className="tdash-kpi-label">{label}</span>
      <span className="tdash-kpi-value">{value}</span>
      <span className="tdash-kpi-sub">{sub}</span>
    </div>
  )
}

function Highlight({ title, app, value, sub, onSelect, tone = '' }) {
  return (
    <div className={`an-highlight ${tone}`}>
      <span className="tdash-kpi-label">{title}</span>
      {app ? (
        <button className="an-highlight-name" onClick={() => onSelect(app.app_id)} title="Open this app">
          {app.name}
        </button>
      ) : (
        <span className="an-highlight-name none">—</span>
      )}
      {value && <strong>{value}</strong>}
      <span className="tdash-kpi-sub">{sub}</span>
    </div>
  )
}

/** One bar split into labelled parts; the list below it carries the numbers. */
function SplitBar({ parts }) {
  const total = parts.reduce((sum, part) => sum + (part.value || 0), 0)
  if (!total) return <p className="tdash-empty">Nothing counted in this window.</p>

  return (
    <div className="tdash-codes">
      <div className="tdash-codebar">
        {parts
          .filter((part) => part.value)
          .map((part) => (
            <div
              key={part.key}
              className={`tdash-codeseg ${part.tone}`}
              style={{ flexGrow: part.value }}
              title={`${part.label}: ${part.value.toLocaleString()}`}
            />
          ))}
      </div>
      <ul className="tdash-codelist">
        {parts.map((part) => (
          <li key={part.key}>
            <span className={`tdash-dot ${part.tone}`} />
            {part.label}
            <strong>{count(part.value)}</strong>
            <span className="muted">{Math.round(((part.value || 0) * 100) / total)}%</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function ShareList({ apps, onSelect }) {
  if (!apps.length) return <p className="tdash-empty">No traffic in this window.</p>
  const top = apps[0].requests

  return (
    <ul className="an-share">
      {apps.slice(0, 10).map((app) => (
        <li key={app.app_id}>
          <button className="an-share-name" onClick={() => onSelect(app.app_id)} title={app.name}>
            {app.name}
          </button>
          <span className="an-share-track">
            <span className="an-share-fill" style={{ width: `${(app.requests * 100) / top}%` }} />
          </span>
          <span className="num">{count(app.requests)}</span>
          <span className="num muted">{app.share}%</span>
        </li>
      ))}
    </ul>
  )
}

function HourBars({ hours }) {
  const peak = Math.max(...hours, 1)
  const busiest = hours.indexOf(Math.max(...hours))

  return (
    <div className="an-hours">
      <div className="an-hours-bars">
        {hours.map((value, hour) => (
          <span
            key={hour}
            className={`an-hour ${hour === busiest && value ? 'peak' : ''}`}
            style={{ height: `${Math.max(2, (value * 100) / peak)}%` }}
            title={`${String(hour).padStart(2, '0')}:00 — ${value.toLocaleString()} requests`}
          />
        ))}
      </div>
      <div className="an-hours-axis">
        {[0, 6, 12, 18, 23].map((hour) => (
          <span key={hour} style={{ left: `${((hour + 0.5) * 100) / 24}%` }}>
            {String(hour).padStart(2, '0')}
          </span>
        ))}
      </div>
      {hours.some(Boolean) && (
        <p className="tdash-empty">
          Busiest at {String(busiest).padStart(2, '0')}:00 with {hours[busiest].toLocaleString()} requests.
        </p>
      )}
    </div>
  )
}

/** Requests per minute for everything counted, with failed requests beneath. */
function TrafficChart({ points, minutes, bucket }) {
  const [hover, setHover] = useState(null)
  if (!points.length || !points.some((point) => point.requests)) {
    return <p className="tdash-empty">No requests in this window.</p>
  }

  const width = 600
  const height = 120
  const pad = 4
  const peak = Math.max(...points.map((point) => point.rpm), 1)

  const x = (index) => pad + (index * (width - pad * 2)) / Math.max(1, points.length - 1)
  const y = (value) => height - pad - (value / peak) * (height - pad * 2)
  const line = points.map((point, index) => `${x(index)},${y(point.rpm)}`).join(' ')
  const area = `${pad},${height - pad} ${line} ${x(points.length - 1)},${height - pad}`
  const errorLine = points
    .map((point, index) => `${x(index)},${y(point.requests ? (point.errors * point.rpm) / point.requests : 0)}`)
    .join(' ')
  const hasErrors = points.some((point) => point.errors)

  function track(event) {
    const box = event.currentTarget.getBoundingClientRect()
    const ratio = (event.clientX - box.left) / box.width
    const index = Math.round(ratio * (points.length - 1))
    if (index >= 0 && index < points.length) setHover({ index, ratio })
  }

  const active = hover ? points[hover.index] : null

  return (
    <div className="tdash-spark an-chart">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`Requests per minute, peaking at ${peak.toFixed(1)}`}
        onMouseMove={track}
        onMouseLeave={() => setHover(null)}
      >
        <polygon points={area} className="tdash-area" />
        <polyline points={line} className="tdash-line" />
        {hasErrors && <polyline points={errorLine} className="an-error-line" />}
        {active && (
          <>
            <line x1={x(hover.index)} y1={pad} x2={x(hover.index)} y2={height - pad} className="tdash-cross" />
            <circle cx={x(hover.index)} cy={y(active.rpm)} r="4" className="tdash-marker" />
          </>
        )}
      </svg>

      <span className="tdash-peak">peak {perMinute(peak)}/min</span>
      <div className="an-chart-axis">
        <span>{stamp(points[0].ts, minutes)}</span>
        <span className="an-legend">
          <span className="tdash-dot info" /> requests/min
          {hasErrors && (
            <>
              <span className="tdash-dot bad" /> failed
            </>
          )}
        </span>
        <span>now</span>
      </div>

      {active && (
        <div className="tdash-tip" style={{ left: `${Math.min(Math.max(hover.ratio * 100, 8), 92)}%` }}>
          <strong>{perMinute(active.rpm)}/min</strong>
          <span>{stamp(active.ts, minutes)}</span>
          <span className="muted">{active.requests.toLocaleString()} requests{bucket > 1 ? ` in ${Math.round(bucket)} min` : ''}</span>
          {active.errors > 0 && <span className="bad">{active.errors.toLocaleString()} failed</span>}
        </div>
      )}
    </div>
  )
}

function Spark({ values }) {
  if (!values.length || !values.some(Boolean)) return <div className="tdash-spark empty an-mini" />
  const width = 300
  const height = 36
  const peak = Math.max(...values, 1)
  const x = (index) => (index * width) / Math.max(1, values.length - 1)
  const y = (value) => height - 2 - (value / peak) * (height - 4)
  const line = values.map((value, index) => `${x(index)},${y(value)}`).join(' ')

  return (
    <div className="tdash-spark an-mini">
      <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true">
        <polygon points={`0,${height} ${line} ${width},${height}`} className="tdash-area" />
        <polyline points={line} className="tdash-line" />
      </svg>
    </div>
  )
}

function AppCard({ app, minutes, onSelect }) {
  const latency = ms(app.p95_ms)

  return (
    <div className="tdash-route">
      <div className="tdash-route-head">
        <div className="tdash-route-name">
          <span className={`dot ${app.status}`} title={STATUS_LABEL[app.status] ?? app.status} />
          <button className="an-card-name" onClick={() => onSelect(app.app_id)} title="Open this app">
            {app.name}
          </button>
          <span className="mono muted">:{app.port ?? '—'}</span>
        </div>
        <span className="tdash-meta">last request {ago(app.last_request)}</span>
      </div>

      <Spark values={app.spark} />

      <ul className="tdash-facts">
        <li>
          <span>Requests</span>
          <strong>{count(app.requests)}</strong>
        </li>
        <li>
          <span>Now</span>
          <strong>{perMinute(app.rpm_now)}/min</strong>
        </li>
        <li>
          <span>Share</span>
          <strong>{app.share}%</strong>
        </li>
        <li className={app.error_rate >= 5 ? 'bad' : ''}>
          <span>Errors</span>
          <strong>
            {count(app.errors)} <small>({app.error_rate}%)</small>
          </strong>
        </li>
        <li className={app.s5xx ? 'bad' : ''}>
          <span>5xx</span>
          <strong>{count(app.s5xx)}</strong>
        </li>
        <li className={latency ? '' : 'dim'}>
          <span>p95</span>
          <strong>{latency ?? 'not logged'}</strong>
        </li>
        <li>
          <span>CPU</span>
          <strong>{app.cpu_percent != null ? `${app.cpu_percent}%` : '—'}</strong>
        </li>
        <li>
          <span>Memory</span>
          <strong>{memory(app.memory_mb)}</strong>
        </li>
        <li className={app.crashes ? 'bad' : ''}>
          <span>Uptime</span>
          <strong>
            {formatUptime(app.uptime_seconds)}
            {app.crashes ? <small> · {app.crashes} crash{app.crashes > 1 ? 'es' : ''}</small> : null}
          </strong>
        </li>
      </ul>

      {app.top_paths.length > 0 ? (
        <ul className="an-card-paths">
          {app.top_paths.slice(0, 3).map((row) => (
            <li key={`${row.method}-${row.path}`}>
              <span className="mono an-method">{row.method}</span>
              <span className="mono an-path" title={row.path}>{row.path}</span>
              <span className="num">{count(row.requests)}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="tdash-empty">No requests in the last {windowLabel(minutes)}.</p>
      )}
    </div>
  )
}
