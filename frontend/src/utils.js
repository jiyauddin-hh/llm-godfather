export function formatUptime(seconds) {
  if (seconds == null) return '—'
  const total = Math.floor(seconds)
  const days = Math.floor(total / 86400)
  const hours = Math.floor((total % 86400) / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const secs = total % 60

  if (days) return `${days}d ${hours}h`
  if (hours) return `${hours}h ${minutes}m`
  if (minutes) return `${minutes}m ${secs}s`
  return `${secs}s`
}

export function formatTime(iso) {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleTimeString([], { hour12: false })
}

export function formatDateTime(iso) {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString([], { hour12: false })
}

export const STATUS_LABEL = {
  running: 'Running',
  stopped: 'Stopped',
  crashed: 'Crashed',
  starting: 'Starting',
  external: 'Running outside',
}

// Top-bar pill filters. Mirrors /api/apps/stats so a pill's count always
// equals the number of apps it leaves in the sidebar ("starting" counts as stopped there).
const COUNTED_STATUSES = ['running', 'crashed', 'external']

export const STATUS_FILTERS = {
  all: { label: 'All apps', match: () => true },
  running: { label: 'Running', match: (app) => app.runtime.status === 'running' },
  stopped: { label: 'Stopped', match: (app) => !COUNTED_STATUSES.includes(app.runtime.status) },
  external: { label: 'Running outside', match: (app) => app.runtime.status === 'external' },
  crashed: { label: 'Crashed', match: (app) => app.runtime.status === 'crashed' },
  ports: { label: 'Port listening', match: (app) => Boolean(app.port && app.runtime.port_open) },
}
