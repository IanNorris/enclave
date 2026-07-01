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
            <button v-if="hasChanges" class="btn-sm" :class="{ pinned: showChanges }" @click="toggleChanges">
              {{ showChanges ? '🟩 Changes on' : '🟩 Show changes' }}
            </button>
            <button class="btn-sm" @click="pin(selected)" :class="{ pinned: isPinned(selected.id) }">
              {{ isPinned(selected.id) ? '📌 Pinned' : '📌 Pin' }}
            </button>
            <span v-if="awaitingAgent" class="await-note">⏳ Awaiting agent</span>
            <button v-if="canApprove" class="btn-sm approve" @click="submitReview('approved')">✅ Approve</button>
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

        <!-- History / activity timeline: the "why" trail -->
        <details v-if="timeline.length" class="section history-section">
          <summary>📜 Review history ({{ timeline.length }})</summary>
          <div class="timeline">
            <div v-for="(ev, i) in timeline" :key="i" class="tl-entry" :class="ev.kind">
              <div class="tl-head">
                <span class="tl-icon">{{ ev.kind === 'review' ? '👤' : '🤖' }}</span>
                <span class="tl-title">
                  <template v-if="ev.kind === 'review'">Review — {{ badgeLabel(ev.state) || ev.state }}</template>
                  <template v-else>Agent revision</template>
                </span>
                <span class="tl-time">{{ fmtTime(ev.at) }}</span>
              </div>
              <template v-if="ev.kind === 'review'">
                <div v-if="ev.note" class="tl-note">{{ ev.note }}</div>
                <div v-for="c in ev.comments" :key="c.id" class="tl-comment" :class="c.status">
                  <div class="tl-c-quote" v-if="c.block_text">{{ c.block_text }}</div>
                  <div class="tl-c-body">
                    <span class="tl-c-status" :class="c.status">{{ c.status === 'addressed' ? '✓' : '•' }}</span>
                    {{ c.comment }}
                  </div>
                  <div v-if="c.resolution" class="tl-c-resolution">
                    ↳ <strong>Agent:</strong> {{ c.resolution.resolution_note || c.resolution.actionable_intent || 'addressed' }}
                  </div>
                </div>
              </template>
              <template v-else>
                <div v-if="ev.summary" class="tl-note">{{ ev.summary }}</div>
                <div v-if="ev.why" class="tl-why"><strong>Why:</strong> {{ ev.why }}</div>
                <div v-if="ev.files.length" class="tl-files">Changed: {{ ev.files.join(', ') }}</div>
              </template>
            </div>
          </div>
        </details>

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
const events = ref([])
const commentStatuses = ref({})
const sectionsEl = ref(null)
const ceInput = ref(null)

// Edit highlighting: {path: [changedLine,...]} since the user's last review.
const changedLines = ref({})
const showChanges = ref(true)
const hasChanges = computed(() => Object.keys(changedLines.value).length > 0)

// Draft inline comments (per session+change), plus overall note.
const draft = ref([])
const overallNote = ref('')
const activeComment = ref(null)

