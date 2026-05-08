<template>
  <div class="bugs-view">
    <h2>Bug & Task Tracker</h2>

    <!-- Filters -->
    <div class="filter-bar">
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
      <div class="modal card"
           @dragover.prevent="dragOver = true"
           @dragleave.prevent="dragOver = false"
           @drop.prevent="handleDrop">
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
        <div class="textarea-wrap" :class="{ 'drag-active': dragOver }">
          <textarea ref="bodyTextarea" v-model="editBug.body"
                    placeholder="Description (markdown) — paste images or drag & drop files"
                    rows="10"
                    @paste="handlePaste"></textarea>
          <div v-if="dragOver" class="drop-overlay">Drop files here</div>
        </div>

        <!-- Pending files (queued for upload on save) -->
        <div v-if="pendingFiles.length" class="attachments-section">
          <label>Pending uploads ({{ pendingFiles.length }})</label>
          <div class="attachment-list">
            <div v-for="(pf, i) in pendingFiles" :key="i" class="attachment-item pending">
              <img v-if="pf.preview" :src="pf.preview" class="att-preview" />
              <span>📎 {{ pf.file.name }} <span class="att-size">({{ formatSize(pf.file.size) }})</span></span>
              <button class="btn-x" @click="pendingFiles.splice(i, 1)">✕</button>
            </div>
          </div>
        </div>

        <!-- Existing attachments (edit mode) -->
        <div v-if="editing && attachments.length" class="attachments-section">
          <label>Attachments</label>
          <div class="attachment-list">
            <a v-for="att in attachments" :key="att.name"
               :href="attachUrl(editBug.id, att.name)"
               target="_blank" class="attachment-item">
              <img v-if="isImage(att.name)" :src="attachUrl(editBug.id, att.name)" class="att-preview" />
              <span>📎 {{ att.name }} <span class="att-size">({{ formatSize(att.size) }})</span></span>
            </a>
          </div>
        </div>

        <div class="upload-row">
          <input type="file" ref="fileInput" @change="addFiles" multiple />
          <span v-if="uploading" class="upload-status">Uploading…</span>
        </div>

        <div class="modal-actions">
          <button class="danger" v-if="editing" @click="deleteBug" style="margin-right:auto">Delete</button>
          <button class="secondary" @click="closeModal">Cancel</button>
          <button class="primary" @click="saveBug" :disabled="!editBug.title?.trim() || uploading">
            {{ editing ? 'Save' : 'Create' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { api } from '../api.js'
import { useSessionStore } from '../stores/session.js'

const { selectedSessionId } = useSessionStore()
const selectedSession = computed(() => selectedSessionId.value)

const bugs = ref([])
const loading = ref(false)
const showOpenOnly = ref(true)
const showModal = ref(false)
const editing = ref(false)
const editBug = ref({})
const attachments = ref([])
const uploading = ref(false)
const fileInput = ref(null)
const bodyTextarea = ref(null)
const pendingFiles = ref([])
const dragOver = ref(false)

function attachUrl(bugId, filename) {
  const token = localStorage.getItem('enclave_token')
  return `/api/bugs/${selectedSession.value}/${bugId}/attachments/${encodeURIComponent(filename)}?token=${encodeURIComponent(token)}`
}

const filteredBugs = computed(() => {
  if (!showOpenOnly.value) return bugs.value
  return bugs.value.filter(b => b.status === 'open' || b.status === 'in_progress')
})

onMounted(async () => {
  if (selectedSession.value) loadBugs()
})

watch(selectedSession, (newVal) => {
  if (newVal) loadBugs()
  else bugs.value = []
})

async function loadBugs() {
  if (!selectedSession.value) { bugs.value = []; return }
  loading.value = true
  try {
    const raw = await api.getBugs(selectedSession.value)
    // Sort by most recently edited first
    raw.sort((a, b) => {
      const aDate = a.updated || a.created || ''
      const bDate = b.updated || b.created || ''
      return bDate.localeCompare(aDate)
    })
    bugs.value = raw
  } catch { bugs.value = [] }
  finally { loading.value = false }
}

function defaultProject() {
  // Use the project from the first existing bug, or '_root' for workspace root
  const first = bugs.value.find(b => b.project)
  const proj = first?.project || '_root'
  // '.' gets collapsed by browser URL normalization, so remap to '_root'
  return proj === '.' ? '_root' : proj
}

function openCreate() {
  editing.value = false
  editBug.value = { title: '', severity: 'medium', type: 'bug', body: '' }
  pendingFiles.value = []
  showModal.value = true
}

function openEdit(bug) {
  editing.value = true
  editBug.value = { ...bug }
  attachments.value = []
  pendingFiles.value = []
  showModal.value = true
  loadAttachments(bug.id)
}

function closeModal() {
  showModal.value = false
  editBug.value = {}
  attachments.value = []
  pendingFiles.value = []
  dragOver.value = false
}

async function saveBug() {
  if (!editBug.value.title?.trim()) return
  let bugId = editBug.value.id
  if (editing.value) {
    await api.updateBug(selectedSession.value, editBug.value.id, {
      title: editBug.value.title,
      status: editBug.value.status,
      severity: editBug.value.severity,
      type: editBug.value.type,
      body: editBug.value.body,
    })
  } else {
    const result = await api.createBug(selectedSession.value, defaultProject(), {
      title: editBug.value.title,
      severity: editBug.value.severity,
      type: editBug.value.type,
      body: editBug.value.body,
    })
    bugId = result?.id || result?.bug_id
  }

  // Upload any pending files
  if (bugId && pendingFiles.value.length) {
    uploading.value = true
    try {
      for (const pf of pendingFiles.value) {
        await uploadFileForBug(bugId, pf.file)
      }
    } finally {
      uploading.value = false
    }
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

async function loadAttachments(bugId) {
  try {
    const token = localStorage.getItem('enclave_token')
    const res = await fetch(`/api/bugs/${selectedSession.value}/${bugId}/attachments`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    if (res.ok) attachments.value = await res.json()
  } catch { attachments.value = [] }
}

async function uploadFileForBug(bugId, file) {
  const token = localStorage.getItem('enclave_token')
  const form = new FormData()
  form.append('file', file)
  await fetch(`/api/bugs/${selectedSession.value}/${bugId}/attachments`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
    body: form,
  })
}

function addFiles() {
  const files = fileInput.value?.files
  if (!files?.length) return

  for (const file of files) {
    const preview = file.type.startsWith('image/') ? URL.createObjectURL(file) : null
    pendingFiles.value.push({ file, preview })
  }

  // If editing, upload immediately
  if (editing.value && editBug.value.id) {
    uploadPendingNow()
  }

  if (fileInput.value) fileInput.value.value = ''
}

async function uploadPendingNow() {
  if (!pendingFiles.value.length || !editBug.value.id) return
  uploading.value = true
  try {
    for (const pf of [...pendingFiles.value]) {
      await uploadFileForBug(editBug.value.id, pf.file)
    }
    pendingFiles.value = []
    await loadAttachments(editBug.value.id)
  } finally {
    uploading.value = false
  }
}

function handlePaste(e) {
  const items = e.clipboardData?.items
  if (!items) return

  for (const item of items) {
    if (item.type.startsWith('image/')) {
      e.preventDefault()
      const file = item.getAsFile()
      if (!file) continue
      const ext = file.type.split('/')[1] || 'png'
      const name = `paste-${Date.now()}.${ext}`
      const named = new File([file], name, { type: file.type })
      const preview = URL.createObjectURL(named)
      pendingFiles.value.push({ file: named, preview })

      // If editing, upload immediately and insert markdown reference
      if (editing.value && editBug.value.id) {
        uploadAndInsertImage(named)
      } else {
        // Insert a placeholder that will become valid after save
        insertAtCursor(`![${name}](attachment:${name})`)
      }
      return
    }
  }
}

function handleDrop(e) {
  dragOver.value = false
  const files = e.dataTransfer?.files
  if (!files?.length) return

  for (const file of files) {
    const preview = file.type.startsWith('image/') ? URL.createObjectURL(file) : null
    pendingFiles.value.push({ file, preview })
  }

  if (editing.value && editBug.value.id) {
    uploadPendingNow()
  }
}

async function uploadAndInsertImage(file) {
  uploading.value = true
  try {
    await uploadFileForBug(editBug.value.id, file)
    const url = `/api/bugs/${selectedSession.value}/${editBug.value.id}/attachments/${file.name}`
    insertAtCursor(`![${file.name}](${url})`)
    pendingFiles.value = pendingFiles.value.filter(pf => pf.file !== file)
    await loadAttachments(editBug.value.id)
  } finally {
    uploading.value = false
  }
}

function insertAtCursor(text) {
  const ta = bodyTextarea.value
  if (!ta) {
    editBug.value.body = (editBug.value.body || '') + '\n' + text
    return
  }
  const start = ta.selectionStart
  const end = ta.selectionEnd
  const val = editBug.value.body || ''
  const before = val.substring(0, start)
  const after = val.substring(end)
  const insert = (before && !before.endsWith('\n') ? '\n' : '') + text + '\n'
  editBug.value.body = before + insert + after
  // Restore cursor after inserted text
  const newPos = start + insert.length
  requestAnimationFrame(() => {
    ta.selectionStart = newPos
    ta.selectionEnd = newPos
    ta.focus()
  })
}

function isImage(name) {
  return /\.(png|jpg|jpeg|gif|webp|svg|bmp)$/i.test(name)
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
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

.attachments-section {
  border-top: 1px solid var(--border);
  padding-top: 0.75rem;
}

.attachments-section label {
  font-size: 0.75rem;
  text-transform: uppercase;
  color: var(--text-muted);
  display: block;
  margin-bottom: 0.5rem;
}

.attachment-list {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  margin-bottom: 0.75rem;
}

.attachment-item {
  font-size: 0.85rem;
  color: var(--accent);
  text-decoration: none;
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.attachment-item.pending {
  color: var(--text);
}

.attachment-item:hover { text-decoration: underline; }

.att-preview {
  width: 40px;
  height: 40px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  flex-shrink: 0;
}

.btn-x {
  background: none;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 0.8rem;
  padding: 0.1rem 0.3rem;
  margin-left: auto;
}
.btn-x:hover { color: var(--danger, #e55); }

.att-size {
  color: var(--text-muted);
  font-size: 0.75rem;
}

.textarea-wrap {
  position: relative;
}

.textarea-wrap textarea {
  width: 100%;
  box-sizing: border-box;
}

.textarea-wrap.drag-active textarea {
  opacity: 0.4;
}

.drop-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 1rem;
  font-weight: 600;
  color: var(--accent);
  border: 2px dashed var(--accent);
  border-radius: var(--radius-sm);
  background: rgba(var(--accent-rgb, 100, 149, 237), 0.08);
  pointer-events: none;
}

.upload-row {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.upload-row input[type="file"] {
  font-size: 0.8rem;
}

.upload-status {
  color: var(--warning);
  font-size: 0.8rem;
}

@media (max-width: 768px) {
  .bug-table { display: block; overflow-x: auto; }
  .modal { width: 95vw !important; max-height: 90vh; }
  .filter-bar { flex-direction: column; gap: 0.5rem; }
}
</style>
