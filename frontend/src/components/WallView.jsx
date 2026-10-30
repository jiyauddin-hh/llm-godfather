import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, wallSocketUrl } from '../api.js'
import AnalyticsView from './AnalyticsView.jsx'
import LogChat from './LogChat.jsx'
import NewsTicker from './NewsTicker.jsx'

// Frames render at desktop width and are scaled to fit their tile, so each one
// reads as a small monitor rather than a squeezed mobile layout.
const FRAME_WIDTH = 1280
const FRAME_HEIGHT = 800

const COLUMN_CHOICES = [1, 2, 3, 4]

const VIEW_KEY = 'godfather.wall.view'
const LOGS_KEY = 'godfather.wall.logs'

function savedLogsOpen() {
  try {
    return localStorage.getItem(LOGS_KEY) !== 'hidden'
  } catch {
    return true
  }
}

function savedView() {
  try {
    return localStorage.getItem(VIEW_KEY) === 'analytics' ? 'analytics' : 'wall'
  } catch {
    return 'wall'
  }
}

// Off by default: reloading a tile throws away whatever state the app had, so
// it should be something you ask for rather than something that just happens.
const REFRESH_CHOICES = [
  { label: 'Off', ms: 0 },
  { label: 'Every 10 seconds', ms: 10_000 },
  { label: 'Every 30 seconds', ms: 30_000 },
  { label: 'Every minute', ms: 60_000 },
  { label: 'Every 5 minutes', ms: 300_000 },
  { label: 'Every 15 minutes', ms: 900_000 },
  { label: 'Every 30 minutes', ms: 1_800_000 },
  { label: 'Every hour', ms: 3_600_000 },
  { label: 'Every 6 hours', ms: 21_600_000 },
]

// A cross-origin frame is granted nothing by default, so an app that asks for
// the camera is refused before the user ever sees a prompt — the panel renders
// and then sits dead. Delegating these restores the app's ability to *ask*;
// the browser still puts its own prompt in front of every one of them, and
// anything not listed here (geolocation, clipboard, payment) stays denied.
const FRAME_ALLOW = 'camera; microphone; display-capture; fullscreen'

const GRID_GAP = 14
const TILE_BAR_HEIGHT = 35
const MIN_TILE_WIDTH = 150
const MIN_TILE_HEIGHT = 110
const TARGET_RATIO = 1.6

/**
 * Choose columns *and* a tile height so every tile lands inside the viewport.
 * Picking columns alone is what leaves the last rows below the fold, so this
 * solves for both and prefers the layout whose tiles stay closest to 16:10.
 */
function computeFit(container, count) {
  if (!container || !count) return { columns: 1, tileHeight: null }

  const width = container.clientWidth
  const available = window.innerHeight - container.getBoundingClientRect().top - 20
  let best = null

  for (let columns = 1; columns <= count; columns++) {
    const rows = Math.ceil(count / columns)
    const tileWidth = (width - GRID_GAP * (columns - 1)) / columns
    const tileHeight = (available - GRID_GAP * (rows - 1)) / rows
    if (tileWidth < MIN_TILE_WIDTH || tileHeight < MIN_TILE_HEIGHT) continue

    const ratio = tileWidth / Math.max(1, tileHeight - TILE_BAR_HEIGHT)
    const score = Math.abs(Math.log(ratio / TARGET_RATIO))
    if (!best || score < best.score) best = { columns, tileHeight, score }
  }

  // Too many tiles for the window: go as square as possible and let them shrink.
  if (!best) {
    const columns = Math.ceil(Math.sqrt(count))
    const rows = Math.ceil(count / columns)
    return { columns, tileHeight: Math.max(MIN_TILE_HEIGHT, (available - GRID_GAP * (rows - 1)) / rows) }
  }
  return { columns: best.columns, tileHeight: best.tileHeight }
}

const KIND_LABEL = {
  blocked: 'Refuses embedding',
  text: 'Not a web page',
  error: 'Error response',
  down: 'Not running',
  noport: 'No port set',
}

/** A live page, scaled down to whatever width the tile happens to be. */
function TileFrame({ url, title, reloadKey }) {
  const boxRef = useRef(null)
  const [scale, setScale] = useState(0.25)
  const [frameHeight, setFrameHeight] = useState(FRAME_HEIGHT)

  useEffect(() => {
    const box = boxRef.current
    if (!box) return
    const observer = new ResizeObserver(([entry]) => {
      const next = entry.contentRect.width / FRAME_WIDTH
      setScale(next)
      // A Fit tile can be taller than 16:10. Give the page a taller window
      // rather than leaving the rest of the tile black.
      setFrameHeight(Math.max(FRAME_HEIGHT, Math.ceil(entry.contentRect.height / next)))
    })
    observer.observe(box)
    return () => observer.disconnect()
  }, [])

  return (
    <div className="tile-body" ref={boxRef}>
      <iframe
        key={reloadKey}
        className="tile-frame"
        src={url}
        title={title}
        allow={FRAME_ALLOW}
        loading="lazy"
        style={{ width: FRAME_WIDTH, height: frameHeight, transform: `scale(${scale})` }}
      />
    </div>
  )
}

