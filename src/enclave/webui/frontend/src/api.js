const BASE = '/api'

let _redirecting = false  // prevent cascading 401 reloads

function getToken() {
  return localStorage.getItem('enclave_token') || ''
}

async function request(path, options = {}) {
  const headers = {
    'Content-Type': 'application/json',
    ...options.headers,
  }
  const token = getToken()
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  const res = await fetch(`${BASE}${path}`, { ...options, headers })
  if (res.status === 401) {
    localStorage.removeItem('enclave_token')
    localStorage.removeItem('enclave_user')
    if (!_redirecting && window.location.pathname !== '/login') {
      _redirecting = true
      window.location.href = '/login'
    }
    throw new Error('Session expired')
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    const detail = err.detail
    const msg = typeof detail === 'string' ? detail : (detail?.message || res.statusText)
    const error = new Error(msg)
    error.status = res.status
    error.detail = detail
    throw error
  }
  return res.json()
}

export const api = {
  // Sessions
  getSessions: () => request('/sessions'),
  getActivity: () => request('/sessions/activity'),
  getProfiles: () => request('/sessions/profiles'),
  createSession: (name, profile = '') => request('/sessions', {
    method: 'POST',
    body: JSON.stringify({ name, profile }),
  }),
  getSession: (id) => request(`/sessions/${id}`),
  stopSession: (id) => request(`/sessions/${id}/stop`, { method: 'POST' }),
  restartSession: (id) => request(`/sessions/${id}/restart`, { method: 'POST' }),
  archiveSession: (id) => request(`/sessions/${id}/archive`, { method: 'POST' }),
  getState: (id) => request(`/sessions/${id}/state`),
  getStateFile: (id, path) => request(`/sessions/${id}/state/${path.split('/').map(encodeURIComponent).join('/')}`),
  clearState: (id) => request(`/sessions/${id}/state/clear`, { method: 'POST' }),
  getSnapshots: (id) => request(`/sessions/${id}/snapshots`),
  createSnapshot: (id, name) => request(`/sessions/${id}/snapshots`, {
    method: 'POST',
    body: JSON.stringify({ name }),
  }),
  deleteSnapshot: (id, filename) => request(`/sessions/${id}/snapshots/${filename}`, { method: 'DELETE' }),
  getLogs: (id, lines = 200) => request(`/sessions/${id}/logs?lines=${lines}`),
  getSessionPrompt: (id) => request(`/sessions/${id}/prompt`),
  updateSessionPrompt: (id, content) => request(`/sessions/${id}/prompt`, {
    method: 'PUT',
    body: JSON.stringify({ content }),
  }),

  // Bugs
  getBugs: (session) => request(`/bugs/${session}`),
  getBug: (session, bugId) => request(`/bugs/${session}/${bugId}`),
  createBug: (session, project, data) => request(`/bugs/${session}/${project}/create`, {
    method: 'POST',
    body: JSON.stringify(data),
  }),
  updateBug: (session, bugId, data) => request(`/bugs/${session}/${bugId}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  }),
  deleteBug: (session, bugId) => request(`/bugs/${session}/${bugId}`, { method: 'DELETE' }),

  // Memories
  getMemories: () => request('/memories'),
  getSymbols: () => request('/memories/symbols'),
  getMemoryStats: () => request('/memories/stats'),

  // Chat
  getChatHistory: (session, limit = 100, offset = 0) =>
    request(`/chat/${session}/history?limit=${limit}&offset=${offset}`),
  getChatEvents: (session, { sinceId, sinceTimestamp, level, types, limit } = {}) => {
    const params = new URLSearchParams()
    if (sinceId != null) params.set('since_id', sinceId)
    if (sinceTimestamp) params.set('since_timestamp', sinceTimestamp)
    if (level) params.set('level', level)
    if (types) params.set('types', types)
    if (limit) params.set('limit', limit)
    const qs = params.toString()
    return request(`/chat/${session}/events${qs ? '?' + qs : ''}`)
  },
  sendChatMessage: (session, content) => request(`/chat/${session}/send`, {
    method: 'POST',
    body: JSON.stringify({ content }),
  }),
  getModels: (session, refresh = false) => request(`/chat/${session}/models${refresh ? '?refresh=true' : ''}`),
  getCredits: (sessionId) => request(`/chat/credits${sessionId ? `?session=${encodeURIComponent(sessionId)}` : ''}`),
  setModel: (session, model) => request(`/chat/${session}/model`, {
    method: 'POST',
    body: JSON.stringify({ content: model }),
  }),

  // Artifacts
  getArtifacts: (id) => request(`/sessions/${id}/artifacts`),
  getArtifactContent: (id, filename) => request(`/sessions/${id}/artifacts/${filename}`),
  getArtifactDiff: (id, filename, v1, v2) => request(`/sessions/${id}/artifacts/${filename}/diff?v1=${v1}&v2=${v2}`),
  getArtifactVersions: (id, filename) => request(`/sessions/${id}/artifacts/${filename}/versions`),
  saveArtifactContent: (id, filename, content, baseVersion = null) =>
    request(`/sessions/${id}/artifacts/${filename}/content`, {
      method: 'PUT',
      body: JSON.stringify({ content, base_version: baseVersion }),
    }),
  artifactUrl: (id, filename) => `/api/sessions/${id}/artifacts/${filename}`,
  rawArtifactUrl: (id, filename) => `/api/sessions/${id}/artifacts/${filename}?raw=1`,

  // OpenSpec changes
  getOpenSpecChanges: (id) => request(`/sessions/${id}/openspec/changes`),
  getOpenSpecChange: (id, name) => request(`/sessions/${id}/openspec/changes/${name}`),
  getOpenSpecState: (id, name) => request(`/sessions/${id}/openspec/changes/${name}/state`),
  postOpenSpecReview: (id, name, state, note = '', comments = []) =>
    request(`/sessions/${id}/openspec/changes/${name}/review`, {
      method: 'POST',
      body: JSON.stringify({ state, note, comments }),
    }),

  // Deferred Asks
  getAsks: (sessionId, status = 'pending') => {
    const params = new URLSearchParams({ status })
    if (sessionId) params.set('session_id', sessionId)
    return request(`/asks?${params}`)
  },
  getAskCount: () => request('/asks/count'),
  answerAsk: (askId, answer) => request(`/asks/${askId}/answer`, {
    method: 'POST',
    body: JSON.stringify({ answer }),
  }),
  dismissAsk: (askId) => request(`/asks/${askId}/dismiss`, { method: 'POST' }),

  // Timeline
  getTimeline: (session, date) => {
    const params = new URLSearchParams()
    if (date) params.set('date', date)
    const qs = params.toString()
    return request(`/chat/${session}/timeline${qs ? '?' + qs : ''}`)
  },

  // Consult panel configuration
  getPanel: () => request('/panel'),
  updatePanel: (members) => request('/panel', {
    method: 'PUT',
    body: JSON.stringify({ members }),
  }),
  getPanelModels: () => request('/panel/models'),

  // Fusion configuration (compound-model presets + Auto Fusion routing)
  getFusion: () => request('/fusion'),
  updateFusion: (doc) => request('/fusion', {
    method: 'PUT',
    body: JSON.stringify(doc),
  }),
  getFusionModels: () => request('/fusion/models'),
  getComplexityHistory: (session) => request(`/chat/complexity${session ? `?session=${encodeURIComponent(session)}` : ''}`),

  // Scheduling
  getSchedules: () => request('/schedules'),
  createSchedule: (payload) => request('/schedules', {
    method: 'POST',
    body: JSON.stringify(payload),
  }),
  cancelSchedule: (id) => request(`/schedules/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  }),

  // Notifications (sessions needing a reply)
  getNotifications: () => request('/notifications'),
  getNotificationCount: () => request('/notifications/count'),
  dismissNotification: (sessionId) => request(`/notifications/${encodeURIComponent(sessionId)}/dismiss`, {
    method: 'POST',
  }),
}
