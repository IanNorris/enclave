<template>
  <div class="app">
    <nav class="sidebar">
      <div class="logo">
        <h1>Enclave</h1>
      </div>
      <ul class="nav-links">
        <li>
          <router-link to="/sessions" active-class="active">
            <span class="icon">⚙</span> Sessions
          </router-link>
        </li>
        <li>
          <router-link to="/chat" active-class="active">
            <span class="icon">💬</span> Chat
          </router-link>
        </li>
        <li>
          <router-link to="/bugs" active-class="active">
            <span class="icon">🐛</span> Bugs
          </router-link>
        </li>
        <li>
          <router-link to="/memories" active-class="active">
            <span class="icon">🧠</span> Memories
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
</script>

<style scoped>
.app {
  display: flex;
  height: 100vh;
}

.sidebar {
  width: 220px;
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  padding: 1rem 0;
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
}
</style>
