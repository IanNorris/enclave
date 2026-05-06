<template>
  <div class="chat-view" @dragover.prevent="onDragOver" @dragleave="onDragLeave" @drop.prevent="onDrop">
    <div class="chat-header">
      <h2>Chat</h2>
      <select v-model="selectedSession" @change="loadHistory">
        <option value="">Select a session…</option>
        <option v-for="s in sessions" :key="s.id" :value="s.id">
          {{ s.name }}{{ s.status === 'running' ? ' (active)' : '' }}
        </option>
      </select>
      <select v-if="models.available.length" v-model="currentModel" @change="changeModel" class="model-select">
        <option v-for="m in models.available" :key="m" :value="m">{{ m }}</option>
      </select>
    </div>

    <div class="chat-container" v-if="selectedSession">
      <!-- Drop overlay -->
      <div v-if="dragging" class="drop-overlay">
        <div class="drop-label">Drop files to attach</div>
      </div>

      <!-- Messages -->
      <div class="messages" ref="messagesEl">
        <div v-for="turn in turns" :key="turn.turn_index" class="turn">
          <div v-if="turn.user_message" class="message user-message">
            <div class="message-meta">
              <span class="sender">User</span>
              <span class="time">{{ formatTime(turn.timestamp) }}</span>
            </div>
            <div class="message-body" v-html="renderMarkdown(turn.user_message)"></div>
          </div>
          <div v-if="turn.assistant_response" class="message assistant-message">
            <div class="message-meta">
              <span class="sender">Agent</span>
              <span class="time">{{ formatTime(turn.timestamp) }}</span>
            </div>
            <div class="message-body" v-html="renderMarkdown(turn.assistant_response)"></div>
          </div>
        </div>
        <div v-if="sending" class="message assistant-message typing">
          <span class="typing-indicator">●●●</span>
        </div>
      </div>

      <!-- Pending files -->
      <div v-if="pendingFiles.length" class="pending-files">
        <div v-for="(f, i) in pendingFiles" :key="i" class="file-chip">
          <img v-if="f.preview" :src="f.preview" class="file-thumb" />
          <span class="file-name">{{ f.file.name }}</span>
          <span class="file-size">{{ formatSize(f.file.size) }}</span>
          <button class="chip-remove" @click="removeFile(i)">✕</button>
        </div>
      </div>

      <!-- Input -->
      <div class="input-bar">
        <button class="secondary attach-btn" @click="$refs.chatFile.click()" title="Attach files">📎</button>
        <input type="file" ref="chatFile" style="display:none" @change="attachFiles" multiple accept="image/*,application/pdf,text/*" />
        <textarea
          v-model="draft"
          placeholder="Send a message… (paste or drop images)"
          @keydown.enter.exact.prevent="send"
          @keydown.shift.enter.exact=""
          @paste="onPaste"
          rows="1"
          ref="inputEl"
        ></textarea>
        <button class="primary" @click="send" :disabled="(!draft.trim() && !pendingFiles.length) || sending">Send</button>
      </div>
    </div>
    <div v-else class="empty-state">
      <p class="muted">Select a session to start chatting.</p>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { api } from '../api.js'
import MarkdownIt from 'markdown-it'

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

const sessions = ref([])
const selectedSession = ref('')
const turns = ref([])
const draft = ref('')
const sending = ref(false)
const messagesEl = ref(null)
const inputEl = ref(null)
const pendingFiles = ref([])
const dragging = ref(false)
const models = ref({ current: null, available: [], preferences: [] })
const currentModel = ref('')
let ws = null
let dragCounter = 0

onMounted(async () => {
  sessions.value = await api.getSessions()
  const running = sessions.value.find(s => s.status === 'running')
  if (running) {
    selectedSession.value = running.id
    loadHistory()
  }
})

onUnmounted(() => {
  if (ws) ws.close()
  pendingFiles.value.forEach(f => { if (f.preview) URL.revokeObjectURL(f.preview) })
})

watch(selectedSession, () => {
  if (ws) { ws.close(); ws = null }
})

