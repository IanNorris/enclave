<template>
  <div class="memories-view">
    <h2>Memories</h2>

    <!-- Stats card -->
    <div class="stats-bar" v-if="stats">
      <div class="stat card">
        <div class="stat-value">{{ stats.records }}</div>
        <div class="stat-label">Records</div>
      </div>
      <div class="stat card">
        <div class="stat-value">{{ stats.symbols }}</div>
        <div class="stat-label">Symbols</div>
      </div>
      <div class="stat card">
        <div class="stat-value">{{ stats.checkpoints }}</div>
        <div class="stat-label">Checkpoints</div>
      </div>
      <div class="stat card">
        <div class="stat-value">{{ stats.drafts?.pending || 0 }}</div>
        <div class="stat-label">Pending Drafts</div>
      </div>
    </div>

    <!-- Tabs -->
    <div class="tabs">
      <button :class="{ active: tab === 'records' }" @click="tab = 'records'">Records</button>
      <button :class="{ active: tab === 'symbols' }" @click="tab = 'symbols'; loadSymbols()">Symbols</button>
    </div>

    <!-- Records tab -->
    <div v-if="tab === 'records'">
      <div class="filter-row">
        <input v-model="search" placeholder="Filter memories…" class="search-input" />
        <select v-model="typeFilter">
          <option value="">All types</option>
          <option value="semantic">Semantic</option>
          <option value="procedural">Procedural</option>
        </select>
        <select v-model="sourceFilter">
          <option value="">All sources</option>
          <option v-for="s in availableSources" :key="s" :value="s">{{ s }}</option>
        </select>
      </div>

      <div class="card" v-if="filteredRecords.length">
        <table class="memory-table">
          <thead>
            <tr>
              <th>Type</th>
              <th>Subject / Rule</th>
              <th>Predicate</th>
              <th>Value</th>
              <th>Source</th>
              <th>Conf</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(r, i) in filteredRecords" :key="i" class="memory-row" @click="selected = r">
              <td><span class="badge" :class="r.type">{{ r.type === 'semantic' ? 'SEM' : 'PRO' }}</span></td>
              <td class="symbol-cell">{{ r.subject || r.rule || '' }}</td>
              <td class="symbol-cell">{{ r.predicate || r.condition || '' }}</td>
              <td class="value-cell">{{ truncate(r.object || r.action || '', 80) }}</td>
              <td class="source-cell">{{ r.source || '' }}</td>
              <td class="conf-cell">{{ r.confidence ? (parseFloat(r.confidence) * 100).toFixed(0) + '%' : '' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else-if="!loading" class="muted">No memories found.</p>
    </div>

    <!-- Symbols tab -->
    <div v-if="tab === 'symbols'">
      <div class="filter-row">
        <input v-model="symbolSearch" placeholder="Filter symbols…" class="search-input" />
      </div>

      <div class="card" v-if="filteredSymbols.length">
        <table class="memory-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Kind</th>
              <th>Refs</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(s, i) in filteredSymbols" :key="i">
              <td class="symbol-name">{{ s.name }}</td>
              <td><span class="badge" :class="s.kind?.toLowerCase()">{{ s.kind }}</span></td>
              <td>{{ s.ref_count ?? '' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else-if="!symbolsLoading" class="muted">No symbols found.</p>
    </div>

    <!-- Detail panel -->
    <div v-if="selected" class="modal-overlay" @click.self="selected = null">
      <div class="modal card detail-panel">
        <div class="detail-header">
          <span class="badge" :class="selected.type">{{ selected.type }}</span>
          <button class="secondary" @click="selected = null">✕</button>
        </div>
        <dl class="detail-fields">
          <template v-if="selected.type === 'semantic'">
            <dt>Subject</dt>
            <dd>{{ selected.subject }}</dd>
            <dt>Predicate</dt>
            <dd>{{ selected.predicate }}</dd>
            <dt>Value</dt>
            <dd class="detail-value">{{ selected.object }}</dd>
          </template>
          <template v-if="selected.type === 'procedural'">
            <dt>Rule</dt>
            <dd>{{ selected.rule }}</dd>
            <dt>Condition</dt>
            <dd class="detail-value">{{ selected.condition }}</dd>
            <dt>Action</dt>
            <dd class="detail-value">{{ selected.action }}</dd>
            <template v-if="selected.scope">
              <dt>Scope</dt>
              <dd>{{ selected.scope }}</dd>
            </template>
          </template>
          <template v-if="selected.source">
            <dt>Source</dt>
            <dd>{{ selected.source }}</dd>
          </template>
          <template v-if="selected.confidence">
            <dt>Confidence</dt>
            <dd>{{ (parseFloat(selected.confidence) * 100).toFixed(1) }}%</dd>
          </template>
          <template v-if="selected.timestamp">
            <dt>Timestamp</dt>
            <dd>{{ selected.timestamp }}</dd>
          </template>
        </dl>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api.js'

const stats = ref(null)
const records = ref([])
const symbols = ref([])
const search = ref('')
const symbolSearch = ref('')
const typeFilter = ref('')
const sourceFilter = ref('')
const loading = ref(true)
const symbolsLoading = ref(false)
const tab = ref('records')
const selected = ref(null)

const availableSources = computed(() => {
  const list = Array.isArray(records.value) ? records.value : []
  const sources = new Set()
  list.forEach(r => { if (r.source) sources.add(r.source) })
  return [...sources].sort()
})

const filteredRecords = computed(() => {
  let list = Array.isArray(records.value) ? records.value : []
  if (typeFilter.value) {
    list = list.filter(r => r.type === typeFilter.value)
  }
  if (sourceFilter.value) {
    list = list.filter(r => r.source === sourceFilter.value)
  }
  if (!search.value) return list
  const q = search.value.toLowerCase()
  return list.filter(r =>
    r.subject?.toLowerCase().includes(q) ||
    r.predicate?.toLowerCase().includes(q) ||
    r.rule?.toLowerCase().includes(q) ||
    r.object?.toLowerCase().includes(q) ||
    r.condition?.toLowerCase().includes(q) ||
    r.action?.toLowerCase().includes(q) ||
    r.source?.toLowerCase().includes(q)
  )
})

const filteredSymbols = computed(() => {
  const list = Array.isArray(symbols.value) ? symbols.value : []
  if (!symbolSearch.value) return list
  const q = symbolSearch.value.toLowerCase()
  return list.filter(s => s.name?.toLowerCase().includes(q) || s.kind?.toLowerCase().includes(q))
})

function truncate(text, len) {
  if (!text) return ''
  return text.length > len ? text.slice(0, len) + '…' : text
}

async function loadSymbols() {
  if (symbols.value.length) return
  symbolsLoading.value = true
  try {
    const data = await api.getSymbols()
    symbols.value = data.symbols || data || []
  } catch (e) {
    console.error('Failed to load symbols:', e)
  } finally {
    symbolsLoading.value = false
  }
}

onMounted(async () => {
  try {
    const [statsData, recordsData] = await Promise.all([
      api.getMemoryStats(),
      api.getMemories(),
    ])
    stats.value = statsData
    records.value = recordsData.records || recordsData || []
  } catch (e) {
    console.error('Failed to load memories:', e)
  } finally {
    loading.value = false
  }
})
</script>

<style scoped>
.stats-bar {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 1rem;
  margin-bottom: 1.5rem;
}

.stat {
  text-align: center;
  padding: 1rem;
}

.stat-value {
  font-size: 1.5rem;
  font-weight: 700;
  color: var(--accent);
}

.stat-label {
  font-size: 0.8rem;
  color: var(--text-muted);
  margin-top: 0.25rem;
}

.tabs {
  display: flex;
  gap: 0;
  border-bottom: 1px solid var(--border);
  margin-bottom: 1.5rem;
}

.tabs button {
  background: none;
  color: var(--text-secondary);
  padding: 0.75rem 1.25rem;
  border-radius: 0;
  border-bottom: 2px solid transparent;
}

.tabs button.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}

.tabs button:hover { color: var(--text-primary); }

.filter-row {
  display: flex;
  gap: 0.75rem;
  margin-bottom: 1rem;
  flex-wrap: wrap;
}

.filter-row .search-input {
  flex: 1;
  min-width: 200px;
}

.filter-row select {
  min-width: 120px;
}

.memory-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
}

.memory-table th {
  text-align: left;
  padding: 0.75rem 0.5rem;
  border-bottom: 1px solid var(--border);
  color: var(--text-muted);
  font-size: 0.75rem;
  text-transform: uppercase;
}

.memory-table td {
  padding: 0.6rem 0.5rem;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}

.memory-row {
  cursor: pointer;
  transition: background 0.1s;
}

.memory-row:hover { background: var(--bg-hover); }

.symbol-cell {
  font-family: monospace;
  font-size: 0.8rem;
  color: var(--text-secondary);
  white-space: nowrap;
}

.value-cell {
  max-width: 300px;
  font-size: 0.8rem;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.source-cell {
  font-size: 0.75rem;
  color: var(--text-muted);
  white-space: nowrap;
}

.conf-cell {
  font-size: 0.75rem;
  color: var(--text-muted);
  text-align: right;
  white-space: nowrap;
}

.symbol-name {
  font-family: monospace;
  font-weight: 500;
}

.badge.semantic { background: #1a2a3a; color: #6ca8e8; }
.badge.procedural { background: #2a1a3a; color: #a86ce8; }
.badge.checkpoint { background: #2a2a1a; color: #e8d86c; }
.badge.concept { background: #1a2a3a; color: #6ca8e8; }
.badge.entity { background: #2a1a3a; color: #a86ce8; }
.badge.function { background: #1a3a2a; color: #6ce8a8; }

.modal-overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.6);
  display: flex; align-items: center; justify-content: center;
  z-index: 100;
}

.detail-panel {
  width: 600px;
  max-height: 80vh;
  overflow-y: auto;
}

.detail-header {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-bottom: 1.25rem;
}

.detail-header button { margin-left: auto; }

.detail-id {
  font-family: monospace;
  font-size: 0.8rem;
  color: var(--text-muted);
}

.detail-fields {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 0.5rem 1rem;
  margin: 0;
}

.detail-fields dt {
  font-size: 0.75rem;
  text-transform: uppercase;
  color: var(--text-muted);
  padding-top: 0.2rem;
}

.detail-fields dd {
  margin: 0;
  font-family: monospace;
  font-size: 0.85rem;
  word-break: break-word;
}

.detail-value {
  white-space: pre-wrap;
}

.muted { color: var(--text-muted); }

@media (max-width: 768px) {
  .stats-bar { grid-template-columns: repeat(2, 1fr); }
  .filter-row { flex-direction: column; }
  .memory-table { display: block; overflow-x: auto; }
  .detail-panel { width: 95vw; }
  .value-cell { max-width: 150px; }
}
</style>
