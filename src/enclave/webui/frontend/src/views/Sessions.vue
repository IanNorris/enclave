<template>
  <div class="sessions-view">
    <div class="sessions-header">
      <h2>Sessions</h2>
      <label class="filter-toggle">
        <input type="checkbox" v-model="showArchived" /> Show archived
      </label>
    </div>
    <div class="session-grid" v-if="filteredSessions.length">
      <div
        v-for="s in filteredSessions"
        :key="s.id"
        class="card session-card"
        :class="{ archived: s.archived }"
        @click="$router.push(`/sessions/${s.id}`)"
      >
        <div class="session-header">
          <span class="session-name">{{ s.name }}</span>
          <div class="session-badges">
            <span v-if="s.archived" class="badge archived">archived</span>
            <span class="badge" :class="s.status">{{ s.status }}</span>
          </div>
        </div>
        <div class="session-meta">
          <span class="session-id">{{ s.id }}</span>
          <button
            class="archive-btn"
            @click.stop="toggleArchive(s)"
            :title="s.archived ? 'Unarchive' : 'Archive'"
          >{{ s.archived ? '📥' : '📦' }}</button>
        </div>
      </div>
    </div>
    <p v-else-if="loading" class="muted">Loading sessions…</p>
    <p v-else class="muted">No sessions found.</p>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api.js'

const sessions = ref([])
const loading = ref(true)
const showArchived = ref(false)

const filteredSessions = computed(() => {
  let list = sessions.value
  if (!showArchived.value) {
    list = list.filter(s => !s.archived)
  }
  // Sort: running first, then alphabetical
  return list.sort((a, b) => {
    if (a.status === 'running' && b.status !== 'running') return -1
    if (b.status === 'running' && a.status !== 'running') return 1
    return a.name.localeCompare(b.name)
  })
})

onMounted(async () => {
  try {
    sessions.value = await api.getSessions()
  } catch (e) {
    console.error('Failed to load sessions:', e)
  } finally {
    loading.value = false
  }
})

async function toggleArchive(session) {
  try {
    const result = await api.archiveSession(session.id)
    session.archived = result.archived
  } catch (e) {
    console.error('Failed to toggle archive:', e)
  }
}
</script>

<style scoped>
.sessions-header {
  display: flex;
  align-items: center;
  gap: 1rem;
  margin-bottom: 1rem;
}

.sessions-header h2 { margin: 0; }

.filter-toggle {
  font-size: 0.85rem;
  color: var(--text-secondary);
  display: flex;
  align-items: center;
  gap: 0.4rem;
  cursor: pointer;
}

.session-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 1rem;
}

.session-card {
  cursor: pointer;
  transition: border-color 0.15s, transform 0.1s;
}

.session-card.archived {
  opacity: 0.6;
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

.session-badges {
  display: flex;
  gap: 0.4rem;
}

.badge.archived {
  background: rgba(160, 160, 160, 0.2);
  color: var(--text-muted);
}

.session-name {
  font-weight: 500;
  font-size: 1rem;
}

.session-meta {
  color: var(--text-muted);
  font-size: 0.8rem;
  font-family: monospace;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.archive-btn {
  background: none;
  border: none;
  cursor: pointer;
  font-size: 1rem;
  padding: 0.2rem;
  opacity: 0.5;
  transition: opacity 0.15s;
}

.archive-btn:hover { opacity: 1; }

.muted {
  color: var(--text-muted);
}

@media (max-width: 768px) {
  .session-list { gap: 0.5rem; }
}
</style>