async function loadHistory() {
  if (!selectedSession.value) { turns.value = []; return }
  try {
    const data = await api.getChatHistory(selectedSession.value, 200)
    turns.value = data.turns || []
    await nextTick()
    scrollToBottom()
    connectWebSocket()
    loadModels()
  } catch (e) {
    console.error('Failed to load history:', e)
  }
}

async function loadModels() {
  try {
    const data = await api.getModels(selectedSession.value)
    models.value = data
    currentModel.value = data.current || ''
  } catch (e) {
    console.error('Failed to load models:', e)
  }
}

async function changeModel() {
  if (!currentModel.value || !selectedSession.value) return
  try {
    await api.setModel(selectedSession.value, currentModel.value)
  } catch (e) {
    console.error('Failed to change model:', e)
  }
}

function connectWebSocket() {
  if (ws) ws.close()
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const token = localStorage.getItem('enclave_token')
  ws = new WebSocket(`${proto}//${location.host}/api/chat/${selectedSession.value}/stream?token=${token}`)

  ws.onmessage = async (event) => {
    const turn = JSON.parse(event.data)
    const idx = turns.value.findIndex(t => t.turn_index === turn.turn_index)
    if (idx >= 0) {
      turns.value[idx] = turn
    } else {
      turns.value.push(turn)
    }
    sending.value = false
    await nextTick()
    scrollToBottom()
  }

  ws.onclose = () => {
    setTimeout(() => {
      if (selectedSession.value) connectWebSocket()
    }, 3000)
  }
}

