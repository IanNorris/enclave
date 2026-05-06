const BASE = '/api'

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
    window.location.href = '/login'
    throw new Error('Session expired')
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || res.statusText)
  }
  return res.json()
}

export const api = {
  // Sessions
  getSessions: () => request('/sessions'),
  getSession: (id) => request(`/sessions/${id}`),
  stopSession: (id) => request(`/sessions/${id}/stop`, { method: 'POST' }),
  restartSession: (id) => request(`/sessions/${id}/restart`, { method: 'POST' }),
  getState: (id) => request(`/sessions/${id}/state`),
  getStateFile: (id, path) => request(`/sessions/${id}/state/file?path=${encodeURIComponent(path)}`),
  clearState: (id) => request(`/sessions/${id}/state/clear`, { method: 'POST' }),
  getSnapshots: (id) => request(`/sessions/${id}/snapshots`),
  createSnapshot: (id, name) => request(`/sessions/${id}/snapshots`, {
    method: 'POST',
    body: JSON.stringify({ name }),
  }),
  deleteSnapshot: (id, filename) => request(`/sessions/${id}/snapshots/${filename}`, { method: 'DELETE' }),
  getLogs: (id, lines = 200) => request(`/sessions/${id}/logs?lines=${lines}`),

  // Bugs
  getBugs: (session) => request(`/bugs/${session}`),
  getBug: (session, bugId) => request(`/bugs/${session}/${bugId}`),
  createBug: (session, data) => request(`/bugs/${session}`, {
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
  sendChatMessage: (session, content) => request(`/chat/${session}/send`, {
    method: 'POST',
    body: JSON.stringify({ content }),
  }),
}
