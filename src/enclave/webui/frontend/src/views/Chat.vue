<template>
  <div class="chat-view" @dragover.prevent="onDragOver" @dragleave="onDragLeave" @drop.prevent="onDrop">
    <div class="chat-header">
      <h2>Chat</h2>
      <div class="model-picker" v-if="selectedSession">
        <select v-if="models.available.length" v-model="currentModel" @change="changeModel" class="model-select">
          <option v-for="m in models.available" :key="m" :value="m">{{ m }}</option>
        </select>
        <button class="model-refresh" @click="refreshModels" :disabled="modelsRefreshing" title="Refresh model list">
          {{ modelsRefreshing ? '⟳' : '↻' }}
        </button>
      </div>
    </div>

    <div class="chat-container" v-if="selectedSession">
      <!-- Drop overlay -->
      <div v-if="dragging" class="drop-overlay">
        <div class="drop-label">Drop files to attach</div>
      </div>

      <!-- Messages -->
      <div class="messages" ref="messagesEl">
        <!-- Load earlier button -->
        <div v-if="hasMore" class="load-earlier">
          <button class="secondary" @click="loadEarlier" :disabled="loadingMore">
            {{ loadingMore ? 'Loading…' : '↑ Load earlier messages' }}
          </button>
        </div>

        <!-- Completed turns from SQLite -->
        <div v-for="(turn, idx) in turns" :key="turn.turn_index ?? `m-${idx}`" class="turn">
          <div v-if="turn.user_message" class="message user-message">
            <div class="message-meta">
              <span class="sender">User</span>
              <span v-if="turn.source === 'queued'" class="queued-badge">queued</span>
              <span class="time">{{ formatTime(turn.timestamp) }}</span>
            </div>
            <div class="message-body" v-html="renderMarkdown(turn.user_message)"></div>
          </div>

          <!-- Persisted events for this turn (tool calls, thinking) -->
          <template v-if="turnEvents[turn.turn_index]?.length">
            <div class="turn-events" :class="{ collapsed: !expandedTurns[turn.turn_index] }">
              <div class="events-toggle" @click="expandedTurns[turn.turn_index] = !expandedTurns[turn.turn_index]">
                <span class="expand-toggle">{{ expandedTurns[turn.turn_index] ? '▼' : '▶' }}</span>
                <span class="events-summary">{{ turnEvents[turn.turn_index].length }} events</span>
              </div>
              <template v-if="expandedTurns[turn.turn_index]">
                <div v-for="(evt, ei) in turnEvents[turn.turn_index]" :key="ei" class="live-event" :class="evt.type">
                  <div v-if="evt.type === 'tool_start' || evt.type === 'tool_complete'" class="tool-block collapsed">
                    <div class="event-header">
                      <span class="event-icon">{{ TOOL_ICONS_MAP[evt.data?.name] || '🔧' }}</span>
                      <span class="event-label">{{ evt.data?.detail || evt.data?.name || 'tool' }}</span>
                      <span v-if="evt.type === 'tool_complete'" class="tool-status" :class="evt.data?.success !== false ? 'success' : 'fail'">
                        {{ evt.data?.success !== false ? '✅' : '❌' }}
                      </span>
                    </div>
                  </div>
                  <div v-else-if="evt.type === 'file_send'" class="tool-block collapsed">
                    <div class="event-header">
                      <span class="event-icon">📎</span>
                      <span class="event-label">{{ evt.data?.filename || 'file' }}</span>
                    </div>
                  </div>
                </div>
              </template>
            </div>
          </template>

          <div v-if="turn.assistant_response" class="message assistant-message">
            <div class="message-meta">
              <span class="sender">Agent</span>
              <span class="time">{{ formatTime(turn.timestamp) }}</span>
            </div>
            <div class="message-body" v-html="renderMarkdown(turn.assistant_response)"></div>
          </div>
        </div>

        <!-- Live streaming section -->
        <div v-if="liveEvents.length || streamingText" class="turn live-turn">
          <!-- Accumulated events (thinking blocks, tool calls) -->
          <div v-for="(evt, i) in liveEvents" :key="i" class="live-event" :class="evt.type">
            <!-- Thinking block -->
            <div v-if="evt.type === 'thinking'" class="thinking-block" :class="{ collapsed: evt.collapsed }">
              <div class="event-header" @click="evt.collapsed = !evt.collapsed">
                <span class="event-icon">🤔</span>
                <span class="event-label">Thinking</span>
                <span class="expand-toggle">{{ evt.collapsed ? '▶' : '▼' }}</span>
              </div>
              <div v-if="!evt.collapsed" class="event-content thinking-content">{{ evt.content }}</div>
            </div>

            <!-- Tool call -->
            <div v-if="evt.type === 'tool'" class="tool-block" :class="{ collapsed: evt.collapsed }">
              <div class="event-header" @click="evt.collapsed = !evt.collapsed">
                <span class="event-icon">{{ evt.icon }}</span>
                <span class="event-label">{{ evt.detail || evt.name }}</span>
                <span v-if="evt.done" class="tool-status" :class="evt.success ? 'success' : 'fail'">
                  {{ evt.success ? '✅' : '❌' }}
                </span>
                <span v-else class="tool-spinner">⏳</span>
                <span class="expand-toggle">{{ evt.collapsed ? '▶' : '▼' }}</span>
              </div>
            </div>
          </div>

          <!-- Streaming text with cursor -->
          <div v-if="streamingText" class="message assistant-message streaming">
            <div class="message-meta">
              <span class="sender">Agent</span>
              <span class="streaming-badge">streaming</span>
            </div>
            <div class="message-body" v-html="renderMarkdown(streamingText + ' ▍')"></div>
          </div>
        </div>

        <!-- Activity status -->
        <div v-if="activityText" class="activity-line">
          {{ activityText }}
        </div>

        <!-- Ask user prompt -->
        <div v-if="askUserPrompt" class="message assistant-message ask-user-block">
          <div class="message-meta">
            <span class="sender">Agent</span>
            <span class="ask-badge">question</span>
          </div>
          <div class="ask-question">{{ askUserPrompt.question }}</div>
          <div v-if="askUserPrompt.choices.length" class="ask-choices">
            <button
              v-for="(choice, i) in askUserPrompt.choices"
              :key="i"
              class="secondary ask-choice-btn"
              @click="answerAskUser(choice)"
            >{{ choice }}</button>
          </div>
          <div class="ask-freeform">
            <input
              v-model="askUserAnswer"
              placeholder="Type your answer…"
              @keydown.enter.prevent="answerAskUser(askUserAnswer)"
            />
            <button class="primary" @click="answerAskUser(askUserAnswer)" :disabled="!askUserAnswer.trim()">Reply</button>
          </div>
        </div>

        <!-- Sending indicator -->
        <div v-if="sending && !streamingText && !activityText" class="message assistant-message typing">
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
import { ref, reactive, onMounted, onUnmounted, nextTick, watch, computed } from 'vue'
import { api } from '../api.js'
import { useSessionStore } from '../stores/session.js'
import MarkdownIt from 'markdown-it'

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

