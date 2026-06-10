<template>
  <div v-if="isLoginPage || !hasToken">
    <router-view />
  </div>
  <div v-else class="app">
    <!-- Mobile header -->
    <div class="mobile-header" :class="{ 'mobile-hidden': isSessionRoute }">
      <button class="hamburger" @click="sidebarOpen = !sidebarOpen">☰</button>
      <span class="mobile-title">Enclave</span>
    </div>

    <!-- Sidebar overlay for mobile -->
    <div v-if="sidebarOpen" class="sidebar-overlay" @click="sidebarOpen = false"></div>

    <nav class="sidebar" :class="{ open: sidebarOpen }">
      <div class="logo">
        <h1>Enclave</h1>
      </div>

      <!-- Recency-sorted session switcher -->
      <div class="session-list-section">
        <div class="session-list-header">
          <span>Sessions</span>
        </div>
        <ul class="session-list">
          <li class="session-row new-row">
            <button class="session-pick" @click="openNewSession">
              <span class="status-icon"><span class="new-glyph">＋</span></span>
              <span class="session-name">New session…</span>
            </button>
          </li>
          <li
            v-for="s in recentSessions"
            :key="s.id"
            class="session-row"
            :class="{ active: s.id === selectedSessionId, pending: isAwaiting(s.id) }"
          >
            <button class="session-pick" @click="pickSession(s.id)">
              <span class="status-icon" :class="rowState(s)">
                <span v-if="rowState(s) === 'pending'" class="q-flash">?</span>
                <span v-else-if="rowState(s) === 'tool'" class="act-cog">⚙</span>
                <span v-else-if="rowState(s) === 'thinking'" class="act-brain">🧠</span>
                <span v-else class="dot"></span>
              </span>
              <span class="session-name">{{ s.concierge ? '🛎️ ' : '' }}{{ s.name }}</span>
            </button>
            <button
              v-if="!s.concierge"
              class="session-archive"
              title="Archive (hide from list)"
              @click.stop="archiveSession(s)"
            >🗄️</button>
          </li>
          <li v-if="!recentSessions.length" class="session-empty muted">No sessions</li>
        </ul>
        <button v-if="hasMoreSessions" class="session-more" @click="showAllSessions = true">
          More… ({{ activeSessions.length }})
        </button>
      </div>

      <!-- All-sessions popup -->
      <div v-if="showAllSessions" class="modal-overlay" @click.self="showAllSessions = false">
        <div class="modal session-modal">
          <div class="session-modal-head">
            <h3>All sessions</h3>
            <label class="show-archived">
              <input type="checkbox" v-model="showArchived" /> Show archived
            </label>
          </div>
          <ul class="session-modal-list">
            <li
              v-for="s in allSessionsSorted"
              :key="s.id"
              class="session-row"
              :class="{ active: s.id === selectedSessionId, pending: isAwaiting(s.id) }"
            >
              <button class="session-pick" @click="pickSession(s.id); showAllSessions = false">
                <span class="status-icon" :class="rowState(s)">
                  <span v-if="rowState(s) === 'pending'" class="q-flash">?</span>
                  <span v-else-if="rowState(s) === 'tool'" class="act-cog">⚙</span>
                  <span v-else-if="rowState(s) === 'thinking'" class="act-brain">🧠</span>
                  <span v-else class="dot"></span>
                </span>
                <span class="session-name">{{ s.concierge ? '🛎️ ' : '' }}{{ s.name }}</span>
                <span v-if="s.archived" class="archived-tag">archived</span>
              </button>
              <button
                v-if="!s.concierge"
                class="session-archive"
                :title="s.archived ? 'Unarchive' : 'Archive'"
                @click.stop="archiveSession(s)"
              >{{ s.archived ? '↩️' : '🗄️' }}</button>
            </li>
          </ul>
          <div class="modal-actions">
            <button class="secondary" @click="showAllSessions = false">Close</button>
          </div>
        </div>
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

      <div class="sidebar-spacer"></div>

      <!-- Settings: global / cross-session sections -->
      <div class="settings-footer">
        <div v-if="settingsOpen" class="settings-menu">
          <router-link
            v-for="item in settingsItems"
            :key="item.to"
            :to="item.to"
            class="settings-item"
            active-class="active"
            @click="onSettingsNav"
          >
            <span class="icon">{{ item.icon }}</span> {{ item.label }}
            <span v-if="item.badge" class="nav-badge">{{ item.badge }}</span>
          </router-link>
        </div>
        <button class="settings-btn" :class="{ open: settingsOpen }" @click="settingsOpen = !settingsOpen">
          <span class="icon">⚙</span> Settings
          <span v-if="pendingAsks > 0" class="nav-badge">{{ pendingAsks }}</span>
        </button>
      </div>
    </nav>
    <main class="content">
      <SessionTabBar v-if="isSessionRoute" @toggle-sidebar="sidebarOpen = !sidebarOpen" />
      <div class="content-body" :class="{ 'content-flush': isChatRoute }">
        <router-view />
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSessionStore } from './stores/session.js'
import { api } from './api.js'
import SessionTabBar from './components/SessionTabBar.vue'

