<template>
  <div class="asks-view">
    <div class="asks-header">
      <h2>Agent Asks</h2>
      <div class="asks-filters">
        <select v-model="filterSession">
          <option value="">All sessions</option>
          <option v-for="s in sessionNames" :key="s.id" :value="s.id">{{ s.name }}</option>
        </select>
        <select v-model="filterStatus">
          <option value="pending">Pending</option>
          <option value="all">All</option>
        </select>
      </div>
    </div>

    <div v-if="loading" class="loading">Loading…</div>

    <div v-else-if="asks.length === 0" class="empty-state">
      <div class="empty-icon">💬</div>
      <p>No {{ filterStatus === 'pending' ? 'pending' : '' }} questions from agents.</p>
      <p class="empty-hint">When an agent uses <code>ask_deferred</code>, non-blocking questions appear here.</p>
    </div>

    <div v-else class="asks-list">
      <div v-for="ask in asks" :key="ask.id" class="ask-card" :class="[ask.priority, ask.status]">
        <div class="ask-header">
          <span class="ask-session">{{ ask.session_name || ask.session_id }}</span>
          <span class="ask-priority" v-if="ask.priority !== 'normal'">{{ ask.priority }}</span>
          <span class="ask-time">{{ formatTime(ask.created_at) }}</span>
          <span v-if="ask.status !== 'pending'" class="ask-status-badge" :class="ask.status">{{ ask.status }}</span>
        </div>

        <div class="ask-question">{{ ask.question }}</div>

        <div v-if="ask.context" class="ask-context">
          <details>
            <summary>Context</summary>
            <div class="context-body">{{ ask.context }}</div>
          </details>
        </div>

        <div v-if="ask.tags?.length" class="ask-tags">
          <span v-for="tag in ask.tags" :key="tag" class="tag">{{ tag }}</span>
        </div>

        <template v-if="ask.status === 'pending'">
          <div v-if="ask.choices?.length" class="ask-choices">
            <button
              v-for="(choice, i) in ask.choices"
              :key="i"
              class="choice-btn"
              @click="submitAnswer(ask.id, choice)"
              :disabled="submitting === ask.id"
            >{{ choice }}</button>
          </div>

          <div class="ask-freeform">
            <input
              v-model="answers[ask.id]"
              :placeholder="ask.choices?.length ? 'Or type your own answer…' : 'Type your answer…'"
              @keydown.enter.prevent="submitAnswer(ask.id, answers[ask.id])"
              :disabled="submitting === ask.id"
            />
            <button class="primary" @click="submitAnswer(ask.id, answers[ask.id])" :disabled="!answers[ask.id]?.trim() || submitting === ask.id">
              Reply
            </button>
            <button class="dismiss-btn" @click="dismissAsk(ask.id)" :disabled="submitting === ask.id">
              Dismiss
            </button>
          </div>
        </template>

        <div v-else-if="ask.status === 'answered'" class="ask-answered">
          <span class="answer-label">Answer:</span> {{ ask.answer }}
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, watch, reactive } from 'vue'
import { api } from '../api.js'

const asks = ref([])
const loading = ref(true)
const filterSession = ref('')
const filterStatus = ref('pending')
const answers = reactive({})
const submitting = ref(null)
const sessionNames = ref([])

async function loadAsks() {
  loading.value = true
  try {
    const data = await api.getAsks(filterSession.value || null, filterStatus.value)
    asks.value = data.asks || []
  } catch (e) {
    console.error('Failed to load asks:', e)
  }
  loading.value = false
}

async function loadSessions() {
  try {
    const data = await api.getSessions()
    sessionNames.value = (data.sessions || []).map(s => ({ id: s.id, name: s.name }))
  } catch (e) {
    console.error('Failed to load sessions:', e)
  }
}

async function submitAnswer(askId, answer) {
  if (!answer?.trim()) return
  submitting.value = askId
  try {
    await api.answerAsk(askId, answer.trim())
    delete answers[askId]
    await loadAsks()
  } catch (e) {
    console.error('Failed to submit answer:', e)
  }
  submitting.value = null
}

async function dismissAsk(askId) {
  submitting.value = askId
  try {
    await api.dismissAsk(askId)
    await loadAsks()
  } catch (e) {
    console.error('Failed to dismiss:', e)
  }
  submitting.value = null
}

function formatTime(ts) {
  if (!ts) return ''
  const d = new Date(ts)
  const now = new Date()
  const diffMs = now - d
  const diffMin = Math.floor(diffMs / 60000)
  if (diffMin < 1) return 'just now'
  if (diffMin < 60) return `${diffMin}m ago`
  const diffHr = Math.floor(diffMin / 60)
  if (diffHr < 24) return `${diffHr}h ago`
  return d.toLocaleDateString()
}

onMounted(() => {
  loadSessions()
  loadAsks()
})

watch([filterSession, filterStatus], () => loadAsks())

// Poll for new asks every 15 seconds
let pollTimer = null
onMounted(() => {
  pollTimer = setInterval(loadAsks, 15000)
})
import { onUnmounted } from 'vue'
onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })
</script>

