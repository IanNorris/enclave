<template>
  <div v-if="isLoginPage || !hasToken">
    <router-view />
  </div>
  <div v-else class="app">
    <!-- Mobile header -->
    <div class="mobile-header">
      <button class="hamburger" @click="sidebarOpen = !sidebarOpen">☰</button>
      <span class="mobile-title">Enclave</span>
      <!-- Session selector surfaced in the top bar while the sidebar is collapsed -->
      <select v-model="selectedSessionId" class="mobile-session-select" aria-label="Active session">
        <option value="">No session</option>
        <option v-for="s in activeSessions" :key="s.id" :value="s.id">
          {{ s.concierge ? '🛎️ ' : '' }}{{ s.name }}{{ s.status === 'running' ? ' ●' : '' }}
        </option>
      </select>
      <button class="mobile-new-session-btn" title="New session" @click="openNewSession">➕</button>
    </div>

    <!-- Sidebar overlay for mobile -->
    <div v-if="sidebarOpen" class="sidebar-overlay" @click="sidebarOpen = false"></div>

    <nav class="sidebar" :class="{ open: sidebarOpen }">
      <div class="logo">
        <h1>Enclave</h1>
      </div>

      <!-- Global session selector -->
      <div class="session-selector">
        <select v-model="selectedSessionId" class="session-select">
          <option value="">No session</option>
          <option v-for="s in activeSessions" :key="s.id" :value="s.id">
            {{ s.concierge ? '🛎️ ' : '' }}{{ s.name }}{{ s.status === 'running' ? ' ●' : '' }}
          </option>
        </select>
        <button class="new-session-btn" title="New session" @click="openNewSession">➕</button>
      </div>

      <!-- New session modal -->
      <div v-if="showNewSession" class="modal-overlay" @click.self="closeNewSession">
        <div class="modal">
          <h3>New Session</h3>
          <label class="modal-label">Name</label>
          <input
            v-model="newName"
            class="modal-input"
            placeholder="my-project"
            :disabled="creating"
            @keydown.enter.prevent="submitNewSession"
          />
          <label class="modal-label">Profile</label>
          <select v-model="newProfile" class="modal-input" :disabled="creating || !profiles.length">
            <option v-for="p in profiles" :key="p.name" :value="p.name">
              {{ p.name }}{{ p.description ? ` — ${p.description}` : '' }}{{ p.default ? ' (default)' : '' }}
            </option>
          </select>
          <p v-if="createError" class="modal-error">{{ createError }}</p>
          <div class="modal-actions">
            <button class="secondary" :disabled="creating" @click="closeNewSession">Cancel</button>
            <button class="primary" :disabled="creating || !newName.trim()" @click="submitNewSession">
              {{ creating ? 'Creating…' : 'Create' }}
            </button>
          </div>
        </div>
      </div>

      <ul class="nav-links">
        <li>
          <router-link to="/sessions" active-class="active" @click="sidebarOpen = false">
            <span class="icon">⚙</span> Sessions
          </router-link>
        </li>
        <li>
          <router-link to="/chat" active-class="active" @click="sidebarOpen = false">
            <span class="icon">💬</span> Chat
          </router-link>
        </li>
        <li>
          <router-link to="/bugs" active-class="active" @click="sidebarOpen = false">
            <span class="icon">🐛</span> Bugs
          </router-link>
        </li>
        <li>
          <router-link to="/memories" active-class="active" @click="sidebarOpen = false">
            <span class="icon">🧠</span> Memories
          </router-link>
        </li>
        <li>
          <router-link to="/panel" active-class="active" @click="sidebarOpen = false">
            <span class="icon">🎛️</span> Panel
          </router-link>
        </li>
        <li>
          <router-link to="/schedules" active-class="active" @click="sidebarOpen = false">
            <span class="icon">⏰</span> Schedules
          </router-link>
        </li>
        <li>
          <router-link to="/artifacts" active-class="active" @click="sidebarOpen = false">
            <span class="icon">📎</span> Artifacts
          </router-link>
        </li>
        <li>
          <router-link to="/asks" active-class="active" @click="sidebarOpen = false">
            <span class="icon">❓</span> Asks
            <span v-if="pendingAsks > 0" class="nav-badge">{{ pendingAsks }}</span>
          </router-link>
        </li>
        <li>
          <router-link to="/timeline" active-class="active" @click="sidebarOpen = false">
            <span class="icon">📅</span> Timeline
          </router-link>
        </li>
      </ul>

      <!-- Notification panel: sessions awaiting a reply -->
      <div v-if="notifications.length" class="notif-panel">
        <div class="notif-header">
          <span>🔔 Needs reply</span>
          <button
            class="notif-toggle"
            :title="pushEnabled ? 'Disable browser notifications' : 'Enable browser notifications'"
            @click="togglePush"
          >{{ pushEnabled ? '🔔' : '🔕' }}</button>
        </div>
        <ul class="notif-list">
          <li v-for="n in notifications" :key="n.session_id" class="notif-item">
            <div class="notif-body" @click="openNotification(n)">
              <div class="notif-name">{{ n.session_name }}</div>
              <div class="notif-reason">{{ notifReason(n) }}</div>
              <div v-if="n.question" class="notif-q">{{ n.question }}</div>
            </div>
            <button class="notif-dismiss" title="Dismiss" @click.stop="dismissNotification(n)">✕</button>
          </li>
        </ul>
      </div>
    </nav>
    <main class="content">
      <router-view />
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSessionStore } from './stores/session.js'
import { api } from './api.js'