const route = useRoute()
const router = useRouter()
const sidebarOpen = ref(false)
const { sessions, selectedSessionId, loadSessions } = useSessionStore()

// Sort helper: concierge pinned to the top, then most-recently-active first.
function byRecency(list) {
  return list.slice().sort((a, b) => {
    if (a.concierge && !b.concierge) return -1
    if (b.concierge && !a.concierge) return 1
    return (b.last_active || 0) - (a.last_active || 0)
  })
}

const activeSessions = computed(() => byRecency(sessions.value.filter(s => !s.archived)))

// How many sessions to show in the sidebar before "More…" (configurable).
const SESSION_LIST_LIMIT = Number(localStorage.getItem('enclave_session_list_limit')) || 10
const recentSessions = computed(() => activeSessions.value.slice(0, SESSION_LIST_LIMIT))
const hasMoreSessions = computed(() => activeSessions.value.length > SESSION_LIST_LIMIT)

// All-sessions popup
const showAllSessions = ref(false)
const showArchived = ref(false)
const allSessionsSorted = computed(() => {
  const list = showArchived.value ? sessions.value : sessions.value.filter(s => !s.archived)
  return byRecency(list)
})

function pickSession(id) {
  selectedSessionId.value = id
  sidebarOpen.value = false
  // If we're on a global/settings page, jump into the session's chat.
  if (!isSessionRoute.value) router.push('/chat')
}

// Sessions currently awaiting a reply / with a pending question (from the
// notifications feed) so their row can pulse and flash a "?".
const awaitingIds = computed(() => new Set(notifications.value.map(n => n.session_id)))
function isAwaiting(id) {
  return awaitingIds.value.has(id)
}
function rowState(s) {
  if (isAwaiting(s.id)) return 'pending'
  if (s.status !== 'running') return 'stopped'
  const a = activityState.value[s.id]
  if (a === 'tool') return 'tool'
  if (a === 'thinking' || a === 'responding') return 'thinking'
  return 'running'
}

async function archiveSession(s) {
  try {
    await api.archiveSession(s.id)
    await loadSessions()
  } catch (e) {
    console.error('Failed to archive session:', e)
  }
}

// ─── Settings menu (global / cross-session sections) ───
const settingsOpen = ref(false)
const settingsItems = computed(() => [
  { to: '/sessions', icon: '🗂️', label: 'Sessions' },
  { to: '/memories', icon: '🧠', label: 'Memories' },
  { to: '/panel', icon: '🎛️', label: 'Panel' },
  { to: '/schedules', icon: '⏰', label: 'Schedules' },
  { to: '/asks', icon: '❓', label: 'Asks', badge: pendingAsks.value || 0 },
])
function onSettingsNav() {
  settingsOpen.value = false
  sidebarOpen.value = false
}

// Routes that operate on the selected session (show the session tab bar).
const SESSION_ROUTE_NAMES = new Set([
  'chat', 'bugs', 'bug-detail', 'artifacts', 'artifact-preview', 'timeline', 'session-settings',
])
const isSessionRoute = computed(() => SESSION_ROUTE_NAMES.has(route.name))
const isChatRoute = computed(() => route.name === 'chat')

const isLoginPage = computed(() => route.name === 'login')
const hasToken = ref(!!localStorage.getItem('enclave_token'))
const pendingAsks = ref(0)

// ─── Notifications (sessions needing a reply) ───
const notifications = ref([])
// Coarse per-session activity state (thinking/tool/responding/idle) streamed
// from the orchestrator's global channel, used to animate the sidebar rows.
const activityState = ref({})
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
    notifWs.onmessage = (ev) => {
      let msg = null
      try { msg = JSON.parse(ev.data) } catch { /* ignore */ }
      if (msg && msg.type === 'session_activity') {
        // Live per-session activity for the sidebar indicators.
        activityState.value = { ...activityState.value, [msg.session_id]: msg.state }
        return
      }
      loadNotifications()
    }
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
  padding: 1rem 0 0;
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

.session-list-section {
  padding: 0.5rem 0.5rem 0.75rem;
  border-bottom: 1px solid var(--border);
}

.session-list-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.25rem 0.5rem;
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-secondary);
}

.session-list {
  list-style: none;
  margin: 0.25rem 0 0;
  padding: 0;
}

.session-row {
  display: flex;
  align-items: center;
  border-radius: var(--radius-sm, 4px);
  position: relative;
}

.session-row:hover {
  background: var(--bg-hover);
}

.session-row.active {
  background: var(--bg-active);
}

.session-pick {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 0.5rem;
  background: none;
  border: none;
  text-align: left;
  cursor: pointer;
  padding: 0.5rem 0.5rem;
  color: var(--text-secondary);
  font-size: 1.1rem;
}

