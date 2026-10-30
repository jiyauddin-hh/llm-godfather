import { useEffect, useRef, useState } from 'react'
import { api, newsSocketUrl } from '../api.js'

// Pixels per second. Normal crosses a wide screen in about eight seconds.
const SPEEDS = [
  { key: 'slow', label: 'Slow', px: 70 },
  { key: 'normal', label: 'Normal', px: 120 },
  { key: 'fast', label: 'Fast', px: 180 },
]
const SPEED_KEY = 'godfather.news.speed'

function savedSpeed() {
  try {
    const saved = localStorage.getItem(SPEED_KEY)
    if (SPEEDS.some((choice) => choice.key === saved)) return saved
  } catch {
    // Private window or blocked storage: fall through to the default.
  }
  // Someone who asked their system for less motion gets the slow tape until
  // they pick otherwise.
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'slow' : 'normal'
}

// Headlines kept for the tape to loop through when nothing new is happening.
const BACKLOG = 40

// A flood (a crash loop, a busy [news] printer) must not bury the tape for
// minutes: past this many waiting, the oldest waiting are dropped.
const MAX_PENDING = 15

function clock(iso) {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
}

/**
 * A TV-style news ticker: one line scrolling right to left, fed live.
 *
 * The tape is driven by hand rather than by a CSS animation. A keyframe loop
 * has to restart whenever its content changes, and a ticker that jumps back to
 * the start every time a headline arrives is unreadable exactly when it
 * matters. Here a new headline simply joins the end of the tape, right behind
 * whatever is scrolling past now, and when the news runs out the recent
 * headlines go round again.
 */
export default function NewsTicker({ onSelectApp }) {
  const viewportRef = useRef(null)
  const trackRef = useRef(null)
  const selectRef = useRef(onSelectApp)
  const [empty, setEmpty] = useState(true)
  const [online, setOnline] = useState(false)
  const [speed, setSpeed] = useState(savedSpeed)
  // Read by the animation loop every frame, so a change applies mid-scroll
  // without tearing the tape down.
  const pxRef = useRef(120)

  selectRef.current = onSelectApp
  pxRef.current = SPEEDS.find((choice) => choice.key === speed)?.px ?? 120

  function chooseSpeed(key) {
    setSpeed(key)
    try {
      localStorage.setItem(SPEED_KEY, key)
    } catch {
      // Not remembered, but still applied.
    }
  }

  useEffect(() => {
    const viewport = viewportRef.current
    const track = trackRef.current

    let backlog = [] // oldest → newest
    let pending = [] // arrived, not on the tape yet
    let cursor = 0
    let newest = 0
    let offset = viewport.clientWidth
    let paused = false
    let last = performance.now()
    let frame = 0
    let socket = null
    let retry = null
    let closed = false

    function build(item, fresh) {
      const el = document.createElement('span')
      el.className = `news-item ${item.level}${fresh ? ' fresh' : ''}`

      const time = document.createElement('span')
      time.className = 'news-time'
      time.textContent = clock(item.created_at)
      const app = document.createElement('span')
      app.className = 'news-app'
      app.textContent = item.app_name || 'God Father'
      const text = document.createElement('span')
      text.className = 'news-text'
      text.textContent = item.message
      el.append(time, app, text)

      if (item.app_id) {
        el.classList.add('news-link')
        el.title = `Open ${item.app_name}`
        el.onclick = () => selectRef.current?.(item.app_id)
      }
      return el
    }

    function next() {
      if (pending.length) return build(pending.shift(), true)
      if (!backlog.length) return null
      const item = backlog[cursor % backlog.length]
      cursor = (cursor + 1) % backlog.length
      return build(item, false)
    }

    // Keep the tape exactly one headline past the right edge. Queueing more
    // than that would leave fresh news stuck behind a long line of old news.
    function fill() {
      const width = viewport.clientWidth
      while (offset + track.scrollWidth <= width) {
        const el = next()
        if (!el) break
        track.appendChild(el)
      }
    }

    function trim() {
      let first = track.firstElementChild
      while (first && offset + first.offsetWidth < 0) {
        offset += first.offsetWidth
        first.remove()
        first = track.firstElementChild
      }
    }

    function tick(now) {
      // Clamped, so a tab coming back from the background does not leap.
      const dt = Math.min(now - last, 100) / 1000
      last = now
      if (!paused && track.firstElementChild) offset -= pxRef.current * dt
      trim()
      fill()
      if (!track.firstElementChild) offset = viewport.clientWidth
      track.style.transform = `translateX(${offset}px)`
      frame = requestAnimationFrame(tick)
    }

    function receive(items, announce) {
      const fresh = items.filter((item) => item.id > newest).sort((a, b) => a.id - b.id)
      if (!fresh.length) return
      newest = fresh[fresh.length - 1].id
      const first = !backlog.length
      backlog = [...backlog, ...fresh].slice(-BACKLOG)
      cursor = cursor % backlog.length
      if (first && !announce) {
        // Opening the wall should show news at once, starting with the latest,
        // not an empty bar waiting for the first headline to crawl in.
        cursor = Math.max(0, backlog.length - 8)
        offset = 0
      }
      if (announce) pending = [...pending, ...fresh].slice(-MAX_PENDING)
      setEmpty(false)
    }

    const onEnter = () => (paused = true)
    const onLeave = () => (paused = false)
    viewport.addEventListener('mouseenter', onEnter)
    viewport.addEventListener('mouseleave', onLeave)

    function connect() {
      socket = new WebSocket(newsSocketUrl())
      socket.onopen = () => {
        setOnline(true)
        // Whatever happened while we were away, including before the first load.
        api
          .news(BACKLOG)
          .then((items) => receive(items, newest > 0))
          .catch(() => {})
      }
      socket.onmessage = (event) => {
        let message
        try {
          message = JSON.parse(event.data)
        } catch {
          return
        }
        if (message.ping) return
        receive([message], true)
      }
      socket.onclose = () => {
        setOnline(false)
        if (!closed) retry = setTimeout(connect, 3000)
      }
    }

    connect()
    frame = requestAnimationFrame(tick)

    return () => {
      closed = true
      cancelAnimationFrame(frame)
      clearTimeout(retry)
      socket?.close()
      viewport.removeEventListener('mouseenter', onEnter)
      viewport.removeEventListener('mouseleave', onLeave)
    }
  }, [])

  return (
    <div className="news-ticker" role="marquee" aria-label="News from your applications">
      <span className={`news-label ${online ? 'on' : ''}`} title={online ? 'Live' : 'Reconnecting…'}>
        <span className="news-pulse" />
        News
      </span>
      <div className="news-viewport" ref={viewportRef}>
        <div className="news-track" ref={trackRef} />
        {empty && (
          <span className="news-empty">
            No news yet. Starts, stops, crashes, errors and anything an app announces will scroll by here.
          </span>
        )}
      </div>
      <div className="news-seg" role="group" aria-label="Ticker speed">
        {SPEEDS.map((choice) => (
          <button
            key={choice.key}
            className={speed === choice.key ? 'active' : ''}
            onClick={() => chooseSpeed(choice.key)}
            title={`${choice.label} scrolling`}
          >
            {choice.label}
          </button>
        ))}
      </div>
    </div>
  )
}
