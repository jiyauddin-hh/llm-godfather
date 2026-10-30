import { useState } from 'react'

/**
 * What the published ports are actually carrying.
 *
 * Everything here is measured on this machine, from each tunnel's own metrics
 * endpoint. cloudflared reports counters that only climb, so the backend
 * samples them on a timer and the rates below are differences between
 * consecutive readings — which is also why a route that has only just gone
 * live shows nothing for the first few seconds.
 *
 * The readings arrive from whoever renders the tab rather than being fetched
 * here, so the compact strip, the per-row sparklines and the full charts are
 * three views of one poll instead of three polls of the same endpoint.
 */

export const WINDOWS = [
  { minutes: 15, label: '15m' },
  { minutes: 60, label: '1h' },
  { minutes: 360, label: '6h' },
  { minutes: 1440, label: '24h' },
]

// 2xx good · 3xx informational · 4xx caller's fault · 5xx ours. Reserved status
// colours, and every segment is labelled so the meaning never rests on hue.
const CODE_TONE = { 2: 'good', 3: 'info', 4: 'warn', 5: 'bad' }

function bytes(value) {
  if (!value) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = value
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size >= 100 || unit === 0 ? Math.round(size) : size.toFixed(1)} ${units[unit]}`
}

function clock(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch {
    return '—'
  }
}

/**
 * One line above the routes table: enough to know whether anything needs
 * attention, and a way in to the charts when it does.
 */
export function TrafficStrip({ data, expanded, onToggle }) {
  if (!data || data.live_routes === 0) return null

  const problem = data.errors > 0

  return (
    <div className={`tstrip ${expanded ? 'open' : ''}`}>
      <span className="tstrip-title">Traffic</span>

      <span className="tstrip-figure">
        <strong>{data.rpm.toFixed(1)}</strong> req/min
      </span>
      <span className="tstrip-figure">
        <strong>{data.requests.toLocaleString()}</strong> requests
      </span>
      <span className={`tstrip-figure ${problem ? 'bad' : ''}`}>
        <strong>{data.errors.toLocaleString()}</strong> errors
      </span>
      <span className="tstrip-figure">
        <strong>{data.concurrent}</strong> in flight
      </span>
      <span className="tstrip-figure">
        <strong>{bytes(data.bytes_sent)}</strong> sent
      </span>

      <span className="tstrip-meta">
        {data.sampling ? `every ${Math.round(data.sample_seconds)}s` : <span className="tdash-stale">sampler stopped</span>}
      </span>

      <button className="btn ghost small" onClick={onToggle}>
        {expanded ? 'Hide charts' : 'Traffic details'}
      </button>
    </div>
  )
}

/**
 * The traffic shape for one route, small enough to live in a table row. The
 * full chart with a crosshair is one click away, so this one carries its
 * readout in a tooltip rather than its own hover layer.
 */
export function RowSparkline({ route }) {
  if (!route || !route.points?.length) {
    return <span className="muted trow-spark-empty">—</span>
  }

  const points = route.points
  const width = 108
  const height = 22
  const peak = Math.max(...points.map((point) => point.rpm), 1)
  const x = (index) =>
    points.length === 1 ? width / 2 : (index * width) / (points.length - 1)
  const y = (value) => height - 2 - (value / peak) * (height - 4)

  const line = points.map((point, index) => `${x(index)},${y(point.rpm)}`).join(' ')

  return (
    <span className="trow-spark">
      <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img">
        <title>
          {`${route.rpm.toFixed(1)} req/min average, peak ${route.peak_rpm.toFixed(1)}`}
        </title>
        <polygon points={`0,${height} ${line} ${width},${height}`} className="tdash-area" />
        <polyline points={line} className="tdash-line" />
      </svg>
      <span className="trow-rpm">{route.rpm.toFixed(1)}</span>
    </span>
  )
}

export default function TunnelDashboard({ data, minutes, onWindowChange }) {
  if (!data) return <p className="empty-hint pad">Reading tunnel metrics…</p>

  if (data.live_routes === 0) {
    return (
      <div className="tdash-idle">
        Nothing is live, so there is nothing to measure yet. Switch a route on and traffic will
        start appearing here within {Math.round(data.sample_seconds)} seconds.
      </div>
    )
  }

  return (
    <div className="tdash">
      <div className="tdash-head">
        <h3>Traffic detail</h3>
        <span className="tdash-meta">
          {data.sampling ? (
            <>
              sampling every {Math.round(data.sample_seconds)}s · last {clock(data.last_sample)} ·
              kept {data.retention_hours}h
            </>
          ) : (
            <span className="tdash-stale">sampler not running</span>
          )}
        </span>
        <div className="seg">
          {WINDOWS.map((window) => (
            <button
              key={window.minutes}
              className={`seg-btn ${minutes === window.minutes ? 'active' : ''}`}
              onClick={() => onWindowChange(window.minutes)}
            >
              {window.label}
            </button>
          ))}
        </div>
      </div>

      <div className="tdash-kpis">
        <Kpi label="Requests" value={data.requests.toLocaleString()} sub={`last ${label(minutes)}`} />
        <Kpi label="Per minute" value={data.rpm.toFixed(1)} sub="average" />
        <Kpi
          label="Errors"
          value={data.errors.toLocaleString()}
          sub={`${data.error_rate.toFixed(1)}% of requests`}
          tone={data.errors > 0 ? 'bad' : ''}
        />
        <Kpi label="In flight" value={data.concurrent} sub="right now" />
        <Kpi label="Sent" value={bytes(data.bytes_sent)} sub={`${bytes(data.bytes_received)} received`} />
      </div>

      {Object.keys(data.codes).length > 0 && <CodeBar codes={data.codes} />}

      <div className="tdash-routes">
        {data.routes.map((route) => (
          <RouteCard key={route.route_id} route={route} minutes={minutes} />
        ))}
      </div>

      <p className="tdash-note">{data.note}</p>
    </div>
  )
}

function label(minutes) {
  return WINDOWS.find((w) => w.minutes === minutes)?.label ?? `${minutes}m`
}

function Kpi({ label: text, value, sub, tone = '' }) {
  return (
    <div className={`tdash-kpi ${tone}`}>
      <span className="tdash-kpi-label">{text}</span>
      <span className="tdash-kpi-value">{value}</span>
      <span className="tdash-kpi-sub">{sub}</span>
    </div>
  )
}

/** Responses by status class, as one bar with every segment labelled. */
function CodeBar({ codes }) {
  const entries = Object.entries(codes)
  const total = entries.reduce((sum, [, count]) => sum + count, 0)
  if (!total) return null

  const grouped = {}
  for (const [code, count] of entries) {
    const family = `${code[0]}xx`
    grouped[family] = (grouped[family] ?? 0) + count
  }

  return (
    <div className="tdash-codes">
      <div className="tdash-codebar">
        {Object.entries(grouped).map(([family, count]) => (
          <div
            key={family}
            className={`tdash-codeseg ${CODE_TONE[Number(family[0])] ?? 'info'}`}
            style={{ flexGrow: count }}
            title={`${count} × ${family}`}
          />
        ))}
      </div>
      <ul className="tdash-codelist">
        {Object.entries(grouped).map(([family, count]) => (
          <li key={family}>
            <span className={`tdash-dot ${CODE_TONE[Number(family[0])] ?? 'info'}`} />
            {family}
            <strong>{count.toLocaleString()}</strong>
            <span className="muted">{Math.round((count * 100) / total)}%</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function RouteCard({ route, minutes }) {
  return (
    <div className="tdash-route">
      <div className="tdash-route-head">
        <div className="tdash-route-name">
          <strong>{route.label}</strong>
          <span className="mono muted">:{route.port}</span>
          {!route.reporting && (
            <span className="pill" title="The tunnel is up but its metrics endpoint is not answering">
              not reporting
            </span>
          )}
        </div>
        {route.public_url && (
          <a className="cf-url" href={route.public_url} target="_blank" rel="noreferrer">
            {route.public_url.replace(/^https:\/\//, '')}
          </a>
        )}
      </div>

      <Sparkline points={route.points} />

      <ul className="tdash-facts">
        <li>
          <span>Requests</span>
          <strong>{route.requests.toLocaleString()}</strong>
        </li>
        <li>
          <span>Per min</span>
          <strong>{route.rpm.toFixed(1)}</strong>
        </li>
        <li>
          <span>Peak</span>
          <strong>{route.peak_rpm.toFixed(1)}/min</strong>
        </li>
        <li className={route.errors ? 'bad' : ''}>
          <span>Errors</span>
          <strong>{route.errors.toLocaleString()}</strong>
        </li>
        <li>
          <span>In flight</span>
          <strong>{route.concurrent}</strong>
        </li>
        <li>
          <span>Sent</span>
          <strong>{bytes(route.bytes_sent)}</strong>
        </li>
        <li>
          <span>Edge</span>
          <strong>{route.edge_location || '—'}</strong>
        </li>
        <li>
          <span>Round trip</span>
          <strong>{route.rtt_ms ? `${route.rtt_ms} ms` : '—'}</strong>
        </li>
        <li>
          <span>Connectors</span>
          <strong>{route.connections}</strong>
        </li>
      </ul>

      {route.points.length === 0 && (
        <p className="tdash-empty">
          No readings in the last {label(minutes)} — either this route has just gone live, or
          nothing has reached it.
        </p>
      )}
    </div>
  )
}

/**
 * Requests per minute over the window. One series, so no legend — the card
 * heading names it. Hovering reads out the bucket under the pointer.
 */
function Sparkline({ points }) {
  const [hover, setHover] = useState(null)

  if (!points.length) return <div className="tdash-spark empty" />

  const width = 600
  const height = 56
  const pad = 3
  const peak = Math.max(...points.map((point) => point.rpm), 1)

  const x = (index) =>
    points.length === 1 ? width / 2 : pad + (index * (width - pad * 2)) / (points.length - 1)
  const y = (value) => height - pad - (value / peak) * (height - pad * 2)

  const line = points.map((point, index) => `${x(index)},${y(point.rpm)}`).join(' ')
  const area = `${pad},${height - pad} ${line} ${x(points.length - 1)},${height - pad}`

  function track(event) {
    const box = event.currentTarget.getBoundingClientRect()
    const ratio = (event.clientX - box.left) / box.width
    const index = Math.round(ratio * (points.length - 1))
    if (index >= 0 && index < points.length) setHover({ index, ratio })
  }

  const active = hover ? points[hover.index] : null

  return (
    <div className="tdash-spark">
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
        {active && (
          <>
            <line x1={x(hover.index)} y1={pad} x2={x(hover.index)} y2={height - pad} className="tdash-cross" />
            <circle cx={x(hover.index)} cy={y(active.rpm)} r="4" className="tdash-marker" />
          </>
        )}
      </svg>

      <span className="tdash-peak">peak {peak.toFixed(1)}/min</span>

      {active && (
        <div className="tdash-tip" style={{ left: `${Math.min(Math.max(hover.ratio * 100, 6), 94)}%` }}>
          <strong>{active.rpm.toFixed(1)}/min</strong>
          <span>{clock(active.ts)}</span>
          {active.errors > 0 && <span className="bad">{active.errors} errors</span>}
          <span className="muted">{bytes(active.bytes_sent)} sent</span>
        </div>
      )}
    </div>
  )
}