<style scoped>
.asks-view {
  max-width: 800px;
  margin: 0 auto;
  padding: 1.5rem;
}

.asks-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 1.5rem;
}

.asks-header h2 { margin: 0; }

.asks-filters {
  display: flex;
  gap: 0.5rem;
}

.asks-filters select {
  padding: 0.3rem 0.5rem;
  border-radius: 6px;
  border: 1px solid rgba(255,255,255,0.15);
  background: var(--bg-card, #1e1e2e);
  color: inherit;
  font-size: 0.85rem;
}

.loading, .empty-state {
  text-align: center;
  padding: 3rem;
  color: #999;
}

.empty-icon { font-size: 3rem; margin-bottom: 0.5rem; }
.empty-hint { font-size: 0.85rem; color: #666; margin-top: 0.5rem; }
.empty-hint code {
  background: rgba(255,255,255,0.08);
  padding: 0.1rem 0.3rem;
  border-radius: 3px;
  font-size: 0.8rem;
}

.asks-list {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.ask-card {
  background: var(--bg-card, #1e1e2e);
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 8px;
  padding: 1rem;
}

.ask-card.high {
  border-left: 3px solid #ef4444;
}

.ask-card.low {
  border-left: 3px solid #64748b;
}

.ask-card.pending {
  border-left: 3px solid #f59e0b;
}

.ask-card.answered {
  opacity: 0.7;
}

.ask-card.dismissed {
  opacity: 0.5;
}

.ask-header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.8rem;
  color: #999;
  margin-bottom: 0.5rem;
}

.ask-session {
  font-weight: 600;
  color: #94a3b8;
}

.ask-priority {
  padding: 0.1rem 0.4rem;
  border-radius: 4px;
  font-size: 0.7rem;
  text-transform: uppercase;
  font-weight: 600;
}

.ask-card.high .ask-priority {
  background: rgba(239, 68, 68, 0.15);
  color: #ef4444;
}

.ask-card.low .ask-priority {
  background: rgba(100, 116, 139, 0.15);
  color: #64748b;
}

.ask-time {
  margin-left: auto;
}

.ask-status-badge {
  padding: 0.1rem 0.4rem;
  border-radius: 4px;
  font-size: 0.7rem;
  text-transform: uppercase;
}

.ask-status-badge.answered {
  background: rgba(74, 222, 128, 0.15);
  color: #4ade80;
}

.ask-status-badge.dismissed {
  background: rgba(100, 116, 139, 0.15);
  color: #64748b;
}

.ask-question {
  font-size: 1rem;
  margin-bottom: 0.5rem;
  line-height: 1.5;
}

.ask-context {
  margin: 0.5rem 0;
}

.ask-context details {
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 6px;
}

.ask-context summary {
  padding: 0.3rem 0.6rem;
  font-size: 0.8rem;
  color: #94a3b8;
  cursor: pointer;
}

.context-body {
  padding: 0.5rem 0.6rem;
  font-size: 0.85rem;
  color: #cbd5e1;
  white-space: pre-wrap;
  border-top: 1px solid rgba(255,255,255,0.05);
}

.ask-tags {
  display: flex;
  gap: 0.3rem;
  margin: 0.4rem 0;
}

.tag {
  padding: 0.1rem 0.4rem;
  border-radius: 4px;
  font-size: 0.7rem;
  background: rgba(255,255,255,0.06);
  color: #94a3b8;
}

.ask-choices {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  margin: 0.5rem 0;
}

.choice-btn {
  padding: 0.4rem 1rem;
  border: 1px solid #4ade80;
  border-radius: 6px;
  background: rgba(74, 222, 128, 0.1);
  color: #4ade80;
  cursor: pointer;
  font-size: 0.85rem;
  transition: all 0.15s;
}

.choice-btn:hover:not(:disabled) {
  background: rgba(74, 222, 128, 0.2);
}

.choice-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.ask-freeform {
  display: flex;
  gap: 0.5rem;
  margin-top: 0.5rem;
}

.ask-freeform input {
  flex: 1;
  padding: 0.4rem 0.6rem;
  border: 1px solid rgba(255,255,255,0.15);
  border-radius: 6px;
  background: rgba(255,255,255,0.05);
  color: inherit;
  font-size: 0.85rem;
}

.ask-freeform .primary {
  padding: 0.4rem 0.8rem;
  border: none;
  border-radius: 6px;
  background: #4ade80;
  color: #000;
  font-weight: 600;
  cursor: pointer;
  font-size: 0.85rem;
}

.ask-freeform .primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.dismiss-btn {
  padding: 0.4rem 0.6rem;
  border: 1px solid rgba(255,255,255,0.15);
  border-radius: 6px;
  background: transparent;
  color: #94a3b8;
  cursor: pointer;
  font-size: 0.8rem;
}

.dismiss-btn:hover:not(:disabled) {
  background: rgba(255,255,255,0.05);
  color: #e2e8f0;
}

.ask-answered {
  margin-top: 0.5rem;
  font-size: 0.9rem;
  color: #4ade80;
}

.answer-label {
  font-weight: 600;
  color: #94a3b8;
}
</style>
