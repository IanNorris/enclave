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
      <div class="search-bar">
        <input v-model="search" placeholder="Filter memories…" />
      </div>

      <div class="card" v-if="filteredRecords.length">
        <table class="memory-table">
          <thead>
            <tr>
              <th>Type</th>
              <th>Content</th>
              <th>Symbols</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(r, i) in filteredRecords" :key="i" class="memory-row" @click="selected = r">
              <td><span class="badge" :class="r.type?.toLowerCase()">{{ r.type }}</span></td>
              <td class="content-cell">{{ truncate(r.content, 120) }}</td>
              <td class="symbols-cell">{{ r.symbols?.join(', ') || '' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else-if="!loading" class="muted">No memories found.</p>
    </div>

    <!-- Symbols tab -->
    <div v-if="tab === 'symbols'">
      <div class="search-bar">
        <input v-model="symbolSearch" placeholder="Filter symbols…" />
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
          <span class="badge" :class="selected.type?.toLowerCase()">{{ selected.type }}</span>
          <button class="secondary" @click="selected = null">✕</button>
        </div>
        <pre class="detail-content">{{ selected.content }}</pre>
        <div v-if="selected.symbols?.length" class="detail-symbols">
          <strong>Symbols:</strong> {{ selected.symbols.join(', ') }}
        </div>
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
const loading = ref(true)
const symbolsLoading = ref(false)
const tab = ref('records')
const selected = ref(null)

const filteredRecords = computed(() => {
  const list = Array.isArray(records.value) ? records.value : []
  if (!search.value) return list
  const q = search.value.toLowerCase()
  return list.filter(r =>
    r.content?.toLowerCase().includes(q) ||
    r.type?.toLowerCase().includes(q) ||
    r.symbols?.some(s => s.toLowerCase().includes(q))
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

.search-bar {
  margin-bottom: 1rem;
  max-width: 400px;
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

.content-cell {
  max-width: 500px;
  white-space: pre-wrap;
  word-break: break-word;
}

.symbols-cell {
  font-family: monospace;
  font-size: 0.75rem;
  color: var(--text-secondary);
  max-width: 200px;
}

.symbol-name {
  font-family: monospace;
  font-weight: 500;
}

.badge.sem { background: #1a2a3a; color: #6ca8e8; }
.badge.pro { background: #2a1a3a; color: #a86ce8; }
.badge.epi { background: #1a3a2a; color: #6ce8a8; }
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
  width: 700px;
  max-height: 80vh;
  overflow-y: auto;
}

.detail-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 1rem;
}

.detail-content {
  font-size: 0.85rem;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0 0 1rem;
  line-height: 1.6;
}

.detail-symbols {
  font-size: 0.8rem;
  color: var(--text-secondary);
  font-family: monospace;
}

.muted { color: var(--text-muted); }
</style>