const route = useRoute()
const router = useRouter()
const sidebarOpen = ref(false)
const { sessions, selectedSessionId, loadSessions } = useSessionStore()
const activeSessions = computed(() => {
  const list = sessions.value.filter(s => !s.archived)
  // Pin the concierge to the top of the list.
  return list.slice().sort((a, b) => {
    if (a.concierge && !b.concierge) return -1
    if (b.concierge && !a.concierge) return 1
    return 0
  })
})
const isLoginPage = computed(() => route.name === 'login')
const hasToken = ref(!!localStorage.getItem('enclave_token'))
const pendingAsks = ref(0)

// ─── Notifications (sessions needing a reply) ───
const notifications = ref([])
const pushEnabled = ref(localStorage.getItem('enclave_push') === '1')
let notifWs = null
let notifReconnect = null
let notifPollTimer = null
const knownAwaiting = new Set()

function notifReason(n) {
  const r = n.reasons || []
  if (r.includes('awaiting') && r.includes('deferred_ask')) return 'Awaiting reply + question'
  if (r.includes('deferred_ask')) return n.ask_count > 1 ? `${n.ask_count} questions` : 'Has a question'
  return 'Awaiting your reply'
}

async function loadNotifications() {
  if (!hasToken.value) return
  try {
    const data = await api.getNotifications()
    notifications.value = data.notifications || []
    maybePush(notifications.value)
  } catch { /* ignore */ }
}

function openNotification(n) {
  selectedSessionId.value = n.session_id
  sidebarOpen.value = false
  router.push('/chat')
}

async function dismissNotification(n) {
  try {
    await api.dismissNotification(n.session_id)
  } catch { /* ignore */ }
  notifications.value = notifications.value.filter(x => x.session_id !== n.session_id)
  knownAwaiting.delete(n.session_id)
}

function maybePush(list) {
  if (!pushEnabled.value || Notification?.permission !== 'granted') return
  for (const n of list) {
    if (knownAwaiting.has(n.session_id)) continue
    knownAwaiting.add(n.session_id)
    if (document.visibilityState === 'visible') continue
    try {
      const note = new Notification(`${n.session_name} needs your reply`, {
        body: n.question || notifReason(n),
        tag: `enclave-${n.session_id}`,
      })
      note.onclick = () => { window.focus(); openNotification(n) }
    } catch { /* ignore */ }
  }
  // Forget sessions that no longer need a reply, so they can re-notify later.
  const ids = new Set(list.map(n => n.session_id))
  for (const id of [...knownAwaiting]) if (!ids.has(id)) knownAwaiting.delete(id)
}

async function togglePush() {
  if (!pushEnabled.value) {
    if (Notification?.permission === 'default') {
      try { await Notification.requestPermission() } catch { /* ignore */ }
    }
    if (Notification?.permission === 'granted') {
      pushEnabled.value = true
      localStorage.setItem('enclave_push', '1')
    }
  } else {
    pushEnabled.value = false
    localStorage.setItem('enclave_push', '0')
  }
}

