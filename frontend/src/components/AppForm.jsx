import { useEffect, useState } from 'react'
import { api } from '../api.js'

const BLANK = {
  name: '',
  description: '',
  command: '',
  working_dir: '',
  port: '',
  envText: '',
  auto_restart: false,
  start_on_boot: false,
  live_reload: false,
  preview_url: '',
  news_enabled: true,
  news_keywords: '',
  needs: [],
  protected: false,
  uses_models: '',
}

function envToText(env) {
  return Object.entries(env ?? {})
    .map(([key, value]) => `${key}=${value}`)
    .join('\n')
}

function textToEnv(text) {
  const env = {}
  for (const rawLine of text.split('\n')) {
    const line = rawLine.trim()
    if (!line || line.startsWith('#')) continue
    const index = line.indexOf('=')
    if (index === -1) throw new Error(`Environment line is missing "=": ${line}`)
    env[line.slice(0, index).trim()] = line.slice(index + 1).trim()
  }
  return env
}

export default function AppForm({ app, apps = [], onClose, onSaved, onError }) {
  const [form, setForm] = useState(BLANK)
  const [saving, setSaving] = useState(false)
  const [portHint, setPortHint] = useState(null)

  useEffect(() => {
    if (app) {
      setForm({
        name: app.name,
        description: app.description,
        command: app.command,
        working_dir: app.working_dir,
        port: app.port ?? '',
        envText: envToText(app.env),
        auto_restart: app.auto_restart,
        start_on_boot: app.start_on_boot,
        live_reload: app.live_reload ?? false,
        preview_url: app.preview_url ?? '',
        news_enabled: app.news_enabled ?? true,
        news_keywords: app.news_keywords ?? '',
        needs: app.needs ?? [],
        protected: app.protected ?? false,
        uses_models: app.uses_models ?? '',
      })
    } else {
      setForm(BLANK)
    }
    setPortHint(null)
  }, [app])

  // Tell the user up front when the port they typed is already taken.
  useEffect(() => {
    const port = Number(form.port)
    if (!port || port < 1 || port > 65535) {
      setPortHint(null)
      return
    }
    const timer = setTimeout(() => {
      api
        .inspectPort(port)
        .then((info) => {
          if (!info.in_use) return setPortHint({ ok: true, text: `Port ${port} is free.` })
          if (app && info.managed_app_id === app.id) {
            return setPortHint({ ok: true, text: `Port ${port} is held by this app.` })
          }
          const who = info.managed_app_name || info.process || 'another process'
          setPortHint({ ok: false, text: `Port ${port} is already used by ${who}.` })
        })
        .catch(() => setPortHint(null))
    }, 400)
    return () => clearTimeout(timer)
  }, [form.port, app])

  function update(field, value) {
    setForm((previous) => ({ ...previous, [field]: value }))
  }

  async function handleSubmit(event) {
    event.preventDefault()
    setSaving(true)
    try {
      const payload = {
        name: form.name,
        description: form.description,
        command: form.command,
        working_dir: form.working_dir,
        port: form.port === '' ? null : Number(form.port),
        env: textToEnv(form.envText),
        auto_restart: form.auto_restart,
        start_on_boot: form.start_on_boot,
        live_reload: form.live_reload,
        preview_url: form.preview_url.trim(),
        news_enabled: form.news_enabled,
        news_keywords: form.news_keywords.trim(),
        needs: form.needs,
        protected: form.protected,
        uses_models: form.uses_models.trim(),
      }
      const saved = app ? await api.updateApp(app.id, payload) : await api.createApp(payload)
      onSaved(saved)
    } catch (error) {
      onError(error.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <form className="modal" onClick={(event) => event.stopPropagation()} onSubmit={handleSubmit}>
        <header className="modal-header">
          <h3>{app ? `Edit ${app.name}` : 'Add application'}</h3>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        <div className="modal-body">
          <label className="input-group">
            <span>Name *</span>
            <input
              required
              value={form.name}
              onChange={(event) => update('name', event.target.value)}
              placeholder="admin-dashboard"
            />
          </label>

          <label className="input-group">
            <span>Description</span>
            <input
              value={form.description}
              onChange={(event) => update('description', event.target.value)}
              placeholder="Internal admin panel"
            />
          </label>

          <label className="input-group">
            <span>Start command *</span>
            <input
              required
              className="mono"
              value={form.command}
              onChange={(event) => update('command', event.target.value)}
              placeholder="npm run dev"
            />
          </label>

          <label className="input-group">
            <span>Working directory *</span>
            <input
              required
              className="mono"
              value={form.working_dir}
              onChange={(event) => update('working_dir', event.target.value)}
              placeholder="/home/hh/projects/admin"
            />
          </label>

          <label className="input-group">
            <span>Port</span>
            <input
              type="number"
              min="1"
              max="65535"
              value={form.port}
              onChange={(event) => update('port', event.target.value)}
              placeholder="3000"
            />
            {portHint && (
              <small className={portHint.ok ? 'hint ok' : 'hint bad'}>{portHint.text}</small>
            )}
          </label>

          <label className="input-group">
            <span>Preview URL (for the live wall)</span>
            <input
              className="mono"
              value={form.preview_url}
              onChange={(event) => update('preview_url', event.target.value)}
              placeholder={form.port ? `http://127.0.0.1:${form.port}/` : '/dashboard or http://localhost:5173'}
            />
            <small className="hint">
              Leave blank to use the port above. Set it when the page you want to watch is on a
              different port or path.
            </small>
          </label>

          <label className="input-group">
            <span>Environment variables</span>
            <textarea
              className="mono"
              rows="4"
              value={form.envText}
              onChange={(event) => update('envText', event.target.value)}
              placeholder={'NODE_ENV=development\nAPI_URL=http://localhost:8000'}
            />
            <small className="hint">One KEY=value per line.</small>
          </label>

          <label className="checkbox row">
            <input
              type="checkbox"
              checked={form.auto_restart}
              onChange={(event) => update('auto_restart', event.target.checked)}
            />
            Restart automatically if it crashes
          </label>

          <label className="checkbox row">
            <input
              type="checkbox"
              checked={form.start_on_boot}
              onChange={(event) => update('start_on_boot', event.target.checked)}
            />
            Start this app whenever God Father starts
          </label>
          {form.start_on_boot && (
            <small className="hint">
              Launched a few seconds after boot. Skipped if something already holds its port.
            </small>
          )}

          <label className="checkbox row">
            <input
              type="checkbox"
              checked={form.live_reload}
              onChange={(event) => update('live_reload', event.target.checked)}
            />
            Live reload — show my edits on the wall as I make them
          </label>
          {form.live_reload && (
            <small className="hint">
              Watches the working directory. A page, stylesheet or bundle just reloads this app's
              tile; anything the server loaded at startup restarts it first, then reloads the tile
              once the port answers again. Leave it off for apps you are not editing.
            </small>
          )}

          <label className="checkbox row">
            <input
              type="checkbox"
              checked={form.news_enabled}
              onChange={(event) => update('news_enabled', event.target.checked)}
            />
            Show this app on the news ticker
          </label>
          {form.news_enabled && (
            <label className="input-group">
              <span>News keywords</span>
              <input
                value={form.news_keywords}
                onChange={(event) => update('news_keywords', event.target.value)}
                placeholder="payment, order, signup, login"
              />
              <small className="hint">
                Comma-separated. A log line containing one of these becomes news, alongside errors,
                warnings, crashes and restarts. For a clean headline, have the app print{' '}
                <code>[news] Payment received ₹25,000</code> (or <code>[news:good]</code>,{' '}
                <code>[news:warn]</code>, <code>[news:error]</code>).
              </small>
            </label>
          )}

          <fieldset className="focus-fields">
            <legend>Focus Mode</legend>

            <label className="checkbox row">
              <input
                type="checkbox"
                checked={form.protected}
                onChange={(event) => update('protected', event.target.checked)}
              />
              🔒 Protected — Focus Mode never stops this app
            </label>

            <label className="input-group">
              <span>Uses AI models (Ollama)</span>
              <input
                className="mono"
                value={form.uses_models}
                onChange={(event) => update('uses_models', event.target.value)}
                placeholder="llama3.1:8b"
              />
              <small className="hint">
                Comma-separated. Focus Mode keeps these loaded and unloads every other model to free
                the GPU.
              </small>
            </label>

            <div className="input-group">
              <span>Needs these apps</span>
              <div className="needs-list">
                {apps
                  .filter((other) => other.id !== app?.id)
                  .map((other) => (
                    <label key={other.id} className="checkbox row">
                      <input
                        type="checkbox"
                        checked={form.needs.includes(other.id)}
                        onChange={(event) =>
                          update(
                            'needs',
                            event.target.checked
                              ? [...form.needs, other.id]
                              : form.needs.filter((id) => id !== other.id)
                          )
                        }
                      />
                      {other.name}
                    </label>
                  ))}
              </div>
              <small className="hint">
                Focus Mode keeps these running (and starts them first) when you focus on this app.
              </small>
            </div>
          </fieldset>
        </div>

        <footer className="modal-footer">
          <button type="button" className="btn ghost" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn primary" disabled={saving}>
            {saving ? 'Saving…' : app ? 'Save changes' : 'Add application'}
          </button>
        </footer>
      </form>
    </div>
  )
}
