import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'

/**
 * What the machine is doing, and which application is responsible.
 *
 * The gauges answer "is it loaded right now"; the chart answers "when", which
 * is the only way to see a handoff — a model moving into VRAM, memory filling
 * while the GPU sits idle. RAM and VRAM share one timeline deliberately, so
 * the two can be read against each other rather than on separate charts.
 */

const WINDOWS = [
  { minutes: 15, label: '15m' },
  { minutes: 60, label: '1h' },
  { minutes: 360, label: '6h' },
  { minutes: 1440, label: '24h' },
]

const TABS = [
  { key: 'cpu', label: 'By CPU' },
  { key: 'memory', label: 'By memory' },
  { key: 'vram', label: 'By VRAM' },
]

function bytes(value, digits = 1) {
  if (!value) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = value
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${unit === 0 ? Math.round(size) : size.toFixed(digits)} ${units[unit]}`
}

function rate(value) {
  return `${bytes(value, 0)}/s`
}

function duration(seconds) {
  if (!seconds) return '—'
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const mins = Math.floor((seconds % 3600) / 60)
  if (days) return `${days}d ${hours}h`
  if (hours) return `${hours}h ${mins}m`
  return `${mins}m`
}

function clock(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  } catch {
    return '—'
  }
}

/** Pressure, not preference: green until it matters, red when it does. */
function tone(percent) {
  if (percent >= 90) return 'bad'
  if (percent >= 75) return 'warn'
  return ''
}

export default function SystemView({ onError }) {
  const [minutes, setMinutes] = useState(60)
  const [data, setData] = useState(null)
  const [tab, setTab] = useState('cpu')
  const [failed, setFailed] = useState(null)

  const load = useCallback(async () => {
    try {
      setData(await api.system(minutes))
      setFailed(null)
    } catch (error) {
      setFailed(error.message)
    }
  }, [minutes])

  useEffect(() => {
    load()
    const timer = setInterval(load, 5000)
    return () => clearInterval(timer)
  }, [load])

  if (!data && failed) {
    return (
      <section className="detail">
        <header className="detail-header">
          <div className="detail-title">
            <div>
              <h2>System</h2>
              <p className="detail-sub">Could not read this machine.</p>
            </div>
          </div>
          <div className="detail-actions">
            <button className="btn ghost" onClick={load}>
              ⟳ Try again
            </button>
          </div>
        </header>
        <div className="banner error">
          <strong>The backend does not have this feature yet.</strong> Restart God Father, then
          reload this page:
        </div>
        <pre className="cf-config">systemctl --user restart godfather</pre>
        <p className="cf-hint">{failed}</p>
      </section>
    )
  }

  if (!data) return <section className="detail"><p className="empty-hint pad">Reading the machine…</p></section>

  if (!data.available) {
    return (
      <section className="detail">
        <header className="detail-header">
          <div className="detail-title">
            <div>
              <h2>System</h2>
            </div>
          </div>
        </header>
        <div className="banner error">{data.unavailable_reason}</div>
      </section>
    )
  }

  const now = data.now
  const gpu = now.gpu
  const vramPercent = gpu?.vram_total ? (gpu.vram_used * 100) / gpu.vram_total : 0
  const rows = tab === 'cpu' ? data.top_cpu : tab === 'memory' ? data.top_memory : data.top_vram

  return (
    <section className="detail">
      <header className="detail-header">
        <div className="detail-title">
          <div>
            <h2>System</h2>
            <p className="detail-sub">
              {data.process_count} processes · up {duration(now.uptime_seconds)} ·{' '}
              {data.monitoring
                ? `sampling every ${Math.round(data.sample_seconds)}s, kept ${data.retention_hours}h`
                : 'sampler not running'}
            </p>
          </div>
        </div>
        <div className="detail-actions">
          <div className="seg">
            {WINDOWS.map((window) => (
              <button
                key={window.minutes}
                className={`seg-btn ${minutes === window.minutes ? 'active' : ''}`}
                onClick={() => setMinutes(window.minutes)}
              >
                {window.label}
              </button>
            ))}
          </div>
        </div>
      </header>

      {failed && (
        <div className="banner warn">
          <strong>Not refreshing.</strong> {failed} — showing the last good reading.
        </div>
      )}

      <div className="sysgauges">
        <Gauge
          title="CPU"
          percent={now.cpu_percent}
          headline={`${now.cpu_percent.toFixed(0)}%`}
          facts={[
            `${now.cpu_count} threads · ${now.cpu_physical} cores`,
            `${(now.cpu_freq_mhz / 1000).toFixed(2)} GHz · ${now.cpu_temp || '—'}°C`,
            `load ${now.load1} / ${now.load5} / ${now.load15}`,
          ]}
        />
        <Gauge
          title="Memory"
          percent={now.mem_percent}
          headline={bytes(now.mem_used)}
          facts={[
            `of ${bytes(now.mem_total)}`,
            `${bytes(now.mem_available)} available`,
            now.swap_used > 0
              ? `swap ${bytes(now.swap_used)} — the machine is short`
              : 'swap unused',
          ]}
          alert={now.swap_used > 0}
        />
        <Gauge
          title="VRAM"
          percent={vramPercent}
          headline={gpu ? bytes(gpu.vram_used) : '—'}
          facts={
            gpu
              ? [`of ${bytes(gpu.vram_total)}`, gpu.name, `GPU ${gpu.percent.toFixed(0)}% · ${gpu.temp}°C`]
              : ['no NVIDIA card']
          }
          muted={!gpu}
        />
        <Gauge
          title="Disk"
          percent={now.disk_percent}
          headline={bytes(now.disk_used)}
          facts={[
            `of ${bytes(now.disk_total)}`,
            `${bytes(now.disk_total - now.disk_used)} free`,
            data.disks.length > 1 ? `${data.disks.length} mounts` : data.disks[0]?.device ?? '',
          ]}
        />
        {gpu && (
          <Gauge
            title="GPU power"
            percent={gpu.power_limit ? (gpu.power * 100) / gpu.power_limit : 0}
            headline={`${gpu.power.toFixed(0)} W`}
            facts={[`limit ${gpu.power_limit.toFixed(0)} W`, `${gpu.clock_mhz.toFixed(0)} MHz`, `fan ${gpu.fan}%`]}
          />
        )}
      </div>

      <Cores values={now.cpu_per_core} />

      <LoadChart points={data.points} minutes={minutes} />

      {data.apps.length > 0 && (
        <div className="sysblock">
          <h3>Your applications</h3>
          <p className="sysblock-sub">
            Each application charged for its whole process tree, so a launcher carries what it
            spawned.
          </p>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Application</th>
                  <th>CPU</th>
                  <th>Memory</th>
                  <th>VRAM</th>
                  <th>Processes</th>
                </tr>
              </thead>
              <tbody>
                {data.apps.map((row) => (
                  <tr key={row.app}>
                    <td className="strong">{row.app}</td>
                    <td className={`mono ${tone(row.cpu)}`}>{row.cpu.toFixed(1)}%</td>
                    <td className="mono">{bytes(row.memory)}</td>
                    <td className="mono">{row.vram ? bytes(row.vram) : '—'}</td>
                    <td className="mono muted">{row.processes}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="sysblock">
        <div className="sysblock-head">
          <h3>Heaviest processes</h3>
          <div className="seg">
            {TABS.map((entry) => (
              <button
                key={entry.key}
                className={`seg-btn ${tab === entry.key ? 'active' : ''}`}
                onClick={() => setTab(entry.key)}
              >
                {entry.label}
              </button>
            ))}
          </div>
        </div>

        <div className="table-wrap">
          {rows.length === 0 ? (
            <p className="empty-hint pad">
              {tab === 'vram' ? 'Nothing is holding VRAM right now.' : 'Nothing to show.'}
            </p>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Process</th>
                  <th>PID</th>
                  <th>CPU</th>
                  <th>Memory</th>
                  <th>VRAM</th>
                  <th>Belongs to</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={`${row.pid}-${row.name}`}>
                    <td className="mono">{row.name}</td>
                    <td className="mono muted">{row.pid}</td>
                    <td className={`mono ${tone(row.cpu)}`}>{row.cpu.toFixed(1)}%</td>
                    <td className="mono">{bytes(row.memory)}</td>
                    <td className="mono">{row.vram ? bytes(row.vram) : '—'}</td>
                    <td>{row.app ? <span className="pill running">{row.app}</span> : <span className="muted">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <Events events={data.events} onError={onError} onCleared={load} />

      {data.gpu_note && <p className="sysnote">{data.gpu_note}</p>}
    </section>
  )
}

function Gauge({ title, percent, headline, facts, muted = false, alert = false }) {
  const level = tone(percent)
  return (
    <div className={`sysgauge ${muted ? 'muted-card' : ''} ${alert ? 'alert' : ''}`}>
      <div className="sysgauge-top">
        <span className="sysgauge-title">{title}</span>
        <span className={`sysgauge-pct ${level}`}>{percent.toFixed(0)}%</span>
      </div>
      <div className="sysgauge-value">{headline}</div>
      <div className="sysgauge-bar">
        <span className={level} style={{ width: `${Math.min(percent, 100)}%` }} />
      </div>
      <ul className="sysgauge-facts">
        {facts.filter(Boolean).map((fact) => (
          <li key={fact}>{fact}</li>
        ))}
      </ul>
    </div>
  )
}

/** Per-core load. Twenty small bars say "one core pinned" in a way one number cannot. */
function Cores({ values }) {
  if (!values?.length) return null
  return (
    <div className="syscores">
      <span className="syscores-label">Cores</span>
      <div className="syscores-bars">
        {values.map((value, index) => (
          <span key={index} className="syscore" title={`core ${index}: ${value.toFixed(0)}%`}>
            <span className={tone(value)} style={{ height: `${Math.max(value, 2)}%` }} />
          </span>
        ))}
      </div>
    </div>
  )
}

/**
 * CPU, memory and VRAM against one timeline. Three series, so a legend is
 * always present; each is also direct-labelled at its right edge, so identity
 * never rests on colour alone.
 */
function LoadChart({ points, minutes }) {
  const [hover, setHover] = useState(null)

  if (!points || points.length < 2) {
    return (
      <div className="sysblock">
        <h3>Load over time</h3>
        <p className="empty-hint pad">
          Not enough readings yet. The first points appear within a minute of the backend starting.
        </p>
      </div>
    )
  }

  const width = 900
  const height = 170
  const pad = { top: 10, right: 46, bottom: 18, left: 32 }

  const SERIES = [
    { key: 'cpu', label: 'CPU', className: 'cpu' },
    { key: 'memory', label: 'RAM', className: 'ram' },
    { key: 'vram', label: 'VRAM', className: 'vram' },
  ]

  const x = (index) =>
    pad.left + (index * (width - pad.left - pad.right)) / Math.max(points.length - 1, 1)
  const y = (value) => pad.top + ((100 - value) * (height - pad.top - pad.bottom)) / 100

  function track(event) {
    const box = event.currentTarget.getBoundingClientRect()
    const ratio = (event.clientX - box.left) / box.width
    const index = Math.round(ratio * (points.length - 1))
    if (index >= 0 && index < points.length) setHover({ index, ratio })
  }

  const active = hover ? points[hover.index] : null

  return (
    <div className="sysblock">
      <div className="sysblock-head">
        <h3>Load over time</h3>
        <ul className="syslegend">
          {SERIES.map((series) => (
            <li key={series.key}>
              <span className={`syslegend-key ${series.className}`} />
              {series.label}
            </li>
          ))}
        </ul>
      </div>

      <p className="sysblock-sub">
        Percentages on one axis over the last {WINDOWS.find((w) => w.minutes === minutes)?.label}.
        Watching RAM and VRAM together is how you see a model move onto the card, or fail to.
      </p>

      <div className="syschart">
        <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" onMouseMove={track} onMouseLeave={() => setHover(null)} role="img" aria-label="CPU, RAM and VRAM usage over time">
          {[0, 25, 50, 75, 100].map((line) => (
            <g key={line}>
              <line x1={pad.left} y1={y(line)} x2={width - pad.right} y2={y(line)} className="sysgrid" />
              <text x={pad.left - 6} y={y(line) + 3} className="sysaxis" textAnchor="end">
                {line}
              </text>
            </g>
          ))}

          {SERIES.map((series) => (
            <polyline
              key={series.key}
              className={`sysline ${series.className}`}
              points={points.map((point, index) => `${x(index)},${y(point[series.key])}`).join(' ')}
            />
          ))}

          {SERIES.map((series) => (
            <text
              key={series.key}
              x={width - pad.right + 6}
              y={y(points[points.length - 1][series.key]) + 3}
              className={`syslabel ${series.className}`}
            >
              {series.label}
            </text>
          ))}

          {active && (
            <>
              <line x1={x(hover.index)} y1={pad.top} x2={x(hover.index)} y2={height - pad.bottom} className="sysgrid strong" />
              {SERIES.map((series) => (
                <circle key={series.key} cx={x(hover.index)} cy={y(active[series.key])} r="3.5" className={`sysdot ${series.className}`} />
              ))}
            </>
          )}
        </svg>

        {active && (
          <div className="systip" style={{ left: `${Math.min(Math.max(hover.ratio * 100, 8), 92)}%` }}>
            <strong>{clock(active.ts)}</strong>
            <span><i className="syslegend-key cpu" />CPU {active.cpu}%</span>
            <span><i className="syslegend-key ram" />RAM {active.memory}% · {bytes(active.mem_used)}</span>
            <span><i className="syslegend-key vram" />VRAM {active.vram}% · {bytes(active.vram_used)}</span>
            {active.swap > 0 && <span className="bad">swap {bytes(active.swap)}</span>}
            <span className="muted">
              disk {rate(active.disk_read)} r · {rate(active.disk_write)} w
            </span>
            <span className="muted">
              net {rate(active.net_recv)} in · {rate(active.net_sent)} out
            </span>
          </div>
        )}
      </div>
    </div>
  )
}

function Events({ events, onError, onCleared }) {
  async function clear() {
    try {
      await api.clearSystemEvents()
      onCleared()
    } catch (error) {
      onError(error.message)
    }
  }

  return (
    <div className="sysblock">
      <div className="sysblock-head">
        <h3>Log</h3>
        {events.length > 0 && (
          <button className="btn ghost small" onClick={clear}>
            Clear
          </button>
        )}
      </div>
      <p className="sysblock-sub">
        Recorded when something crosses a line, with whatever was holding the resource at that
        moment. Kept a week — longer than the charts reach.
      </p>

      {events.length === 0 ? (
        <p className="empty-hint pad">
          Nothing has crossed a threshold. CPU 90% · memory 90% · swap 25% · disk 90% · CPU 85°C ·
          GPU 80°C · VRAM 90%.
        </p>
      ) : (
        <ul className="sysevents">
          {events.map((event) => (
            <li key={event.id} className={event.level}>
              <span className="sysevent-time">{clock(event.ts)}</span>
              <span className={`sysevent-level ${event.level}`}>{event.level}</span>
              <span className="sysevent-msg">
                {event.message}
                {event.culprits.length > 0 && (
                  <span className="sysevent-who">
                    {event.culprits
                      .slice(0, 3)
                      .map((who) => `${who.app || who.name} ${bytes(who.vram || who.memory)}`)
                      .join(' · ')}
                  </span>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
