import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import AppDetail from './components/AppDetail.jsx'
import AiView from './components/AiView.jsx'
import AppForm from './components/AppForm.jsx'
import CameraView from './components/CameraView.jsx'
import CloudflareView from './components/CloudflareView.jsx'
import DiscoverModal from './components/DiscoverModal.jsx'
import FocusBanner from './components/FocusBanner.jsx'
import FocusDialog from './components/FocusDialog.jsx'
import PortsView from './components/PortsView.jsx'
import Sidebar from './components/Sidebar.jsx'
import SystemView from './components/SystemView.jsx'
import WallView from './components/WallView.jsx'

const POLL_MS = 3000

export default function App() {
  const [apps, setApps] = useState([])
  const [stats, setStats] = useState(null)
  const [network, setNetwork] = useState(null)
  const [selectedId, setSelectedId] = useState(null)
  const [view, setView] = useState('apps')
  const [statusFilter, setStatusFilter] = useState('all')
  const [online, setOnline] = useState(true)
  const [formState, setFormState] = useState(null) // null | { app: object|null }
  const [toast, setToast] = useState(null)
  const [confirming, setConfirming] = useState(null)
  const [scanning, setScanning] = useState(false)
  const [focus, setFocus] = useState(null)
  const [focusing, setFocusing] = useState(null) // the app whose Focus preview is open
  const [dismissedFocus, setDismissedFocus] = useState(() => {
    try {
      return localStorage.getItem('gf-focus-dismissed') || ''
    } catch {
      return ''
    }
  })

  const selectedRef = useRef(selectedId)
  selectedRef.current = selectedId

  const notify = useCallback((message, tone = 'info') => {
    setToast({ message, tone })
    setTimeout(() => setToast(null), 4000)
  }, [])

  const onError = useCallback((message) => notify(message, 'error'), [notify])
  const onNotice = useCallback((message) => notify(message, 'success'), [notify])

  const refresh = useCallback(async () => {
    try {
      const [list, summary, focusState] = await Promise.all([
        api.listApps(),
        api.stats(),
        api.focus().catch(() => null),
      ])
      setApps(list)
      setStats(summary)
      setFocus(focusState)
      setOnline(true)

      // Keep a valid selection as apps are added or removed.
      if (list.length && !list.some((app) => app.id === selectedRef.current)) {
        setSelectedId(list[0].id)
      } else if (!list.length) {
        setSelectedId(null)
      }
    } catch {
      setOnline(false)
    }
  }, [])

  // Wait for each poll to finish before scheduling the next, so a slow backend
  // is never handed a growing queue of identical requests.
  useEffect(() => {
    let timer = null
    let stopped = false
    const tick = async () => {
      await refresh()
      if (!stopped) timer = setTimeout(tick, POLL_MS)
    }
    tick()
    return () => {
      stopped = true
      clearTimeout(timer)
    }
  }, [refresh])

  // Network details change rarely — fetch once rather than on every poll.
  useEffect(() => {
    api.network().then(setNetwork).catch(() => setNetwork(null))
  }, [])

  const selected = apps.find((app) => app.id === selectedId) ?? null

  async function handleDelete(app) {
    try {
      await api.deleteApp(app.id)
      setConfirming(null)
      onNotice(`${app.name} removed`)
      refresh()
    } catch (error) {
      onError(error.message)
    }
  }

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">GF</span>
          <div>
            <h1>God Father</h1>
            <p>Application, log &amp; port manager</p>
          </div>
        </div>

        <div className="topbar-stats">
          {[
            ['all', 'Apps', stats?.total, ''],
            ['running', 'Running', stats?.running, 'running'],
            ['stopped', 'Stopped', stats?.stopped, 'stopped'],
            ['external', 'Outside', stats?.external, 'external'],
            ['crashed', 'Crashed', stats?.crashed, 'crashed'],
            ['ports', 'Ports up', stats?.ports_in_use, 'info'],
          ].map(([key, label, value, tone]) => (
            <StatPill
              key={key}
              label={label}
              value={value ?? '—'}
              tone={tone}
              active={statusFilter === key}
              onClick={() => setStatusFilter(statusFilter === key ? 'all' : key)}
            />
          ))}
        </div>

        <div className={`backend-status ${online ? 'up' : 'down'}`}>
          <span className="pulse" />
          {online ? 'Backend connected' : 'Backend unreachable'}
        </div>
      </header>

      {focus?.phase && (focus.active || focus.busy || focus.ended_at !== dismissedFocus) && (
        <FocusBanner
          focus={focus}
          onChanged={refresh}
          onError={onError}
          onDismiss={() => {
            setDismissedFocus(focus.ended_at)
            try {
              localStorage.setItem('gf-focus-dismissed', focus.ended_at)
            } catch {
              /* private window: the banner just reappears next visit */
            }
          }}
        />
      )}

      <div className="body">
        <Sidebar
          apps={apps}
          statusFilter={statusFilter}
          onClearStatusFilter={() => setStatusFilter('all')}
          selectedId={selectedId}
          view={view}
          onSelect={(id) => {
            setSelectedId(id)
            setView('apps')
          }}
          onViewChange={setView}
          onCreate={() => setFormState({ app: null })}
          onScan={() => setScanning(true)}
        />

        <main className="content">
          {view === 'wall' ? (
            <WallView
              onError={onError}
              onNotice={onNotice}
              onSelectApp={(id) => {
                setSelectedId(id)
                setView('apps')
              }}
            />
          ) : view === 'camera' ? (
            <CameraView onError={onError} onNotice={onNotice} />
          ) : view === 'system' ? (
            <SystemView onError={onError} onNotice={onNotice} />
          ) : view === 'ai' ? (
            <AiView
              onError={onError}
              onNotice={onNotice}
              onSelectApp={(id) => {
                setSelectedId(id)
                setView('apps')
              }}
            />
          ) : view === 'cloudflare' ? (
            <CloudflareView
              onError={onError}
              onNotice={onNotice}
              onSelectApp={(id) => {
                setSelectedId(id)
                setView('apps')
              }}
            />
          ) : view === 'ports' ? (
            <PortsView
              onError={onError}
              onNotice={onNotice}
              onSelectApp={(id) => {
                setSelectedId(id)
                setView('apps')
              }}
            />
          ) : selected ? (
            <AppDetail
              key={selected.id}
              app={selected}
              apps={apps}
              network={network}
              onChanged={refresh}
              onEdit={(app) => setFormState({ app })}
              onDelete={setConfirming}
              onFocus={setFocusing}
              focusActive={Boolean(focus?.active || focus?.busy)}
              onError={onError}
              onNotice={onNotice}
            />
          ) : (
            <div className="placeholder">
              <h2>No application selected</h2>
              <p>
                Register your first application — its command, folder and port — and God Father will run
                it, capture its logs and watch its port for you.
              </p>
              <div className="placeholder-actions">
                <button className="btn primary" onClick={() => setScanning(true)}>
                  ⌕ Scan this system for running apps
                </button>
                <button className="btn ghost" onClick={() => setFormState({ app: null })}>
                  + Add one manually
                </button>
              </div>
            </div>
          )}
        </main>
      </div>

      {formState && (
        <AppForm
          app={formState.app}
          apps={apps}
          onClose={() => setFormState(null)}
          onError={onError}
          onSaved={(saved) => {
            setFormState(null)
            setSelectedId(saved.id)
            setView('apps')
            onNotice(`${saved.name} saved`)
            refresh()
          }}
        />
      )}

      {focusing && (
        <FocusDialog
          app={focusing}
          onClose={() => setFocusing(null)}
          onError={onError}
          onStarted={() => {
            setFocusing(null)
            onNotice(`Focus Mode starting on ${focusing.name}`)
            refresh()
          }}
        />
      )}

      {scanning && (
        <DiscoverModal
          onClose={() => setScanning(false)}
          onError={onError}
          onImported={(result) => {
            setScanning(false)
            const skipped = result.skipped.length ? `, ${result.skipped.length} skipped` : ''
            onNotice(`Imported ${result.created.length} application(s)${skipped}`)
            if (result.created.length) {
              setSelectedId(result.created[0].id)
              setView('apps')
            }
            refresh()
          }}
        />
      )}

      {confirming && (
        <div className="modal-backdrop" onClick={() => setConfirming(null)}>
          <div className="modal small" onClick={(event) => event.stopPropagation()}>
            <header className="modal-header">
              <h3>Delete {confirming.name}?</h3>
            </header>
            <div className="modal-body">
              <p>
                This stops the process if it is running and removes the application, its events and its
                captured logs. This cannot be undone.
              </p>
            </div>
            <footer className="modal-footer">
              <button className="btn ghost" onClick={() => setConfirming(null)}>
                Cancel
              </button>
              <button className="btn danger" onClick={() => handleDelete(confirming)}>
                Delete
              </button>
            </footer>
          </div>
        </div>
      )}

      {toast && <div className={`toast ${toast.tone}`}>{toast.message}</div>}
    </div>
  )
}

function StatPill({ label, value, tone = '', active, onClick }) {
  return (
    <button
      type="button"
      className={`stat-pill ${tone} ${active ? 'active' : ''}`}
      onClick={onClick}
      aria-pressed={active}
      title={active ? 'Click to show all apps' : `Show only these apps in the sidebar`}
    >
      <span className="stat-pill-value">{value}</span>
      <span className="stat-pill-label">{label}</span>
    </button>
  )
}
