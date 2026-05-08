<template>
  <div v-if="isLoginPage || !hasToken">
    <router-view />
  </div>
  <div v-else class="app">
    <!-- Mobile header -->
    <div class="mobile-header">
      <button class="hamburger" @click="sidebarOpen = !sidebarOpen">☰</button>
      <span class="mobile-title">Enclave</span>
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
          <router-link to="/artifacts" active-class="active" @click="sidebarOpen = false">
            <span class="icon">📎</span> Artifacts
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
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useSessionStore } from './stores/session.js'

const route = useRoute()
const sidebarOpen = ref(false)
const { sessions, selectedSessionId, loadSessions } = useSessionStore()
const activeSessions = computed(() => sessions.value.filter(s => !s.archived))
const isLoginPage = computed(() => route.name === 'login')
const hasToken = ref(!!localStorage.getItem('enclave_token'))

// Only load sessions when authenticated and not on login page
onMounted(() => {
  if (!isLoginPage.value && hasToken.value) loadSessions()
})
watch(isLoginPage, (isLogin) => {
  if (!isLogin) {
    hasToken.value = !!localStorage.getItem('enclave_token')
    if (hasToken.value) loadSessions()
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