const TOOL_ICONS = {
  bash: '🖥️', read_bash: '📖', write_bash: '⌨️', stop_bash: '⏹️',
  view: '📄', edit: '✏️', create: '📝', grep: '🔍', glob: '📁',
  web_fetch: '🌐', web_search: '🔎', task: '🤖', read_agent: '📨',
  sql: '🗃️', ask_user: '❓', list_bash: '📋',
}

const { selectedSessionId } = useSessionStore()
const selectedSession = computed(() => selectedSessionId.value)
const turns = ref([])
const hasMore = ref(false)
const loadingMore = ref(false)
const INITIAL_LIMIT = 50
const draft = ref('')
const sending = ref(false)
const messagesEl = ref(null)
const inputEl = ref(null)
const pendingFiles = ref([])
const dragging = ref(false)
const models = ref({ current: null, available: [], preferences: [] })
const currentModel = ref('')
const modelsRefreshing = ref(false)

// Live streaming state
const liveEvents = ref([])
const streamingText = ref('')
const activityText = ref('')
const askUserPrompt = ref(null)
const askUserAnswer = ref('')
let currentThinkingIdx = -1

// Persisted events per turn (turn_index → events array)
const turnEvents = ref({})
const expandedTurns = ref({})
const TOOL_ICONS_MAP = TOOL_ICONS

let ws = null
let dragCounter = 0

onMounted(async () => {
  if (selectedSession.value) {
    loadHistory()
  }
})

onUnmounted(() => {
  if (ws) ws.close()
  pendingFiles.value.forEach(f => { if (f.preview) URL.revokeObjectURL(f.preview) })
})

watch(selectedSession, (newVal) => {
  if (ws) { ws.close(); ws = null }
  clearLiveState()
  if (newVal) loadHistory()
})

