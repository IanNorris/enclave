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
          {{ s.name }}{{ s.status === 'running' ? ' ●' : '' }}
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
            {{ s.name }}{{ s.status === 'running' ? ' ●' : '' }}
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
const activeSessions = computed(() => sessions.value.filter(s => !s.archived))
const isLoginPage = computed(() => route.name === 'login')
const hasToken = ref(!!localStorage.getItem('enclave_token'))
const pendingAsks = ref(0)

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
  }
})
onUnmounted(() => { if (askPollTimer) clearInterval(askPollTimer) })
watch(isLoginPage, (isLogin) => {
  if (!isLogin) {
    hasToken.value = !!localStorage.getItem('enclave_token')
    if (hasToken.value) {
      loadSessions()
      pollAskCount()
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