export default function WallView({ onSelectApp, onError, onNotice }) {
  const [view, setView] = useState(savedView)
  const [logsOpen, setLogsOpen] = useState(savedLogsOpen)
  const [tiles, setTiles] = useState([])
  const [loading, setLoading] = useState(true)
  const [mode, setMode] = useState(2) // 1 | 2 | 3 | 4 | 'fit'
  const [fit, setFit] = useState({ columns: 2, tileHeight: null })
  const wallRef = useRef(null)
  const [showAll, setShowAll] = useState(false)
  const [paused, setPaused] = useState({})
  const [reloadKeys, setReloadKeys] = useState({})
  const [refreshMs, setRefreshMs] = useState(0)
  const [lastRefresh, setLastRefresh] = useState(null)
  const [net, setNet] = useState(null)
  const [netBusy, setNetBusy] = useState(false)
  const [portDraft, setPortDraft] = useState('')
  const [lastChange, setLastChange] = useState(null)
  // Revision last rendered per tile. Null until the first probe, so arriving
  // at the wall long after an edit does not reload everything on sight.
  const revisions = useRef(null)

  const bumpTiles = useCallback((ids) => {
    setReloadKeys((previous) => {
      const next = { ...previous }
      for (const id of ids) next[id] = (next[id] ?? 0) + 1
      return next
    })
  }, [])

  /** Reload the tiles whose watched files changed since we last looked. */
  const applyRevisions = useCallback(
    (list) => {
      const previous = revisions.current
      const current = {}
      const moved = []
      for (const tile of list) {
        const rev = tile.rev ?? 0
        current[tile.app_id] = rev
        if (previous && (previous[tile.app_id] ?? 0) !== rev) moved.push(tile.app_id)
      }
      revisions.current = current
      if (moved.length) bumpTiles(moved)
    },
    [bumpTiles]
  )

  const load = useCallback(async () => {
    try {
      const list = await api.wall()
      setTiles(list)
      applyRevisions(list)
    } catch (error) {
      onError(error.message)
    } finally {
      setLoading(false)
    }
  }, [onError, applyRevisions])

  useEffect(() => {
    load()
    // Only re-probe; this does not reload the frames themselves.
    const timer = setInterval(load, 15000)
    return () => clearInterval(timer)
  }, [load])

  // The probe timer is the wrong shape for an edit — waiting it out is the
  // very delay live reload exists to remove — so changes arrive on their own.
  useEffect(() => {
    let socket = null
    let retry = null
    let closed = false

    const connect = () => {
      socket = new WebSocket(wallSocketUrl())

      socket.onmessage = (event) => {
        let message
        try {
          message = JSON.parse(event.data)
        } catch {
          return
        }
        if (message.ping) return

        const rev = message.rev ?? 0
        if (revisions.current) {
          if ((revisions.current[message.app_id] ?? 0) === rev) return
          revisions.current[message.app_id] = rev
        }
        bumpTiles([message.app_id])
        setLastChange({ ...message, at: new Date() })
        // A restart moved the status dot through stopped, and a file may have
        // turned an unshowable app into a page worth framing.
        if (message.restarted) load()
      }

      socket.onclose = () => {
        if (!closed) retry = setTimeout(connect, 3000)
      }
    }

    connect()
    return () => {
      closed = true
      clearTimeout(retry)
      socket?.close()
    }
  }, [bumpTiles, load])

  useEffect(() => {
    api
      .wallNetwork()
      .then((state) => {
        setNet(state)
        setPortDraft(String(state.port))
      })
      .catch(() => setNet(null))
  }, [])

  async function toggleNetwork() {
    setNetBusy(true)
    try {
      const next = await api.setWallNetwork(!net?.enabled, Number(portDraft) || undefined)
      setNet(next)
      setPortDraft(String(next.port))
      onNotice(next.enabled ? `Wall published at ${next.url}` : 'Wall is no longer on the network')
      if (next.enabled && !next.firewall_managed && next.manual_command) {
        onError(`Firewall not managed — run: ${next.manual_command}`)
      }
    } catch (error) {
      onError(error.message)
    } finally {
      setNetBusy(false)
    }
  }

  const live = useMemo(() => tiles.filter((tile) => tile.kind === 'web'), [tiles])
  const others = useMemo(() => tiles.filter((tile) => tile.kind !== 'web'), [tiles])
  const watched = useMemo(() => tiles.filter((tile) => tile.watching).length, [tiles])
  const visible = showAll ? tiles : live

  // The log panel takes width from the tiles, so opening or closing it changes the fit.
  useEffect(() => {
    if (mode !== 'fit') return
    const recompute = () => setFit(computeFit(wallRef.current, visible.length))
    recompute()
    window.addEventListener('resize', recompute)
    return () => window.removeEventListener('resize', recompute)
  }, [mode, visible.length, logsOpen])

  const columns = mode === 'fit' ? fit.columns : mode

  const reloadTile = (id) => setReloadKeys((p) => ({ ...p, [id]: (p[id] ?? 0) + 1 }))

  const reloadAll = useCallback(() => {
    bumpTiles(tiles.map((tile) => tile.app_id))
    setLastRefresh(new Date())
    load()
  }, [tiles, load, bumpTiles])

  useEffect(() => {
    if (!refreshMs) return
    const timer = setInterval(reloadAll, refreshMs)
    return () => clearInterval(timer)
  }, [refreshMs, reloadAll])
  const togglePause = (id) => setPaused((p) => ({ ...p, [id]: !p[id] }))

  function toggleLogs() {
    setLogsOpen((open) => {
      try {
        localStorage.setItem(LOGS_KEY, open ? 'hidden' : 'shown')
      } catch {
        // Not remembered, but still applied.
      }
      return !open
    })
  }

  function chooseView(next) {
    setView(next)
    try {
      localStorage.setItem(VIEW_KEY, next)
    } catch {
      // Private window or blocked storage: the choice just is not remembered.
    }
  }

  return (
    <section className="detail">
      <header className="detail-header">
        <div className="detail-title">
          <div>
            <h2>Live wall</h2>
            <p className="detail-sub">
              {view === 'analytics'
                ? 'Requests, errors and load for every application, read from their own logs'
                : loading
                ? 'Probing applications…'
                : `${live.length} rendering live · ${others.length} cannot be shown` +
                  (watched ? ` · ⚡ ${watched} watching for edits` : '') +
                  (lastRefresh ? ` · refreshed ${lastRefresh.toLocaleTimeString([], { hour12: false })}` : '')}
            </p>
            {view === 'wall' && lastChange && (
              <p className="detail-sub live-note">
                ⚡ {lastChange.name}: {lastChange.changed}
                {lastChange.restarted ? ' — restarted and reloaded' : ' — tile reloaded'} at{' '}
                {lastChange.at.toLocaleTimeString([], { hour12: false })}
              </p>
            )}
          </div>
        </div>

        {view === 'wall' && (
          <div className="detail-actions">
            <div className="seg">
              {COLUMN_CHOICES.map((count) => (
                <button
                  key={count}
                  className={`seg-btn ${mode === count ? 'active' : ''}`}
                  onClick={() => setMode(count)}
                  title={`${count} per row`}
                >
                  {count}×
                </button>
              ))}
              <button
                className={`seg-btn ${mode === 'fit' ? 'active' : ''}`}
                onClick={() => setMode('fit')}
                title="Fit every tile on screen, no scrolling"
              >
                Fit
              </button>
            </div>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={showAll}
                onChange={(event) => setShowAll(event.target.checked)}
              />
              Include unshowable
            </label>
            <label className="refresh-pick">
              <span>Refresh</span>
              <select value={refreshMs} onChange={(event) => setRefreshMs(Number(event.target.value))}>
                {REFRESH_CHOICES.map((choice) => (
                  <option key={choice.ms} value={choice.ms}>
                    {choice.label}
                  </option>
                ))}
              </select>
            </label>
            <button className="btn ghost" onClick={reloadAll} title="Reload every tile now">
              ⟳ Refresh now
            </button>
            <button
              className={`btn ghost ${logsOpen ? 'active' : ''}`}
              onClick={toggleLogs}
              title={logsOpen ? 'Hide the live log panel' : 'Show every app’s log as a live feed'}
            >
              💬 Live logs
            </button>
          </div>
        )}
      </header>

      <NewsTicker onSelectApp={onSelectApp} />

      <div className="tabs wall-tabs">
        <button className={`tab ${view === 'wall' ? 'active' : ''}`} onClick={() => chooseView('wall')}>
          ▦ Wall
        </button>
        <button
          className={`tab ${view === 'analytics' ? 'active' : ''}`}
          onClick={() => chooseView('analytics')}
        >
          📊 Analytics
        </button>
      </div>

      {view === 'analytics' && <AnalyticsView onSelectApp={onSelectApp} onError={onError} />}

      {/* Hidden rather than unmounted, so switching tabs does not reload every frame. */}
      <div hidden={view !== 'wall'}>
        <div className={`wall-layout ${logsOpen ? 'with-logs' : ''}`}>
          <div className="wall-main">
            <div className={`netwall ${net?.enabled ? 'on' : ''}`}>
              <div className="netwall-main">
                <strong>Watch this wall from another device</strong>
                <p>
                  {net?.enabled
                    ? 'Published on your local network as a read-only page — no start, stop or edit controls.'
                    : `Publishes a read-only copy of this wall on its own port, so you can open it on a phone or another machine${net?.lan_ip ? ` at ${net.lan_ip}` : ''}.`}
                </p>
              </div>

              {net?.enabled && net.url ? (
                <a className="netwall-url" href={net.url} target="_blank" rel="noreferrer">
                  {net.url}
                </a>
              ) : (
                <label className="netwall-port">
                  <span>Port</span>
                  <input
                    type="number"
                    min="1"
                    max="65535"
                    value={portDraft}
                    onChange={(event) => setPortDraft(event.target.value)}
                  />
                </label>
              )}

              <button
                className={`btn ${net?.enabled ? 'danger' : 'success'}`}
                disabled={netBusy || !net?.lan_ip}
                onClick={toggleNetwork}
              >
                {netBusy ? 'Working…' : net?.enabled ? 'Stop publishing' : 'Open on local network'}
              </button>
            </div>

            {net?.enabled && !net.firewall_managed && net.manual_command && (
              <div className="banner warn">
                The port is bound, but ufw may still block it. Allow it with:{' '}
                <code>{net.manual_command}</code>
              </div>
            )}

            {!loading && visible.length === 0 && (
              <p className="empty-hint pad">
                Nothing to display yet. Start an application that serves a web page and it will appear here.
              </p>
            )}

            <div
              ref={wallRef}
              className={`wall ${mode === 'fit' ? 'fit' : ''}`}
              style={{
                gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`,
                ...(mode === 'fit' && fit.tileHeight ? { '--tile-h': `${fit.tileHeight}px` } : {}),
              }}
            >
              {visible.map((tile) => {
                const isPaused = paused[tile.app_id]
                const showFrame = tile.kind === 'web' && tile.url && !isPaused

                return (
                  <article key={tile.app_id} className={`tile ${tile.kind}`}>
                    <header className="tile-bar">
                      <span className={`dot ${tile.status}`} />
                      <button className="tile-name" onClick={() => onSelectApp(tile.app_id)} title="Open this app">
                        {tile.name}
                      </button>
                      <span className="tile-port">:{tile.port ?? '—'}</span>
                      {tile.watching && (
                        <span className="tile-live" title="Live reload is on for this app">
                          ⚡
                        </span>
                      )}

                      <div className="tile-actions">
                        {tile.kind === 'web' && (
                          <>
                            <button className="icon-btn" title={isPaused ? 'Resume' : 'Pause'} onClick={() => togglePause(tile.app_id)}>
                              {isPaused ? '▶' : '❚❚'}
                            </button>
                            <button className="icon-btn" title="Reload" onClick={() => reloadTile(tile.app_id)}>
                              ⟳
                            </button>
                            <a className="icon-btn" href={tile.url} target="_blank" rel="noreferrer" title="Open in a new tab">
                              ↗
                            </a>
                          </>
                        )}
                      </div>
                    </header>

                    {showFrame ? (
                      <TileFrame url={tile.url} title={tile.name} reloadKey={reloadKeys[tile.app_id] ?? 0} />
                    ) : (
                      <div className="tile-body">
                        <div className="tile-placeholder">
                          <strong>{isPaused ? 'Paused' : (KIND_LABEL[tile.kind] ?? tile.kind)}</strong>
                          {!isPaused && tile.note && <span>{tile.note}</span>}
                          {isPaused && (
                            <button className="btn ghost small" onClick={() => togglePause(tile.app_id)}>
                              Resume
                            </button>
                          )}
                          {!isPaused && tile.kind === 'error' && (
                            <span className="tile-tip">
                              If this app's page lives on another port or path, set its preview URL in Edit.
                            </span>
                          )}
                        </div>
                      </div>
                    )}
                  </article>
                )
              })}
            </div>
          </div>
          {logsOpen && <LogChat onSelectApp={onSelectApp} onHide={toggleLogs} />}
        </div>
      </div>
    </section>
  )
}
