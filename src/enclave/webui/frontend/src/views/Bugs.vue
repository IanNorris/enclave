<template>
  <div class="bugs-view">
    <h2>Bug Database</h2>

    <!-- Session selector -->
    <div class="filter-bar">
      <select v-model="selectedSession" @change="loadBugs">
        <option value="">Select a session…</option>
        <option v-for="s in sessions" :key="s.id" :value="s.id">{{ s.name }}</option>
      </select>
      <button class="primary" @click="showCreate = true" v-if="selectedSession">+ New Bug</button>
    </div>

    <!-- Bug list -->
    <div v-if="bugs.length" class="bug-list">
      <div v-for="bug in bugs" :key="bug.id" class="card bug-card">
        <div class="bug-header">
          <span class="bug-id">{{ bug.id }}</span>
          <span class="badge" :class="bug.status">{{ bug.status }}</span>
          <span class="badge severity" :class="bug.severity">{{ bug.severity }}</span>
        </div>
        <div class="bug-title">{{ bug.title }}</div>
        <div class="bug-meta" v-if="bug.created">
          {{ new Date(bug.created).toLocaleDateString() }}
        </div>
      </div>
    </div>
    <p v-else-if="selectedSession && !loading" class="muted">No bugs found for this session.</p>

    <!-- Create modal -->
    <div v-if="showCreate" class="modal-overlay" @click.self="showCreate = false">
      <div class="modal card">
        <h3>New Bug</h3>
        <input v-model="newBug.title" placeholder="Title" />
        <select v-model="newBug.severity">
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
          <option value="critical">Critical</option>
        </select>
        <textarea v-model="newBug.body" placeholder="Description (markdown)" rows="6"></textarea>
        <div class="modal-actions">
          <button class="secondary" @click="showCreate = false">Cancel</button>
          <button class="primary" @click="createBug" :disabled="!newBug.title.trim()">Create</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api.js'

const sessions = ref([])
const selectedSession = ref('')
const bugs = ref([])
const loading = ref(false)
const showCreate = ref(false)
const newBug = ref({ title: '', severity: 'medium', body: '' })

onMounted(async () => {
  sessions.value = await api.getSessions()
  // Auto-select first running session
  const running = sessions.value.find(s => s.status === 'running')
  if (running) {
    selectedSession.value = running.id
    loadBugs()
  }
})

async function loadBugs() {
  if (!selectedSession.value) { bugs.value = []; return }
  loading.value = true
  try {
    bugs.value = await api.getBugs(selectedSession.value)
  } catch { bugs.value = [] }
  finally { loading.value = false }
}

async function createBug() {
  if (!newBug.value.title.trim()) return
  await api.createBug(selectedSession.value, {
    title: newBug.value.title,
    severity: newBug.value.severity,
    body: newBug.value.body,
  })
  showCreate.value = false
  newBug.value = { title: '', severity: 'medium', body: '' }
  loadBugs()
}
</script>

<style scoped>
.filter-bar {
  display: flex;
  gap: 1rem;
  margin-bottom: 1.5rem;
  align-items: center;
}

.filter-bar select {
  max-width: 300px;
}

.bug-list {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.bug-card {
  cursor: pointer;
  transition: border-color 0.15s;
}

.bug-card:hover {
  border-color: var(--accent);
}

.bug-header {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  margin-bottom: 0.4rem;
}

.bug-id {
  font-family: monospace;
  font-size: 0.8rem;
  color: var(--text-muted);
}

.bug-title {
  font-weight: 500;
  margin-bottom: 0.25rem;
}

.bug-meta {
  font-size: 0.8rem;
  color: var(--text-muted);
}

.badge.open { background: var(--badge-running); color: var(--badge-running-text); }
.badge.resolved { background: var(--badge-stopped); color: var(--text-muted); }
.badge.severity.critical { background: #3a1a1a; color: var(--danger); }
.badge.severity.high { background: #3a2a1a; color: var(--warning); }
.badge.severity.medium { background: var(--bg-hover); color: var(--text-secondary); }
.badge.severity.low { background: var(--bg-hover); color: var(--text-muted); }

.modal-overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.6);
  display: flex; align-items: center; justify-content: center;
  z-index: 100;
}

.modal {
  width: 500px;
  display: flex; flex-direction: column; gap: 1rem;
}

.modal h3 { margin: 0; }
.modal-actions { display: flex; gap: 0.5rem; justify-content: flex-end; }
.muted { color: var(--text-muted); }
</style>
