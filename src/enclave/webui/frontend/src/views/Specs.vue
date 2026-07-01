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
            <span v-if="ch.review && ch.review.state && ch.review.state !== 'none'" class="review-badge" :class="ch.review.state">{{ badgeLabel(ch.review.state) }}</span>
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
            <span class="state-badge" :class="stateClass">{{ stateLabel }}</span>
            <button class="btn-sm" @click="pin(selected)" :class="{ pinned: isPinned(selected.id) }">
              {{ isPinned(selected.id) ? '📌 Pinned' : '📌 Pin' }}
            </button>
            <button class="btn-sm approve" @click="submitReview('approved')">✅ Approve</button>
          </div>
        </div>

        <!-- Progress -->
        <div class="detail-progress">
          <div class="bar"><div class="fill" :style="{ width: selected.taskProgress.percent + '%' }"></div></div>
          <span class="pct">{{ selected.taskProgress.percent }}% · {{ selected.taskProgress.done }}/{{ selected.taskProgress.total }} tasks</span>
        </div>

        <p class="comment-hint muted">💬 Tap any paragraph or bullet to comment on it. Comments batch into one review.</p>

        <!-- Sections (inline-commentable via event delegation on this container) -->
        <div class="sections" ref="sectionsEl" @click="onSectionClick">
          <details v-for="sec in sectionList" :key="sec.key" class="section" :open="sec.open">
            <summary>{{ sec.label }}</summary>
            <div class="md" :data-section="sec.key" :data-path="sec.path" v-html="renderMarkdown(sec.source)"></div>
          </details>
        </div>

        <!-- Inline comment editor (bottom sheet — phone-friendly, no offset math) -->
        <div v-if="activeComment" class="comment-editor">
          <div class="ce-quote">{{ activeComment.block_text }}</div>
          <textarea v-model="activeComment.comment" placeholder="Your comment on this block…" rows="2" ref="ceInput"></textarea>
          <div class="ce-actions">
            <button class="btn-sm" @click="cancelComment">Cancel</button>
            <button class="btn-sm changes" :disabled="!activeComment.comment.trim()" @click="addComment">Add comment</button>
          </div>
        </div>

        <!-- Sticky submit bar -->
        <div v-if="draft.length" class="submit-bar">
          <span class="draft-count">{{ draft.length }} comment{{ draft.length > 1 ? 's' : '' }}</span>
          <input v-model="overallNote" class="overall-note" placeholder="Overall note (optional)" />
          <button class="btn-sm ghost" @click="clearDraft">Discard</button>
          <button class="btn-sm changes" @click="submitReview('changes_requested')">Submit review ({{ draft.length }})</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch, onMounted, computed, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useSessionStore } from '../stores/session.js'
import { api } from '../api.js'
import MarkdownIt from 'markdown-it'
import { dataLinePlugin, sliceBlock } from '../lib/dataLine.js'

const { selectedSessionId } = useSessionStore()
const selectedSession = selectedSessionId
const router = useRouter()

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
md.use(dataLinePlugin)
function renderMarkdown(text) { return text ? md.render(text) : '<p class="muted">— empty —</p>' }

const changes = ref([])
const loading = ref(false)
const selected = ref(null)
const detail = ref({ proposal: '', design: '', tasks: '', specs: {} })
const derivedState = ref('none')
const sectionsEl = ref(null)
const ceInput = ref(null)

// Draft inline comments (per session+change), plus overall note.
const draft = ref([])
const overallNote = ref('')
const activeComment = ref(null)

// The rendered sections, each carrying its source + path so a clicked block can
// be mapped back to its section source for the frozen quote.
const sectionList = computed(() => {
  const d = detail.value
  const list = [
    { key: 'proposal', label: 'Proposal', path: selected.value?.proposalPath || 'proposal.md', source: d.proposal, open: true },
    { key: 'design', label: 'Design', path: selected.value?.designPath || 'design.md', source: d.design, open: false },
    { key: 'tasks', label: 'Tasks', path: selected.value?.tasksPath || 'tasks.md', source: d.tasks, open: true },
  ]
  for (const [path, content] of Object.entries(d.specs || {})) {
    list.push({ key: `spec:${path}`, label: `Spec — ${path}`, path, source: content, open: false })
  }
  return list.filter(s => s.source != null)
})

