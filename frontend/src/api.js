const BASE = import.meta.env.VITE_API_BASE ?? ''

async function request(path, options = {}) {
  const response = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })

  if (response.status === 204) return null

  const body = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = body?.detail
    throw new Error(
      typeof detail === 'string'
        ? detail
        : Array.isArray(detail)
          ? detail.map((d) => `${d.loc?.slice(-1)}: ${d.msg}`).join(', ')
          : `Request failed (${response.status})`
    )
  }
  return body
}

export const api = {
  health: () => request('/api/health'),
  stats: () => request('/api/apps/stats'),

  listApps: () => request('/api/apps'),
  getApp: (id) => request(`/api/apps/${id}`),
  createApp: (data) => request('/api/apps', { method: 'POST', body: JSON.stringify(data) }),
  updateApp: (id, data) => request(`/api/apps/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteApp: (id) => request(`/api/apps/${id}`, { method: 'DELETE' }),

  start: (id) => request(`/api/apps/${id}/start`, { method: 'POST' }),
  stop: (id) => request(`/api/apps/${id}/stop`, { method: 'POST' }),
  restart: (id) => request(`/api/apps/${id}/restart`, { method: 'POST' }),
  occupant: (id) => request(`/api/apps/${id}/occupant`),
  takeover: (id) => request(`/api/apps/${id}/takeover`, { method: 'POST' }),

  focus: () => request('/api/focus'),
  focusPreview: (id) => request(`/api/focus/preview/${id}`, { method: 'POST' }),
  focusStart: (id) => request(`/api/focus/start/${id}`, { method: 'POST' }),
  focusExit: () => request('/api/focus/exit', { method: 'POST' }),

  logs: (id, limit = 500) => request(`/api/apps/${id}/logs?limit=${limit}`),
  clearLogs: (id) => request(`/api/apps/${id}/logs`, { method: 'DELETE' }),
  events: (id) => request(`/api/apps/${id}/events`),

  ports: () => request('/api/ports'),
  inspectPort: (port) => request(`/api/ports/${port}`),
  portOccupant: (port) => request(`/api/ports/${port}/occupant`),
  killPort: (port) => request(`/api/ports/${port}/kill`, { method: 'POST' }),

  network: () => request('/api/network'),
  shareStatus: (id) => request(`/api/apps/${id}/share`),
  setShare: (id, enabled) =>
    request(`/api/apps/${id}/share`, { method: 'POST', body: JSON.stringify({ enabled }) }),

  wall: () => request('/api/wall'),
  wallNetwork: () => request('/api/wall/network'),
  setWallNetwork: (enabled, port) =>
    request('/api/wall/network', { method: 'POST', body: JSON.stringify({ enabled, port }) }),

  captures: (limit = 200) => request(`/api/captures?limit=${limit}`),
  ringSummary: () => request('/api/captures/ring'),
  cameraSettings: () => request('/api/captures/settings'),
  setCameraSettings: (data) =>
    request('/api/captures/settings', { method: 'POST', body: JSON.stringify(data) }),
  testShot: () => request('/api/captures/test', { method: 'POST' }),
  deleteCapture: (id) => request(`/api/captures/${id}`, { method: 'DELETE' }),
  clearCaptures: () => request('/api/captures', { method: 'DELETE' }),

  system: (minutes = 60) => request(`/api/system?minutes=${minutes}`),
  systemNow: () => request('/api/system/now'),
  clearSystemEvents: () => request('/api/system/events', { method: 'DELETE' }),

  ai: (minutes = 60) => request(`/api/ai?minutes=${minutes}`),
  aiProviders: () => request('/api/ai/providers'),
  aiScan: (minutes = 60) => request(`/api/ai/scan?minutes=${minutes}`, { method: 'POST' }),
  aiDeclare: (data, minutes = 60) =>
    request(`/api/ai/links?minutes=${minutes}`, { method: 'POST', body: JSON.stringify(data) }),
  aiForget: (linkId) => request(`/api/ai/links/${linkId}`, { method: 'DELETE' }),
  clearAiCalls: () => request('/api/ai/calls', { method: 'DELETE' }),

  traffic: (minutes = 60) => request(`/api/traffic?minutes=${minutes}`),
  trafficScan: (minutes = 60) => request(`/api/traffic/scan?minutes=${minutes}`, { method: 'POST' }),

  cloudflare: () => request('/api/cloudflare'),
  cloudflareMetrics: (minutes = 60) => request(`/api/cloudflare/metrics?minutes=${minutes}`),
  cloudflareConfig: () => request('/api/cloudflare/config'),
  addRoute: (data) => request('/api/cloudflare/routes', { method: 'POST', body: JSON.stringify(data) }),
  editRoute: (id, data) =>
    request(`/api/cloudflare/routes/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteRoute: (id) => request(`/api/cloudflare/routes/${id}`, { method: 'DELETE' }),
  routeHeaderValue: (id) => request(`/api/cloudflare/routes/${id}/header-value`),
  routeTokens: (id) => request(`/api/cloudflare/routes/${id}/tokens`),
  revokeToken: (id, tokenId) =>
    request(`/api/cloudflare/routes/${id}/tokens/${tokenId}`, { method: 'DELETE' }),
  revokeAllTokens: (id) =>
    request(`/api/cloudflare/routes/${id}/tokens/revoke-all`, { method: 'POST' }),
  routeUp: (id) => request(`/api/cloudflare/routes/${id}/up`, { method: 'POST' }),
  routeDown: (id) => request(`/api/cloudflare/routes/${id}/down`, { method: 'POST' }),
  chooseTunnel: (name, create = false) =>
    request('/api/cloudflare/tunnel', { method: 'POST', body: JSON.stringify({ name, create }) }),

  news: (limit = 50) => request(`/api/news?limit=${limit}`),
  clearNews: () => request('/api/news', { method: 'DELETE' }),
  logTape: (limit = 40) => request(`/api/news/logs?limit=${limit}`),

  discover: () => request('/api/discover'),
  importApps: (items) => request('/api/discover/import', { method: 'POST', body: JSON.stringify({ items }) }),
}

// Plain <img src> rather than a fetch, so the browser caches each still.
export function captureImageUrl(id) {
  return `${BASE}/api/captures/${id}/image`
}

// Straight into <video src>, so the browser streams it and gives us its own
// play, scrub and fullscreen controls for free. `at` is only there to bust the
// cache — the clip is rendered fresh and differs every few seconds.
// Footage preserved for one capture — only a power cut has any.
export function captureClipUrl(id) {
  return `${BASE}/api/captures/${id}/clip`
}

export function ringClipUrl(minutes, at) {
  return `${BASE}/api/captures/ring/clip?minutes=${minutes}${at ? `&at=${at}` : ''}`
}

function socketUrl(path) {
  if (BASE) return `${BASE.replace(/^http/, 'ws')}${path}`
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}${path}`
}

export function logSocketUrl(appId) {
  return socketUrl(`/api/apps/${appId}/logs/ws`)
}

// Tells the wall which tile just changed on disk, so it can reload that one
// frame instead of waiting out the 15-second probe or reloading all of them.
export function wallSocketUrl() {
  return socketUrl('/api/wall/ws')
}

// One message per headline for the news ticker.
export function newsSocketUrl() {
  return socketUrl('/api/news/ws')
}

// Every captured line from every app, for the ticker's All logs mode.
export function logTapeSocketUrl() {
  return socketUrl('/api/news/logs/ws')
}
