<template>
  <div class="specs-view">
    <div class="specs-header">
      <h2>📋 Specs</h2>
      <span class="muted hint">OpenSpec change proposals — read, track, and review</span>
    </div>

    <div v-if="!selectedSession" class="empty-state">
      <p class="muted">Select a session to view specs.</p>
    </div>

    <div v-else-if="!loading && !changes.length" class="empty-state">
      <p class="muted">No OpenSpec changes found. Agents scaffold them with the <code>openspec</code> CLI under <code>openspec/changes/</code>.</p>
    </div>

    <div v-else class="specs-grid">
      <!-- Change list -->
      <div class="card change-list">
        <div
          v-for="ch in changes"
          :key="ch.id"
          class="change-item"
          :class="{ active: selected && selected.id === ch.id }"
          @click="select(ch.id)"
        >
          <div class="change-top">
            <span class="change-name">{{ ch.id }}</span>
            <span v-if="ch.review" class="review-badge" :class="ch.review.state">{{ reviewLabel(ch.review.state) }}</span>
          </div>
          <div class="mini-progress">
            <div class="mini-bar"><div class="mini-fill" :style="{ width: ch.taskProgress.percent + '%' }"></div></div>
            <span class="mini-pct">{{ ch.taskProgress.done }}/{{ ch.taskProgress.total }}</span>
          </div>
        </div>
      </div>

      <!-- Detail -->
      <div class="card change-detail" v-if="selected">
        <div class="detail-bar">
          <h3>{{ selected.id }}</h3>
          <div class="detail-actions">
            <button class="btn-sm" @click="pin(selected)" :class="{ pinned: isPinned(selected.id) }">
              {{ isPinned(selected.id) ? '📌 Pinned' : '📌 Pin' }}
            </button>
            <button class="btn-sm approve" @click="review('approved')">✅ Approve</button>
            <button class="btn-sm changes" @click="openRequest = !openRequest">✏️ Request changes</button>
          </div>
        </div>

        <!-- Progress -->
        <div class="detail-progress">
          <div class="bar"><div class="fill" :style="{ width: selected.taskProgress.percent + '%' }"></div></div>
          <span class="pct">{{ selected.taskProgress.percent }}% · {{ selected.taskProgress.done }}/{{ selected.taskProgress.total }} tasks</span>
        </div>
        <div v-if="selected.review" class="review-line" :class="selected.review.state">
          {{ reviewLabel(selected.review.state) }}
          <span v-if="selected.review.note" class="review-note">— {{ selected.review.note }}</span>
        </div>

        <!-- Request-changes composer -->
        <div v-if="openRequest" class="request-box">
          <textarea v-model="requestNote" placeholder="What needs to change?" rows="3"></textarea>
          <div class="request-actions">
            <button class="btn-sm" @click="openRequest = false">Cancel</button>
            <button class="btn-sm changes" :disabled="!requestNote.trim()" @click="review('changes_requested')">Send to agent</button>
          </div>
        </div>

        <!-- Sections -->
        <details class="section" open>
          <summary>Proposal</summary>
          <div class="md" v-html="renderMarkdown(detail.proposal)"></div>
        </details>
        <details class="section">
          <summary>Design</summary>
          <div class="md" v-html="renderMarkdown(detail.design)"></div>
        </details>
        <details class="section" open>
          <summary>Tasks</summary>
          <div class="md" v-html="renderMarkdown(detail.tasks)"></div>
        </details>
        <details class="section" v-for="(content, path) in detail.specs" :key="path">
          <summary>Spec — {{ path }}</summary>
          <div class="md" v-html="renderMarkdown(content)"></div>
        </details>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useSessionStore } from '../stores/session.js'
import { api } from '../api.js'
import MarkdownIt from 'markdown-it'

const { selectedSessionId } = useSessionStore()
const selectedSession = selectedSessionId
const router = useRouter()

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
function renderMarkdown(text) { return text ? md.render(text) : '<p class="muted">— empty —</p>' }

const changes = ref([])
const loading = ref(false)
const selected = ref(null)
const detail = ref({ proposal: '', design: '', tasks: '', specs: {} })
const openRequest = ref(false)
const requestNote = ref('')

function reviewLabel(state) {
  return state === 'approved' ? '✅ Approved'
    : state === 'changes_requested' ? '✏️ Changes requested'
    : '💬 Commented'
}

async function load() {
  if (!selectedSession.value) { changes.value = []; return }
  loading.value = true
  try {
    const data = await api.getOpenSpecChanges(selectedSession.value)
    changes.value = data.changes || []
    if (changes.value.length && !selected.value) await select(changes.value[0].id)
  } catch { changes.value = [] } finally { loading.value = false }
}

async function select(id) {
  openRequest.value = false
  requestNote.value = ''
  try {
    detail.value = await api.getOpenSpecChange(selectedSession.value, id)
    selected.value = changes.value.find(c => c.id === id) || { id }
  } catch { /* ignore */ }
}

async function review(state) {
  if (!selected.value) return
  const name = selected.value.id
  const note = state === 'changes_requested' ? requestNote.value.trim() : ''
  try {
    const rec = await api.postOpenSpecReview(selectedSession.value, name, state, note)
    selected.value.review = rec
    const ch = changes.value.find(c => c.id === name)
    if (ch) ch.review = rec
  } catch { /* ignore */ }
  // Notify the agent via a tagged chat message (plain language, not a slash command).
  const msg = state === 'approved'
    ? `[OpenSpec feedback on change '${name}'] APPROVE`
    : `[OpenSpec feedback on change '${name}'] REQUEST CHANGES: ${note}`
  localStorage.setItem(`enclave:${selectedSession.value}:pendingFeedback`, msg)
  openRequest.value = false
  requestNote.value = ''
  router.push('/chat')
}

