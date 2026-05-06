<template>
  <div class="session-detail" v-if="session">
    <div class="header">
      <button class="secondary" @click="$router.push('/sessions')">← Back</button>
      <h2>{{ session.name }}</h2>
      <span class="badge" :class="session.status">{{ session.status }}</span>
    </div>

    <!-- Controls -->
    <div class="controls card">
      <button class="danger" v-if="session.status === 'running'" @click="stop">Stop</button>
      <button class="primary" v-else @click="restart">Start</button>
      <button class="secondary" @click="restart" v-if="session.status === 'running'">Restart</button>
      <button class="secondary" @click="showSnapshot = true">Create Snapshot</button>
      <button class="danger" @click="clearState" style="margin-left:auto">Clear State</button>
    </div>

    <!-- Tabs -->
    <div class="tabs">
      <button
        v-for="tab in tabs"
        :key="tab"
        :class="{ active: activeTab === tab }"
        @click="activeTab = tab"
      >{{ tab }}</button>
    </div>

    <!-- Logs tab -->
    <div v-if="activeTab === 'Logs'" class="tab-content">
      <div class="log-viewer card">
        <pre class="log-output" ref="logEl">{{ logs }}</pre>
      </div>
    </div>

    <!-- State tab -->
    <div v-if="activeTab === 'State'" class="tab-content">
      <div class="state-grid">
        <div class="card file-list">
          <h3>Files ({{ stateFiles.length }})</h3>
          <div class="file-tree">
            <div
              v-for="f in stateFiles"
              :key="f.path"
              class="file-item"
              :class="{ active: selectedFile === f.path }"
              @click="viewFile(f.path)"
            >
              {{ f.path }}
              <span class="file-size">{{ formatSize(f.size) }}</span>
            </div>
          </div>
        </div>
        <div class="card file-content" v-if="fileContent !== null">
          <h3>{{ selectedFile }}</h3>
          <pre>{{ fileContent }}</pre>
        </div>
      </div>
    </div>

    <!-- Snapshots tab -->
    <div v-if="activeTab === 'Snapshots'" class="tab-content">
      <div v-if="snapshots.length" class="snapshot-list">
        <div v-for="snap in snapshots" :key="snap.filename" class="card snapshot-item">
          <div class="snap-info">
            <strong>{{ snap.name }}</strong>
            <span class="muted">{{ formatSize(snap.size) }} — {{ new Date(snap.created).toLocaleString() }}</span>
          </div>
          <div class="snap-actions">
            <button class="secondary" @click="downloadSnapshot(snap.filename)">Download</button>
            <button class="danger" @click="deleteSnapshot(snap.filename)">Delete</button>
          </div>
        </div>
      </div>
      <p v-else class="muted">No snapshots yet.</p>
    </div>

    <!-- Prompt tab -->
    <div v-if="activeTab === 'Prompt'" class="tab-content">
      <div class="card prompt-section">
        <h3>Session Prompt</h3>
        <p class="muted" style="margin:0 0 0.75rem;font-size:0.85rem">
          Custom instructions for this session. Applied on next agent restart.
        </p>
        <textarea
          v-model="sessionPrompt"
          class="prompt-editor"
          placeholder="Add custom instructions for this session…"
          rows="12"
        ></textarea>
        <div class="prompt-actions">
          <button class="primary" @click="savePrompt" :disabled="promptSaving">
            {{ promptSaving ? 'Saving…' : 'Save' }}
          </button>
          <span v-if="promptSaved" class="save-indicator">✓ Saved</span>
        </div>
      </div>

      <details v-for="(content, name) in basePrompts" :key="name" class="card base-prompt">
        <summary>{{ name }} <span class="muted">(read-only)</span></summary>
        <pre class="base-prompt-content">{{ content }}</pre>
      </details>
    </div>

    <!-- Snapshot modal -->
    <div v-if="showSnapshot" class="modal-overlay" @click.self="showSnapshot = false">
      <div class="modal card">
        <h3>Create Snapshot</h3>
        <input v-model="snapshotName" placeholder="Snapshot name" @keydown.enter="createSnapshot" :disabled="snapshotCreating" />
        <p v-if="snapshotCreating" class="creating-status">⏳ Creating snapshot… (this may take a moment for large sessions)</p>
        <div class="modal-actions">
          <button class="secondary" @click="showSnapshot = false" :disabled="snapshotCreating">Cancel</button>
          <button class="primary" @click="createSnapshot" :disabled="!snapshotName.trim() || snapshotCreating">
            {{ snapshotCreating ? 'Creating…' : 'Create' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api.js'

const route = useRoute()
const id = route.params.id

const session = ref(null)
const tabs = ['Logs', 'State', 'Snapshots', 'Prompt']
const activeTab = ref('Logs')

const logs = ref('')
const logEl = ref(null)

const stateFiles = ref([])
const selectedFile = ref(null)
const fileContent = ref(null)

const snapshots = ref([])
const showSnapshot = ref(false)
const snapshotName = ref('')
const snapshotCreating = ref(false)

const sessionPrompt = ref('')
const promptSaving = ref(false)
const promptSaved = ref(false)
const basePrompts = ref({})

onMounted(async () => {
  const sessions = await api.getSessions()
  session.value = sessions.find(s => s.id === id) || { id, name: id, status: 'unknown' }
  loadLogs()
  loadSnapshots()
})

async function loadLogs() {
  try {
    const data = await api.getLogs(id, 500)
    logs.value = data.lines?.join('\n') || data.output || ''
    await nextTick()
    if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight
  } catch (e) {
    logs.value = `Error loading logs: ${e.message}`
  }
}

async function loadState() {
  try {
    const data = await api.getState(id)
    stateFiles.value = data.files || []
  } catch (e) {
    stateFiles.value = []
  }
}

async function viewFile(path) {
  selectedFile.value = path
  try {
    const data = await api.getStateFile(id, path)
    fileContent.value = data.content || '(empty)'
  } catch (e) {
    fileContent.value = `Error: ${e.message}`
  }
}

async function loadSnapshots() {
  try {
    snapshots.value = await api.getSnapshots(id)
  } catch { snapshots.value = [] }
}

async function stop() {
  await api.stopSession(id)
  session.value.status = 'stopped'
}

async function restart() {
  await api.restartSession(id)
  session.value.status = 'running'
}

async function clearState() {
  if (!confirm('Clear all session state? This cannot be undone.')) return
  await api.clearState(id)
  stateFiles.value = []
  fileContent.value = null
}

async function createSnapshot() {
  if (!snapshotName.value.trim()) return
  snapshotCreating.value = true
  try {
    await api.createSnapshot(id, snapshotName.value.trim())
    showSnapshot.value = false
    snapshotName.value = ''
    activeTab.value = 'Snapshots'
    await loadSnapshots()
  } finally {
    snapshotCreating.value = false
  }
}

function downloadSnapshot(filename) {
  window.open(`/api/sessions/${id}/snapshots/${filename}/download`, '_blank')
}

async function deleteSnapshot(filename) {
  if (!confirm('Delete this snapshot?')) return
  await api.deleteSnapshot(id, filename)
  loadSnapshots()
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// Load state when tab switches
import { watch } from 'vue'
watch(activeTab, (tab) => {
  if (tab === 'State' && !stateFiles.value.length) loadState()
  if (tab === 'Prompt' && !sessionPrompt.value && !Object.keys(basePrompts.value).length) loadPrompt()
})

async function loadPrompt() {
  try {
    const data = await api.getSessionPrompt(id)
    sessionPrompt.value = data.session_prompt || ''
    basePrompts.value = data.base_prompts || {}
  } catch (e) {
    console.error('Failed to load prompt:', e)
  }
}

async function savePrompt() {
  promptSaving.value = true
  promptSaved.value = false
  try {
    await api.updateSessionPrompt(id, sessionPrompt.value)
    promptSaved.value = true
    setTimeout(() => { promptSaved.value = false }, 3000)
  } catch (e) {
    console.error('Failed to save prompt:', e)
  } finally {
    promptSaving.value = false
  }
}
</script>

<style scoped>
.header {
  display: flex;
  align-items: center;
  gap: 1rem;
  margin-bottom: 1.5rem;
}

.header h2 { margin: 0; }

.controls {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  margin-bottom: 1.5rem;
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

.tabs button:hover {
  color: var(--text-primary);
}

.log-viewer {
  max-height: 600px;
  overflow: hidden;
}

.log-output {
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 0.8rem;
  line-height: 1.5;
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 560px;
  overflow-y: auto;
  color: var(--text-secondary);
}

.state-grid {
  display: grid;
  grid-template-columns: 300px 1fr;
  gap: 1rem;
}

.file-list h3 {
  margin: 0 0 0.75rem;
  font-size: 0.9rem;
}

.file-tree {
  max-height: 500px;
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
}

.file-item:hover { background: var(--bg-hover); }
.file-item.active { background: var(--bg-active); color: var(--accent); }

.file-size {
  color: var(--text-muted);
  font-size: 0.7rem;
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
  max-height: 500px;
  overflow-y: auto;
  margin: 0;
}

.snapshot-list { display: flex; flex-direction: column; gap: 0.75rem; }

.snapshot-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.snap-info { display: flex; flex-direction: column; gap: 0.25rem; }
.snap-info .muted { font-size: 0.8rem; color: var(--text-muted); }
.snap-actions { display: flex; gap: 0.5rem; }

.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.modal {
  width: 400px;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.modal h3 { margin: 0; }
.modal-actions { display: flex; gap: 0.5rem; justify-content: flex-end; }

.muted { color: var(--text-muted); }

.prompt-section { margin-bottom: 1rem; }
.prompt-section h3 { margin: 0 0 0.5rem; }

.prompt-editor {
  width: 100%;
  min-height: 200px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.85rem;
  line-height: 1.5;
  resize: vertical;
  background: var(--bg-main);
  color: var(--text-primary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0.75rem;
}

.prompt-actions {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-top: 0.75rem;
}

.save-indicator {
  color: var(--success, #4ade80);
  font-size: 0.85rem;
}

.base-prompt {
  margin-bottom: 0.75rem;
  cursor: pointer;
}

.base-prompt summary {
  font-size: 0.9rem;
  padding: 0.5rem 0;
}

.base-prompt-content {
  font-size: 0.8rem;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 400px;
  overflow-y: auto;
  margin: 0.5rem 0 0;
  color: var(--text-secondary);
}

.creating-status {
  color: var(--warning);
  font-size: 0.85rem;
  margin: 0;
  animation: pulse 1.5s ease-in-out infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}
</style>
