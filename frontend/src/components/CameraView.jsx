import { useCallback, useEffect, useState } from 'react'
import { api, captureImageUrl, captureClipUrl, ringClipUrl } from '../api.js'

const KIND_LABEL = {
  started: 'Started',
  stopped: 'Stopped',
  crashed: 'Crashed',
  takeover: 'Taken over',
  shutdown: 'Shut down',
  power_loss: 'Power cut',
  test: 'Test shot',
}

function ago(seconds) {
  if (seconds === null || seconds === undefined) return 'no frame yet'
  if (seconds < 90) return `${Math.round(seconds)}s ago`
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`
  return `${Math.round(seconds / 3600)} h ago`
}

/* Alive and producing frames are different things, and the gap between them is
   the failure worth showing: a stream that died silently looks exactly like one
   that is working until the moment you need the footage. */
function recorderStatus(state) {
  if (!state) return { tone: 'idle', label: 'Loading…', detail: '' }
  if (!state.enabled) {
    return { tone: 'idle', label: 'Not recording', detail: 'the camera log is off' }
  }
  if (state.recording && state.healthy) {
    return {
      tone: 'live',
      label: 'Recording',
      detail: `newest frame ${ago(state.newest_age)} · holding ${state.ring_frames} frames (${state.ring_mb} MB)`,
    }
  }
  if (state.recording) {
    return {
      tone: 'stalled',
      label: 'Stalled',
      detail: `the camera is open but no frame has landed — newest is ${ago(state.newest_age)}`,
    }
  }
  return {
    tone: 'down',
    label: 'Stopped',
    detail: state.stream_error || 'the recording is not running',
  }
}

function when(iso) {
  const at = new Date(iso)
  return `${at.toLocaleDateString([], { day: '2-digit', month: 'short' })} ${at.toLocaleTimeString([], { hour12: false })}`
}

export default function CameraView({ onError, onNotice }) {
  const [state, setState] = useState(null)
  const [shots, setShots] = useState([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [viewing, setViewing] = useState(null)
  const [capDraft, setCapDraft] = useState('')
  const [clip, setClip] = useState(null)
  const [everyDraft, setEveryDraft] = useState('')
  const [keepDraft, setKeepDraft] = useState('')
  const [rescueDraft, setRescueDraft] = useState('')

  const load = useCallback(async () => {
    try {
      const [settings, list] = await Promise.all([api.cameraSettings(), api.captures()])
      setState(settings)
      setShots(list)
      setCapDraft((previous) => previous || String(settings.max_mb))
      setEveryDraft((previous) => previous || String(settings.interval))
      setKeepDraft((previous) => previous || String(settings.keep_minutes))
      setRescueDraft((previous) => previous || String(settings.incident_minutes))
    } catch (error) {
      onError(error.message)
    } finally {
      setLoading(false)
    }
  }, [onError])

  useEffect(() => {
    load()
    const timer = setInterval(load, 5000)
    return () => clearInterval(timer)
  }, [load])

  async function save(changes, message) {
    setBusy(true)
    try {
      setState(await api.setCameraSettings(changes))
      if (message) onNotice(message)
      load()
    } catch (error) {
      onError(error.message)
    } finally {
      setBusy(false)
    }
  }

  async function testShot() {
    setBusy(true)
    try {
      const shot = await api.testShot()
      onNotice(shot.ok ? 'Camera works — the shot is at the top of the log.' : `No photo: ${shot.note}`)
      if (!shot.ok) onError(shot.note)
      load()
    } catch (error) {
      onError(error.message)
    } finally {
      setBusy(false)
    }
  }

  async function remove(id) {
    try {
      await api.deleteCapture(id)
      setViewing(null)
      load()
    } catch (error) {
      onError(error.message)
    }
  }

  async function clearAll() {
    setBusy(true)
    try {
      await api.clearCaptures()
      onNotice('Camera log cleared')
      load()
    } catch (error) {
      onError(error.message)
    } finally {
      setBusy(false)
    }
  }

  const blocked = state && !state.available
  const status = recorderStatus(state)

  return (
    <section className="detail">
      <header className="detail-header">
        <div className="detail-title">
          <div>
            <h2>Camera log</h2>
            <p className="detail-sub">
              {loading
                ? 'Loading…'
                : `${state?.count ?? 0} captures · ${state?.used_mb ?? 0} MB of ${state?.max_mb ?? 0} MB`}
            </p>
          </div>
        </div>

        <div className="detail-actions">
          <button
            className="btn ghost clip-live"
            onClick={() => setClip({ mode: 'ring', minutes: 5, at: Date.now() })}
            disabled={!state?.recording || (state?.ring_frames ?? 0) < 2}
            title={
              (state?.ring_frames ?? 0) < 2
                ? 'Nothing recorded yet'
                : 'The last five minutes of the live recording, ending just now'
            }
          >
            ▶ Live · last 5 min
          </button>

          <button
            className="btn ghost clip-cut"
            onClick={() =>
              setClip({
                mode: 'capture',
                id: state.last_cut_id,
                at: state.last_cut_at,
                frames: state.last_cut_frames,
              })
            }
            disabled={!state?.last_cut_id}
            title={
              state?.last_cut_id
                ? `The five minutes leading up to the power cut of ${when(state.last_cut_at)}`
                : 'No power cut on record — nothing to play'
            }
          >
            ▶ Power cut · last 5 min
          </button>

          <button className="btn ghost" onClick={testShot} disabled={busy || blocked}>
            ◉ Test shot
          </button>
          <button className="btn danger ghost" onClick={clearAll} disabled={busy || !shots.length}>
            Clear log
          </button>
        </div>
      </header>

      {blocked && (
        <div className="banner warn">
          Captures cannot run here: {state.unavailable_reason}
        </div>
      )}

      {!loading && (
        <div className={`recbar ${status.tone}`}>
          <span className="recdot" />
          <strong>{status.label}</strong>
          <span className="recdetail">{status.detail}</span>
          {state?.restarts > 0 && (
            <span className="recrestarts" title="the stream died and was restarted">
              {state.restarts} restart{state.restarts === 1 ? '' : 's'}
            </span>
          )}
        </div>
      )}

      <div className={`netwall ${state?.enabled ? 'on' : ''}`}>
        <div className="netwall-main">
          <strong>Keep a rolling recording, so nothing has to be photographed on demand</strong>
          <p>
            {state?.enabled
              ? `On. A frame every ${state.interval}s, the last ${state.keep_minutes} minutes kept. Stops, crashes and takeovers are filed from it — and if the power is cut, the last frame before the machine died is recovered on the next boot.`
              : 'Off. Turn it on and the camera records continuously in the background. Nothing is uploaded — frames stay on this machine and older ones are deleted automatically.'}
          </p>
          <label className="netwall-check">
            <input
              type="checkbox"
              checked={state?.capture_starts ?? false}
              disabled={busy || blocked}
              onChange={(event) =>
                save(
                  { capture_starts: event.target.checked },
                  event.target.checked ? 'Also filing app starts' : 'No longer filing app starts',
                )
              }
            />
            <span>
              Also file a frame when an app <em>starts</em> — off by default, because a boot
              autostarts every app in turn and each one photographs an empty room
            </span>
          </label>
        </div>

        <label className="netwall-port">
          <span>Camera</span>
          <select
            value={state?.device ?? ''}
            disabled={busy || blocked}
            onChange={(event) => save({ device: event.target.value })}
          >
            {(state?.devices ?? []).map((path) => (
              <option key={path} value={path}>
                {path}
              </option>
            ))}
          </select>
        </label>

        <label className="netwall-port">
          <span>Frame every (s)</span>
          <input
            type="number"
            min="2"
            max="300"
            value={everyDraft}
            disabled={busy || blocked}
            onChange={(event) => setEveryDraft(event.target.value)}
            onBlur={() => {
              const value = Number(everyDraft)
              if (value >= 2 && value <= 300 && value !== state?.interval) {
                save({ interval: value }, `Recording a frame every ${value}s`)
              }
            }}
          />
        </label>

        <label className="netwall-port">
          <span>Keep last (min)</span>
          <input
            type="number"
            min="1"
            max="1440"
            value={keepDraft}
            disabled={busy || blocked}
            onChange={(event) => setKeepDraft(event.target.value)}
            onBlur={() => {
              const value = Number(keepDraft)
              if (value >= 1 && value <= 1440 && value !== state?.keep_minutes) {
                save({ keep_minutes: value }, `Keeping the last ${value} minutes of recording`)
              }
            }}
          />
        </label>

        <label className="netwall-port">
          <span>Rescue on cut (min)</span>
          <input
            type="number"
            min="0.5"
            max="120"
            step="0.5"
            value={rescueDraft}
            disabled={busy || blocked}
            onChange={(event) => setRescueDraft(event.target.value)}
            onBlur={() => {
              const value = Number(rescueDraft)
              if (value >= 0.5 && value <= 120 && value !== state?.incident_minutes) {
                save({ incident_minutes: value }, `Rescuing ${value} minutes of run-up after a power cut`)
              }
            }}
          />
        </label>

        <label className="netwall-port">
          <span>Keep up to (MB)</span>
          <input
            type="number"
            min="1"
            max="100000"
            value={capDraft}
            disabled={busy}
            onChange={(event) => setCapDraft(event.target.value)}
            onBlur={() => {
              const value = Number(capDraft)
              if (value >= 1 && value !== state?.max_mb) {
                save({ max_mb: value }, `Keeping up to ${value} MB — oldest deleted first`)
              }
            }}
          />
        </label>

        <button
          className={`btn ${state?.enabled ? 'danger' : 'success'}`}
          disabled={busy || blocked}
          onClick={() => save({ enabled: !state?.enabled }, state?.enabled ? 'Captures off' : 'Captures on')}
        >
          {busy ? 'Working…' : state?.enabled ? 'Turn off' : 'Turn on'}
        </button>
      </div>

      {!loading && !shots.length && (
        <p className="empty-hint pad">
          Nothing filed yet. With the recording on, the next stop, crash or takeover is filed here
          from the frame nearest the moment it happened — and so is the last frame before a power
          cut, recovered when the machine comes back.
        </p>
      )}

      <div className="shots">
        {shots.map((shot) => (
          <article key={shot.id} className={`shot ${shot.kind} ${shot.ok ? '' : 'failed'}`}>
            <div className="shot-image">
              {shot.ok && shot.file ? (
                <img
                  src={captureImageUrl(shot.id)}
                  alt={`${shot.app_name} ${shot.kind}`}
                  loading="lazy"
                  onClick={() => setViewing(shot)}
                />
              ) : (
                <div className="shot-missing">
                  <strong>No photo</strong>
                  <span>{shot.note || 'the camera did not produce a frame'}</span>
                </div>
              )}
            </div>
            <header className="shot-bar">
              <span className={`shot-kind ${shot.kind}`}>{KIND_LABEL[shot.kind] ?? shot.kind}</span>
              <span className="shot-app">{shot.app_name || 'unknown'}</span>
              <time className="shot-time">{when(shot.created_at)}</time>
            </header>
            {shot.message && <p className="shot-note">{shot.message}</p>}
            {shot.ok && shot.note && <p className="shot-note dim">{shot.note}</p>}
            {shot.clip && (
              <button
                className="btn ghost shot-clip"
                onClick={() =>
                  setClip({
                    mode: 'capture',
                    id: shot.id,
                    at: shot.created_at,
                  })
                }
              >
                ▶ The minutes before this cut
              </button>
            )}
          </article>
        ))}
      </div>

      {clip && (
        <div className="modal-backdrop" onClick={() => setClip(null)}>
          <div className="modal clip-modal" onClick={(event) => event.stopPropagation()}>
            <header className="modal-header">
              <h3>
                {clip.mode === 'capture'
                  ? `Power cut · the minutes leading up to ${when(clip.at)}`
                  : `Live recording · last ${clip.minutes} minutes`}
              </h3>
              <button type="button" className="icon-btn" onClick={() => setClip(null)} aria-label="Close">
                ✕
              </button>
            </header>
            <div className="modal-body">
              <video
                key={clip.mode === 'capture' ? `cap-${clip.id}` : `ring-${clip.minutes}-${clip.at}`}
                className="clip-video"
                src={clip.mode === 'capture' ? captureClipUrl(clip.id) : ringClipUrl(clip.minutes, clip.at)}
                controls
                autoPlay
                loop
                playsInline
              />
              <p className="hint">
                {clip.mode === 'capture'
                  ? 'Rescued from the recording when the machine came back — everything else from that period was swept. It ends at the moment the power went.'
                  : 'A timelapse of the live recording, ending a few seconds ago.'}
                {' '}The clock burnt into each frame is when that frame was actually taken; if it
                jumps, the recording had a gap there.
              </p>
            </div>
            <footer className="modal-footer">
              <div className="clip-windows">
                {clip.mode === 'ring' &&
                  [1, 5, state?.keep_minutes ?? 20]
                    .filter((m, i, all) => m > 0 && all.indexOf(m) === i)
                    .sort((a, b) => a - b)
                    .map((m) => (
                      <button
                        key={m}
                        className={`btn ghost ${clip.minutes === m ? 'active' : ''}`}
                        onClick={() => setClip({ mode: 'ring', minutes: m, at: Date.now() })}
                      >
                        {m} min
                      </button>
                    ))}
                {clip.mode === 'ring' && (
                  <button
                    className="btn ghost"
                    onClick={() => setClip({ ...clip, at: Date.now() })}
                    title="Re-render with everything recorded since"
                  >
                    ↻ Refresh
                  </button>
                )}
              </div>
              <a
                className="btn ghost"
                href={clip.mode === 'capture' ? captureClipUrl(clip.id) : ringClipUrl(clip.minutes, clip.at)}
                download
              >
                Download
              </a>
            </footer>
          </div>
        </div>
      )}

      {viewing && (
        <div className="modal-backdrop" onClick={() => setViewing(null)}>
          <div className="modal shot-modal" onClick={(event) => event.stopPropagation()}>
            <header className="modal-header">
              <h3>
                {KIND_LABEL[viewing.kind] ?? viewing.kind} · {viewing.app_name} · {when(viewing.created_at)}
              </h3>
              <button type="button" className="icon-btn" onClick={() => setViewing(null)} aria-label="Close">
                ✕
              </button>
            </header>
            <div className="modal-body">
              <img className="shot-full" src={captureImageUrl(viewing.id)} alt="" />
              {viewing.message && <p className="hint">{viewing.message}</p>}
            </div>
            <footer className="modal-footer">
              <button className="btn danger ghost" onClick={() => remove(viewing.id)}>
                Delete this capture
              </button>
              <button className="btn ghost" onClick={() => setViewing(null)}>
                Close
              </button>
            </footer>
          </div>
        </div>
      )}
    </section>
  )
}
