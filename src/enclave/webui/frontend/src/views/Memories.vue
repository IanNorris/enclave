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

    <!-- Search -->
    <div class="search-bar">
      <input v-model="search" placeholder="Filter memories…" />
    </div>

    <!-- Records table -->
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
          <tr v-for="(r, i) in filteredRecords" :key="i">
            <td><span class="badge" :class="r.type.toLowerCase()">{{ r.type }}</span></td>
            <td class="content-cell">{{ r.content }}</td>
            <td class="symbols-cell">{{ r.symbols?.join(', ') || '' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-else-if="!loading" class="muted">No memories found.</p>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api.js'

const stats = ref(null)
const records = ref([])
const search = ref('')
const loading = ref(true)

const filteredRecords = computed(() => {
  if (!search.value) return records.value
  const q = search.value.toLowerCase()
  return records.value.filter(r =>
    r.content?.toLowerCase().includes(q) ||
    r.symbols?.some(s => s.toLowerCase().includes(q))
  )
})

onMounted(async () => {
  try {
    const [statsData, recordsData] = await Promise.all([
      api.getMemoryStats(),
      api.getMemories(),
    ])
    stats.value = statsData
    records.value = recordsData
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

.badge.sem { background: #1a2a3a; color: #6ca8e8; }
.badge.pro { background: #2a1a3a; color: #a86ce8; }
.badge.epi { background: #1a3a2a; color: #6ce8a8; }

.muted { color: var(--text-muted); }
</style>
