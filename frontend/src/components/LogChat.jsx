import { useEffect, useMemo, useRef, useState } from 'react'
import { api, logTapeSocketUrl } from '../api.js'

// Lines kept on screen. Older ones drift off the top.
const KEEP = 80

// Lines allowed to wait for their turn. A chatty app can print hundreds a
// second; past this the oldest waiting are skipped, and the feed says so.
const MAX_QUEUE = 60

// How long a line gets before the next one appears, by how far behind we are.
// Calm when things are quiet, quicker only when it has to catch up.
function gapFor(waiting) {
  if (waiting > 30) return 140
  if (waiting > 10) return 280
  return 520
}

// An access-log line says one thing — this request got this status — wrapped
// in an address and a second timestamp. Covers the "GET /x HTTP/1.1" 200 form
// that http.server, uvicorn, Flask and gunicorn all print.
const ACCESS = /"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) (\S+) HTTP\/[\d.]+" (\d{3})/
// A date-and-time the app printed at the start of the line; the panel shows its own.
const STAMP = /^\[?\d{4}-\d\d-\d\d[T ][\d:.,]+(?:Z|[+-]\d\d:?\d\d)?\]?\s*[-|]?\s*/

/** The line as it reads best in a narrow panel, and the colour it deserves. */
function tidy(message) {
  const request = ACCESS.exec(message.message)
  if (request) {
    const status = Number(request[3])
    const level = status >= 500 ? 'error' : status >= 400 ? 'warn' : message.level
    return { text: `${request[1]} ${request[2]} → ${status}`, level }
  }
  return { text: message.message.replace(STAMP, '') || message.message, level: message.level }
}

function clock(iso) {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
}

/**
 * Every application's log as one plain, live list, newest at the bottom.
 *
 * A line never moves sideways and never races: it appears, sits still, and
 * drifts up only as newer lines arrive. Lines are let in one at a time at a
 * readable pace, so a burst from one app arrives as a steady flow rather than
 * a blur, and hovering stops the flow entirely while you read.
 */