const STATE_META = {
  none: { label: '', cls: '' },
  commented: { label: '💬 Commented', cls: 'commented' },
  changes_requested: { label: '✏️ Changes requested', cls: 'changes_requested' },
  revised_pending_review: { label: '🔵 Review changes', cls: 'revised' },
  approved: { label: '✅ Approved', cls: 'approved' },
}
const stateLabel = computed(() => STATE_META[derivedState.value]?.label || '')
const stateClass = computed(() => STATE_META[derivedState.value]?.cls || '')
function badgeLabel(state) { return STATE_META[state]?.label || '' }

async function load() {
  if (!selectedSession.value) { changes.value = []; return }
  loading.value = true
  try {
    const data = await api.getOpenSpecChanges(selectedSession.value)
    changes.value = data.changes || []
    if (changes.value.length && !selected.value) await select(changes.value[0].id)
  } catch { changes.value = [] } finally { loading.value = false }
}

function draftKey(id) { return `enclave:${selectedSession.value}:specdraft:${id}` }
function loadDraft(id) {
  try {
    const saved = JSON.parse(localStorage.getItem(draftKey(id)) || 'null')
    draft.value = saved?.comments || []
    overallNote.value = saved?.overallNote || ''
  } catch { draft.value = []; overallNote.value = '' }
}
function persistDraft() {
  if (!selected.value) return
  localStorage.setItem(draftKey(selected.value.id), JSON.stringify({
    comments: draft.value, overallNote: overallNote.value,
  }))
}

async function select(id) {
  activeComment.value = null
  try {
    detail.value = await api.getOpenSpecChange(selectedSession.value, id)
    selected.value = changes.value.find(c => c.id === id) || { id }
    loadDraft(id)
    await refreshState(id)
    nextTick(applyBadges)
  } catch { /* ignore */ }
}

async function refreshState(id) {
  try {
    const s = await api.getOpenSpecState(selectedSession.value, id)
    derivedState.value = s.state || 'none'
  } catch { derivedState.value = 'none' }
}

// ─── Inline commenting via event delegation on the sections container ───
function onSectionClick(e) {
  const block = e.target.closest('[data-line]')
  if (!block || !sectionsEl.value.contains(block)) return
  const container = block.closest('[data-section]')
  if (!container) return
  const section = container.getAttribute('data-section')
  const path = container.getAttribute('data-path')
  const startLine = parseInt(block.getAttribute('data-line'), 10)
  const sec = sectionList.value.find(s => s.key === section)
  const blockText = sliceBlock(sec?.source || '', startLine)
  activeComment.value = {
    id: 'c_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
    section, path, start_line: startLine, end_line: startLine,
    block_text: blockText, comment: '',
  }
  nextTick(() => ceInput.value?.focus())
}
function cancelComment() { activeComment.value = null }
function addComment() {
  if (!activeComment.value?.comment.trim()) return
  draft.value.push({ ...activeComment.value, comment: activeComment.value.comment.trim() })
  activeComment.value = null
  persistDraft()
  nextTick(applyBadges)
}
function clearDraft() {
  draft.value = []
  overallNote.value = ''
  if (selected.value) localStorage.removeItem(draftKey(selected.value.id))
  nextTick(applyBadges)
}

