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
      <button :class="{ active: tab === 'graph' }" @click="tab = 'graph'; initGraph()">Graph</button>
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

    <!-- Graph tab -->
    <div v-if="tab === 'graph'" class="graph-container">
      <div class="graph-controls">
        <label><input type="checkbox" v-model="graphShowLabels" @change="updateGraphLabels"> Labels</label>
        <label>Min connections: <input type="number" v-model.number="graphMinRefs" min="0" max="20" style="width:50px" @change="rebuildGraph"></label>
        <button class="secondary" @click="resetGraphZoom">Reset zoom</button>
      </div>
      <div ref="graphEl" class="graph-svg-wrap"></div>
      <p v-if="!records.length && !loading" class="muted">No memories to visualize.</p>
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
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import { api } from '../api.js'
import * as d3 from 'd3'

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

// Graph state
const graphEl = ref(null)
const graphShowLabels = ref(true)
const graphMinRefs = ref(1)
let simulation = null
let svgSelection = null

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

onUnmounted(() => {
  if (simulation) simulation.stop()
})

// ─── Graph ────────────────────────────────────────────

function buildGraphData() {
  const nodeMap = new Map()
  const links = []
  const list = Array.isArray(records.value) ? records.value : []

  function ensureNode(name, kind) {
    if (!name) return null
    if (!nodeMap.has(name)) {
      nodeMap.set(name, { id: name, kind, refCount: 0 })
    }
    const n = nodeMap.get(name)
    n.refCount++
    return n
  }

  for (const r of list) {
    if (r.type === 'semantic') {
      const s = ensureNode(r.subject, 'subject')
      const o = ensureNode(r.object, 'object')
      if (s && o && s.id !== o.id) {
        links.push({ source: s.id, target: o.id, label: r.predicate || '' })
      }
    } else if (r.type === 'procedural') {
      const s = ensureNode(r.rule, 'rule')
      const o = ensureNode(r.action, 'action')
      if (s && o && s.id !== o.id) {
        links.push({ source: s.id, target: o.id, label: r.condition || '' })
      }
    }
  }

  // Filter by min refs
  const minR = graphMinRefs.value || 0
  const validIds = new Set()
  for (const [id, node] of nodeMap) {
    if (node.refCount >= minR) validIds.add(id)
  }

  const nodes = [...nodeMap.values()].filter(n => validIds.has(n.id))
  const filteredLinks = links.filter(l => validIds.has(l.source) && validIds.has(l.target))

  return { nodes, links: filteredLinks }
}

const KIND_COLORS = {
  subject: '#6ca8e8',
  object: '#a86ce8',
  rule: '#e8a86c',
  action: '#6ce8a8',
}

async function initGraph() {
  await nextTick()
  rebuildGraph()
}

