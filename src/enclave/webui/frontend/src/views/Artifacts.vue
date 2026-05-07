<template>
  <div class="artifacts-view">
    <div class="artifacts-header">
      <h2>Artifacts</h2>
    </div>

    <div v-if="!selectedSession" class="empty-state">
      <p class="muted">Select a session to view artifacts.</p>
    </div>

    <div v-else-if="artifacts.length" class="artifacts-grid">
      <div class="card file-list">
        <h3>Files ({{ artifacts.length }})</h3>
        <div class="file-tree">
          <div
            v-for="art in artifacts"
            :key="art.filename"
            class="file-item"
            :class="{ active: selectedArtifact?.filename === art.filename }"
            @click="viewArtifact(art)"
          >
            <div class="artifact-info">
              <span class="file-name">{{ art.title || art.filename }}</span>
              <span class="artifact-desc" v-if="art.description">{{ art.description }}</span>
            </div>
            <div class="artifact-meta">
              <span v-if="art.version > 1" class="version-badge">v{{ art.version }}</span>
              <span class="file-size">{{ formatSize(art.size) }}</span>
              <span class="artifact-date">{{ new Date(art.updated || art.created).toLocaleDateString() }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Content panel -->
      <div class="card file-content" v-if="selectedArtifact && (artifactContent !== null || diffContent !== null)">
        <div class="artifact-header-bar">
          <h3>{{ selectedArtifact.title || selectedArtifact.filename }}</h3>
          <div class="header-actions">
            <button v-if="selectedArtifact.version > 1 && !showingDiff" class="btn-sm" @click="showVersionHistory">⏱ History</button>
            <button v-if="showingDiff" class="btn-sm" @click="closeDiff">✕ Close diff</button>
            <a :href="api.artifactUrl(selectedSession, selectedArtifact.filename)" target="_blank" class="secondary open-link">Open ↗</a>
          </div>
        </div>

        <!-- Diff view -->
        <div v-if="showingDiff && diffContent !== null" class="diff-panel">
          <div class="diff-controls">
            <label>Compare: </label>
            <select v-model="diffV1" @change="loadDiff">
              <option v-for="v in versionOptions" :key="v" :value="v">v{{ v }}</option>
            </select>
            <span>→</span>
            <select v-model="diffV2" @change="loadDiff">
              <option v-for="v in versionOptions" :key="v" :value="v">v{{ v }}</option>
            </select>
          </div>
          <pre v-if="diffContent" class="diff-output">{{ diffContent }}</pre>
          <p v-else class="muted">No changes between these versions.</p>
        </div>

        <!-- Normal content view -->
        <template v-else>
          <div v-if="selectedArtifact.filename.endsWith('.md')" class="artifact-rendered" v-html="renderMarkdown(artifactContent)"></div>
          <pre v-else>{{ artifactContent }}</pre>
        </template>
      </div>
      <div class="card file-content" v-else-if="artifactLoading">
        <p class="muted">Loading…</p>
      </div>
    </div>

    <p v-else class="muted">No artifacts yet. Agents can publish artifacts using the <code>publish_artifact</code> tool.</p>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { useSessionStore } from '../stores/session.js'
import { api } from '../api.js'
import MarkdownIt from 'markdown-it'

const md = new MarkdownIt({ html: true, linkify: true, breaks: true })
const { selectedSessionId } = useSessionStore()
const selectedSession = computed(() => selectedSessionId.value)

const artifacts = ref([])
const selectedArtifact = ref(null)
const artifactContent = ref(null)
const artifactLoading = ref(false)

// Versioning state
const showingDiff = ref(false)
const diffContent = ref(null)
const diffV1 = ref(1)
const diffV2 = ref(2)
const versionOptions = ref([])

onMounted(() => {
  if (selectedSession.value) loadArtifacts()
})

watch(selectedSession, (v) => {
  selectedArtifact.value = null
  artifactContent.value = null
  closeDiff()
  if (v) loadArtifacts()
  else artifacts.value = []
})

async function loadArtifacts() {
  try {
    artifacts.value = await api.getArtifacts(selectedSession.value)
  } catch { artifacts.value = [] }
}

async function viewArtifact(art) {
  selectedArtifact.value = art
  artifactContent.value = null
  closeDiff()
  artifactLoading.value = true
  try {
    if (art.filename.match(/\.(md|txt|json|yaml|yml|csv|log)$/i)) {
      const data = await api.getArtifactContent(selectedSession.value, art.filename)
      artifactContent.value = data.content || ''
    } else {
      window.open(api.artifactUrl(selectedSession.value, art.filename), '_blank')
      selectedArtifact.value = null
    }
  } catch (e) {
    artifactContent.value = `Error: ${e.message}`
  } finally {
    artifactLoading.value = false
  }
}

async function showVersionHistory() {
  const art = selectedArtifact.value
  if (!art) return
  const currentVersion = art.version || 1
  // Build version options: 1 .. currentVersion
  const opts = []
  for (let i = 1; i <= currentVersion; i++) opts.push(i)
  versionOptions.value = opts
  diffV1.value = currentVersion - 1
  diffV2.value = currentVersion
  showingDiff.value = true
  await loadDiff()
}

async function loadDiff() {
  if (!selectedArtifact.value) return
  try {
    const data = await api.getArtifactDiff(
      selectedSession.value,
      selectedArtifact.value.filename,
      diffV1.value,
      diffV2.value
    )
    diffContent.value = data.diff || ''
  } catch (e) {
    diffContent.value = `Error loading diff: ${e.message}`
  }
}

function closeDiff() {
  showingDiff.value = false
  diffContent.value = null
}

function renderMarkdown(text) {
  return md.render(text || '')
}

function formatSize(bytes) {
  if (!bytes) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
</script>

<style scoped>
.artifacts-header {
  display: flex;
  align-items: center;
  gap: 1rem;
  margin-bottom: 1.5rem;
}
.artifacts-header h2 { margin: 0; }

.empty-state {
  text-align: center;
  padding: 3rem;
}

.artifacts-grid {
  display: grid;
  grid-template-columns: 380px 1fr;
  gap: 1rem;
}

.file-list h3 {
  margin: 0 0 0.75rem;
  font-size: 0.9rem;
}

.file-tree {
  max-height: 600px;
  overflow-y: auto;
}

.file-item {
  padding: 0.35rem 0.5rem;
  font-size: 0.8rem;
  font-family: monospace;
  cursor: pointer;
  border-radius: var(--radius-sm);
  display: flex;
  justify-content: space-between;
  gap: 0.5rem;
  min-width: 0;
}
.file-item:hover { background: var(--bg-hover); }
.file-item.active { background: var(--bg-active); color: var(--accent); }

.file-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
  flex: 1;
}

.file-size {
  color: var(--text-muted);
  font-size: 0.7rem;
  white-space: nowrap;
  flex-shrink: 0;
}

.artifact-info {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.artifact-desc {
  font-size: 0.7rem;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.artifact-meta {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  flex-shrink: 0;
  gap: 0.1rem;
}

.version-badge {
  font-size: 0.65rem;
  background: var(--accent);
  color: var(--bg-main);
  padding: 0.05rem 0.3rem;
  border-radius: 3px;
  font-weight: 600;
}

.artifact-date {
  font-size: 0.65rem;
  color: var(--text-muted);
}

.artifact-header-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.75rem;
}
.artifact-header-bar h3 {
  margin: 0;
  font-size: 0.9rem;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.btn-sm {
  font-size: 0.75rem;
  padding: 0.2rem 0.5rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-card);
  color: var(--text);
  cursor: pointer;
}
.btn-sm:hover { background: var(--bg-hover); }

.open-link {
  font-size: 0.8rem;
  padding: 0.25rem 0.5rem;
  text-decoration: none;
}

.diff-panel {
  border-top: 1px solid var(--border);
  padding-top: 0.75rem;
}

.diff-controls {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.75rem;
  font-size: 0.8rem;
}
.diff-controls select {
  padding: 0.2rem 0.4rem;
  font-size: 0.8rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-card);
  color: var(--text);
}

.diff-output {
  font-size: 0.75rem;
  font-family: 'JetBrains Mono', monospace;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 500px;
  overflow-y: auto;
  margin: 0;
  background: var(--bg-main);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0.5rem;
  line-height: 1.4;
}

.file-content h3 {
  margin: 0 0 0.75rem;
  font-size: 0.85rem;
  font-family: monospace;
  color: var(--text-muted);
}

.file-content pre {
  font-size: 0.8rem;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 600px;
  overflow-y: auto;
  margin: 0;
}

.artifact-rendered {
  font-size: 0.9rem;
  line-height: 1.6;
  max-height: 700px;
  overflow-y: auto;
}
.artifact-rendered :deep(h1) { font-size: 1.3rem; margin: 0.5rem 0; }
.artifact-rendered :deep(h2) { font-size: 1.1rem; margin: 0.5rem 0; }
.artifact-rendered :deep(h3) { font-size: 1rem; margin: 0.5rem 0; }
.artifact-rendered :deep(p) { margin: 0 0 0.5rem; }
.artifact-rendered :deep(pre) {
  background: var(--bg-main);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0.5rem;
  overflow-x: auto;
  font-size: 0.8rem;
}
.artifact-rendered :deep(code) {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.85em;
}
.artifact-rendered :deep(a) { color: var(--accent); }
.artifact-rendered :deep(img) { max-width: 100%; border-radius: var(--radius-sm); }
</style>
