import { useState } from 'react'
import { api } from '../api.js'

/**
 * Local-network sharing for one application. The toggle is the whole feature;
 * everything else here explains what it did and how to reach the app.
 */
export default function SharePanel({ app, network, onChanged, onError, onNotice }) {
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)

  const { runtime } = app
  const shared = app.share_lan
  const noPort = !app.port

  async function toggle() {
    setBusy(true)
    try {
      const result = await api.setShare(app.id, !shared)
      onNotice(
        result.enabled
          ? `${app.name} is reachable at ${result.lan_url}`
          : `${app.name} is local-only again`
      )
      if (result.enabled && result.firewall_managed === false) {
        onError(`Firewall not managed — run manually: ${result.manual_command}`)
      }
      onChanged()
    } catch (error) {
      onError(error.message)
    } finally {
      setBusy(false)
    }
  }

  function copy() {
    if (!runtime.lan_url) return
    navigator.clipboard?.writeText(runtime.lan_url)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <div className="share-panel">
      <div className="share-head">
        <div>
          <h4>Local network access</h4>
          <p className="share-sub">
            {noPort
              ? 'Set a port for this application before it can be shared.'
              : shared
                ? 'Other devices on your network can reach this app.'
                : `Only reachable on this machine (localhost:${app.port}).`}
          </p>
        </div>

        <button
          className={`btn ${shared ? 'danger' : 'success'}`}
          disabled={busy || noPort}
          onClick={toggle}
        >
          {busy ? 'Working…' : shared ? 'Make local-only' : 'Open to local network'}
        </button>
      </div>

      {shared && runtime.lan_url && (
        <div className="share-url-row">
          <code className="share-url">{runtime.lan_url}</code>
          <button className="btn ghost small" onClick={copy}>
            {copied ? 'Copied' : 'Copy'}
          </button>
        </div>
      )}

      {shared && (
        <ul className="share-facts">
          <li>
            <span className="share-key">How</span>
            {runtime.share_detail ?? '—'}
          </li>
          <li>
            <span className="share-key">Firewall</span>
            {runtime.firewall_open === true
              ? `open for ${network?.subnet ?? 'your subnet'}`
              : runtime.firewall_open === false
                ? 'rule not present'
                : 'not managed by God Father'}
          </li>
        </ul>
      )}

      {network?.setup_hint && !network.ufw_manageable && (
        <div className="share-hint">
          {network.setup_hint}
          <br />
          Until then the port is bound, but ufw may still block it.
        </div>
      )}

      {!network?.lan_ip && (
        <div className="share-hint">
          No local network interface detected — this machine may not be on a LAN.
        </div>
      )}
    </div>
  )
}
