<template>
  <div class="sessions-view">
    <h2>Sessions</h2>
    <div class="session-grid" v-if="sessions.length">
      <div
        v-for="s in sessions"
        :key="s.id"
        class="card session-card"
        @click="$router.push(`/sessions/${s.id}`)"
      >
        <div class="session-header">
          <span class="session-name">{{ s.name }}</span>
          <span class="badge" :class="s.status">{{ s.status }}</span>
        </div>
        <div class="session-meta">
          <span class="session-id">{{ s.id }}</span>
        </div>
      </div>
    </div>
    <p v-else-if="loading" class="muted">Loading sessions…</p>
    <p v-else class="muted">No sessions found.</p>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api.js'

const sessions = ref([])
const loading = ref(true)

onMounted(async () => {
  try {
    sessions.value = await api.getSessions()
    // Sort: running first, then alphabetical
    sessions.value.sort((a, b) => {
      if (a.status === 'running' && b.status !== 'running') return -1
      if (b.status === 'running' && a.status !== 'running') return 1
      return a.name.localeCompare(b.name)
    })
  } catch (e) {
    console.error('Failed to load sessions:', e)
  } finally {
    loading.value = false
  }
})
</script>

<style scoped>
.session-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 1rem;
}

.session-card {
  cursor: pointer;
  transition: border-color 0.15s, transform 0.1s;
}

.session-card:hover {
  border-color: var(--accent);
  transform: translateY(-1px);
}

.session-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.5rem;
}

.session-name {
  font-weight: 500;
  font-size: 1rem;
}

.session-meta {
  color: var(--text-muted);
  font-size: 0.8rem;
  font-family: monospace;
}

.muted {
  color: var(--text-muted);
}
</style>