export default function LogChat({ onSelectApp, onHide }) {
  const [messages, setMessages] = useState([])
  const [online, setOnline] = useState(false)
  const [held, setHeld] = useState(false)
  const [waiting, setWaiting] = useState(0)
  const [only, setOnly] = useState(null) // app_id, or null for every app
  const asideRef = useRef(null)
  const listRef = useRef(null)
  const [height, setHeight] = useState(null)
  const queue = useRef([])
  const skipped = useRef(0)
  const heldRef = useRef(false)
  const onlyRef = useRef(null)

  heldRef.current = held
  onlyRef.current = only

  // Apps seen so far, for the filter chips.
  const [apps, setApps] = useState([])

  useEffect(() => {
    let socket = null
    let retry = null
    let closed = false
    let newest = 0

    const remember = (items) =>
      setApps((previous) => {
        const known = new Map(previous.map((app) => [app.id, app]))
        for (const item of items) if (!known.has(item.app_id)) known.set(item.app_id, { id: item.app_id, name: item.app_name })
        return known.size === previous.length ? previous : [...known.values()].sort((a, b) => a.name.localeCompare(b.name))
      })

    function enqueue(item) {
      if (item.id <= newest) return
      newest = item.id
      remember([item])
      if (onlyRef.current && item.app_id !== onlyRef.current) return
      queue.current.push(item)
      if (queue.current.length > MAX_QUEUE) {
        skipped.current += queue.current.length - MAX_QUEUE
        queue.current.splice(0, queue.current.length - MAX_QUEUE)
      }
      setWaiting(queue.current.length)
    }

    function connect() {
      socket = new WebSocket(logTapeSocketUrl())
      socket.onopen = () => {
        setOnline(true)
        api
          .logTape(30)
          .then((items) => {
            const ordered = items.sort((a, b) => a.id - b.id)
            if (!newest) {
              // The recent past is shown as it was, not replayed line by line.
              newest = ordered.length ? ordered[ordered.length - 1].id : 0
              remember(ordered)
              setMessages(ordered.slice(-KEEP))
            } else {
              ordered.forEach(enqueue)
            }
          })
          .catch(() => {})
      }
      socket.onmessage = (event) => {
        let message
        try {
          message = JSON.parse(event.data)
        } catch {
          return
        }
        if (!message.ping) enqueue(message)
      }
      socket.onclose = () => {
        setOnline(false)
        if (!closed) retry = setTimeout(connect, 3000)
      }
    }

    connect()
    return () => {
      closed = true
      clearTimeout(retry)
      socket?.close()
    }
  }, [])

  // Let one line in at a time, at a pace that depends on how far behind we are.
  useEffect(() => {
    let last = 0
    const timer = setInterval(() => {
      if (heldRef.current || !queue.current.length) return
      const now = performance.now()
      if (now - last < gapFor(queue.current.length)) return
      last = now

      const item = queue.current.shift()
      const gap = skipped.current
      skipped.current = 0
      setWaiting(queue.current.length)
      setMessages((previous) => {
        const next = gap ? [...previous, { id: `skip-${item.id}`, skip: gap }, item] : [...previous, item]
        return next.slice(-KEEP)
      })
    }, 60)
    return () => clearInterval(timer)
  }, [])

  // Fill the screen from where the panel starts down to the bottom edge, so the
  // newest line — at the bottom — is always visible. Measured from the panel's
  // container, which does not stick, so scrolling does not change the answer.
  useEffect(() => {
    const aside = asideRef.current
    const layout = aside?.parentElement
    const scroller = aside?.closest('.content')
    if (!aside || !layout) return

    const measure = () => {
      // Stacked under the tiles on a narrow screen: the stylesheet's height applies.
      if (getComputedStyle(aside).position !== 'sticky') return setHeight(null)
      const viewTop = scroller ? scroller.getBoundingClientRect().top : 0
      const viewHeight = scroller ? scroller.clientHeight : window.innerHeight
      const offset = layout.getBoundingClientRect().top - viewTop + (scroller?.scrollTop ?? 0)
      setHeight(Math.max(360, Math.floor(viewHeight - offset - 20)))
    }

    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(aside.closest('.detail') ?? layout)
    window.addEventListener('resize', measure)
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', measure)
    }
  }, [])

  // Follow the newest line unless the reader is holding the feed.
  useEffect(() => {
    const list = listRef.current
    if (list && !held) list.scrollTop = list.scrollHeight
  }, [messages, held])

  function onScroll() {
    const list = listRef.current
    if (!list) return
    const fromBottom = list.scrollHeight - list.scrollTop - list.clientHeight
    if (fromBottom > 60 && !held) setHeld(true)
  }

  function resume() {
    setHeld(false)
  }

  function chooseApp(id) {
    setOnly(id)
    queue.current = []
    skipped.current = 0
    setWaiting(0)
  }

  const shown = useMemo(
    () => (only ? messages.filter((message) => message.skip || message.app_id === only) : messages),
    [messages, only]
  )

  return (
    <aside
      className="logchat"
      ref={asideRef}
      style={height ? { height } : undefined}
      aria-label="Live logs from every application"
    >
      <header className="logchat-head">
        <div className="logchat-title">
          <span className={`logchat-dot ${online ? 'on' : ''}`} />
          <strong>Live logs</strong>
          <span className="logchat-sub">{online ? (held ? 'paused' : 'live') : 'reconnecting…'}</span>
        </div>
        <button className="logchat-hide" onClick={onHide} title="Hide the log panel">
          ✕
        </button>
      </header>

      {apps.length > 1 && (
        <div className="logchat-chips" role="group" aria-label="Show logs from">
          <button className={only === null ? 'active' : ''} onClick={() => chooseApp(null)}>
            All
          </button>
          {apps.map((app) => (
            <button
              key={app.id}
              className={only === app.id ? 'active' : ''}
              onClick={() => chooseApp(only === app.id ? null : app.id)}
            >
              {app.name}
            </button>
          ))}
        </div>
      )}

      <div
        className="logchat-list"
        ref={listRef}
        onScroll={onScroll}
        onMouseEnter={() => setHeld(true)}
        onMouseLeave={resume}
      >
        {shown.length === 0 && (
          <p className="logchat-empty">
            Quiet for now. Every line your applications write will appear here, newest at the bottom.
          </p>
        )}
        {shown.map((message) =>
          message.skip ? (
            <div key={message.id} className="logchat-skip">
              {message.skip} line{message.skip > 1 ? 's' : ''} skipped to keep up
            </div>
          ) : (
            <LogLine key={message.id} message={message} onSelectApp={onSelectApp} />
          )
        )}
      </div>

      {held && (
        <button className="logchat-resume" onClick={resume}>
          {waiting ? `${waiting} new line${waiting > 1 ? 's' : ''} ↓` : 'Back to live ↓'}
        </button>
      )}
    </aside>
  )
}

function LogLine({ message, onSelectApp }) {
  const { text, level } = tidy(message)
  return (
    <div
      className={`logchat-msg ${level}`}
      onClick={() => onSelectApp?.(message.app_id)}
      title={`${message.message}\n\nClick to open ${message.app_name}`}
    >
      <span className="logchat-time">{clock(message.created_at)}</span>
      <span className="logchat-app">{message.app_name}</span>
      <span className="logchat-text">{text}</span>
    </div>
  )
}