function rebuildGraph() {
  if (!graphEl.value) return
  if (simulation) { simulation.stop(); simulation = null }

  const container = graphEl.value
  container.innerHTML = ''

  const { nodes, links } = buildGraphData()
  if (!nodes.length) return

  const width = container.clientWidth || 800
  const height = Math.max(400, container.clientHeight || 500)

  const svg = d3.select(container).append('svg')
    .attr('width', width)
    .attr('height', height)
    .attr('viewBox', [0, 0, width, height])

  svgSelection = svg

  const g = svg.append('g')

  // Zoom
  const zoom = d3.zoom()
    .scaleExtent([0.1, 5])
    .on('zoom', (event) => g.attr('transform', event.transform))
  svg.call(zoom)
  svg.__zoomBehavior = zoom

  // Arrow marker
  svg.append('defs').append('marker')
    .attr('id', 'arrow')
    .attr('viewBox', '0 -5 10 10')
    .attr('refX', 20)
    .attr('refY', 0)
    .attr('markerWidth', 6)
    .attr('markerHeight', 6)
    .attr('orient', 'auto')
    .append('path')
    .attr('d', 'M0,-5L10,0L0,5')
    .attr('fill', '#555')

  simulation = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(links).id(d => d.id).distance(100))
    .force('charge', d3.forceManyBody().strength(-200))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collision', d3.forceCollide().radius(30))

  const link = g.append('g')
    .selectAll('line')
    .data(links)
    .join('line')
    .attr('stroke', '#444')
    .attr('stroke-opacity', 0.6)
    .attr('marker-end', 'url(#arrow)')

  const node = g.append('g')
    .selectAll('circle')
    .data(nodes)
    .join('circle')
    .attr('r', d => 4 + Math.min(d.refCount * 2, 16))
    .attr('fill', d => KIND_COLORS[d.kind] || '#888')
    .attr('stroke', '#222')
    .attr('stroke-width', 1)
    .call(drag(simulation))

  node.append('title').text(d => `${d.id} (${d.kind}, ${d.refCount} refs)`)

  // Click node to select memories containing it
  node.on('click', (event, d) => {
    const matching = (records.value || []).filter(r =>
      r.subject === d.id || r.object === d.id || r.rule === d.id || r.action === d.id
    )
    if (matching.length === 1) {
      selected.value = matching[0]
    } else if (matching.length > 0) {
      // Switch to records tab filtered by this node
      search.value = d.id
      tab.value = 'records'
    }
  })

  const label = g.append('g')
    .selectAll('text')
    .data(nodes)
    .join('text')
    .text(d => d.id.length > 20 ? d.id.slice(0, 18) + '…' : d.id)
    .attr('font-size', '10px')
    .attr('fill', '#ccc')
    .attr('dx', 12)
    .attr('dy', 4)
    .attr('display', graphShowLabels.value ? null : 'none')

  simulation.on('tick', () => {
    link
      .attr('x1', d => d.source.x)
      .attr('y1', d => d.source.y)
      .attr('x2', d => d.target.x)
      .attr('y2', d => d.target.y)
    node
      .attr('cx', d => d.x)
      .attr('cy', d => d.y)
    label
      .attr('x', d => d.x)
      .attr('y', d => d.y)
  })
}

function updateGraphLabels() {
  if (!graphEl.value) return
  const svg = d3.select(graphEl.value).select('svg')
  svg.selectAll('text').attr('display', graphShowLabels.value ? null : 'none')
}

function resetGraphZoom() {
  if (!graphEl.value) return
  const svg = d3.select(graphEl.value).select('svg')
  const zoom = svg.node()?.__zoomBehavior
  if (zoom) svg.transition().duration(300).call(zoom.transform, d3.zoomIdentity)
}

function drag(sim) {
  return d3.drag()
    .on('start', (event, d) => {
      if (!event.active) sim.alphaTarget(0.3).restart()
      d.fx = d.x; d.fy = d.y
    })
    .on('drag', (event, d) => {
      d.fx = event.x; d.fy = event.y
    })
    .on('end', (event, d) => {
      if (!event.active) sim.alphaTarget(0)
      d.fx = null; d.fy = null
    })
}
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

/* Graph */
.graph-container {
  position: relative;
}

.graph-controls {
  display: flex;
  gap: 1rem;
  align-items: center;
  margin-bottom: 0.75rem;
  font-size: 0.85rem;
}

.graph-controls label {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  color: var(--text-secondary);
}

.graph-svg-wrap {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  min-height: 500px;
  overflow: hidden;
}

.graph-svg-wrap svg {
  display: block;
  cursor: grab;
}

.graph-svg-wrap svg:active {
  cursor: grabbing;
}

@media (max-width: 768px) {
  .stats-bar { grid-template-columns: repeat(2, 1fr); }
  .filter-row { flex-direction: column; }
  .memory-table { display: block; overflow-x: auto; }
  .detail-panel { width: 95vw; }
  .value-cell { max-width: 150px; }
}
</style>
