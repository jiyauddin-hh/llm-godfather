import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import TunnelDashboard, { RowSparkline, TrafficStrip } from './TunnelDashboard.jsx'

/**
 * Publish local ports to the internet through Cloudflare Tunnel.
 *
 * The tab has two modes and shows the same table in both. Without a domain,
 * every live route gets a temporary trycloudflare.com address. Once
 * `cloudflared tunnel login` has been run, routes take hostnames you choose
 * and the addresses stop changing. Nothing has to be rebuilt in between — the
 * routes are the same rows either way.
 */
export default function CloudflareView({ onSelectApp, onError, onNotice }) {
  const [state, setState] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState(null)
  const [adding, setAdding] = useState(false)
  const [editing, setEditing] = useState(null)
  const [showConfig, setShowConfig] = useState(null)
  const [confirming, setConfirming] = useState(null)

  const [failure, setFailure] = useState(null)
  const [metrics, setMetrics] = useState(null)
  const [minutes, setMinutes] = useState(60)
  const [showTraffic, setShowTraffic] = useState(false)

  const load = useCallback(async () => {
    try {
      setState(await api.cloudflare())
      setFailure(null)
    } catch (error) {
      // Keep whatever we last rendered rather than blanking the tab on one
      // failed poll; only a first load with nothing to show is an error state.
      setFailure(error.message)
    } finally {
      setLoading(false)
    }

    // Metrics are fetched alongside, not by the dashboard itself, so the strip
    // above the table and the charts below it never disagree about the moment
    // they are describing.
    try {
      setMetrics(await api.cloudflareMetrics(minutes))
    } catch {
      setMetrics(null)
    }
  }, [minutes])

  useEffect(() => {
    load()
    const timer = setInterval(load, 4000)
    return () => clearInterval(timer)
  }, [load])

  const named = state?.mode === 'named'
  const routes = state?.routes ?? []
  const live = routes.filter((route) => route.enabled).length

  async function toggle(route) {
    setBusyId(route.id)
    try {
      const result = route.enabled ? await api.routeDown(route.id) : await api.routeUp(route.id)
      onNotice(
        result.enabled
          ? `Port ${route.port} is going live${result.public_url ? ` at ${result.public_url}` : '…'}`
          : `Port ${route.port} is no longer published`
      )
      load()
    } catch (error) {
      onError(error.message)
    } finally {
      setBusyId(null)
    }
  }

  async function remove(route) {
    try {
      await api.deleteRoute(route.id)
      setConfirming(null)
      onNotice(`Route for port ${route.port} removed`)
      load()
    } catch (error) {
      onError(error.message)
    }
  }

  async function openConfig() {
    try {
      setShowConfig(await api.cloudflareConfig())
    } catch (error) {
      onError(error.message)
    }
  }

  if (loading) {
    return (
      <section className="detail">
        <p className="empty-hint pad">Checking Cloudflare…</p>
      </section>
    )
  }

  // Nothing to render and nothing arriving. The overwhelmingly likely cause is
  // a backend still running the code from before this tab existed, so say that
  // rather than showing an empty page and leaving someone guessing.
  if (!state) {
    return (
      <section className="detail">
        <header className="detail-header">
          <div className="detail-title">
            <div>
              <h2>Cloudflare</h2>
              <p className="detail-sub">Could not reach the Cloudflare API.</p>
            </div>
          </div>
          <div className="detail-actions">
            <button className="btn ghost" onClick={load}>
              ⟳ Try again
            </button>
          </div>
        </header>

        <div className="banner error">
          <strong>The backend does not have this feature yet.</strong> Restart God Father so it picks
          it up, then reload this page:
        </div>
        <pre className="cf-config">systemctl --user restart godfather</pre>
        <p className="cf-hint">
          Restarting stops every managed application. Those with start on boot come back by
          themselves.
          {failure ? ` · ${failure}` : ''}
        </p>
      </section>
    )
  }

  return (
    <section className="detail">
      <header className="detail-header">
        <div className="detail-title">
          <div>
            <h2>Cloudflare</h2>
            <p className="detail-sub">
              {state.installed
                ? `${live} of ${routes.length} route(s) live · ${state.version || 'cloudflared'}`
                : 'cloudflared is not installed on this machine.'}
            </p>
          </div>
        </div>
        <div className="detail-actions">
          {named && (
            <button className="btn ghost" onClick={openConfig}>
              View config.yml
            </button>
          )}
          <button className="btn ghost" onClick={load}>
            ⟳ Refresh
          </button>
          <button
            className="btn primary"
            disabled={!state.installed}
            onClick={() => setAdding(true)}
          >
            + Publish a port
          </button>
        </div>
      </header>

      {failure && (
        <div className="banner warn">
          <strong>Not refreshing.</strong> {failure} — what you see below is the last good reading.
        </div>
      )}

      {!state.installed && <div className="banner error">{state.install_hint}</div>}

      <ModeCard state={state} onError={onError} onNotice={onNotice} onChanged={load} />

      <TrafficStrip
        data={metrics}
        expanded={showTraffic}
        onToggle={() => setShowTraffic((open) => !open)}
      />

      <div className="table-wrap">
        {routes.length === 0 ? (
          <p className="empty-hint pad">
            No ports published yet. Pick one with <strong>Publish a port</strong> — it can be a
            registered application or any port listening on this machine.
          </p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Port</th>
                <th>Serving</th>
                <th>{named ? 'Hostname' : 'Address'}</th>
                <th>State</th>
                <th>Traffic</th>
                <th>Tunnel process</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {routes.map((route) => (
                <RouteRow
                  key={route.id}
                  route={route}
                  named={named}
                  traffic={metrics?.routes?.find((entry) => entry.route_id === route.id) ?? null}
                  busy={busyId === route.id}
                  onToggle={() => toggle(route)}
                  onEdit={() => setEditing(route)}
                  onDelete={() => setConfirming(route)}
                  onSelectApp={onSelectApp}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>

      {showTraffic && (
        <TunnelDashboard data={metrics} minutes={minutes} onWindowChange={setMinutes} />
      )}

      {adding && (
        <RouteForm
          named={named}
          onClose={() => setAdding(false)}
          onError={onError}
          onSaved={(route) => {
            setAdding(false)
            onNotice(`Port ${route.port} added. Switch it on when you are ready.`)
            load()
          }}
        />
      )}

      {editing && (
        <RouteForm
          route={editing}
          named={named}
          onClose={() => setEditing(null)}
          onError={onError}
          onSaved={() => {
            setEditing(null)
            onNotice('Route updated')
            load()
          }}
        />
      )}

      {showConfig && (
        <div className="modal-backdrop" onClick={() => setShowConfig(null)}>
          <div className="modal wide" onClick={(event) => event.stopPropagation()}>
            <header className="modal-header">
              <h3>Generated configuration</h3>
              <p className="modal-sub">
                God Father writes this from the routes above. Editing it by hand will be overwritten.
              </p>
            </header>
            <div className="modal-body">
              <p className="cf-config-path">{showConfig.path}</p>
              <pre className="cf-config">{showConfig.text || 'Not written yet.'}</pre>
            </div>
            <footer className="modal-footer">
              <button className="btn ghost" onClick={() => setShowConfig(null)}>
                Close
              </button>
            </footer>
          </div>
        </div>
      )}

      {confirming && (
        <div className="modal-backdrop" onClick={() => setConfirming(null)}>
          <div className="modal small" onClick={(event) => event.stopPropagation()}>
            <header className="modal-header">
              <h3>Remove the route for port {confirming.port}?</h3>
            </header>
            <div className="modal-body">
              <p>
                This takes the port off the internet and deletes the tunnel process God Father
                created for it. Your application itself keeps running.
              </p>
              {confirming.hostname && (
                <p>
                  The DNS record for <code>{confirming.hostname}</code> stays in your Cloudflare
                  account — remove it there if you want it gone.
                </p>
              )}
            </div>
            <footer className="modal-footer">
              <button className="btn ghost" onClick={() => setConfirming(null)}>
                Cancel
              </button>
              <button className="btn danger" onClick={() => remove(confirming)}>
                Remove route
              </button>
            </footer>
          </div>
        </div>
      )}
    </section>
  )
}

/** The banner that explains which mode this machine is in, and how to change it. */
function ModeCard({ state, onError, onNotice, onChanged }) {
  const [name, setName] = useState(state.tunnel_name || '')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    setName(state.tunnel_name || '')
  }, [state.tunnel_name])

  if (!state.installed) return null

  if (state.mode !== 'named') {
    return (
      <div className="cf-mode">
        <div className="cf-mode-head">
          <span className="pill">Temporary addresses</span>
          <strong>No domain configured</strong>
        </div>
        <p>
          Routes go live on random <code>trycloudflare.com</code> addresses that change every time
          the tunnel restarts. Good for showing someone your work today; not something to link to.
        </p>
        <p className="cf-hint">{state.login_hint}</p>
      </div>
    )
  }

  const known = state.tunnels ?? []
  const isNew = name.trim() !== '' && !known.includes(name.trim())

  async function save() {
    setBusy(true)
    try {
      await api.chooseTunnel(name.trim(), isNew)
      onNotice(isNew ? `Tunnel '${name.trim()}' created and selected` : `Using tunnel '${name.trim()}'`)
      onChanged()
    } catch (error) {
      onError(error.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="cf-mode on">
      <div className="cf-mode-head">
        <span className="pill running">Your own hostnames</span>
        <strong>{state.tunnel_name ? `Tunnel: ${state.tunnel_name}` : 'Choose a tunnel'}</strong>
      </div>
      <p>
        Logged in to Cloudflare. Give each route a hostname on one of your domains and God Father
        creates the DNS record for it. Addresses stay the same across restarts.
      </p>
      <div className="cf-tunnel-pick">
        <input
          list="cf-tunnel-names"
          value={name}
          placeholder="tunnel name, e.g. godfather"
          onChange={(event) => setName(event.target.value)}
        />
        <datalist id="cf-tunnel-names">
          {known.map((entry) => (
            <option key={entry} value={entry} />
          ))}
        </datalist>
        <button
          className="btn primary"
          disabled={busy || !name.trim() || name.trim() === state.tunnel_name}
          onClick={save}
        >
          {busy ? 'Working…' : isNew ? 'Create and use' : 'Use this tunnel'}
        </button>
      </div>
      {isNew && name.trim() && (
        <p className="cf-hint">
          No tunnel called <code>{name.trim()}</code> yet — it will be created in your account.
        </p>
      )}
    </div>
  )
}

function RouteRow({ route, named, traffic, busy, onToggle, onEdit, onDelete, onSelectApp }) {
  const [copied, setCopied] = useState(false)

  function copy() {
    // share_url carries the secret path when there is one — the bare address
    // would 404, so copying anything else hands out a link that cannot work.
    const link = route.share_url || route.public_url
    if (!link) return
    navigator.clipboard?.writeText(link)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  const protections = [
    route.guard_oauth ? 'OAuth required' : '',
    route.guard_header_set ? `sends ${route.guard_header_name}` : '',
    route.guard_secret_path ? 'secret path required' : '',
  ].filter(Boolean)

  // Live, but the address has not appeared yet — a quick tunnel takes a few
  // seconds to be told what it is called. If the tunnel behind it has already
  // died there is no address coming, and saying so beats waiting forever.
  const waiting = route.enabled && !route.public_url
  const stalled = waiting && route.last_error

  return (
    <tr className={route.enabled ? 'cf-live' : ''}>
      <td className="mono strong">
        {route.port}
        {route.guarded && (
          <span className="cf-guard" title={`Protected — ${protections.join(', ')}`}>
            🔒
          </span>
        )}
      </td>

      <td>
        {route.serves_app_id ? (
          <button className="link" onClick={() => onSelectApp(route.serves_app_id)}>
            {route.serves_app_name}
          </button>
        ) : (
          <span className="muted">{route.port_open ? 'unregistered' : 'nothing listening'}</span>
        )}
      </td>

      <td>
        {route.public_url ? (
          <span className="cf-url-cell">
            <a
              href={route.share_url || route.public_url}
              target="_blank"
              rel="noreferrer"
              className="cf-url"
            >
              {route.public_url.replace(/^https:\/\//, '')}
              {/* The secret is never spelled out in the table — it would be
                  read over a shoulder or caught in a screenshot. Copy takes
                  the whole address. */}
              {route.guard_secret_path && <span className="cf-url-secret">/…</span>}
            </a>
            <button className="btn ghost small" onClick={copy}>
              {copied ? 'Copied' : 'Copy'}
            </button>
          </span>
        ) : named && route.hostname ? (
          <span className="muted mono">{route.hostname}</span>
        ) : stalled ? (
          <span className="cf-url-failed" title={route.last_error}>
            no address — {route.last_error}
          </span>
        ) : waiting ? (
          <span className="muted">waiting for an address…</span>
        ) : named ? (
          <span className="muted">no hostname set</span>
        ) : (
          <span className="muted">assigned when live</span>
        )}
      </td>

      <td>
        <span className={`pill ${route.enabled ? 'running' : 'stopped'}`}>
          {route.enabled ? 'live' : 'off'}
        </span>
        {!route.port_open && <span className="cf-warn" title="Nothing is serving this port">!</span>}
      </td>

      <td className="trow-traffic">
        {route.enabled ? <RowSparkline route={traffic} /> : <span className="muted">—</span>}
      </td>

      <td>
        {route.app_id ? (
          <button className="link" onClick={() => onSelectApp(route.app_id)}>
            {route.app_name}
          </button>
        ) : (
          <span className="muted">—</span>
        )}
      </td>

      <td className="row-action">
        <button className="btn ghost small" onClick={onEdit} disabled={busy}>
          Edit
        </button>
        <button
          className={`btn small ${route.enabled ? 'warn' : 'success'}`}
          onClick={onToggle}
          disabled={busy}
        >
          {busy ? '…' : route.enabled ? 'Take down' : 'Go live'}
        </button>
        <button className="btn ghost small danger" onClick={onDelete} disabled={busy}>
          Remove
        </button>
      </td>
    </tr>
  )
}

/** A random, URL-safe string. Generated in the browser so a value being
 *  chosen never travels anywhere before the person has decided to save it. */
function randomSecret(bytes = 24) {
  const raw = new Uint8Array(bytes)
  crypto.getRandomValues(raw)
  return btoa(String.fromCharCode(...raw)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/**
 * A credential box with a Generate button.
 *
 * Shown as plain text once generated rather than dots: a value you have just
 * been given is useless if you cannot read it, and this is the only moment it
 * is on screen — the server stores it hashed and never sends it back.
 */
function SecretField({
  label,
  value,
  onChange,
  placeholder,
  hint,
  generateLabel = 'Generate',
  // Supplied only for a credential the server can still produce. Its absence
  // is what makes the eye a plain visibility toggle instead of a lookup.
  onFetchStored = null,
  storedNote = '',
}) {
  const [revealed, setRevealed] = useState(false)
  const [busy, setBusy] = useState(false)

  function generate() {
    onChange(randomSecret())
    setRevealed(true)
  }

  async function toggle() {
    if (revealed) {
      setRevealed(false)
      return
    }
    // Nothing typed yet and the stored one can be looked up — fetch it, so
    // the eye answers "what is it currently?" rather than "what have I typed?"
    if (!value && onFetchStored) {
      setBusy(true)
      try {
        const stored = await onFetchStored()
        if (stored) onChange(stored)
      } finally {
        setBusy(false)
      }
    }
    setRevealed(true)
  }

  const canToggle = Boolean(value) || Boolean(onFetchStored)

  return (
    <>
      <label className="input-group">
        <span>{label}</span>
        <span className="cf-secret-row">
          <input
            type={revealed ? 'text' : 'password'}
            autoComplete="new-password"
            value={value}
            onChange={(event) => onChange(event.target.value)}
            placeholder={placeholder}
          />
          {canToggle && (
            <button
              type="button"
              className="btn ghost small cf-eye"
              onClick={toggle}
              disabled={busy}
              title={revealed ? 'Hide' : 'Show'}
              aria-label={revealed ? 'Hide' : 'Show'}
            >
              {busy ? '…' : revealed ? '🙈' : '👁'}
            </button>
          )}
          <button type="button" className="btn ghost small" onClick={generate}>
            {generateLabel}
          </button>
        </span>
      </label>
      {storedNote && <p className="hint">{storedNote}</p>}
      {hint && <p className="hint">{hint}</p>}
    </>
  )
}

/**
 * Who currently holds a key for this route.
 *
 * The keys themselves are not here and cannot be: only their hashes were ever
 * stored. So this lists who was granted access and when they last used it,
 * which is what you need to decide whether to take it away.
 */
function TokenList({ routeId, onError }) {
  const [tokens, setTokens] = useState(null)

  const load = useCallback(async () => {
    try {
      setTokens(await api.routeTokens(routeId))
    } catch (error) {
      onError(error.message)
    }
  }, [routeId, onError])

  useEffect(() => {
    load()
  }, [load])

  async function revoke(tokenId) {
    try {
      await api.revokeToken(routeId, tokenId)
      load()
    } catch (error) {
      onError(error.message)
    }
  }

  async function revokeAll() {
    try {
      await api.revokeAllTokens(routeId)
      load()
    } catch (error) {
      onError(error.message)
    }
  }

  if (tokens === null) return null

  const live = tokens.filter((token) => !token.revoked && !token.expired)

  return (
    <div className="cf-tokens">
      <div className="cf-tokens-head">
        <strong>Access granted to</strong>
        {live.length > 0 && (
          <button type="button" className="btn ghost small danger" onClick={revokeAll}>
            Revoke all
          </button>
        )}
      </div>

      {tokens.length === 0 ? (
        <p className="hint">
          Nobody yet. A client gets a key by connecting to the address and having
          someone approve it with the password.
        </p>
      ) : (
        <ul className="cf-token-list">
          {tokens.map((token) => {
            const dead = token.revoked || token.expired
            return (
              <li key={token.id} className={dead ? 'dead' : ''}>
                <span className="cf-token-name">{token.label || token.client_id}</span>
                <span className="muted">
                  {token.revoked
                    ? 'revoked'
                    : token.expired
                      ? 'expired'
                      : token.last_used_at
                        ? `last used ${token.last_used_at.slice(0, 16).replace('T', ' ')}`
                        : 'never used'}
                </span>
                {!dead && (
                  <button
                    type="button"
                    className="btn ghost small danger"
                    onClick={() => revoke(token.id)}
                  >
                    Revoke
                  </button>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

function RouteForm({ route, named, onClose, onSaved, onError }) {
  const editing = Boolean(route)
  const [port, setPort] = useState(route ? String(route.port) : '')
  const [hostname, setHostname] = useState(route?.hostname ?? '')
  const [ports, setPorts] = useState([])
  const [busy, setBusy] = useState(false)

  // Protection. Opened by default on a route that already has some, so it is
  // never a surprise that editing this route touches it.
  const [showGuard, setShowGuard] = useState(Boolean(route?.guarded))
  const [headerName, setHeaderName] = useState(route?.guard_header_name ?? '')
  // The stored value is never sent to the browser, so this starts empty and
  // an empty box means "leave it alone" rather than "clear it".
  const [headerValue, setHeaderValue] = useState('')
  const [secretPath, setSecretPath] = useState(Boolean(route?.guard_secret_path))
  const [useOauth, setUseOauth] = useState(Boolean(route?.guard_oauth))
  const [password, setPassword] = useState('')
  const hasStoredValue = Boolean(route?.guard_header_set)
  const hasPassword = Boolean(route?.guard_oauth_ready)

  // OAuth publishes its endpoints at the root of the address, and a secret
  // path would hide them from the client that has to find them. The backend
  // enforces this too; the form just stops it looking available.
  useEffect(() => {
    if (useOauth) setSecretPath(false)
  }, [useOauth])

  // Sensible starting points, filled the moment the section is opened on a
  // route that has nothing set. Authorization is what almost every service
  // expects, and a password nobody has to invent is a password that gets used
  // rather than skipped. Both are still editable.
  useEffect(() => {
    if (!showGuard) return
    setHeaderName((current) => (current || route?.guard_header_set ? current : 'Authorization'))
    setPassword((current) => (current || hasPassword ? current : randomSecret(12)))
  }, [showGuard, hasPassword, route])

  useEffect(() => {
    api.ports().then(setPorts).catch(() => setPorts([]))
  }, [])

  async function submit(event) {
    event.preventDefault()
    const value = Number(port)
    if (!value || value < 1 || value > 65535) {
      onError('Enter a port between 1 and 65535.')
      return
    }

    if (headerName.trim() && !headerValue.trim() && !hasStoredValue) {
      onError('Give the header a value, or clear its name.')
      return
    }
    if (useOauth && !password.trim() && !hasPassword) {
      onError('Set an approval password before switching OAuth on.')
      return
    }

    const guard = {
      guard_header_name: headerName.trim(),
      guard_secret_path: secretPath,
      guard_oauth: useOauth,
    }
    if (password.trim()) guard.guard_oauth_password = password.trim()
    // Only send the value when it was actually typed. Omitting it keeps what
    // is stored; sending "" is how the backend is told to forget it.
    if (headerValue.trim()) guard.guard_header_value = headerValue.trim()
    else if (!headerName.trim()) guard.guard_header_value = ''

    setBusy(true)
    try {
      const saved = editing
        ? await api.editRoute(route.id, { port: value, hostname, ...guard })
        : await api.addRoute({
            port: value,
            hostname,
            ...guard,
            guard_header_value: guard.guard_header_value ?? '',
          })
      onSaved(saved)
    } catch (error) {
      onError(error.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <form onSubmit={submit}>
          <header className="modal-header">
            <h3>{editing ? `Edit port ${route.port}` : 'Publish a port'}</h3>
            <p className="modal-sub">
              {named
                ? 'Traffic for the hostname you choose will reach this port on this machine.'
                : 'This port will be reachable on a temporary address while the tunnel runs.'}
            </p>
          </header>

          <div className="modal-body">
            <label className="input-group">
              <span>Local port</span>
              <input
                type="number"
                min="1"
                max="65535"
                list="cf-open-ports"
                value={port}
                onChange={(event) => setPort(event.target.value)}
                placeholder="8419"
                autoFocus={!editing}
              />
            </label>
            <datalist id="cf-open-ports">
              {ports.map((entry) => (
                <option key={entry.port} value={entry.port}>
                  {entry.managed_app_name || entry.process || 'listening'}
                </option>
              ))}
            </datalist>

            {named && (
              <label className="input-group">
                <span>Hostname</span>
                <input
                  type="text"
                  value={hostname}
                  onChange={(event) => setHostname(event.target.value)}
                  placeholder="app.example.com"
                />
              </label>
            )}

            <p className="hint">
              {named
                ? 'Must be on a domain in your Cloudflare account. The DNS record is created when you switch the route on.'
                : 'Add a hostname later, once a domain is set up — the route itself will not need rebuilding.'}
            </p>

            <div className="cf-guard-block">
              <button
                type="button"
                className="cf-guard-toggle"
                onClick={() => setShowGuard((open) => !open)}
              >
                {showGuard ? '▾' : '▸'} Protection
                {route?.guarded && !showGuard && <span className="pill running">on</span>}
              </button>

              {showGuard && (
                <div className="cf-guard-fields">
                  <p className="hint">
                    Anything published here is reachable by the whole internet. These
                    put a small proxy in front of the port — your application is not
                    changed and does not need to know.
                  </p>

                  <label className="input-group">
                    <span>Send a header</span>
                    <input
                      type="text"
                      value={headerName}
                      onChange={(event) => setHeaderName(event.target.value)}
                      placeholder="Authorization"
                    />
                  </label>

                  <SecretField
                    label="Header value"
                    value={headerValue}
                    onChange={setHeaderValue}
                    placeholder={hasStoredValue ? '•••••••• (unchanged)' : 'Bearer …'}
                    onFetchStored={
                      editing && hasStoredValue
                        ? async () => {
                            try {
                              return (await api.routeHeaderValue(route.id)).value
                            } catch (error) {
                              onError(error.message)
                              return ''
                            }
                          }
                        : null
                    }
                    storedNote={
                      hasStoredValue ? 'Press 👁 to read back the value that is saved.' : ''
                    }
                    hint="Added to every request on the way to your application, so a caller
                          with nowhere to put a credential can still reach a service that
                          wants one. Paste the value your application already expects —
                          Generate is only useful when you control both ends."
                  />

                  <label className="checkbox row">
                    <input
                      type="checkbox"
                      checked={useOauth}
                      onChange={(event) => setUseOauth(event.target.checked)}
                    />
                    <span>Require OAuth</span>
                  </label>
                  <p className="hint">
                    Callers must sign in before anything reaches your application.
                    Clients register themselves, so there is nothing to configure on
                    their side beyond the address — this is the one method
                    claude.ai&apos;s connector was built for. Your application is not
                    changed and never sees the token.
                  </p>

                  {useOauth && (
                    <SecretField
                      label="Approval password"
                      value={password}
                      onChange={setPassword}
                      placeholder={hasPassword ? '•••••••• (unchanged)' : 'choose one'}
                      generateLabel="New"
                      storedNote={
                        hasPassword
                          ? 'A password is set. It is stored as a one-way hash, so it cannot be read back — press New for a different one.'
                          : ''
                      }
                      hint="Typed on the approval screen when a client asks for access —
                            this is what stops anyone who finds the URL granting themselves
                            a key. One has been generated for you: press 👁 and keep a copy
                            before you save."
                    />
                  )}

                  <label className="checkbox row">
                    <input
                      type="checkbox"
                      checked={secretPath}
                      disabled={useOauth}
                      onChange={(event) => setSecretPath(event.target.checked)}
                    />
                    <span>Require a secret path</span>
                  </label>
                  {useOauth && (
                    <p className="hint">
                      Not available with OAuth — the sign-in endpoints have to be
                      findable at the root of the address.
                    </p>
                  )}
                  <p className="hint">
                    Requests must arrive under a long random path; everything else gets
                    a 404. Copy the address from the table to get the full link.{' '}
                    {secretPath && route?.guard_secret_path
                      ? 'The existing path is kept, so links already handed out keep working.'
                      : ''}
                    <strong> This hides the address, it does not authenticate anyone</strong> —
                    treat the link like a password.
                  </p>
                </div>
              )}
            </div>

            {editing && route.guard_oauth && (
              <TokenList routeId={route.id} onError={onError} />
            )}

            {editing && route.last_error && (
              <div className="banner warn">
                <strong>Last error</strong> {route.last_error}
              </div>
            )}
          </div>

          <footer className="modal-footer">
            <button type="button" className="btn ghost" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="btn primary" disabled={busy}>
              {busy ? 'Saving…' : editing ? 'Save changes' : 'Add route'}
            </button>
          </footer>
        </form>
      </div>
    </div>
  )
}