function pinKey() { return `enclave:${selectedSession.value}:pinnedSpec` }
function isPinned(id) {
  try { return JSON.parse(localStorage.getItem(pinKey()) || 'null')?.changeId === id } catch { return false }
}
function pin(ch) {
  if (isPinned(ch.id)) { localStorage.removeItem(pinKey()); selected.value = { ...selected.value }; return }
  const path = ch.proposalPath || ch.tasksPath || ch.designPath
  localStorage.setItem(pinKey(), JSON.stringify({ type: 'openspec', changeId: ch.id, path, title: ch.id }))
  selected.value = { ...selected.value }
}

onMounted(load)
watch(selectedSession, () => { selected.value = null; load() })
</script>

<style scoped>
.specs-view { padding: 1rem; }
.specs-header { display: flex; align-items: baseline; gap: 0.75rem; margin-bottom: 1rem; }
.specs-header h2 { margin: 0; }
.hint { font-size: 0.8rem; }
.empty-state { padding: 2rem; }
.muted { color: var(--text-muted, #999); }
.specs-grid { display: grid; grid-template-columns: 260px 1fr; gap: 1rem; }
.card { background: var(--bg-card, #1a1a22); border: 1px solid var(--border, #333); border-radius: 10px; padding: 0.75rem; }
.change-item { padding: 0.55rem 0.6rem; border-radius: 8px; cursor: pointer; border: 1px solid transparent; }
.change-item:hover { background: var(--bg-main, #15151a); }
.change-item.active { border-color: var(--accent, #7c9eff); background: var(--bg-main, #15151a); }
.change-top { display: flex; align-items: center; justify-content: space-between; gap: 0.4rem; }
.change-name { font-family: monospace; font-size: 0.85rem; }
.review-badge { font-size: 0.62rem; padding: 1px 5px; border-radius: 10px; white-space: nowrap; }
.review-badge.approved { background: #14532d; color: #4ade80; }
.review-badge.changes_requested { background: #5a2a14; color: #fb923c; }
.review-badge.commented { background: #1e3a5f; color: #7c9eff; }
.mini-progress { display: flex; align-items: center; gap: 0.4rem; margin-top: 0.4rem; }
.mini-bar { flex: 1; height: 5px; background: #12121a; border-radius: 3px; overflow: hidden; }
.mini-fill { height: 100%; background: linear-gradient(90deg, var(--accent, #7c9eff), #4ade80); }
.mini-pct { font-size: 0.68rem; color: var(--text-muted, #999); }
.detail-bar { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 0.6rem; }
.detail-bar h3 { margin: 0; font-family: monospace; }
.detail-actions { display: flex; gap: 0.4rem; flex-wrap: wrap; }
.btn-sm { background: var(--bg-main, #15151a); border: 1px solid var(--border, #333); color: inherit; padding: 0.3rem 0.7rem; border-radius: 6px; cursor: pointer; font-size: 0.8rem; }
.btn-sm:hover { border-color: var(--accent, #7c9eff); }
.btn-sm.pinned { border-color: var(--accent, #7c9eff); color: var(--accent, #7c9eff); }
.btn-sm.approve:hover { border-color: #4ade80; color: #4ade80; }
.btn-sm.changes:hover { border-color: #fb923c; color: #fb923c; }
.detail-progress { display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.5rem; }
.detail-progress .bar { flex: 1; height: 8px; background: #12121a; border-radius: 4px; overflow: hidden; }
.detail-progress .fill { height: 100%; background: linear-gradient(90deg, var(--accent, #7c9eff), #4ade80); transition: width 0.5s; }
.detail-progress .pct { font-size: 0.78rem; color: var(--text-muted, #999); white-space: nowrap; }
.review-line { font-size: 0.78rem; padding: 0.3rem 0.5rem; border-radius: 6px; margin-bottom: 0.6rem; }
.review-line.approved { background: #14532d33; color: #4ade80; }
.review-line.changes_requested { background: #5a2a1433; color: #fb923c; }
.review-note { color: var(--text-muted, #aaa); }
.request-box { margin-bottom: 0.7rem; }
.request-box textarea { width: 100%; background: var(--bg-main, #15151a); border: 1px solid var(--border, #333); border-radius: 6px; color: inherit; padding: 0.5rem; font-family: inherit; resize: vertical; }
.request-actions { display: flex; justify-content: flex-end; gap: 0.4rem; margin-top: 0.4rem; }
.section { border: 1px solid var(--border, #2a2a36); border-radius: 8px; margin-bottom: 0.5rem; padding: 0 0.7rem; }
.section > summary { cursor: pointer; padding: 0.5rem 0; font-weight: 600; font-size: 0.9rem; }
.md { font-size: 0.88rem; line-height: 1.6; padding-bottom: 0.6rem; }
.md :deep(h1) { font-size: 1.2rem; } .md :deep(h2) { font-size: 1.05rem; } .md :deep(h3) { font-size: 0.95rem; }
.md :deep(pre) { background: var(--bg-main, #15151a); border: 1px solid var(--border, #333); border-radius: 6px; padding: 0.6rem; overflow-x: auto; }
.md :deep(code) { font-family: monospace; font-size: 0.85em; }
.md :deep(table) { border-collapse: collapse; }
.md :deep(th), .md :deep(td) { border: 1px solid var(--border, #333); padding: 0.3rem 0.6rem; }
@media (max-width: 760px) { .specs-grid { grid-template-columns: 1fr; } }
</style>
