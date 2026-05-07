import { ref, watch } from 'vue'
import { api } from '../api.js'

// Global reactive state for the selected session
const sessions = ref([])
const selectedSessionId = ref(localStorage.getItem('enclave_selected_session') || '')
const loading = ref(false)

// Persist selection
watch(selectedSessionId, (id) => {
  if (id) localStorage.setItem('enclave_selected_session', id)
  else localStorage.removeItem('enclave_selected_session')
})

const selectedSession = {
  get id() { return selectedSessionId.value },
  get name() {
    const s = sessions.value.find(s => s.id === selectedSessionId.value)
    return s?.name || selectedSessionId.value
  },
  get status() {
    const s = sessions.value.find(s => s.id === selectedSessionId.value)
    return s?.status || 'unknown'
  },
}

async function loadSessions() {
  loading.value = true
  try {
    sessions.value = await api.getSessions()
    // Auto-select first running session if nothing selected
    if (!selectedSessionId.value || !sessions.value.find(s => s.id === selectedSessionId.value)) {
      const running = sessions.value.find(s => s.status === 'running')
      if (running) selectedSessionId.value = running.id
    }
  } catch (e) {
    console.error('Failed to load sessions:', e)
  } finally {
    loading.value = false
  }
}

function selectSession(id) {
  selectedSessionId.value = id
}

export function useSessionStore() {
  return {
    sessions,
    selectedSessionId,
    selectedSession,
    loading,
    loadSessions,
    selectSession,
  }
}
