import { useMemo, useState } from 'react'
import { STATUS_FILTERS, STATUS_LABEL } from '../utils.js'

export default function Sidebar({
  apps,
  statusFilter = 'all',
  onClearStatusFilter,
  selectedId,
  view,
  onSelect,
  onViewChange,
  onCreate,
  onScan,
}) {
  const [query, setQuery] = useState('')

  const filtered = useMemo(() => {
    const term = query.trim().toLowerCase()
    const byStatus = apps.filter(STATUS_FILTERS[statusFilter].match)
    if (!term) return byStatus
    return byStatus.filter(
      (app) =>
        app.name.toLowerCase().includes(term) ||
        String(app.port ?? '').includes(term) ||
        app.description.toLowerCase().includes(term)
    )
  }, [apps, query, statusFilter])

  return (
    <aside className="sidebar">
      <nav className="sidebar-nav">
        <button
          className={`nav-item ${view === 'apps' ? 'active' : ''}`}
          onClick={() => onViewChange('apps')}
        >
          <span className="nav-icon">▤</span> Applications
        </button>
        <button
          className={`nav-item ${view === 'wall' ? 'active' : ''}`}
          onClick={() => onViewChange('wall')}
        >
          <span className="nav-icon">▦</span> Live wall
        </button>
        <button
          className={`nav-item ${view === 'ports' ? 'active' : ''}`}
          onClick={() => onViewChange('ports')}
        >
          <span className="nav-icon">⇄</span> Port map
        </button>
        <button
          className={`nav-item ${view === 'system' ? 'active' : ''}`}
          onClick={() => onViewChange('system')}
        >
          <span className="nav-icon">◍</span> System
        </button>
        <button
          className={`nav-item ${view === 'ai' ? 'active' : ''}`}
          onClick={() => onViewChange('ai')}
        >
          <span className="nav-icon">◈</span> AI models
        </button>
        <button
          className={`nav-item ${view === 'cloudflare' ? 'active' : ''}`}
          onClick={() => onViewChange('cloudflare')}
        >
          <span className="nav-icon">☁</span> Cloudflare
        </button>
        <button
          className={`nav-item ${view === 'camera' ? 'active' : ''}`}
          onClick={() => onViewChange('camera')}
        >
          <span className="nav-icon">◉</span> Camera log
        </button>
      </nav>

      <div className="sidebar-search">
        <input
          type="search"
          placeholder="Search apps or ports…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>

      {statusFilter !== 'all' && (
        <div className="sidebar-filter">
          <span>
            {STATUS_FILTERS[statusFilter].label} · {filtered.length}
          </span>
          <button className="sidebar-filter-clear" onClick={onClearStatusFilter} title="Show all apps">
            ✕
          </button>
        </div>
      )}

      <div className="sidebar-list">
        {filtered.length === 0 && (
          <p className="empty-hint">
            {!apps.length
              ? 'No applications yet.'
              : statusFilter !== 'all' && !query.trim()
                ? `No ${STATUS_FILTERS[statusFilter].label.toLowerCase()} apps.`
                : 'No match.'}
          </p>
        )}

        {filtered.map((app) => (
          <button
            key={app.id}
            className={`app-item ${selectedId === app.id && view === 'apps' ? 'active' : ''}`}
            onClick={() => onSelect(app.id)}
          >
            <span className={`dot ${app.runtime.status}`} title={STATUS_LABEL[app.runtime.status]} />
            <span className="app-item-body">
              <span className="app-item-name">
                {app.name}
                {app.protected && (
                  <span className="lock" title="Protected — Focus Mode never stops this app">
                    {' '}🔒
                  </span>
                )}
              </span>
              <span className="app-item-meta">
                {app.port ? `:${app.port}` : 'no port'}
                {app.runtime.pid ? ` · pid ${app.runtime.pid}` : ''}
                {app.start_on_boot ? ' · ⏻' : ''}
                {app.share_lan ? ' · ⇆' : ''}
              </span>
            </span>
          </button>
        ))}
      </div>

      <div className="sidebar-actions">
        <button className="btn block" onClick={onScan}>
          ⌕ Scan this system
        </button>
        <button className="btn primary block" onClick={onCreate}>
          + Add application
        </button>
      </div>
    </aside>
  )
}