// The rendered sections, each carrying its source + path so a clicked block can
// be mapped back to its section source for the frozen quote.
const sectionList = computed(() => {
  const d = detail.value
  const hasChg = (p) => showChanges.value && (changedLines.value[p]?.length > 0)
  const list = [
    { key: 'proposal', label: 'Proposal', path: selected.value?.proposalPath || 'proposal.md', source: d.proposal, open: true },
    { key: 'design', label: 'Design', path: selected.value?.designPath || 'design.md', source: d.design, open: false },
    { key: 'tasks', label: 'Tasks', path: selected.value?.tasksPath || 'tasks.md', source: d.tasks, open: true },
  ]
  for (const [path, content] of Object.entries(d.specs || {})) {
    list.push({ key: `spec:${path}`, label: `Spec — ${path}`, path, source: content, open: false })
  }
  // Auto-open any section that has changes to show, so highlights aren't hidden.
  return list.filter(s => s.source != null).map(s => ({ ...s, open: s.open || hasChg(s.path) }))
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

// State-driven CTA: what actions make sense right now.
const canApprove = computed(() => ['none', 'commented', 'revised_pending_review'].includes(derivedState.value))
const awaitingAgent = computed(() => derivedState.value === 'changes_requested')

// Resolution lookup: comment id -> {resolution_note, actionable_intent} from the
// latest agent_revision that resolved it.
const resolutionByComment = computed(() => {
  const map = {}
  for (const e of events.value) {
    if (e.type !== 'agent_revision') continue
    for (const r of (e.resolutions || [])) {
      if (r.comment_id) map[r.comment_id] = { ...r, at: e.at }
    }
  }
  return map
})

// History timeline: review + agent_revision events, newest first, with the
// comments (and their resolutions) attached to each review.
const timeline = computed(() => {
  return [...events.value].reverse().map(e => {
    if (e.type === 'review') {
      return {
        kind: 'review', at: e.at, state: e.state, note: e.overall_note,
        comments: (e.comments || []).map(c => ({
          ...c,
          status: commentStatuses.value[c.id] || 'open',
          resolution: resolutionByComment.value[c.id] || null,
        })),
      }
    }
    return {
      kind: 'revision', at: e.at, summary: e.summary, why: e.why,
      files: e.files_changed || [],
    }
  })
})
function fmtTime(iso) { try { return new Date(iso).toLocaleString() } catch { return iso } }

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
  changedLines.value = {}
  try {
    detail.value = await api.getOpenSpecChange(selectedSession.value, id)
    selected.value = changes.value.find(c => c.id === id) || { id }
    loadDraft(id)
    await refreshState(id)
    await refreshDiff(id)
    nextTick(decorate)
  } catch { /* ignore */ }
}

async function refreshState(id) {
  try {
    const s = await api.getOpenSpecState(selectedSession.value, id)
    derivedState.value = s.state || 'none'
    events.value = s.events || []
    commentStatuses.value = s.comment_statuses || {}
  } catch { derivedState.value = 'none'; events.value = []; commentStatuses.value = {} }
}

async function refreshDiff(id) {
  try {
    const d = await api.getOpenSpecDiff(selectedSession.value, id)
    changedLines.value = d.changed || {}
    // Default the highlight on when there are changes and the change is pending
    // re-approval (that's when "what changed since I reviewed" matters most).
    showChanges.value = hasChanges.value
  } catch { changedLines.value = {} }
}

function toggleChanges() {
  showChanges.value = !showChanges.value
  nextTick(decorate)
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
  nextTick(decorate)
}
function clearDraft() {
  draft.value = []
  overallNote.value = ''
  if (selected.value) localStorage.removeItem(draftKey(selected.value.id))
  nextTick(decorate)
}

// Post-render decoration: mark draft-commented blocks AND agent-changed blocks.
// Re-applied after every render (v-html re-render wipes injected classes), keyed
// off data-line + data-section. Changed-line numbers are relative to the current
// (rendered) version, so they map straight onto data-line.
function decorate() {
  const root = sectionsEl.value
  if (!root) return
  root.querySelectorAll('.has-draft-comment, .changed-block')
    .forEach(el => el.classList.remove('has-draft-comment', 'changed-block'))
  // Draft comment badges
  for (const c of draft.value) {
    const container = root.querySelector(`[data-section="${cssEsc(c.section)}"]`)
    const block = container?.querySelector(`[data-line="${c.start_line}"]`)
    if (block) block.classList.add('has-draft-comment')
  }
  // Agent edit highlights (by file path → changed lines)
  if (showChanges.value) {
    for (const sec of sectionList.value) {
      const lines = changedLines.value[sec.path]
      if (!lines || !lines.length) continue
      const container = root.querySelector(`[data-section="${cssEsc(sec.key)}"]`)
      if (!container) continue
      const blocks = Array.from(container.querySelectorAll('[data-line]'))
        .map(el => [parseInt(el.getAttribute('data-line'), 10), el])
        .sort((a, b) => a[0] - b[0])
      const lineSet = new Set(lines)
      // A block "owns" source lines from its data-line up to the next block's.
      for (let i = 0; i < blocks.length; i++) {
        const start = blocks[i][0]
        const end = i + 1 < blocks.length ? blocks[i + 1][0] : Infinity
        for (const ln of lineSet) {
          if (ln >= start && ln < end) { blocks[i][1].classList.add('changed-block'); break }
        }
      }
    }
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
// Re-run decoration whenever the change set or toggle changes (DOM re-renders
// via sectionList auto-open, so defer to nextTick).
watch([changedLines, showChanges], () => nextTick(decorate))
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
.md :deep([data-line].changed-block) { background: rgba(74,222,128,0.12); box-shadow: -6px 0 0 #4ade80; }
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
.await-note { font-size: 0.75rem; color: var(--text-muted, #999); }

/* Review history timeline */
.history-section .timeline { padding: 0.3rem 0 0.6rem; }
.tl-entry { border-left: 2px solid var(--border, #333); padding: 0.3rem 0 0.5rem 0.7rem; margin-left: 0.3rem; }
.tl-entry.review { border-left-color: #7c9eff; }
.tl-entry.revision { border-left-color: #4ade80; }
.tl-head { display: flex; align-items: baseline; gap: 0.5rem; font-size: 0.82rem; }
.tl-title { font-weight: 600; }
.tl-time { color: var(--text-muted, #999); font-size: 0.72rem; margin-left: auto; }
.tl-note { font-size: 0.82rem; margin: 0.25rem 0; }
.tl-why { font-size: 0.8rem; color: var(--text-muted, #bbb); margin: 0.2rem 0; }
.tl-files { font-size: 0.72rem; color: var(--text-muted, #999); font-family: monospace; }
.tl-comment { margin: 0.35rem 0 0.35rem 0.5rem; padding-left: 0.5rem; border-left: 2px solid var(--border, #333); }
.tl-comment.addressed { border-left-color: #4ade80; }
.tl-c-quote { font-size: 0.7rem; color: var(--text-muted, #888); white-space: pre-wrap; max-height: 3em; overflow-y: auto; margin-bottom: 0.15rem; }
.tl-c-body { font-size: 0.82rem; }
.tl-c-status { font-weight: 700; }
.tl-c-status.addressed { color: #4ade80; }
.tl-c-status.open { color: #fb923c; }
.tl-c-resolution { font-size: 0.78rem; color: #4ade80; margin-top: 0.15rem; padding-left: 0.8rem; }
</style>