.status-icon {
  width: 1.2em;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 0.95em;
}

.status-icon .dot {
  width: 0.6em;
  height: 0.6em;
  border-radius: 50%;
  background: #6b7280;
}

.status-icon.running .dot {
  background: #22c55e;
}

.act-cog {
  display: inline-block;
  font-size: 0.95em;
  line-height: 1;
  animation: cogSpin 2.4s linear infinite;
}

.act-brain {
  display: inline-block;
  font-size: 0.9em;
  line-height: 1;
  animation: brainPulse 1.3s ease-in-out infinite;
}

@keyframes cogSpin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

@keyframes brainPulse {
  0%, 100% { transform: scale(0.85); opacity: 0.6; }
  50% { transform: scale(1.1); opacity: 1; }
}

.status-icon.pending .q-flash {
  color: var(--accent);
  font-weight: 700;
  animation: qFlash 1s steps(1, end) infinite;
}

.new-glyph {
  color: var(--accent);
  font-weight: 600;
}

.new-row .session-pick {
  color: var(--accent);
}

.session-row.pending {
  animation: rowPulse 1.6s ease-in-out infinite;
}

@keyframes qFlash {
  0%, 50% { opacity: 1; }
  51%, 100% { opacity: 0.15; }
}

@keyframes rowPulse {
  0%, 100% { background: transparent; }
  50% { background: var(--bg-active); }
}

.session-row.active .session-pick {
  color: var(--accent);
  font-weight: 500;
}

.session-name {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
}

.run-dot {
  color: #22c55e;
  font-size: 0.6rem;
}

.session-archive {
  position: absolute;
  right: 0.2rem;
  top: 50%;
  transform: translateY(-50%);
  background: var(--bg-hover);
  border: none;
  border-radius: 4px;
  cursor: pointer;
  font-size: 0.9rem;
  padding: 0.25rem 0.4rem;
  opacity: 0;
  flex-shrink: 0;
  transition: opacity 0.15s;
}

.session-row:hover .session-archive {
  opacity: 0.85;
}

.session-archive:hover {
  opacity: 1;
}

.session-empty {
  padding: 0.45rem 0.5rem;
  font-size: 0.82rem;
}

.session-more {
  width: 100%;
  margin-top: 0.35rem;
  background: none;
  border: none;
  color: var(--accent);
  cursor: pointer;
  font-size: 0.78rem;
  padding: 0.35rem 0.5rem;
  text-align: left;
}

.session-more:hover {
  text-decoration: underline;
}

.session-modal {
  width: 420px;
  max-width: 92vw;
  max-height: 80vh;
  display: flex;
  flex-direction: column;
}

.session-modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 0.5rem;
}

.session-modal-head h3 {
  margin: 0;
}

.show-archived {
  font-size: 0.78rem;
  color: var(--text-secondary);
  display: flex;
  align-items: center;
  gap: 0.3rem;
  cursor: pointer;
}

.session-modal-list {
  list-style: none;
  margin: 0;
  padding: 0;
  overflow-y: auto;
  flex: 1;
}

.session-modal-list .session-pick {
  justify-content: space-between;
}

.archived-tag {
  font-size: 0.68rem;
  color: var(--text-secondary);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0 0.35rem;
  margin-left: 0.4rem;
}

.sidebar-spacer {
  flex: 1;
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
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-main);
  min-width: 0;
}

.content-body {
  flex: 1;
  overflow-y: auto;
  padding: 2rem;
  min-height: 0;
}

/* Full-height app views (chat) manage their own scrolling and padding. */
.content-body.content-flush {
  overflow: hidden;
  padding: 0;
  display: flex;
  flex-direction: column;
}

/* ─── Settings footer ─── */
.settings-footer {
  border-top: 1px solid var(--border);
  padding: 0.5rem;
}

.settings-menu {
  display: flex;
  flex-direction: column;
  margin-bottom: 0.35rem;
}

.settings-item {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  padding: 0.5rem 0.6rem;
  color: var(--text-secondary);
  text-decoration: none;
  font-size: 0.85rem;
  border-radius: var(--radius-sm, 4px);
}

.settings-item:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.settings-item.active {
  background: var(--bg-active);
  color: var(--accent);
  font-weight: 500;
}

.settings-btn {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 0.6rem;
  padding: 0.55rem 0.6rem;
  background: none;
  border: none;
  color: var(--text-secondary);
  font-size: 0.95rem;
  cursor: pointer;
}

.settings-btn:hover {
  color: var(--text-primary);
}

.settings-btn.open {
  color: var(--accent);
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

  /* On session routes the tab bar carries its own hamburger, so the
     standalone mobile header is redundant. */
  .mobile-header.mobile-hidden {
    display: none;
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
    flex: 1;
    overflow: hidden;
  }

  .content-body {
    padding: 1rem;
  }
}
</style>
