import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'

/**
 * Which application uses which AI model, and how much.
 *
 * The System tab answers "what is this machine doing". This one answers the
 * question a layer up, and its whole design turns on the fact that the answer
 * comes from sources of very different strength. A model *declared* by hand or
 * *observed* on the wire is known; one *detected* in an application's source is
 * a reading of that source, and is labelled as one.
 *
 * The same care applies to the counting. Only two things can produce a real
 * token count — an application reporting its own usage, or a usage block found
 * in its captured log — so an application God Father can see talking but not
 * counting shows "not counted" rather than a zero. A zero would be a lie.
 */

const WINDOWS = [
  { minutes: 15, label: '15m' },
  { minutes: 60, label: '1h' },
  { minutes: 360, label: '6h' },
  { minutes: 1440, label: '24h' },
]

const SOURCE_NOTE = {
  declared: 'written down by hand',
  observed: 'seen in use',
  detected: 'found in this application’s own files',
}

function tokens(value) {
  if (!value) return '0'
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`
  if (value >= 1000) return `${(value / 1000).toFixed(1)}K`
  return String(value)
}

function money(value) {
  if (!value) return '—'
  if (value < 0.01) return `$${value.toFixed(4)}`
  return `$${value.toFixed(2)}`
}

function bytes(value, digits = 1) {
  if (!value) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = value
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${unit === 0 ? Math.round(size) : size.toFixed(digits)} ${units[unit]}`
}

function clock(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  } catch {
    return '—'
  }
}