function connectNotifWs() {
  if (!hasToken.value) return
  const token = localStorage.getItem('enclave_token')
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  try {
    notifWs = new WebSocket(`${proto}//${location.host}/api/notifications/stream?token=${token}`)
    notifWs.onmessage = () => { loadNotifications() }
    notifWs.onclose = () => {
      notifWs = null
      if (hasToken.value && !notifReconnect) {
        notifReconnect = setTimeout(() => { notifReconnect = null; connectNotifWs() }, 3000)
      }
    }
    notifWs.onerror = () => { try { notifWs.close() } catch { /* ignore */ } }
  } catch { /* ignore */ }
}

// ─── New session ───
const showNewSession = ref(false)
const newName = ref('')
const newProfile = ref('')
const profiles = ref([])
const creating = ref(false)
const createError = ref('')

async function openNewSession() {
  showNewSession.value = true
  newName.value = ''
  createError.value = ''
  if (!profiles.value.length) {
    try {
      const data = await api.getProfiles()
      profiles.value = data.profiles || []
    } catch (e) {
      createError.value = `Failed to load profiles: ${e.message}`
    }
  }
  const def = profiles.value.find(p => p.default) || profiles.value[0]
  newProfile.value = def ? def.name : ''
}

function closeNewSession() {
  if (creating.value) return
  showNewSession.value = false
}

async function submitNewSession() {
  const name = newName.value.trim()
  if (!name || creating.value) return
  creating.value = true
  createError.value = ''
  try {
    const data = await api.createSession(name, newProfile.value)
    await loadSessions()
    if (data.session) selectedSessionId.value = data.session
    showNewSession.value = false
    router.push('/chat')
  } catch (e) {
    createError.value = e.message || 'Failed to create session'
  } finally {
    creating.value = false
  }
}

async function pollAskCount() {
  if (!hasToken.value) return
  try {
    const data = await api.getAskCount()
    pendingAsks.value = data.count || 0
  } catch { /* ignore */ }
}

let askPollTimer = null

// Only load sessions when authenticated and not on login page
onMounted(() => {
  if (!isLoginPage.value && hasToken.value) {
    loadSessions()
    pollAskCount()
    askPollTimer = setInterval(pollAskCount, 30000)
    loadNotifications()
    connectNotifWs()
    notifPollTimer = setInterval(loadNotifications, 30000)
  }
})
onUnmounted(() => {
  if (askPollTimer) clearInterval(askPollTimer)
  if (notifPollTimer) clearInterval(notifPollTimer)
  if (notifReconnect) clearTimeout(notifReconnect)
  if (notifWs) { try { notifWs.close() } catch { /* ignore */ } }
})
watch(isLoginPage, (isLogin) => {
  if (!isLogin) {
    hasToken.value = !!localStorage.getItem('enclave_token')
    if (hasToken.value) {
      loadSessions()
      pollAskCount()
      loadNotifications()
      connectNotifWs()
    }
  }
})
</script>

<style scoped>
.app {
  display: flex;
  height: 100vh;
}

.mobile-header {
  display: none;
}

.sidebar-overlay {
  display: none;
}

.sidebar {
  width: 220px;
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  padding: 1rem 0;
  flex-shrink: 0;
}

.logo {
  padding: 0 1.25rem 1rem;
  border-bottom: 1px solid var(--border);
}

.logo h1 {
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0;
}

.session-selector {
  padding: 0.5rem 1rem;
  border-bottom: 1px solid var(--border);
  display: flex;
  gap: 0.4rem;
  align-items: center;
}

