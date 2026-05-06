<template>
  <div class="chat-view">
    <div class="chat-header">
      <h2>Chat</h2>
      <select v-model="selectedSession" @change="loadHistory">
        <option value="">Select a session…</option>
        <option v-for="s in sessions" :key="s.id" :value="s.id">
          {{ s.name }}{{ s.status === 'running' ? ' (active)' : '' }}
        </option>
      </select>
    </div>

    <div class="chat-container" v-if="selectedSession">
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

      <!-- Input -->
      <div class="input-bar">
        <textarea
          v-model="draft"
          placeholder="Send a message…"
          @keydown.enter.exact.prevent="send"
          @keydown.shift.enter.exact=""
          rows="1"
          ref="inputEl"
        ></textarea>
        <button class="primary" @click="send" :disabled="!draft.trim() || sending">Send</button>
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
let ws = null

onMounted(async () => {
  sessions.value = await api.getSessions()
  // Auto-select first running session
  const running = sessions.value.find(s => s.status === 'running')
  if (running) {
    selectedSession.value = running.id
    loadHistory()
  }
})

onUnmounted(() => {
  if (ws) ws.close()
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
  } catch (e) {
    console.error('Failed to load history:', e)
  }
}

function connectWebSocket() {
  if (ws) ws.close()
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const token = localStorage.getItem('enclave_token')
  ws = new WebSocket(`${proto}//${location.host}/api/chat/${selectedSession.value}/stream?token=${token}`)

  ws.onmessage = async (event) => {
    const turn = JSON.parse(event.data)
    // Update or append
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
    // Reconnect after delay
    setTimeout(() => {
      if (selectedSession.value) connectWebSocket()
    }, 3000)
  }
}

async function send() {
  if (!draft.value.trim() || !selectedSession.value) return
  const content = draft.value.trim()
  draft.value = ''
  sending.value = true

  try {
    await api.sendChatMessage(selectedSession.value, content)
  } catch (e) {
    console.error('Send failed:', e)
    draft.value = content
    sending.value = false
  }
}

function scrollToBottom() {
  if (messagesEl.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
  }
}

function renderMarkdown(text) {
  if (!text) return ''
  // Truncate very long messages for display performance
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

.chat-container {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

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

.empty-state {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}

.muted { color: var(--text-muted); }
</style>