function clearLiveState() {
  liveEvents.value = []
  streamingText.value = ''
  activityText.value = ''
  askUserPrompt.value = null
  askUserAnswer.value = ''
  currentThinkingIdx = -1
  turnEvents.value = {}
  expandedTurns.value = {}
}

async function loadHistory() {
  if (!selectedSession.value) { turns.value = []; return }
  try {
    const data = await api.getChatHistory(selectedSession.value, INITIAL_LIMIT)
    turns.value = data.turns || []
    // If we got exactly INITIAL_LIMIT turns, there may be more
    hasMore.value = turns.value.length >= INITIAL_LIMIT
    // Load persisted events (tool calls, thinking, etc.)
    await loadEvents()
    await nextTick()
    scrollToBottom()
    connectWebSocket()
    loadModels()
  } catch (e) {
    console.error('Failed to load history:', e)
  }
}

async function loadEvents() {
  if (!selectedSession.value) return
  try {
    const data = await api.getChatEvents(selectedSession.value, { limit: 2000 })
    const events = data.events || []
    // Group events by turn: assign each event to the turn whose timestamp
    // is closest-before the event timestamp
    const grouped = {}
    for (const evt of events) {
      // Skip non-visual event types
      if (!['tool_start', 'tool_complete', 'file_send'].includes(evt.type)) continue
      // Find the best matching turn
      let bestTurn = null
      for (const t of turns.value) {
        if (t.turn_index == null) continue
        if (t.timestamp && t.timestamp <= evt.timestamp) {
          bestTurn = t.turn_index
        }
      }
      if (bestTurn != null) {
        if (!grouped[bestTurn]) grouped[bestTurn] = []
        grouped[bestTurn].push(evt)
      }
    }
    turnEvents.value = grouped
  } catch (e) {
    console.error('Failed to load events:', e)
  }
}

