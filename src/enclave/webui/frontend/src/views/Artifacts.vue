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
              <span class="file-size">{{ formatSize(art.size) }}</span>
              <span class="artifact-date">{{ new Date(art.created).toLocaleDateString() }}</span>
            </div>
          </div>
        </div>
      </div>
      <div class="card file-content" v-if="selectedArtifact && artifactContent !== null">
        <div class="artifact-header-bar">
          <h3>{{ selectedArtifact.title || selectedArtifact.filename }}</h3>
          <a :href="api.artifactUrl(selectedSession, selectedArtifact.filename)" target="_blank" class="secondary open-link">Open ↗</a>
        </div>
        <div v-if="selectedArtifact.filename.endsWith('.md')" class="artifact-rendered" v-html="renderMarkdown(artifactContent)"></div>
        <pre v-else>{{ artifactContent }}</pre>
      </div>
      <div class="card file-content" v-else-if="artifactLoading">
        <p class="muted">Loading…</p>
      </div>
    </div>

    <p v-else class="muted">No artifacts yet. Agents can register files in the <code>artifacts/</code> folder.</p>
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

onMounted(() => {
  if (selectedSession.value) loadArtifacts()
})

watch(selectedSession, (v) => {
  selectedArtifact.value = null
  artifactContent.value = null
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

function renderMarkdown(text) {
  return md.render(text || '')
}

function formatSize(bytes) {
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

.open-link {
  font-size: 0.8rem;
  padding: 0.25rem 0.5rem;
  text-decoration: none;
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