.session-select {
  width: 100%;
  font-size: 0.85rem;
  padding: 0.4rem 0.5rem;
  background: var(--bg-main);
  color: var(--text-primary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
}

.new-session-btn {
  flex-shrink: 0;
  width: 2rem;
  height: 2rem;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.95rem;
  line-height: 1;
  background: var(--bg-main);
  color: var(--text-primary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}

.new-session-btn:hover {
  background: var(--bg-hover);
  color: var(--accent);
}

.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.modal {
  background: var(--bg-sidebar);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 1.25rem;
  width: 320px;
  max-width: 90vw;
}

.modal h3 {
  margin: 0 0 0.75rem;
  font-size: 1.05rem;
  color: var(--text-primary);
}

.modal-label {
  display: block;
  font-size: 0.75rem;
  color: var(--text-secondary);
  margin: 0.6rem 0 0.25rem;
}

.modal-input {
  width: 100%;
  box-sizing: border-box;
  font-size: 0.85rem;
  padding: 0.45rem 0.5rem;
  background: var(--bg-main);
  color: var(--text-primary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
}

.modal-error {
  color: #ef4444;
  font-size: 0.8rem;
  margin: 0.6rem 0 0;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
  margin-top: 1rem;
}

.modal-actions button {
  padding: 0.4rem 0.9rem;
  font-size: 0.85rem;
  border-radius: var(--radius-sm, 4px);
  cursor: pointer;
  border: 1px solid var(--border);
}

.modal-actions .primary {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}

.modal-actions .primary:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.modal-actions .secondary {
  background: var(--bg-main);
  color: var(--text-primary);
}

.nav-links {
  list-style: none;
  padding: 0.5rem 0;
  margin: 0;
}

.nav-links li a {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.625rem 1.25rem;
  color: var(--text-secondary);
  text-decoration: none;
  font-size: 0.9rem;
  transition: background 0.15s, color 0.15s;
}

.nav-links li a:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.nav-links li a.active {
  background: var(--bg-active);
  color: var(--accent);
  font-weight: 500;
}

.icon {
  font-size: 1.1rem;
}

.nav-badge {
  margin-left: auto;
  background: #ef4444;
  color: white;
  font-size: 0.7rem;
  font-weight: 600;
  padding: 0.1rem 0.4rem;
  border-radius: 10px;
  min-width: 1.2rem;
  text-align: center;
}

.notif-panel {
  margin-top: auto;
  border-top: 1px solid var(--border);
  padding: 0.5rem 0;
  max-height: 40vh;
  overflow-y: auto;
}

.notif-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.25rem 1.25rem;
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-secondary);
}

.notif-toggle {
  background: none;
  border: none;
  cursor: pointer;
  font-size: 0.9rem;
  padding: 0;
}

.notif-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.notif-item {
  display: flex;
  align-items: flex-start;
  gap: 0.4rem;
  padding: 0.4rem 1rem 0.4rem 1.25rem;
}

.notif-item:hover {
  background: var(--bg-hover);
}

.notif-body {
  flex: 1;
  min-width: 0;
  cursor: pointer;
}

.notif-name {
  font-size: 0.85rem;
  font-weight: 500;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.notif-reason {
  font-size: 0.7rem;
  color: var(--accent);
}

.notif-q {
  font-size: 0.72rem;
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.notif-dismiss {
  background: none;
  border: none;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 0.8rem;
  flex-shrink: 0;
}

.notif-dismiss:hover {
  color: #ef4444;
}

.content {
  flex: 1;
  overflow-y: auto;
  padding: 2rem;
  background: var(--bg-main);
  min-width: 0;
}

/* ─── Mobile ─── */
@media (max-width: 768px) {
  .app {
    flex-direction: column;
  }

  .mobile-header {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.5rem 1rem;
    background: var(--bg-sidebar);
    border-bottom: 1px solid var(--border);
    z-index: 60;
  }

  .hamburger {
    background: none;
    border: none;
    color: var(--text-primary);
    font-size: 1.5rem;
    cursor: pointer;
    padding: 0.25rem;
  }

  .mobile-title {
    font-size: 1.1rem;
    font-weight: 600;
    color: var(--text-primary);
  }

  .mobile-session-select {
    margin-left: auto;
    max-width: 55vw;
    font-size: 0.85rem;
    padding: 0.35rem 0.5rem;
    background: var(--bg-main);
    color: var(--text-primary);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 4px);
  }

  .mobile-new-session-btn {
    flex-shrink: 0;
    width: 2rem;
    height: 2rem;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.95rem;
    line-height: 1;
    background: var(--bg-main);
    color: var(--text-primary);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 4px);
    cursor: pointer;
  }

  .sidebar {
    position: fixed;
    top: 0;
    left: -260px;
    width: 250px;
    height: 100vh;
    z-index: 70;
    transition: left 0.25s ease;
    padding-top: 1rem;
  }

  .sidebar.open {
    left: 0;
  }

  .sidebar-overlay {
    display: block;
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    z-index: 65;
  }

  .nav-links li a {
    padding: 0.875rem 1.25rem;
    font-size: 1rem;
  }

  .content {
    padding: 1rem;
    flex: 1;
    overflow-y: auto;
  }
}
</style>
