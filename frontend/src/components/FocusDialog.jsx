import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'

const GB = 1024 ** 3

function gb(bytes) {
  return `${(bytes / GB).toFixed(1)} GB`
}

// Preview first, act on Confirm. Focus Mode stops real applications, so the
// list of what goes down — and why everything else stays — is shown before
// anything is touched.
export default function FocusDialog({ app, onClose, onStarted, onError }) {
  const [preview, setPreview] = useState(null)
  const [starting, setStarting] = useState(false)
  // The parent re-renders on every poll with fresh callbacks; the preview must
  // be taken once per app, not once per poll.
  const callbacks = useRef({ onError, onClose })
  callbacks.current = { onError, onClose }

  useEffect(() => {
    api
      .focusPreview(app.id)
      .then(setPreview)
      .catch((error) => {
        callbacks.current.onError(error.message)
        callbacks.current.onClose()
      })
  }, [app.id])

  async function confirm() {
    setStarting(true)
    try {
      await api.focusStart(app.id)
      onStarted()
    } catch (error) {
      onError(error.message)
      setStarting(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal wide" onClick={(event) => event.stopPropagation()}>
        <header className="modal-header">
          <h3>◎ Focus on {app.name}</h3>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        <div className="modal-body focus-preview">
          {!preview ? (
            <p className="muted">Working out what to stop and what to keep…</p>
          ) : (
            <>
              {preview.blocked && <div className="banner warn">{preview.blocked}</div>}

              <div className="focus-summary">
                <div>
                  <strong>{preview.stop.length}</strong>
                  <span>apps stopped</span>
                </div>
                <div>
                  <strong>{preview.freed.ram_mb >= 1024 ? `${(preview.freed.ram_mb / 1024).toFixed(1)} GB` : `${Math.round(preview.freed.ram_mb)} MB`}</strong>
                  <span>RAM freed</span>
                </div>
                <div>
                  <strong>{gb(preview.freed.vram_bytes)}</strong>
                  <span>
                    GPU memory freed
                    {preview.gpu ? ` (of ${gb(preview.gpu.vram_used)} used / ${gb(preview.gpu.vram_total)})` : ''}
                  </span>
                </div>
              </div>

              {preview.warnings.map((text) => (
                <div key={text} className="banner warn">
                  {text}
                </div>
              ))}

              <FocusSection title="Kept" tone="keep" empty="Nothing else is needed.">
                {preview.keep.map((item) => (
                  <li key={item.id}>
                    <span className="focus-name">{item.name}</span>
                    <span className="focus-why">{item.reason}</span>
                    <span className={`focus-action ${item.action === 'start' ? 'start' : ''}`}>{item.action}</span>
                  </li>
                ))}
              </FocusSection>

              <FocusSection title="Stopped" tone="stop" empty="Nothing else is running under God Father.">
                {preview.stop.map((item) => (
                  <li key={item.id}>
                    <span className="focus-name">{item.name}</span>
                    <span className="focus-why">
                      {item.port ? `port ${item.port}` : ''}
                      {item.memory_mb ? ` · ${Math.round(item.memory_mb)} MB` : ''}
                    </span>
                    {item.warning && <span className="focus-warn">{item.warning}</span>}
                  </li>
                ))}
              </FocusSection>

              <FocusSection
                title="AI models"
                tone="stop"
                empty={
                  preview.wanted_models.length
                    ? `Only the models ${app.name} uses are loaded.`
                    : `No models loaded. Set "Uses AI models" on ${app.name} to keep its model loaded.`
                }
              >
                {preview.unload_models.map((model) => (
                  <li key={model.name}>
                    <span className="focus-name mono">{model.name}</span>
                    <span className="focus-why">{gb(model.vram)} on the GPU</span>
                    <span className="focus-action">unload{model.pinned ? ' (reloaded on exit)' : ''}</span>
                  </li>
                ))}
                {preview.keep_models.map((name) => (
                  <li key={name}>
                    <span className="focus-name mono">{name}</span>
                    <span className="focus-why">used by {app.name}</span>
                    <span className="focus-action">keep</span>
                  </li>
                ))}
              </FocusSection>

              <FocusSection title="Not controlled" tone="muted" empty="">
                {preview.not_controlled.map((item) => (
                  <li key={item.id}>
                    <span className="focus-name">{item.name}</span>
                    <span className="focus-why">{item.reason}</span>
                  </li>
                ))}
              </FocusSection>

              <p className="hint">
                What is running now is saved first. <strong>Exit Focus Mode</strong> restarts exactly these apps
                and stops anything Focus Mode started.
              </p>
            </>
          )}
        </div>

        <footer className="modal-footer">
          <button className="btn ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn primary"
            disabled={!preview || preview.blocked || starting}
            onClick={confirm}
          >
            {starting ? 'Starting…' : `Confirm — focus on ${app.name}`}
          </button>
        </footer>
      </div>
    </div>
  )
}

function FocusSection({ title, tone, empty, children }) {
  const items = (Array.isArray(children) ? children.flat() : [children]).filter(Boolean)
  if (!items.length && !empty) return null
  return (
    <section className={`focus-section ${tone}`}>
      <h4>
        {title} <span className="muted">({items.length})</span>
      </h4>
      {items.length ? <ul>{items}</ul> : <p className="muted">{empty}</p>}
    </section>
  )
}