async function loadEarlier() {
  if (loadingMore.value || !selectedSession.value) return
  loadingMore.value = true
  try {
    const data = await api.getChatHistory(selectedSession.value, INITIAL_LIMIT, turns.value.length)
    const older = data.turns || []
    if (older.length) {
      // Prepend older turns, preserve scroll position
      const el = messagesEl.value
      const prevHeight = el ? el.scrollHeight : 0
      turns.value = [...older, ...turns.value]
      hasMore.value = older.length >= INITIAL_LIMIT
      await nextTick()
      if (el) el.scrollTop = el.scrollHeight - prevHeight
    } else {
      hasMore.value = false
    }
  } catch (e) {
    console.error('Failed to load earlier messages:', e)
  } finally {
    loadingMore.value = false
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

async function refreshModels() {
  if (!selectedSession.value) return
  modelsRefreshing.value = true
  try {
    const data = await api.getModels(selectedSession.value, true)
    models.value = data
    currentModel.value = data.current || currentModel.value || ''
  } catch (e) {
    console.error('Failed to refresh models:', e)
  } finally {
    modelsRefreshing.value = false
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
    const msg = JSON.parse(event.data)
    handleStreamEvent(msg)
    await nextTick()
    scrollToBottom()
  }

  ws.onclose = () => {
    setTimeout(() => {
      if (selectedSession.value) connectWebSocket()
    }, 3000)
  }
}

function handleStreamEvent(msg) {
  const type = msg.type

  if (type === 'turn') {
    // Completed turn from SQLite — merge into turns list
    const idx = turns.value.findIndex(t => t.turn_index === msg.turn_index)
    if (idx >= 0) {
      turns.value[idx] = msg
    } else {
      // Remove any queued message that matches this turn's user_message
      if (msg.user_message) {
        const qIdx = turns.value.findIndex(t => t.source === 'queued' && t.user_message === msg.user_message)
        if (qIdx >= 0) turns.value.splice(qIdx, 1)
      }
      // Remove any live-cache entry that matches this turn's assistant_response
      if (msg.assistant_response) {
        const lIdx = turns.value.findIndex(t => t.source === 'live' && t.assistant_response === msg.assistant_response)
        if (lIdx >= 0) turns.value.splice(lIdx, 1)
      }
      turns.value.push(msg)
    }
    // Only clear live state if this turn has an assistant response
    if (msg.assistant_response) {
      clearLiveState()
    }
    sending.value = false
    return
  }

  if (type === 'turn_start') {
    // New turn — reset streaming text but keep accumulated live events
    streamingText.value = ''
    activityText.value = ''
    currentThinkingIdx = -1
    return
  }

  if (type === 'delta') {
    streamingText.value = msg.content || ''
    activityText.value = ''
    sending.value = false
    return
  }

  if (type === 'thinking') {
    const phase = msg.phase || 'delta'
    if (phase === 'end') {
      // Finalize thinking block — auto-collapse
      if (currentThinkingIdx >= 0 && currentThinkingIdx < liveEvents.value.length) {
        liveEvents.value[currentThinkingIdx].content = msg.content || liveEvents.value[currentThinkingIdx].content
        liveEvents.value[currentThinkingIdx].collapsed = true
      }
      currentThinkingIdx = -1
    } else {
      // Start or delta
      if (currentThinkingIdx < 0 || currentThinkingIdx >= liveEvents.value.length) {
        // Create new thinking block
        liveEvents.value.push(reactive({
          type: 'thinking',
          content: msg.content || '',
          collapsed: false,
        }))
        currentThinkingIdx = liveEvents.value.length - 1
      } else {
        liveEvents.value[currentThinkingIdx].content = msg.content || ''
      }
    }
    return
  }

  if (type === 'tool_start') {
    currentThinkingIdx = -1  // End any open thinking block
    const icon = TOOL_ICONS[msg.name] || '🔧'
    liveEvents.value.push(reactive({
      type: 'tool',
      name: msg.name || 'unknown',
      detail: msg.detail || '',
      icon,
      done: false,
      success: true,
      collapsed: false,
    }))
    activityText.value = ''
    return
  }

  if (type === 'tool_complete') {
    // Find the last tool event with this name that isn't done
    for (let i = liveEvents.value.length - 1; i >= 0; i--) {
      const evt = liveEvents.value[i]
      if (evt.type === 'tool' && evt.name === msg.name && !evt.done) {
        evt.done = true
        evt.success = msg.success !== false
        evt.collapsed = true
        break
      }
    }
    return
  }

  if (type === 'activity') {
    activityText.value = msg.text || ''
    return
  }

  if (type === 'ask_user') {
    askUserPrompt.value = {
      question: msg.question || '',
      choices: msg.choices || [],
    }
    askUserAnswer.value = ''
    return
  }

  if (type === 'response') {
    // Final response text — keep it visible until the turn poll picks it up
    if (msg.content) {
      streamingText.value = msg.content
    }
    activityText.value = ''
    // Collapse all remaining live events
    liveEvents.value.forEach(e => { e.collapsed = true })
    return
  }

  if (type === 'turn_end') {
    sending.value = false
    activityText.value = ''
    return
  }
}

async function send() {
  if ((!draft.value.trim() && !pendingFiles.value.length) || !selectedSession.value) return
  const content = draft.value.trim()
  draft.value = ''
  sending.value = true

  // Immediately show the message as "queued"
  if (content) {
    const ts = new Date().toISOString()
    turns.value.push({
      turn_index: null,
      user_message: content,
      assistant_response: null,
      timestamp: ts,
      source: 'queued',
    })
    await nextTick()
    scrollToBottom()
  }

  try {
    const token = localStorage.getItem('enclave_token')
    const files = [...pendingFiles.value]

    if (files.length > 0) {
      for (let i = 0; i < files.length; i++) {
        const form = new FormData()
        form.append('file', files[i].file)
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
    // Remove the queued message on failure
    const idx = turns.value.findIndex(t => t.source === 'queued' && t.user_message === content)
    if (idx >= 0) turns.value.splice(idx, 1)
    sending.value = false
  }
}

async function answerAskUser(answer) {
  if (!answer?.trim() || !selectedSession.value) return
  const text = answer.trim()
  askUserPrompt.value = null
  askUserAnswer.value = ''
  // Show as queued user message
  turns.value.push({
    turn_index: null,
    user_message: text,
    assistant_response: null,
    timestamp: new Date().toISOString(),
    source: 'queued',
  })
  await nextTick()
  scrollToBottom()
  try {
    await api.sendChatMessage(selectedSession.value, text)
  } catch (e) {
    console.error('Failed to send answer:', e)
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
  // Strip <current_datetime>...</current_datetime> tags injected by the system
  let cleaned = text.replace(/<current_datetime>[^<]*<\/current_datetime>/g, '')
  // Trim excessive whitespace left behind
  cleaned = cleaned.replace(/\n{3,}/g, '\n\n').trim()
  // Convert mxc:// URLs to proxied URLs for images
  cleaned = cleaned.replace(/mxc:\/\/([^/\s]+)\/([^)\s]+)/g, '/api/chat/media/$1/$2')
  const display = cleaned.length > 10000 ? cleaned.slice(0, 10000) + '\n\n…(truncated)' : cleaned
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

.model-picker {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  margin-left: auto;
}

.model-select {
  width: auto;
  max-width: 260px;
  font-size: 0.8rem;
  padding: 0.4rem 0.5rem;
  background: var(--bg-main);
  color: var(--text-primary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
}

.model-refresh {
  font-size: 1rem;
  padding: 0.25rem 0.4rem;
  background: none;
  border: 1px solid var(--border);
  color: var(--text-secondary);
  border-radius: var(--radius-sm, 4px);
  cursor: pointer;
  line-height: 1;
}
.model-refresh:hover { color: var(--text-primary); background: var(--bg-hover); }
.model-refresh:disabled { opacity: 0.5; cursor: wait; }

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

.load-earlier {
  text-align: center;
  padding: 0.5rem 0;
}

.load-earlier button {
  font-size: 0.8rem;
}

.queued-badge {
  font-size: 0.65rem;
  color: #e8a735;
  background: rgba(232, 167, 53, 0.15);
  padding: 0.1rem 0.4rem;
  border-radius: 3px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.ask-user-block {
  max-width: 90%;
}

.ask-badge {
  font-size: 0.65rem;
  color: #e8a735;
  background: rgba(232, 167, 53, 0.15);
  padding: 0.1rem 0.4rem;
  border-radius: 3px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.ask-question {
  font-size: 0.9rem;
  margin-bottom: 0.75rem;
  line-height: 1.5;
}

.ask-choices {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-bottom: 0.75rem;
}

.ask-choice-btn {
  font-size: 0.8rem;
  padding: 0.4rem 0.8rem;
}

.ask-freeform {
  display: flex;
  gap: 0.5rem;
}

.ask-freeform input {
  flex: 1;
  font-size: 0.85rem;
  padding: 0.4rem 0.6rem;
}

.ask-freeform button {
  padding: 0.4rem 0.8rem;
  font-size: 0.8rem;
}

/* Live streaming styles */
.live-turn {
  border-left: 2px solid var(--accent);
  padding-left: 0.75rem;
  margin-left: 0.25rem;
}

.live-event {
  margin-bottom: 0.5rem;
}

.thinking-block,
.tool-block {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
}

.event-header {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.4rem 0.6rem;
  cursor: pointer;
  font-size: 0.8rem;
  color: var(--text-secondary);
  user-select: none;
}

.event-header:hover {
  background: var(--bg-main);
}

.event-icon {
  flex-shrink: 0;
}

.event-label {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.expand-toggle {
  flex-shrink: 0;
  font-size: 0.65rem;
  color: var(--text-muted);
}

.thinking-content {
  padding: 0.5rem 0.6rem;
  font-size: 0.78rem;
  font-style: italic;
  color: var(--text-muted);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 200px;
  overflow-y: auto;
  border-top: 1px solid var(--border);
}

.tool-status { flex-shrink: 0; }
.tool-status.success { color: var(--success); }
.tool-status.fail { color: var(--danger); }

.tool-spinner {
  flex-shrink: 0;
  animation: pulse 1.5s ease-in-out infinite;
}

.streaming-badge {
  font-size: 0.65rem;
  color: var(--accent);
  background: rgba(99, 179, 237, 0.15);
  padding: 0.1rem 0.4rem;
  border-radius: 3px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.streaming .message-body {
  opacity: 0.95;
}

.activity-line {
  font-size: 0.78rem;
  color: var(--text-muted);
  padding: 0.25rem 0;
  font-style: italic;
  animation: fadeInOut 2s ease-in-out infinite;
}

@keyframes fadeInOut {
  0%, 100% { opacity: 0.6; }
  50% { opacity: 1; }
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

@media (max-width: 768px) {
  .chat-view { height: calc(100vh - 3.5rem); }
  .chat-header { flex-direction: column; align-items: stretch; gap: 0.5rem; }
  .chat-header select { max-width: 100%; }
  .message { max-width: 95%; }
  .input-bar { gap: 0.4rem; }
  .input-bar button { padding: 0.5rem 1rem; }
  .file-chip .file-name { max-width: 80px; }
}

/* Persisted turn events */
.turn-events {
  margin: 0.25rem 0;
  padding-left: 0.5rem;
  border-left: 2px solid var(--border-color, #333);
}
.events-toggle {
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.2rem 0;
  font-size: 0.8rem;
  color: var(--text-secondary, #888);
  user-select: none;
}
.events-toggle:hover {
  color: var(--text-primary, #ccc);
}
.events-summary {
  font-style: italic;
}
</style>
