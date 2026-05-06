<template>
  <div class="bugs-view">
    <h2>Bug & Task Tracker</h2>

    <!-- Session selector + filters -->
    <div class="filter-bar">
      <select v-model="selectedSession" @change="loadBugs">
        <option value="">Select a session…</option>
        <option v-for="s in sessions" :key="s.id" :value="s.id">{{ s.name }}</option>
      </select>
      <label class="filter-toggle">
        <input type="checkbox" v-model="showOpenOnly" /> Open only
      </label>
      <button class="primary" @click="openCreate" v-if="selectedSession">+ New</button>
    </div>

    <!-- Bug list -->
    <div v-if="filteredBugs.length" class="bug-list">
      <div v-for="bug in filteredBugs" :key="bug.id" class="card bug-card" @click="openEdit(bug)">
        <div class="bug-header">
          <span class="bug-id">{{ bug.id }}</span>
          <span class="badge type" :class="bug.type || 'bug'">{{ bug.type || 'bug' }}</span>
          <span class="badge" :class="bug.status">{{ bug.status }}</span>
          <span class="badge severity" :class="bug.severity">{{ bug.severity }}</span>
        </div>
        <div class="bug-title">{{ bug.title }}</div>
        <div class="bug-meta" v-if="bug.created">
          {{ new Date(bug.created).toLocaleDateString() }}
          <span v-if="bug.updated"> · updated {{ new Date(bug.updated).toLocaleDateString() }}</span>
        </div>
      </div>
    </div>
    <p v-else-if="selectedSession && !loading" class="muted">No {{ showOpenOnly ? 'open ' : '' }}items found.</p>

    <!-- Create/Edit modal -->
    <div v-if="showModal" class="modal-overlay" @click.self="closeModal">
      <div class="modal card">
        <h3>{{ editing ? `Edit ${editBug.id}` : 'New Item' }}</h3>
        <input v-model="editBug.title" placeholder="Title" />
        <div class="form-row">
          <div class="form-field">
            <label>Type</label>
            <select v-model="editBug.type">
              <option value="bug">Bug</option>
              <option value="task">Task</option>
            </select>
          </div>
          <div class="form-field">
            <label>Severity</label>
            <select v-model="editBug.severity">
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
              <option value="critical">Critical</option>
            </select>
          </div>
          <div class="form-field" v-if="editing">
            <label>Status</label>
            <select v-model="editBug.status">
              <option value="open">Open</option>
              <option value="in_progress">In Progress</option>
              <option value="resolved">Resolved</option>
              <option value="closed">Closed</option>
            </select>
          </div>
        </div>
        <textarea v-model="editBug.body" placeholder="Description (markdown)" rows="10"></textarea>
        <div class="modal-actions">
          <button class="danger" v-if="editing" @click="deleteBug" style="margin-right:auto">Delete</button>
          <button class="secondary" @click="closeModal">Cancel</button>
          <button class="primary" @click="saveBug" :disabled="!editBug.title?.trim()">
            {{ editing ? 'Save' : 'Create' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api.js'

const sessions = ref([])
const selectedSession = ref('')
const bugs = ref([])
const loading = ref(false)
const showOpenOnly = ref(true)
const showModal = ref(false)
const editing = ref(false)
const editBug = ref({})

const filteredBugs = computed(() => {
  if (!showOpenOnly.value) return bugs.value
  return bugs.value.filter(b => b.status === 'open' || b.status === 'in_progress')
})

onMounted(async () => {
  sessions.value = await api.getSessions()
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

function openCreate() {
  editing.value = false
  editBug.value = { title: '', severity: 'medium', type: 'bug', body: '' }
  showModal.value = true
}

function openEdit(bug) {
  editing.value = true
  editBug.value = { ...bug }
  showModal.value = true
}

function closeModal() {
  showModal.value = false
  editBug.value = {}
}

async function saveBug() {
  if (!editBug.value.title?.trim()) return
  if (editing.value) {
    await api.updateBug(selectedSession.value, editBug.value.id, {
      title: editBug.value.title,
      status: editBug.value.status,
      severity: editBug.value.severity,
      type: editBug.value.type,
      body: editBug.value.body,
    })
  } else {
    await api.createBug(selectedSession.value, {
      title: editBug.value.title,
      severity: editBug.value.severity,
      type: editBug.value.type,
      body: editBug.value.body,
    })
  }
  closeModal()
  loadBugs()
}

async function deleteBug() {
  if (!confirm(`Delete ${editBug.value.id}?`)) return
  await api.deleteBug(selectedSession.value, editBug.value.id)
  closeModal()
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

.filter-bar select { max-width: 300px; }

.filter-toggle {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.85rem;
  color: var(--text-secondary);
  cursor: pointer;
}

.filter-toggle input { cursor: pointer; }

.bug-list {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.bug-card {
  cursor: pointer;
  transition: border-color 0.15s;
}

.bug-card:hover { border-color: var(--accent); }

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

.bug-title { font-weight: 500; margin-bottom: 0.25rem; }

.bug-meta { font-size: 0.8rem; color: var(--text-muted); }

.badge.open { background: var(--badge-running); color: var(--badge-running-text); }
.badge.in_progress { background: #1a2a3a; color: #6ca8e8; }
.badge.resolved { background: var(--badge-stopped); color: var(--text-muted); }
.badge.closed { background: var(--badge-stopped); color: var(--text-muted); }
.badge.type.bug { background: #3a1a1a; color: var(--danger); }
.badge.type.task { background: #1a2a3a; color: var(--accent); }
.badge.severity.critical { background: #3a1a1a; color: var(--danger); }
.badge.severity.high { background: #3a2a1a; color: var(--warning); }
.badge.severity.medium { background: var(--bg-hover); color: var(--text-secondary); }
.badge.severity.low { background: var(--bg-hover); color: var(--text-muted); }

.form-row {
  display: flex;
  gap: 1rem;
}

.form-field {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.form-field label {
  font-size: 0.75rem;
  color: var(--text-muted);
  text-transform: uppercase;
}

.modal-overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.6);
  display: flex; align-items: center; justify-content: center;
  z-index: 100;
}

.modal {
  width: 600px;
  max-height: 85vh;
  overflow-y: auto;
  display: flex; flex-direction: column; gap: 1rem;
}

.modal h3 { margin: 0; }
.modal-actions { display: flex; gap: 0.5rem; justify-content: flex-end; }
.muted { color: var(--text-muted); }
</style>