/** "4m ago" reads faster than a timestamp when the question is "is this now". */
function ago(iso) {
  if (!iso) return 'never'
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return '—'
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000))
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`
  return `${Math.round(seconds / 86400)}d ago`
}

/** The mirror of `ago`, for a moment that has not happened yet. */
function until(iso) {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const seconds = Math.round((then - Date.now()) / 1000)
  if (seconds <= 0) return 'any moment'
  if (seconds < 60) return `in ${seconds}s`
  if (seconds < 3600) return `in ${Math.round(seconds / 60)}m`
  return `in ${Math.round(seconds / 3600)}h`
}

export default function AiView({ onError, onNotice, onSelectApp }) {
  const [minutes, setMinutes] = useState(60)
  const [data, setData] = useState(null)
  const [failed, setFailed] = useState(null)
  const [tab, setTab] = useState('apps')
  const [expanded, setExpanded] = useState(null)
  const [scanning, setScanning] = useState(false)
  const [adding, setAdding] = useState(false)

  const load = useCallback(async () => {
    try {
      setData(await api.ai(minutes))
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

  async function rescan() {
    setScanning(true)
    try {
      await api.aiScan(minutes)
      await load()
      onNotice('Re-read every application’s folder')
    } catch (error) {
      onError(error.message)
    } finally {
      setScanning(false)
    }
  }

  if (!data && failed) {
    return (
      <section className="detail">
        <header className="detail-header">
          <div className="detail-title">
            <div>
              <h2>AI models</h2>
              <p className="detail-sub">Could not read model usage.</p>
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

  if (!data) {
    return (
      <section className="detail">
        <p className="empty-hint pad">Reading model usage…</p>
      </section>
    )
  }

  const usingApps = data.apps.length
  const measured = data.apps.filter((row) => row.measured).length
  const liveApps = data.apps.filter((row) => row.live).length

  return (
    <section className="detail">
      <header className="detail-header">
        <div className="detail-title">
          <div>
            <h2>AI models</h2>
            <p className="detail-sub">
              {usingApps} application{usingApps === 1 ? '' : 's'} · {data.models.length} model
              {data.models.length === 1 ? '' : 's'} ·{' '}
              {data.monitoring
                ? `watching every ${Math.round(data.sample_seconds)}s`
                : 'watcher not running'}
              {data.last_sample ? ` · last look ${ago(data.last_sample)}` : ''}
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
          <button className="btn ghost" onClick={rescan} disabled={scanning}>
            {scanning ? '⟳ Reading…' : '⌕ Re-scan folders'}
          </button>
        </div>
      </header>

      {failed && (
        <div className="banner warn">
          <strong>Not refreshing.</strong> {failed} — showing the last good reading.
        </div>
      )}

      {data.unavailable_reason && <div className="banner warn">{data.unavailable_reason}</div>}

      <div className="aistats">
        <Stat
          title="Calls"
          value={data.totals.calls.toLocaleString()}
          facts={[
            `${data.totals.errors} failed`,
            data.totals.avg_latency_ms ? `${Math.round(data.totals.avg_latency_ms)} ms average` : 'no timings reported',
            `${measured} of ${usingApps} applications counted`,
          ]}
          alert={data.totals.errors > 0}
        />
        <Stat
          title="Tokens"
          value={tokens(data.totals.input_tokens + data.totals.output_tokens)}
          facts={[
            `${tokens(data.totals.input_tokens)} in`,
            `${tokens(data.totals.output_tokens)} out`,
            'counted only where reported',
          ]}
        />
        <Stat
          title="Cost"
          value={money(data.totals.cost_usd)}
          facts={[
            'hosted models only',
            'local models are free to run',
            'priced from a built-in table',
          ]}
          muted={!data.totals.cost_usd}
        />
        <Stat
          title="Talking now"
          value={String(liveApps)}
          facts={
            data.live.length
              ? data.live.slice(0, 3).map((row) => `${row.app} → ${row.provider_label}`)
              : ['no open connections to a provider']
          }
          muted={!liveApps}
          good={liveApps > 0}
        />
      </div>

      <UsageChart points={data.points} minutes={minutes} />

      {data.live.length > 0 && (
        <div className="sysblock">
          <h3>Open right now</h3>
          <p className="sysblock-sub">
            A registered application holding a socket to a provider this second. One connection
            carries many calls, so this says <em>talking</em> — never <em>how many times</em>.
          </p>
          <ul className="ailive">
            {data.live.map((row) => (
              <li key={`${row.app_id}-${row.provider}`}>
                <span className="pulse-dot" />
                <button className="linkish" onClick={() => onSelectApp(row.app_id)}>
                  {row.app}
                </button>
                <span className="ailive-arrow">→</span>
                <span className={`pill ${row.local ? 'port' : 'running'}`}>{row.provider_label}</span>
                <span className="muted mono">{row.remote}</span>
                <span className="muted">
                  {row.connections} connection{row.connections === 1 ? '' : 's'}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="sysblock">
        <div className="sysblock-head">
          <h3>{tab === 'apps' ? 'Your applications' : 'Models'}</h3>
          <div className="seg">
            <button
              className={`seg-btn ${tab === 'apps' ? 'active' : ''}`}
              onClick={() => setTab('apps')}
            >
              By application
            </button>
            <button
              className={`seg-btn ${tab === 'models' ? 'active' : ''}`}
              onClick={() => setTab('models')}
            >
              By model
            </button>
          </div>
        </div>

        {tab === 'apps' ? (
          <AppTable
            rows={data.apps}
            expanded={expanded}
            onExpand={setExpanded}
            onSelectApp={onSelectApp}
            onError={onError}
            onNotice={onNotice}
            onChanged={load}
            minutes={minutes}
          />
        ) : (
          <ModelTable rows={data.models} />
        )}

        <div className="aiadd-row">
          <button className="btn ghost small" onClick={() => setAdding((open) => !open)}>
            {adding ? '× Close' : '+ Say which model an application uses'}
          </button>
          <span className="cf-hint">
            Detection reads an application’s files and can only find what is written there. Anything
            you add here outranks it.
          </span>
        </div>

        {adding && (
          <DeclareForm
            apps={data.apps}
            minutes={minutes}
            onError={onError}
            onDone={(message) => {
              setAdding(false)
              onNotice(message)
              load()
            }}
          />
        )}
      </div>

      <LocalModels reachable={data.ollama_reachable} models={data.local_models} />

      <CallLog calls={data.calls} onError={onError} onCleared={load} />

      <HowToCount empty={data.totals.calls === 0} />
    </section>
  )
}

function Stat({ title, value, facts, muted = false, alert = false, good = false }) {
  return (
    <div className={`sysgauge ${muted ? 'muted-card' : ''} ${alert ? 'alert' : ''}`}>
      <div className="sysgauge-top">
        <span className="sysgauge-title">{title}</span>
        {good && <span className="sysgauge-pct good">live</span>}
      </div>
      <div className="sysgauge-value">{value}</div>
      <ul className="sysgauge-facts">
        {facts.filter(Boolean).map((fact) => (
          <li key={fact}>{fact}</li>
        ))}
      </ul>
    </div>
  )
}

/**
 * Tokens as stacked bars on the left axis, open connections as a line on the
 * right. Two axes rather than one, because the two series are not comparable
 * and drawing them as if they were would be the chart telling a small lie —
 * and because connections are the only series an uninstrumented application
 * contributes to at all.
 */
function UsageChart({ points, minutes }) {
  const [hover, setHover] = useState(null)

  const scale = useMemo(() => {
    const peakTokens = Math.max(
      ...points.map((point) => point.input_tokens + point.output_tokens),
      1
    )
    const peakLinks = Math.max(...points.map((point) => point.connections), 1)
    return { peakTokens, peakLinks }
  }, [points])

  if (!points || points.length < 2) {
    return (
      <div className="sysblock">
        <h3>Use over time</h3>
        <p className="empty-hint pad">
          Nothing recorded in this window yet. The first points appear within a minute of the
          backend starting.
        </p>
      </div>
    )
  }

  const width = 900
  const height = 170
  const pad = { top: 12, right: 52, bottom: 18, left: 46 }
  const plotWidth = width - pad.left - pad.right
  const plotHeight = height - pad.top - pad.bottom
  const step = plotWidth / points.length
  const barWidth = Math.max(1.5, step * 0.7)

  const x = (index) => pad.left + index * step + step / 2
  const yTokens = (value) => pad.top + plotHeight - (value * plotHeight) / scale.peakTokens
  const yLinks = (value) => pad.top + plotHeight - (value * plotHeight) / scale.peakLinks

  const anyTokens = scale.peakTokens > 1
  const anyLinks = points.some((point) => point.connections > 0)

  function track(event) {
    const box = event.currentTarget.getBoundingClientRect()
    const ratio = (event.clientX - box.left) / box.width
    const index = Math.min(points.length - 1, Math.max(0, Math.floor(ratio * points.length)))
    setHover({ index, ratio })
  }

  const active = hover ? points[hover.index] : null

  return (
    <div className="sysblock">
      <div className="sysblock-head">
        <h3>Use over time</h3>
        <ul className="syslegend">
          <li>
            <span className="syslegend-key tokens-in" />
            Tokens in
          </li>
          <li>
            <span className="syslegend-key tokens-out" />
            Tokens out
          </li>
          <li>
            <span className="syslegend-key links" />
            Connections
          </li>
        </ul>
      </div>

      <p className="sysblock-sub">
        Over the last {WINDOWS.find((entry) => entry.minutes === minutes)?.label}. Bars are tokens,
        and only exist where an application reports them. The line is open connections, which every
        running application contributes to — a line with no bars under it is an application talking
        to a model without counting it.
      </p>

      <div className="syschart">
        <svg
          viewBox={`0 0 ${width} ${height}`}
          preserveAspectRatio="none"
          onMouseMove={track}
          onMouseLeave={() => setHover(null)}
          role="img"
          aria-label="Token use and provider connections over time"
        >
          {[0, 0.5, 1].map((fraction) => (
            <g key={fraction}>
              <line
                x1={pad.left}
                y1={pad.top + plotHeight * (1 - fraction)}
                x2={width - pad.right}
                y2={pad.top + plotHeight * (1 - fraction)}
                className="sysgrid"
              />
              <text
                x={pad.left - 6}
                y={pad.top + plotHeight * (1 - fraction) + 3}
                className="sysaxis"
                textAnchor="end"
              >
                {anyTokens ? tokens(Math.round(scale.peakTokens * fraction)) : ''}
              </text>
              <text
                x={width - pad.right + 6}
                y={pad.top + plotHeight * (1 - fraction) + 3}
                className="sysaxis"
              >
                {anyLinks ? Math.round(scale.peakLinks * fraction) : ''}
              </text>
            </g>
          ))}

          {points.map((point, index) => {
            const inHeight = pad.top + plotHeight - yTokens(point.input_tokens)
            const outHeight = pad.top + plotHeight - yTokens(point.output_tokens)
            return (
              <g key={point.ts}>
                <rect
                  className="aibar in"
                  x={x(index) - barWidth / 2}
                  y={yTokens(point.input_tokens + point.output_tokens)}
                  width={barWidth}
                  height={Math.max(inHeight, point.input_tokens ? 1 : 0)}
                />
                <rect
                  className="aibar out"
                  x={x(index) - barWidth / 2}
                  y={yTokens(point.output_tokens)}
                  width={barWidth}
                  height={Math.max(outHeight, point.output_tokens ? 1 : 0)}
                />
              </g>
            )
          })}

          {anyLinks && (
            <polyline
              className="sysline links"
              points={points.map((point, index) => `${x(index)},${yLinks(point.connections)}`).join(' ')}
            />
          )}

          {active && (
            <line
              x1={x(hover.index)}
              y1={pad.top}
              x2={x(hover.index)}
              y2={height - pad.bottom}
              className="sysgrid strong"
            />
          )}
        </svg>

        {active && (
          <div
            className="systip"
            style={{ left: `${Math.min(Math.max(hover.ratio * 100, 10), 90)}%` }}
          >
            <strong>{clock(active.ts)}</strong>
            <span>
              <i className="syslegend-key tokens-in" />
              {tokens(active.input_tokens)} in
            </span>
            <span>
              <i className="syslegend-key tokens-out" />
              {tokens(active.output_tokens)} out
            </span>
            <span>
              <i className="syslegend-key links" />
              {active.connections} connection{active.connections === 1 ? '' : 's'}
            </span>
            <span className="muted">
              {active.calls} call{active.calls === 1 ? '' : 's'} · {money(active.cost_usd)}
            </span>
            {active.errors > 0 && <span className="bad">{active.errors} failed</span>}
          </div>
        )}
      </div>
    </div>
  )
}

function AppTable({ rows, expanded, onExpand, onSelectApp, onError, onNotice, onChanged, minutes }) {
  if (!rows.length) {
    return (
      <p className="empty-hint pad">
        No application here reaches a model yet — nothing declared, nothing found in any folder, and
        nothing seen on the wire.
      </p>
    )
  }

  async function forget(link) {
    try {
      await api.aiForget(link.id)
      onNotice(`Removed ${link.model || link.provider_label}`)
      onChanged()
    } catch (error) {
      onError(error.message)
    }
  }

  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>Application</th>
            <th>Uses</th>
            <th>Models</th>
            <th>Calls</th>
            <th>Tokens</th>
            <th>Cost</th>
            <th>Last call</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const key = row.app_id ?? `x-${row.app}`
            const open = expanded === key
            return (
              <Fragment key={key}>
                <tr
                  className={`aiRow ${row.live ? 'cf-live' : ''}`}
                  onClick={() => onExpand(open ? null : key)}
                >
                  <td className="strong">
                    <span className="aiapp">
                      {row.live && <span className="pulse-dot" title="talking to a model now" />}
                      {row.app_id ? (
                        <button
                          className="linkish"
                          onClick={(event) => {
                            event.stopPropagation()
                            onSelectApp(row.app_id)
                          }}
                        >
                          {row.app}
                        </button>
                      ) : (
                        <span title="reported usage from something not registered here">
                          {row.app}
                        </span>
                      )}
                      <span className="aicaret">{open ? '▾' : '▸'}</span>
                    </span>
                  </td>
                  <td>
                    {row.provider_labels.length ? (
                      <span className="aipills">
                        {row.provider_labels.map((label) => (
                          <span key={label} className="pill added">
                            {label}
                          </span>
                        ))}
                      </span>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td className="mono">
                    {row.models.length ? (
                      <span className="aimodels">
                        {row.models.slice(0, 2).join(', ')}
                        {row.models.length > 2 && (
                          <span className="muted"> +{row.models.length - 2}</span>
                        )}
                      </span>
                    ) : (
                      <span className="muted">provider only</span>
                    )}
                  </td>
                  <td className="mono">
                    {row.measured ? (
                      row.calls.toLocaleString()
                    ) : (
                      <span className="muted" title="nothing has reported a token count for this app">
                        not counted
                      </span>
                    )}
                  </td>
                  <td className="mono">
                    {row.measured ? (
                      <>
                        {tokens(row.input_tokens)} <span className="muted">in</span> ·{' '}
                        {tokens(row.output_tokens)} <span className="muted">out</span>
                      </>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td className="mono">{row.measured ? money(row.cost_usd) : <span className="muted">—</span>}</td>
                  <td className="mono muted">{row.measured ? ago(row.last_used) : '—'}</td>
                </tr>

                {open && (
                  <tr className="aidetail-row">
                    <td colSpan={7}>
                      <div className="aidetail">
                        {row.links.length === 0 ? (
                          <p className="cf-hint">
                            Counted usage only — nothing in this application’s folder says which
                            model it reaches.
                          </p>
                        ) : (
                          <ul className="aievidence">
                            {row.links.map((link) => (
                              <li key={link.id}>
                                <span className={`aisource ${link.source}`}>{link.source}</span>
                                <span className="aievidence-model mono">
                                  {link.model || `${link.provider_label} (model unknown)`}
                                </span>
                                <span className="aievidence-why">
                                  {link.evidence || SOURCE_NOTE[link.source]}
                                </span>
                                <span className="muted">{ago(link.last_seen)}</span>
                                {link.source === 'declared' && (
                                  <button
                                    className="btn ghost small"
                                    onClick={(event) => {
                                      event.stopPropagation()
                                      forget(link)
                                    }}
                                  >
                                    Remove
                                  </button>
                                )}
                              </li>
                            ))}
                          </ul>
                        )}
                        {!row.measured && row.app_id && (
                          <p className="cf-hint">
                            To count this one, have it post each call to{' '}
                            <code>/api/ai/usage</code> — or print its usage block to stdout, which
                            God Father is already capturing and will read.
                          </p>
                        )}
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function ModelTable({ rows }) {
  if (!rows.length) {
    return <p className="empty-hint pad">No models known yet.</p>
  }
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>Model</th>
            <th>Served by</th>
            <th>Used by</th>
            <th>Calls</th>
            <th>Tokens</th>
            <th>Cost</th>
            <th>Price / 1M</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.provider}-${row.model}`}>
              <td className="mono strong">{row.model || '(not reported)'}</td>
              <td>
                <span className={`pill ${row.local ? 'port' : 'added'}`}>{row.provider_label}</span>
              </td>
              <td>
                {row.apps.length ? (
                  <span className="aipills">
                    {row.apps.slice(0, 3).map((name) => (
                      <span key={name} className="pill">
                        {name}
                      </span>
                    ))}
                    {row.apps.length > 3 && <span className="muted">+{row.apps.length - 3}</span>}
                  </span>
                ) : (
                  <span className="muted">—</span>
                )}
              </td>
              <td className="mono">{row.calls ? row.calls.toLocaleString() : <span className="muted">—</span>}</td>
              <td className="mono">
                {row.calls ? (
                  <>
                    {tokens(row.input_tokens)} <span className="muted">in</span> ·{' '}
                    {tokens(row.output_tokens)} <span className="muted">out</span>
                  </>
                ) : (
                  <span className="muted">—</span>
                )}
              </td>
              <td className="mono">{row.cost_usd ? money(row.cost_usd) : <span className="muted">—</span>}</td>
              <td className="mono muted">
                {row.local
                  ? 'runs here'
                  : row.price_in
                    ? `$${row.price_in} / $${row.price_out}`
                    : 'not priced'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function DeclareForm({ apps, minutes, onError, onDone }) {
  const registered = apps.filter((row) => row.app_id)
  const [appId, setAppId] = useState(registered[0]?.app_id ?? '')
  const [model, setModel] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(event) {
    event.preventDefault()
    if (!appId || !model.trim()) return
    setBusy(true)
    try {
      await api.aiDeclare({ app_id: Number(appId), model: model.trim() }, minutes)
      setModel('')
      onDone(`${model.trim()} recorded`)
    } catch (error) {
      onError(error.message)
    } finally {
      setBusy(false)
    }
  }

  if (!registered.length) {
    return <p className="cf-hint pad">Register an application first.</p>
  }

  return (
    <form className="aideclare" onSubmit={submit}>
      <select value={appId} onChange={(event) => setAppId(event.target.value)}>
        {registered.map((row) => (
          <option key={row.app_id} value={row.app_id}>
            {row.app}
          </option>
        ))}
      </select>
      <input
        type="text"
        placeholder="model id, for example claude-sonnet-5 or qwen2.5-coder:14b"
        value={model}
        onChange={(event) => setModel(event.target.value)}
      />
      <button className="btn primary" type="submit" disabled={busy || !model.trim()}>
        {busy ? 'Saving…' : 'Add'}
      </button>
    </form>
  )
}

/**
 * Ollama is the one local runtime that will simply tell you what it is doing,
 * so the tab asks. Loaded is the interesting state: a downloaded model costs
 * disk, a loaded one is holding the VRAM the System tab is showing you.
 */
function LocalModels({ reachable, models }) {
  if (!reachable) return null
  const loaded = models.filter((model) => model.loaded)
  const idle = models.filter((model) => !model.loaded)

  return (
    <div className="sysblock">
      <h3>Models on this machine</h3>
      <p className="sysblock-sub">
        From Ollama. {loaded.length ? `${loaded.length} in memory now` : 'None loaded right now'} ·{' '}
        {idle.length} more downloaded. A loaded model holds VRAM whether or not anything is asking
        it anything.
      </p>
      <ul className="ailocal">
        {loaded.map((model) => (
          <li key={model.name} className="loaded">
            <span className="pulse-dot" />
            <span className="mono strong">{model.name}</span>
            <span className="muted">{bytes(model.vram || model.size)} in memory</span>
            {until(model.expires_at) && (
              <span className="muted">unloads {until(model.expires_at)}</span>
            )}
          </li>
        ))}
        {idle.map((model) => (
          <li key={model.name}>
            <span className="mono">{model.name}</span>
            <span className="muted">downloaded</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function CallLog({ calls, onError, onCleared }) {
  async function clear() {
    try {
      await api.clearAiCalls()
      onCleared()
    } catch (error) {
      onError(error.message)
    }
  }

  return (
    <div className="sysblock">
      <div className="sysblock-head">
        <h3>Call log</h3>
        {calls.length > 0 && (
          <button className="btn ghost small" onClick={clear}>
            Clear
          </button>
        )}
      </div>
      <p className="sysblock-sub">
        Every counted call, newest first. <em>reported</em> came from the application itself;{' '}
        <em>log</em> was read out of the output God Father was already capturing. Kept a fortnight.
      </p>

      {calls.length === 0 ? (
        <p className="empty-hint pad">Nothing counted yet.</p>
      ) : (
        <ul className="sysevents">
          {calls.map((call) => (
            <li key={call.id} className={call.ok ? '' : 'warning'}>
              <span className="sysevent-time">{clock(call.ts)}</span>
              <span className={`aisource ${call.source === 'log' ? 'detected' : 'observed'}`}>
                {call.source === 'log' ? 'log' : 'reported'}
              </span>
              <span className="sysevent-msg">
                <span>
                  <strong>{call.app_name || 'unknown'}</strong>
                  <span className="muted"> → </span>
                  <span className="mono">{call.model || call.provider_label || 'unnamed model'}</span>
                </span>
                <span className="sysevent-who">
                  {tokens(call.input_tokens)} in · {tokens(call.output_tokens)} out
                  {call.cost_usd ? ` · ${money(call.cost_usd)}` : ''}
                  {call.latency_ms ? ` · ${Math.round(call.latency_ms)} ms` : ''}
                  {call.error ? ` · ${call.error}` : ''}
                </span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/**
 * Shown quietly once there is data, and prominently while there is none —
 * because "no numbers yet" is a question about what to do next, not a state to
 * sit and look at.
 */
function HowToCount({ empty }) {
  const [open, setOpen] = useState(empty)

  useEffect(() => {
    if (empty) setOpen(true)
  }, [empty])

  return (
    <div className="sysblock">
      <div className="sysblock-head">
        <h3>Getting exact numbers</h3>
        <button className="btn ghost small" onClick={() => setOpen((value) => !value)}>
          {open ? 'Hide' : 'Show'}
        </button>
      </div>

      {open && (
        <>
          <p className="sysblock-sub">
            God Father can see <em>that</em> an application is talking to a model without decrypting
            anything, but a token count only exists inside the application. There are two ways to
            get it out, and neither needs a proxy.
          </p>

          <p className="cf-hint">
            <strong>1 · Report it.</strong> One request after each call. The app is matched by its
            registered name, its id, or the port it serves.
          </p>
          <pre className="cf-config">{`response = client.messages.create(model=MODEL, ...)

requests.post("http://127.0.0.1:8000/api/ai/usage", json={
    "app": "my-app",
    "model": MODEL,
    "input_tokens": response.usage.input_tokens,
    "output_tokens": response.usage.output_tokens,
})`}</pre>

          <p className="cf-hint">
            <strong>2 · Print it.</strong> If the application simply prints its usage, God Father
            reads it out of the log it is already capturing — no code change beyond the print, and
            the OpenAI, Anthropic, Gemini and Ollama shapes are all understood.
          </p>
          <pre className="cf-config">{`print({"model": MODEL,
       "input_tokens": usage.input_tokens,
       "output_tokens": usage.output_tokens})`}</pre>
        </>
      )}
    </div>
  )
}