// Post-render decoration: mark commented blocks. Re-applied after every render
// (v-html re-render wipes injected classes), keyed off data-line + data-section.
function applyBadges() {
  const root = sectionsEl.value
  if (!root) return
  root.querySelectorAll('.has-draft-comment').forEach(el => el.classList.remove('has-draft-comment'))
  for (const c of draft.value) {
    const container = root.querySelector(`[data-section="${cssEsc(c.section)}"]`)
    if (!container) continue
    const block = container.querySelector(`[data-line="${c.start_line}"]`)
    if (block) block.classList.add('has-draft-comment')
  }
}
function cssEsc(s) { return (s || '').replace(/["\\]/g, '\\$&') }

async function submitReview(state) {
  if (!selected.value) return
  const name = selected.value.id
  const comments = state === 'changes_requested' ? draft.value : []
  const note = overallNote.value.trim()
  let reviewId = ''
  try {
    const res = await api.postOpenSpecReview(selectedSession.value, name, state, note, comments)
    reviewId = res.review_id || ''
    derivedState.value = res.state || derivedState.value
  } catch { /* ignore */ }
  // Build the tagged message the agent will act on.
  let msg
  if (state === 'approved') {
    msg = `[OpenSpec review on change '${name}'] APPROVE` + (note ? `\n\nNote: ${note}` : '')
  } else {
    const lines = [`[OpenSpec review on change '${name}'] CHANGES REQUESTED  (review_id: ${reviewId})`]
    if (note) lines.push('', `Overall: ${note}`)
    lines.push('', `Inline comments (${comments.length}):`, '')
    comments.forEach((c, i) => {
      lines.push(`${i + 1}. [${c.path}:${c.start_line}]`)
      c.block_text.split('\n').forEach(q => lines.push(`   > ${q}`))
      lines.push(`   Comment: ${c.comment}`, '')
    })
    lines.push(
      "NOTE: locate each block by its QUOTED TEXT (line numbers may have shifted).",
      "After applying, call openspec_revision_log(change, summary, why, " +
      `in_response_to='${reviewId}', related_comment_ids=[...]).`,
    )
    msg = lines.join('\n')
  }
  // Clear the draft — it's now submitted.
  clearDraft()
  localStorage.setItem(`enclave:${selectedSession.value}:pendingFeedback`, msg)
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
watch(selectedSession, () => { selected.value = null; draft.value = []; load() })
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

/* Review cycle: state badge, inline comments, submit bar */
.state-badge { font-size: 0.72rem; padding: 2px 8px; border-radius: 10px; white-space: nowrap; }
.state-badge.commented { background: #1e3a5f; color: #7c9eff; }
.state-badge.changes_requested { background: #5a2a14; color: #fb923c; }
.state-badge.revised { background: #1e3a5f; color: #7c9eff; }
.state-badge.approved { background: #14532d; color: #4ade80; }
.comment-hint { font-size: 0.75rem; margin: 0.2rem 0 0.5rem; }
.sections { position: relative; }
/* Commentable blocks: subtle affordance on hover/tap */
.md :deep([data-line]) { position: relative; border-radius: 4px; transition: background 0.12s; cursor: pointer; }
.md :deep([data-line]:hover) { background: rgba(124,158,255,0.08); box-shadow: -6px 0 0 rgba(124,158,255,0.25); }
.md :deep([data-line].has-draft-comment) { background: rgba(251,146,60,0.10); box-shadow: -6px 0 0 #fb923c; }
.comment-editor {
  position: fixed; left: 50%; transform: translateX(-50%); bottom: 12px; z-index: 60;
  width: min(680px, 94vw);
  background: var(--bg-card, #1a1a22); border: 1px solid var(--accent, #7c9eff);
  border-radius: 10px; padding: 0.7rem; box-shadow: 0 8px 30px rgba(0,0,0,.5);
}
.ce-quote { font-size: 0.75rem; color: var(--text-muted, #999); border-left: 3px solid var(--border, #444);
  padding-left: 0.5rem; margin-bottom: 0.4rem; white-space: pre-wrap; max-height: 4.5em; overflow-y: auto; }
.comment-editor textarea, .overall-note {
  width: 100%; background: var(--bg-main, #15151a); border: 1px solid var(--border, #333);
  border-radius: 6px; color: inherit; padding: 0.45rem; font-family: inherit; resize: vertical; box-sizing: border-box;
}
.ce-actions { display: flex; justify-content: flex-end; gap: 0.4rem; margin-top: 0.4rem; }
.submit-bar {
  position: sticky; bottom: 0; display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;
  background: var(--bg-card, #1a1a22); border: 1px solid var(--accent, #7c9eff);
  border-radius: 10px; padding: 0.5rem 0.7rem; margin-top: 0.8rem;
}
.submit-bar .draft-count { font-size: 0.78rem; color: var(--accent, #7c9eff); font-weight: 600; white-space: nowrap; }
.submit-bar .overall-note { flex: 1; min-width: 140px; width: auto; }
.btn-sm.ghost { opacity: 0.7; }
</style>