async function send() {
  if ((!draft.value.trim() && !pendingFiles.value.length) || !selectedSession.value) return
  const content = draft.value.trim()
  draft.value = ''
  sending.value = true

  try {
    const token = localStorage.getItem('enclave_token')
    const files = [...pendingFiles.value]

    if (files.length > 0) {
      // Upload each file sequentially
      for (let i = 0; i < files.length; i++) {
        const form = new FormData()
        form.append('file', files[i].file)
        // Send the text message with the last file
        if (i === files.length - 1 && content) {
          form.append('message', content)
        }
        await fetch(`/api/chat/${selectedSession.value}/upload`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${token}` },
          body: form,
        })
      }
      clearPendingFiles()
    } else {
      await api.sendChatMessage(selectedSession.value, content)
    }
  } catch (e) {
    console.error('Send failed:', e)
    draft.value = content
    sending.value = false
  }
}

function addFiles(fileList) {
  for (const file of fileList) {
    const entry = { file, preview: null }
    if (file.type.startsWith('image/')) {
      entry.preview = URL.createObjectURL(file)
    }
    pendingFiles.value.push(entry)
  }
}

function removeFile(index) {
  const f = pendingFiles.value[index]
  if (f.preview) URL.revokeObjectURL(f.preview)
  pendingFiles.value.splice(index, 1)
}

function clearPendingFiles() {
  pendingFiles.value.forEach(f => { if (f.preview) URL.revokeObjectURL(f.preview) })
  pendingFiles.value = []
}

function attachFiles(event) {
  const files = event.target.files
  if (files?.length) addFiles(files)
  event.target.value = ''
}

function onPaste(event) {
  const items = event.clipboardData?.items
  if (!items) return
  const imageFiles = []
  for (const item of items) {
    if (item.type.startsWith('image/')) {
      const file = item.getAsFile()
      if (file) imageFiles.push(file)
    }
  }
  if (imageFiles.length) {
    event.preventDefault()
    addFiles(imageFiles)
  }
}

function onDragOver(event) {
  dragCounter++
  dragging.value = true
}

function onDragLeave() {
  dragCounter--
  if (dragCounter <= 0) {
    dragging.value = false
    dragCounter = 0
  }
}

function onDrop(event) {
  dragging.value = false
  dragCounter = 0
  const files = event.dataTransfer?.files
  if (files?.length) addFiles(files)
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function scrollToBottom() {
  if (messagesEl.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
  }
}

function renderMarkdown(text) {
  if (!text) return ''
  const display = text.length > 10000 ? text.slice(0, 10000) + '\n\n…(truncated)' : text
  return md.render(display)
}

function formatTime(ts) {
  if (!ts) return ''
  const d = new Date(ts)
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}
</script>

<style scoped>
.chat-view {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 4rem);
}

.chat-header {
  display: flex;
  align-items: center;
  gap: 1rem;
  margin-bottom: 1rem;
}

.chat-header h2 { margin: 0; }
.chat-header select { max-width: 300px; }
.model-select { font-size: 0.8rem; max-width: 220px; }

.messages {
  flex: 1;
  overflow-y: auto;
  padding: 1rem 0;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.turn {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.message {
  max-width: 85%;
  border-radius: var(--radius);
  padding: 0.75rem 1rem;
}

.user-message {
  align-self: flex-end;
  background: #1a2a4a;
  border: 1px solid #2a3a5a;
}

.assistant-message {
  align-self: flex-start;
  background: var(--bg-card);
  border: 1px solid var(--border);
}

.message-meta {
  display: flex;
  gap: 0.75rem;
  margin-bottom: 0.4rem;
  font-size: 0.75rem;
}

.sender {
  font-weight: 600;
  color: var(--text-secondary);
}

.time {
  color: var(--text-muted);
}

.message-body {
  font-size: 0.9rem;
  line-height: 1.6;
}

.message-body :deep(p) { margin: 0 0 0.5rem; }
.message-body :deep(p:last-child) { margin: 0; }
.message-body :deep(pre) {
  background: var(--bg-main);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0.75rem;
  overflow-x: auto;
  font-size: 0.8rem;
}
.message-body :deep(code) {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.85em;
}
.message-body :deep(a) { color: var(--accent); }

.typing-indicator {
  animation: blink 1.2s infinite;
  color: var(--text-muted);
  font-size: 1.2rem;
  letter-spacing: 2px;
}

@keyframes blink {
  0%, 100% { opacity: 0.3; }
  50% { opacity: 1; }
}

.input-bar {
  display: flex;
  gap: 0.75rem;
  padding: 1rem 0 0;
  border-top: 1px solid var(--border);
  align-items: flex-end;
}

.input-bar .attach-btn {
  padding: 0.5rem 0.7rem;
  font-size: 1.1rem;
  cursor: pointer;
}

.input-bar textarea {
  flex: 1;
  resize: none;
  min-height: 42px;
  max-height: 150px;
  font-family: inherit;
}

.input-bar button {
  align-self: flex-end;
  padding: 0.6rem 1.5rem;
}

.pending-files {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  padding: 0.5rem 0;
}

.file-chip {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0.3rem 0.5rem;
  font-size: 0.8rem;
}

.file-thumb {
  width: 32px;
  height: 32px;
  object-fit: cover;
  border-radius: 3px;
}

.file-name {
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-size {
  color: var(--text-muted);
  font-size: 0.75rem;
}

.chip-remove {
  background: none;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  padding: 0 0.2rem;
  font-size: 0.9rem;
}

.chip-remove:hover { color: var(--text-primary); }

.drop-overlay {
  position: absolute;
  inset: 0;
  background: rgba(26, 42, 74, 0.85);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 50;
  border-radius: var(--radius);
  border: 2px dashed var(--accent);
}

.drop-label {
  font-size: 1.2rem;
  color: var(--accent);
  font-weight: 600;
}

.chat-container {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  position: relative;
}

.empty-state {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}

.muted { color: var(--text-muted); }

@media (max-width: 768px) {
  .chat-view { height: calc(100vh - 3.5rem); }
  .chat-header { flex-direction: column; align-items: stretch; gap: 0.5rem; }
  .chat-header select { max-width: 100%; }
  .message { max-width: 95%; }
  .input-bar { gap: 0.4rem; }
  .input-bar button { padding: 0.5rem 1rem; }
  .file-chip .file-name { max-width: 80px; }
}
</style>
