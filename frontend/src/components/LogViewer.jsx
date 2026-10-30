import { useEffect, useMemo, useRef, useState } from 'react'
import { api, logSocketUrl } from '../api.js'
import { formatTime } from '../utils.js'

const MAX_LINES = 5000

export default function LogViewer({ appId, onError }) {
  const [lines, setLines] = useState([])
  const [filter, setFilter] = useState('')
  const [autoScroll, setAutoScroll] = useState(true)
  const [connected, setConnected] = useState(false)

  const bottomRef = useRef(null)
  const scrollRef = useRef(null)
  const socketRef = useRef(null)

  useEffect(() => {
    setLines([])
    let closedByUs = false

    const socket = new WebSocket(logSocketUrl(appId))
    socketRef.current = socket

    socket.onopen = () => setConnected(true)
    socket.onclose = () => {
      setConnected(false)
      // A closed socket without an explicit unmount means the backend went away.
      if (!closedByUs) onError?.('Log stream disconnected — is the backend running?')
    }
    socket.onerror = () => setConnected(false)
    socket.onmessage = (event) => {
      const line = JSON.parse(event.data)
      if (line.text === '__ping__') return
      setLines((previous) => {
        const next = [...previous, line]
        return next.length > MAX_LINES ? next.slice(-MAX_LINES) : next
      })
    }

    return () => {
      closedByUs = true
      socket.close()
      socketRef.current = null
    }
  }, [appId, onError])

  useEffect(() => {
    if (autoScroll) bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [lines, autoScroll])

  const visible = useMemo(() => {
    const term = filter.trim().toLowerCase()
    if (!term) return lines
    return lines.filter((line) => line.text.toLowerCase().includes(term))
  }, [lines, filter])

  async function handleClear() {
    try {
      await api.clearLogs(appId)
      setLines([])
    } catch (error) {
      onError?.(error.message)
    }
  }

  function handleDownload() {
    const text = lines.map((line) => `${line.ts}\t${line.stream}\t${line.text}`).join('\n')
    const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }))
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `app-${appId}.log`
    anchor.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="log-viewer">
      <div className="log-toolbar">
        <span className={`stream-status ${connected ? 'live' : 'offline'}`}>
          <span className="pulse" /> {connected ? 'Live' : 'Offline'}
        </span>

        <input
          className="log-filter"
          type="search"
          placeholder="Filter lines…"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
        />

        <label className="checkbox">
          <input
            type="checkbox"
            checked={autoScroll}
            onChange={(event) => setAutoScroll(event.target.checked)}
          />
          Auto-scroll
        </label>

        <span className="log-count">
          {visible.length}
          {filter ? ` / ${lines.length}` : ''} lines
        </span>

        <button className="btn ghost small" onClick={handleDownload} disabled={!lines.length}>
          Download
        </button>
        <button className="btn ghost small danger" onClick={handleClear}>
          Clear
        </button>
      </div>

      <div className="log-output" ref={scrollRef}>
        {visible.length === 0 ? (
          <p className="empty-hint pad">
            {filter ? 'Nothing matches that filter.' : 'No output yet. Start the application to see logs.'}
          </p>
        ) : (
          visible.map((line, index) => (
            <div key={index} className={`log-line ${line.stream}`}>
              <span className="log-ts">{formatTime(line.ts)}</span>
              <span className="log-text">{line.text || ' '}</span>
            </div>
          ))
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
