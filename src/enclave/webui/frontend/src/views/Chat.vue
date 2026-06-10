<template>
  <div class="chat-view" @dragover.prevent="onDragOver" @dragleave="onDragLeave" @drop.prevent="onDrop">
    <div v-if="selectedSession" class="chat-body" :class="bodyClass">
      <div class="chat-container" :style="chatPaneStyle">
      <!-- Floating agent status (non-scrolling) -->
      <div v-if="agentState !== 'unknown'" class="agent-status-float" :class="agentStateClass">
        <span class="status-indicator"></span>
        <span class="status-label">{{ agentStateLabel }}</span>
      </div>

      <!-- Artifacts handle on the right edge -->
      <div class="doc-tools">
        <button
          v-if="openDoc && !isMobilePortrait"
          class="doc-orient-btn"
          :title="outerOrientation === 'horizontal' ? 'Stack vertically' : 'Place side by side'"
          @click="toggleOuterOrientation"
        >{{ outerOrientation === 'horizontal' ? '⬍' : '⬌' }}</button>
        <div class="doc-menu-wrap">
          <button class="doc-handle" @click="toggleDocMenu" title="Open a document beside the chat">📄</button>
          <div v-if="docMenuOpen" class="doc-menu">
            <div v-if="!docList.length" class="doc-menu-empty muted">No editable documents</div>
            <button
              v-for="d in docList"
              :key="d.filename"
              class="doc-menu-item"
              :class="{ active: d.filename === openDoc }"
              @click="openDocument(d.filename)"
            >{{ d.title || d.filename }}</button>
          </div>
        </div>
      </div>

      <!-- Drop overlay -->
      <div v-if="dragging" class="drop-overlay">
        <div class="drop-label">Drop files to attach</div>
      </div>

      <!-- Messages -->
      <div class="messages" ref="messagesEl" @scroll="onMessagesScroll" @click="onMessagesClick">
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

          <!-- Persisted event segments for this turn (tool calls interleaved with responses) -->
          <template v-if="turnEvents[turn.turn_index]?.length">
            <!-- File attachments always visible (not collapsed with tool calls) -->
            <template v-for="(seg, si) in turnEvents[turn.turn_index]" :key="'fs-'+si">
              <template v-for="(evt, ei) in seg.tools" :key="'fs-'+si+'-'+ei">
                <div v-if="evt.type === 'file_send'" class="file-send-block">
                  <div class="file-send-label">
                    <span class="event-icon">📎</span>
                    <span>{{ evt.data?.filename || 'file' }}</span>
                  </div>
                  <div v-if="evt.data?.mimetype?.startsWith('image/')" class="file-send-preview">
                    <img v-if="evt.data?.file_path" :src="workspaceFileUrl(evt.data.file_path)" class="file-send-img clickable-img" @click="openLightbox(workspaceFileUrl(evt.data.file_path))" />
                    <img v-else-if="evt.data?.mxc_url" :src="mediaUrl(evt.data.mxc_url)" class="file-send-img clickable-img" @click="openLightbox(mediaUrl(evt.data.mxc_url))" />
                  </div>
                </div>
              </template>
            </template>

            <div class="turn-events" :class="{ collapsed: !expandedTurns[turn.turn_index] }">
              <div class="events-toggle" @click="expandedTurns[turn.turn_index] = !expandedTurns[turn.turn_index]">
                <span class="expand-toggle">{{ expandedTurns[turn.turn_index] ? '▼' : '▶' }}</span>
                <span class="events-summary">{{ countSegmentEvents(turnEvents[turn.turn_index]) }} events</span>
              </div>
              <template v-if="expandedTurns[turn.turn_index]">
                <template v-for="(seg, si) in turnEvents[turn.turn_index]" :key="si">
                  <!-- Tool calls in this segment -->
                  <div v-for="(evt, ei) in seg.tools" :key="`${si}-${ei}`" class="live-event" :class="evt.type">
                    <div v-if="evt.type === 'thinking'" class="thinking-block collapsed">
                      <div class="event-header" @click="evt.expanded = !evt.expanded">
                        <span class="event-icon">🤔</span>
                        <span class="event-label">Thinking</span>
                        <span class="expand-toggle">{{ evt.expanded ? '▼' : '▶' }}</span>
                      </div>
                      <div v-if="evt.expanded" class="event-content thinking-content">{{ evt.data?.content }}</div>
                    </div>
                    <div v-else-if="evt.type === 'tool_start' || evt.type === 'tool_complete'" class="tool-block collapsed">
                      <div class="event-header">
                        <span class="event-icon">{{ TOOL_ICONS_MAP[evt.data?.name] || '🔧' }}</span>
                        <span class="event-label">{{ evt.data?.detail || evt.data?.name || 'tool' }}</span>
                        <span v-if="evt.type === 'tool_complete'" class="tool-status" :class="evt.data?.success !== false ? 'success' : 'fail'">
                          {{ evt.data?.success !== false ? '✅' : '❌' }}
                        </span>
                      </div>
                    </div>
                  </div>
                  <!-- Intermediate response after this batch of tool calls -->
                  <div v-if="seg.response" class="message assistant-message segment-response">
                    <div class="message-meta">
                      <span class="sender">Agent</span>
                      <span v-if="seg.responseTimestamp" class="time">{{ formatTime(seg.responseTimestamp) }}</span>
                    </div>
                    <div class="message-body" v-html="renderMarkdown(seg.response)"></div>
                  </div>
                </template>
              </template>
            </div>
          </template>

          <!-- Structured response card -->
          <div v-if="turn.structured" class="message assistant-message structured-card major-response">
            <div class="structured-title" v-if="turn.structured.title">{{ turn.structured.title }}</div>
            <div class="structured-summary" v-html="renderMarkdown(turn.structured.summary)"></div>
            <div v-if="turn.structured.images?.length" class="structured-images">
              <img v-for="(img, ii) in turn.structured.images" :key="ii"
                   :src="workspaceFileUrl(img)"
                   class="structured-img clickable-img" @click="openLightbox(workspaceFileUrl(img))" />
            </div>
            <details v-if="turn.structured.details" class="structured-details">
              <summary>Details</summary>
              <div class="structured-details-body" v-html="renderMarkdown(turn.structured.details)"></div>
              <div class="structured-details-actions">
                <button class="btn-sm" @click="downloadMarkdown(turn.structured)">📥 Download as Markdown</button>
              </div>
            </details>
            <div v-if="turn.structured.actions?.length" class="structured-actions">
              <div v-for="(action, ai) in turn.structured.actions" :key="ai" class="structured-action">
                <img v-if="action.image"
                     :src="workspaceFileUrl(action.image)"
                     class="action-img clickable-img" @click="openLightbox(workspaceFileUrl(action.image))" />
                <button class="action-btn" @click="sendActionReply(action.label)">{{ action.label }}</button>
              </div>
            </div>
            <div class="message-meta">
              <span class="sender">Agent</span>
              <span class="time">{{ formatTime(turn.timestamp) }}</span>
            </div>
          </div>

          <!-- Regular response (non-structured) -->
          <div v-else-if="turn.assistant_response" class="message assistant-message" :class="{ 'major-response': turn.is_major || (!turn.user_message && turn.assistant_response) }">
            <div class="message-meta">
              <span class="sender">Agent</span>
              <span class="time">{{ formatTime(turn.timestamp) }}</span>
            </div>
            <div class="message-body" v-html="renderMarkdown(turn.assistant_response)"></div>
          </div>
        </div>

        <!-- Live streaming section -->
        <div v-if="liveEvents.length || streamingText" class="turn live-turn">
          <!-- Collapsed tool summary when many events -->
          <div v-if="collapsedLiveCount > 0" class="live-events-collapsed">
            <span class="collapsed-summary" @click="liveEventsExpanded = !liveEventsExpanded">
              {{ liveEventsExpanded ? '▼' : '▶' }} {{ collapsedLiveCount }} earlier tool calls
            </span>
            <template v-if="liveEventsExpanded">
              <div v-for="(evt, i) in collapsedLiveEvents" :key="'c-'+i" class="live-event" :class="evt.type">
                <div v-if="evt.type === 'thinking'" class="thinking-block collapsed">
                  <div class="event-header" @click="evt.collapsed = !evt.collapsed">
                    <span class="event-icon">🤔</span>
                    <span class="event-label">Thinking</span>
                    <span class="expand-toggle">{{ evt.collapsed ? '▶' : '▼' }}</span>
                  </div>
                  <div v-if="!evt.collapsed" class="event-content thinking-content">{{ evt.content }}</div>
                </div>
                <div v-if="evt.type === 'tool'" class="tool-block collapsed">
                  <div class="event-header" @click="evt.collapsed = !evt.collapsed">
                    <span class="event-icon">{{ evt.icon }}</span>
                    <span class="event-label">{{ evt.detail || evt.name }}</span>
                    <span v-if="evt.done" class="tool-status" :class="evt.success ? 'success' : 'fail'">
                      {{ evt.success ? '✅' : '❌' }}
                    </span>
                    <span v-else class="tool-spinner">⏳</span>
                  </div>
                </div>
                <div v-if="evt.type === 'file_send'" class="tool-block">
                  <div class="event-header">
                    <span class="event-icon">📎</span>
                    <span class="event-label">{{ evt.filename }}</span>
                  </div>
                </div>
              </div>
            </template>
          </div>

          <!-- Visible (recent) events -->
          <div v-for="(evt, i) in visibleLiveEvents" :key="'v-'+i" class="live-event" :class="evt.type">
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

            <!-- File sent by agent -->
            <div v-if="evt.type === 'file_send'" class="tool-block">
              <div class="event-header">
                <span class="event-icon">📎</span>
                <span class="event-label">{{ evt.filename }}</span>
              </div>
              <div v-if="evt.mimetype?.startsWith('image/')" class="file-send-preview">
                <img v-if="evt.filePath" :src="workspaceFileUrl(evt.filePath)" class="file-send-img clickable-img" @click="openLightbox(workspaceFileUrl(evt.filePath))" />
                <img v-else-if="evt.mxcUrl" :src="mediaUrl(evt.mxcUrl)" class="file-send-img clickable-img" @click="openLightbox(mediaUrl(evt.mxcUrl))" />
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

      <!-- Jump to latest button -->
      <transition name="fade">
        <button v-if="hasNewContent && !isScrollPinned" class="jump-to-latest" @click="jumpToLatest">
          ↓ Jump to latest
        </button>
      </transition>

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
      <template v-if="openDoc">
        <div
          class="outer-divider"
          :class="outerOrientation"
          @pointerdown="startOuterDrag"
        ><div class="divider-grip"></div></div>
        <DocumentPane
          class="outer-doc"
          :style="docPaneStyle"
          :session="selectedSession"
          :filename="openDoc"
          :mobile="isMobilePortrait"
          :refresh-tick="docRefreshTick"
          @close="closeDoc"
        />
      </template>
    </div>
    <div v-else class="empty-state">
      <p class="muted">Select a session to start chatting.</p>
    </div>

    <!-- Lightbox gallery overlay -->
    <teleport to="body">
      <transition name="lightbox-fade">
        <div v-if="lightboxImage" class="lightbox-overlay" @click.self="lightboxImage = null">
          <button class="lightbox-close" @click="lightboxImage = null" title="Close">✕</button>
          <button v-if="lightboxIndex > 0" class="lightbox-nav lightbox-prev" @click="lightboxPrev" title="Previous">‹</button>
          <img :src="lightboxImageUrl" class="lightbox-img" @click.stop />
          <button v-if="lightboxIndex < galleryImages.length - 1" class="lightbox-nav lightbox-next" @click="lightboxNext" title="Next">›</button>
          <div class="lightbox-counter" v-if="galleryImages.length > 1">
            {{ lightboxIndex + 1 }} / {{ galleryImages.length }}
          </div>
        </div>
      </transition>
    </teleport>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted, nextTick, watch, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api.js'
import { useSessionStore } from '../stores/session.js'
import { useModels } from '../composables/useModels.js'
import DocumentPane from '../components/DocumentPane.vue'
import MarkdownIt from 'markdown-it'

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

// Render ```mermaid fenced code blocks as diagram containers; everything else
// falls through to the default fenced-code renderer. The raw source is HTML
// escaped so textContent recovers it for mermaid to parse.
const defaultFence = md.renderer.rules.fence?.bind(md.renderer.rules) ||
  ((tokens, idx, opts, env, self) => self.renderToken(tokens, idx, opts))
md.renderer.rules.fence = (tokens, idx, options, env, self) => {
  const info = (tokens[idx].info || '').trim().toLowerCase()
  if (info === 'mermaid') {
    return `<div class="mermaid">${md.utils.escapeHtml(tokens[idx].content)}</div>`
  }
  return defaultFence(tokens, idx, options, env, self)
}

// Lazily load mermaid only when a diagram is actually present (it is a large
// dependency, so keep it out of the initial bundle).
let _mermaidPromise = null
function ensureMermaid() {
  if (!_mermaidPromise) {
    _mermaidPromise = import('mermaid').then(({ default: mermaid }) => {
      mermaid.initialize({
        startOnLoad: false,
        theme: 'dark',
        securityLevel: 'strict',
      })
      return mermaid
    })
  }
  return _mermaidPromise
}

const TOOL_ICONS = {
  bash: '🖥️', read_bash: '📖', write_bash: '⌨️', stop_bash: '⏹️',
  view: '📄', edit: '✏️', create: '📝', grep: '🔍', glob: '📁',
  web_fetch: '🌐', web_search: '🔎', task: '🤖', read_agent: '📨',
  sql: '🗃️', ask_user: '❓', list_bash: '📋',
}

const { selectedSessionId } = useSessionStore()
const selectedSession = computed(() => selectedSessionId.value)
const route = useRoute()
const router = useRouter()

// ─── Document workspace (side-by-side artifact editing) ───
const EDITABLE_DOC_RE = /\.(md|txt|json|yaml|yml|csv|log)$/i
const openDoc = ref('')
const docList = ref([])
const docMenuOpen = ref(false)
const outerOrientation = ref(localStorage.getItem('enclave_outer_orientation') || 'horizontal')
const outerSize = ref(parseFloat(localStorage.getItem('enclave_outer_size')) || 58) // % for chat pane
const isMobilePortrait = ref(false)
const docRefreshTick = ref(0) // bumped on agent activity → DocumentPane re-checks for external changes
let _mqlPortrait = null

const bodyClass = computed(() => {
  if (isMobilePortrait.value && openDoc.value) return 'mobile-portrait'
  return openDoc.value ? outerOrientation.value : 'no-doc'
})
const chatPaneStyle = computed(() => {
  if (!openDoc.value || isMobilePortrait.value) return {}
  return { flexBasis: `${outerSize.value}%` }
})
const docPaneStyle = computed(() => {
  if (!openDoc.value || isMobilePortrait.value) return {}
  return { flexBasis: `${100 - outerSize.value}%` }
})

function docStorageKey(id) { return `enclave_open_doc_${id}` }

async function loadDocList() {
  if (!selectedSession.value) { docList.value = []; return }
  try {
    const arts = await api.getArtifacts(selectedSession.value)
    docList.value = (arts || []).filter(a => EDITABLE_DOC_RE.test(a.filename))
  } catch { docList.value = [] }
}
function toggleDocMenu() {
  docMenuOpen.value = !docMenuOpen.value
  if (docMenuOpen.value) loadDocList()
}
function openDocument(filename) {
  openDoc.value = filename
  docMenuOpen.value = false
  if (selectedSession.value) localStorage.setItem(docStorageKey(selectedSession.value), filename)
}
function closeDoc() {
  openDoc.value = ''
  if (selectedSession.value) localStorage.removeItem(docStorageKey(selectedSession.value))
}
function toggleOuterOrientation() {
  outerOrientation.value = outerOrientation.value === 'horizontal' ? 'vertical' : 'horizontal'
  localStorage.setItem('enclave_outer_orientation', outerOrientation.value)
}

function startOuterDrag(e) {
  const body = e.currentTarget.parentElement
  if (!body) return
  const rect = body.getBoundingClientRect()
  const horizontal = outerOrientation.value === 'horizontal'
  function move(ev) {
    let pct = horizontal
      ? ((ev.clientX - rect.left) / rect.width) * 100
      : ((ev.clientY - rect.top) / rect.height) * 100
    outerSize.value = Math.min(85, Math.max(15, pct))
  }
  function up() {
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', up)
    document.body.style.userSelect = ''
    localStorage.setItem('enclave_outer_size', String(outerSize.value))
  }
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', up)
  document.body.style.userSelect = 'none'
}

function restoreOpenDoc() {
  // Deep-link (?doc=) takes priority, else the last-open doc for this session.
  const q = route.query.doc
  if (typeof q === 'string' && q) { openDoc.value = q; return }
  if (selectedSession.value) {
    openDoc.value = localStorage.getItem(docStorageKey(selectedSession.value)) || ''
  } else {
    openDoc.value = ''
  }
}

watch(() => route.query.doc, (d) => {
  if (typeof d === 'string' && d) openDocument(d)
})

const turns = ref([])
const hasMore = ref(false)
const loadingMore = ref(false)
const INITIAL_LIMIT = 120
const draft = ref('')
const sending = ref(false)
const messagesEl = ref(null)
const inputEl = ref(null)

// ─── Per-session draft persistence ───
// Keep an unsent message around if the user navigates away and returns.
const DRAFT_PREFIX = 'enclave_chat_draft_'
function draftKey(id) { return DRAFT_PREFIX + id }
function loadDraft(id) {
  draft.value = id ? (localStorage.getItem(draftKey(id)) || '') : ''
}
watch(draft, (v) => {
  const id = selectedSession.value
  if (!id) return
  if (v) localStorage.setItem(draftKey(id), v)
  else localStorage.removeItem(draftKey(id))
})

// ─── Mermaid diagram rendering ───
let _mermaidObserver = null
let _mermaidTimer = null
async function processMermaid() {
  const root = messagesEl.value
  if (!root) return
  const nodes = root.querySelectorAll('div.mermaid:not([data-processed])')
  if (!nodes.length) return
  try {
    const mermaid = await ensureMermaid()
    await mermaid.run({ nodes: Array.from(nodes), suppressErrors: true })
  } catch { /* incomplete/invalid diagram — ignore */ }
}
function scheduleMermaid() {
  if (_mermaidTimer) clearTimeout(_mermaidTimer)
  // Debounce so streaming deltas don't trigger renders on every frame.
  _mermaidTimer = setTimeout(processMermaid, 300)
}

const pendingFiles = ref([])
const dragging = ref(false)
const { loadModels, loadCredits, applyCreditsUpdate } = useModels()

// Live streaming state
const liveEvents = ref([])
const streamingText = ref('')
const activityText = ref('')
const askUserPrompt = ref(null)
const askUserAnswer = ref('')
let currentThinkingIdx = -1
const liveEventsExpanded = ref(false)
const LIVE_EVENTS_VISIBLE = 5

const collapsedLiveCount = computed(() => Math.max(0, liveEvents.value.length - LIVE_EVENTS_VISIBLE))
const collapsedLiveEvents = computed(() => liveEvents.value.slice(0, collapsedLiveCount.value))
const visibleLiveEvents = computed(() => {
  if (liveEvents.value.length <= LIVE_EVENTS_VISIBLE) return liveEvents.value
  return liveEvents.value.slice(-LIVE_EVENTS_VISIBLE)
})

// Agent state tracking
const agentState = ref('unknown') // 'idle' | 'thinking' | 'tool' | 'responding' | 'waiting_user' | 'unknown'
const agentToolName = ref('')
const agentLastUpdate = ref(null)
let stateIdleTimer = null

function setAgentState(state, toolName = '') {
  agentState.value = state
  agentToolName.value = toolName
  agentLastUpdate.value = new Date()
  // Clear any pending idle timer
  if (stateIdleTimer) { clearTimeout(stateIdleTimer); stateIdleTimer = null }
}

const agentStateLabel = computed(() => {
  switch (agentState.value) {
    case 'thinking': return '🤔 Thinking…'
    case 'tool': return `⚙️ Running ${agentToolName.value || 'tool'}…`
    case 'responding': return '💬 Responding…'
    case 'waiting_user': return '❓ Waiting for input'
    case 'idle': return '😴 Idle'
    default: return ''
  }
})

const agentStateClass = computed(() => agentState.value)

// Persisted events per turn (turn_index → events array)
const turnEvents = ref({})
const expandedTurns = ref({})
const TOOL_ICONS_MAP = TOOL_ICONS

// Stream events that indicate the agent has resumed work after an ask_user
// (used to auto-dismiss a lingering question card).
const AGENT_PROGRESS_EVENTS = new Set([
  'turn_start', 'delta', 'thinking', 'tool_start', 'tool_complete',
  'structured_response', 'turn', 'turn_end',
])

// Events after which an open artifact may have changed on disk.
const DOC_REFRESH_EVENTS = new Set([
  'tool_complete', 'response', 'structured_response', 'turn_end', 'file_send',
])

function countSegmentEvents(segments) {
  if (!segments) return 0
  return segments.reduce((sum, seg) => sum + seg.tools.length + (seg.response ? 1 : 0), 0)
}

let ws = null
let dragCounter = 0

onMounted(async () => {
  // Track mobile-portrait to force the doc-on-top stacked layout with tabs.
  _mqlPortrait = window.matchMedia('(max-width: 820px) and (orientation: portrait)')
  const applyPortrait = (e) => { isMobilePortrait.value = e.matches }
  applyPortrait(_mqlPortrait)
  _mqlPortrait.addEventListener('change', applyPortrait)
  _mqlPortrait._handler = applyPortrait

  restoreOpenDoc()
  if (selectedSession.value) {
    loadHistory()
    loadDocList()
  }
  loadDraft(selectedSession.value)
  window.addEventListener('keydown', onLightboxKey)
})

// Attach a MutationObserver to the messages container so mermaid diagrams are
// rendered whenever new content is injected via v-html (history, live stream,
// structured responses). Re-attaches if the container is recreated.
watch(messagesEl, (el) => {
  if (_mermaidObserver) { _mermaidObserver.disconnect(); _mermaidObserver = null }
  if (el) {
    _mermaidObserver = new MutationObserver(scheduleMermaid)
    _mermaidObserver.observe(el, { childList: true, subtree: true })
    scheduleMermaid()
  }
})

onUnmounted(() => {
  if (ws) ws.close()
  if (_mermaidObserver) { _mermaidObserver.disconnect(); _mermaidObserver = null }
  if (_mermaidTimer) clearTimeout(_mermaidTimer)
  pendingFiles.value.forEach(f => { if (f.preview) URL.revokeObjectURL(f.preview) })
  window.removeEventListener('keydown', onLightboxKey)
  if (_mqlPortrait && _mqlPortrait._handler) {
    _mqlPortrait.removeEventListener('change', _mqlPortrait._handler)
    _mqlPortrait = null
  }
})

watch(selectedSession, (newVal) => {
  if (ws) { ws.close(); ws = null }
  clearLiveState()
  docMenuOpen.value = false
  restoreOpenDoc()
  loadDraft(newVal)
  if (newVal) { loadHistory(); loadDocList() }
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
    scrollToBottom(true)
    connectWebSocket()
    loadModels(selectedSession.value)
    loadCredits(selectedSession.value)
  } catch (e) {
    console.error('Failed to load history:', e)
  }
}

async function loadEvents() {
  if (!selectedSession.value) return
  try {
    // Only load events for the displayed turn range
    const firstTs = turns.value.length > 0 ? turns.value[0].timestamp : null
    const data = await api.getChatEvents(selectedSession.value, {
      limit: 5000,
      sinceTimestamp: firstTs || undefined,
    })
    const events = data.events || []
    // Tool calls (and file sends) are collapsed under their turn. Agent
    // responses and structured cards are NOT grouped here: history
    // reconstruction now surfaces every response as its own bubble and marks
    // structured cards on dedicated turns, so grouping them here would
    // duplicate (responses) or double-map (cards) them.
    const grouped = {}
    for (const evt of events) {
      if (!['tool_start', 'tool_complete', 'file_send', 'thinking'].includes(evt.type)) continue
      // Find the best matching turn (latest turn that started before this event)
      let bestTurn = null
      for (const t of turns.value) {
        const ti = t.turn_index
        if (ti == null) continue
        if (t.timestamp && t.timestamp <= evt.timestamp) {
          bestTurn = ti
        }
      }
      if (bestTurn == null) continue
      if (!grouped[bestTurn]) grouped[bestTurn] = [{ tools: [], response: null }]
      grouped[bestTurn][0].tools.push(evt)
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

  // Any sign of the agent resuming forward progress means a pending ask_user
  // question has been answered (via the composer, Matrix, or another client),
  // so clear the lingering question card. ask_user blocks the turn, so these
  // events only arrive after the answer was delivered.
  if (askUserPrompt.value && AGENT_PROGRESS_EVENTS.has(type)) {
    askUserPrompt.value = null
    askUserAnswer.value = ''
  }

  // After the agent does work it may have rewritten an open artifact (via
  // publish_artifact or a direct file edit). Nudge the document pane to re-check
  // for external changes on these completion-ish events.
  if (openDoc.value && DOC_REFRESH_EVENTS.has(type)) {
    docRefreshTick.value++
  }

  if (type === 'credits') {
    // Live "AI Credits" update from the orchestrator: account entitlement
    // snapshot + per-session consumed AI Units. Routed through the shared
    // composable so the global tab bar reflects it too.
    applyCreditsUpdate(msg)
    return
  }

  if (type === 'turn') {
    // Completed turn from SQLite — merge into turns list
    const idx = turns.value.findIndex(t => t.turn_index === msg.turn_index)
    if (idx >= 0) {
      turns.value[idx] = msg
    } else {
      // Remove any queued message that matches this turn's user_message.
      // The SDK wraps messages with <current_datetime>, so check if the
      // turn's user_message contains the queued text (or vice versa).
      if (msg.user_message) {
        const qIdx = turns.value.findIndex(t =>
          t.source === 'queued' && (
            t.user_message === msg.user_message ||
            msg.user_message.includes(t.user_message)
          )
        )
        if (qIdx >= 0) {
          turns.value.splice(qIdx, 1)
        } else {
          // Fallback: remove the most recent queued message
          for (let i = turns.value.length - 1; i >= 0; i--) {
            if (turns.value[i].source === 'queued') {
              turns.value.splice(i, 1)
              break
            }
          }
        }
      }
      // Remove any live-cache entry that matches this turn's assistant_response
      if (msg.assistant_response) {
        const lIdx = turns.value.findIndex(t => t.source === 'live' && t.assistant_response === msg.assistant_response)
        if (lIdx >= 0) {
          // Preserve is_major and structured data from the live turn
          if (turns.value[lIdx].is_major) msg.is_major = true
          if (turns.value[lIdx].structured) msg.structured = turns.value[lIdx].structured
          turns.value.splice(lIdx, 1)
        }
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
    setAgentState('thinking')
    return
  }

  if (type === 'delta') {
    streamingText.value = msg.content || ''
    activityText.value = ''
    sending.value = false
    setAgentState('responding')
    return
  }

  if (type === 'thinking') {
    const phase = msg.phase || 'delta'
    setAgentState('thinking')
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
    setAgentState('tool', msg.detail || msg.name || 'tool')
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
    setAgentState('thinking') // Back to thinking after tool completes
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
    setAgentState('waiting_user')
    askUserPrompt.value = {
      question: msg.question || '',
      choices: msg.choices || [],
    }
    askUserAnswer.value = ''
    return
  }

  if (type === 'file_send') {
    // Agent sent a file — show it as a live event
    const filename = msg.filename || 'file'
    const mxcUrl = msg.mxc_url || ''
    liveEvents.value.push(reactive({
      type: 'file_send',
      filename,
      filePath: msg.file_path || '',
      mxcUrl,
      mimetype: msg.mimetype || '',
      collapsed: false,
    }))
    nextTick(() => scrollToBottom())
    return
  }

  if (type === 'response') {
    setAgentState('responding')
    // Final response — promote to a synthetic turn immediately so it
    // survives live state clears (turn_start, next response, etc.).
    // The SQLite poll will eventually replace it with the real turn.
    if (msg.content) {
      // Remove any existing live-source turn with the same content
      const existIdx = turns.value.findIndex(t => t.source === 'live' && t.assistant_response === msg.content)
      if (existIdx < 0) {
        turns.value.push({
          turn_index: null,
          user_message: null,
          assistant_response: msg.content,
          timestamp: new Date().toISOString(),
          source: 'live',
          is_major: true,
        })
      }
      streamingText.value = ''
    }
    activityText.value = ''
    // Flush the live tool/thinking blocks — they belong to the segment we just
    // finalized into a turn above, and leaving them here pins them to the
    // bottom while later segments render above (only cleared on reload before).
    liveEvents.value = []
    currentThinkingIdx = -1
    nextTick(() => scrollToBottom())
    return
  }

  if (type === 'structured_response') {
    setAgentState('responding')
    const summary = msg.summary || ''
    if (summary) {
      turns.value.push({
        turn_index: null,
        user_message: null,
        assistant_response: summary,
        timestamp: new Date().toISOString(),
        source: 'live',
        is_major: true,
        structured: {
          title: msg.title || '',
          summary: msg.summary || '',
          details: msg.details || '',
          actions: msg.actions || [],
          images: msg.images || [],
        },
      })
      streamingText.value = ''
    }
    activityText.value = ''
    liveEvents.value = []
    currentThinkingIdx = -1
    nextTick(() => scrollToBottom())
    return
  }

  if (type === 'turn_end') {
    sending.value = false
    activityText.value = ''
    // Set idle after a short delay (agent may start a new turn immediately)
    if (stateIdleTimer) clearTimeout(stateIdleTimer)
    stateIdleTimer = setTimeout(() => setAgentState('idle'), 3000)
    return
  }
}

async function send() {
  if ((!draft.value.trim() && !pendingFiles.value.length) || !selectedSession.value) return
  const content = draft.value.trim()
  draft.value = ''
  sending.value = true

  // Sending a message answers any pending ask_user question — dismiss its card.
  if (askUserPrompt.value) {
    askUserPrompt.value = null
    askUserAnswer.value = ''
  }

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
  scrollToBottom(true)
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

function onMessagesClick(event) {
  // Delegate click on any <img> inside rendered markdown to open lightbox
  const img = event.target.closest('.message-body img')
  if (img && img.src) {
    event.preventDefault()
    openLightbox(img.src)
  }
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// Auto-scroll pinning: only scroll down if user is already at the bottom
const isScrollPinned = ref(true)
const hasNewContent = ref(false)

function onMessagesScroll() {
  if (!messagesEl.value) return
  const el = messagesEl.value
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60
  isScrollPinned.value = atBottom
  if (atBottom) hasNewContent.value = false
}

function scrollToBottom(force = false) {
  if (!messagesEl.value) return
  if (force || isScrollPinned.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
    hasNewContent.value = false
  } else {
    hasNewContent.value = true
  }
}

function jumpToLatest() {
  isScrollPinned.value = true
  hasNewContent.value = false
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
  // Convert mxc:// URLs to proxied URLs for images (with auth token)
  const authToken = encodeURIComponent(localStorage.getItem('enclave_token') || '')
  cleaned = cleaned.replace(/mxc:\/\/([^/\s]+)\/([^)\s]+)/g,
    (_, server, id) => `/api/chat/media/${server}/${id}?token=${authToken}`)
  // Rewrite /workspace/ paths to proxy URLs so embedded images work
  if (selectedSession.value) {
    cleaned = cleaned.replace(/\/workspace\/([^)\s"']+)/g, (_, p) =>
      `/api/chat/${selectedSession.value}/file/${p.split('/').map(encodeURIComponent).join('/')}?token=${authToken}`)
  }
  const display = cleaned.length > 10000 ? cleaned.slice(0, 10000) + '\n\n…(truncated)' : cleaned
  return md.render(display)
}

function downloadMarkdown(structured) {
  const parts = []
  if (structured.title) parts.push(`# ${structured.title}\n`)
  if (structured.summary) parts.push(structured.summary + '\n')
  if (structured.details) parts.push('---\n\n' + structured.details + '\n')
  if (structured.actions?.length) {
    parts.push('## Options\n')
    structured.actions.forEach((a, i) => parts.push(`${i + 1}. ${a.label}\n`))
  }
  const blob = new Blob([parts.join('\n')], { type: 'text/markdown' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = (structured.title || 'response').replace(/[^a-zA-Z0-9_-]/g, '_') + '.md'
  a.click()
  URL.revokeObjectURL(url)
}

function sendActionReply(label) {
  if (!selectedSession.value) return
  draft.value = label
  send()
}

function workspaceFileUrl(filePath) {
  // Strip leading /workspace/ prefix if present, then build the proxy URL
  // Each path segment is individually encoded to preserve slashes
  let rel = filePath
  if (rel.startsWith('/workspace/')) rel = rel.slice('/workspace/'.length)
  else if (rel.startsWith('/')) rel = rel.slice(1)
  const encoded = rel.split('/').map(encodeURIComponent).join('/')
  const token = localStorage.getItem('enclave_token') || ''
  return `/api/chat/${selectedSession.value}/file/${encoded}?token=${encodeURIComponent(token)}`
}

function mediaUrl(mxcUrl) {
  // Convert mxc://server/mediaId to proxied URL with auth
  if (!mxcUrl || !mxcUrl.startsWith('mxc://')) return ''
  const parts = mxcUrl.slice('mxc://'.length).split('/')
  if (parts.length < 2) return ''
  const token = localStorage.getItem('enclave_token') || ''
  return `/api/chat/media/${parts[0]}/${parts[1]}?token=${encodeURIComponent(token)}`
}

// Gallery / lightbox
const lightboxImage = ref(null) // stores the URL of the currently viewed image

const galleryImages = computed(() => {
  const imgs = []
  // Collect from turns
  for (const turn of turns.value) {
    if (turn.structured?.images?.length) {
      for (const img of turn.structured.images) imgs.push(workspaceFileUrl(img))
    }
    if (turn.structured?.actions?.length) {
      for (const a of turn.structured.actions) {
        if (a.image) imgs.push(workspaceFileUrl(a.image))
      }
    }
    // Persisted events
    const evts = turnEvents.value[turn.turn_index]
    if (evts) {
      for (const seg of evts) {
        for (const evt of (seg.events || [])) {
          if (evt.type === 'file_send' && evt.data?.mimetype?.startsWith('image/')) {
            if (evt.data.file_path) imgs.push(workspaceFileUrl(evt.data.file_path))
            else if (evt.data.mxc_url) imgs.push(mediaUrl(evt.data.mxc_url))
          }
        }
      }
    }
  }
  // Collect from live events
  for (const evt of liveEvents.value) {
    if (evt.type === 'file_send' && evt.mimetype?.startsWith('image/')) {
      if (evt.filePath) imgs.push(workspaceFileUrl(evt.filePath))
      else if (evt.mxcUrl) imgs.push(mediaUrl(evt.mxcUrl))
    }
  }
  return imgs
})

const lightboxIndex = computed(() => {
  if (!lightboxImage.value) return -1
  const idx = galleryImages.value.indexOf(lightboxImage.value)
  return idx >= 0 ? idx : 0
})

const lightboxImageUrl = computed(() => lightboxImage.value || '')

function openLightbox(url) {
  lightboxImage.value = url
}

function lightboxPrev() {
  const idx = lightboxIndex.value
  if (idx > 0) lightboxImage.value = galleryImages.value[idx - 1]
}

function lightboxNext() {
  const idx = lightboxIndex.value
  if (idx < galleryImages.value.length - 1) lightboxImage.value = galleryImages.value[idx + 1]
}

function onLightboxKey(e) {
  if (!lightboxImage.value) return
  if (e.key === 'Escape') lightboxImage.value = null
  else if (e.key === 'ArrowLeft') lightboxPrev()
  else if (e.key === 'ArrowRight') lightboxNext()
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
  height: 100%;
  min-height: 0;
  overflow-x: hidden;
}

/* Floating agent status pill (sits above the messages, does not scroll). */
.agent-status-float {
  position: absolute;
  top: 0.5rem;
  left: 50%;
  transform: translateX(-50%);
  z-index: 5;
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.78rem;
  color: #999;
  padding: 0.2rem 0.7rem;
  border-radius: 12px;
  background: var(--bg-sidebar);
  border: 1px solid var(--border);
  pointer-events: none;
}

.agent-status-float .status-indicator {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #666;
}

/* Artifacts handle on the right edge of the chat pane. */
.doc-tools {
  position: absolute;
  top: 50%;
  right: 0;
  transform: translateY(-50%);
  z-index: 6;
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  align-items: flex-end;
}

.doc-handle {
  background: var(--bg-sidebar);
  border: 1px solid var(--border);
  border-right: none;
  border-radius: 8px 0 0 8px;
  cursor: pointer;
  padding: 0.6rem 0.4rem;
  font-size: 1.1rem;
  line-height: 1;
  box-shadow: -1px 0 4px rgba(0,0,0,0.15);
}

.doc-handle:hover {
  background: var(--bg-hover);
}

.doc-orient-btn {
  background: var(--bg-sidebar);
  border: 1px solid var(--border);
  border-right: none;
  border-radius: 8px 0 0 8px;
  cursor: pointer;
  padding: 0.5rem 0.4rem;
  font-size: 1rem;
  line-height: 1;
}

.chat-header {
  display: flex;
  align-items: center;
  gap: 1rem;
  margin-bottom: 1rem;
}

.chat-header h2 { margin: 0; }

/* Agent status indicator */
.agent-status {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.8rem;
  color: #999;
  padding: 0.2rem 0.6rem;
  border-radius: 12px;
  background: rgba(255,255,255,0.05);
}

.agent-status .status-indicator {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #666;
}

.agent-status.thinking .status-indicator {
  background: #f59e0b;
  animation: pulse 1.5s ease-in-out infinite;
}

.agent-status.tool .status-indicator {
  background: #3b82f6;
  animation: spin-dot 1s linear infinite;
  border-radius: 2px;
  width: 8px;
  height: 8px;
}

.agent-status.responding .status-indicator {
  background: #4ade80;
  animation: pulse 0.8s ease-in-out infinite;
}

.agent-status.waiting_user .status-indicator {
  background: #f97316;
  animation: pulse 2s ease-in-out infinite;
}

.agent-status.idle .status-indicator {
  background: #666;
}

@keyframes pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.4; transform: scale(0.8); }
}

@keyframes spin-dot {
  0% { transform: rotate(0deg); }
  100% { transform: rotate(360deg); }
}

.model-picker {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  margin-left: auto;
}

.ai-credits {
  font-size: 0.78rem;
  padding: 0.3rem 0.55rem;
  background: var(--bg-main);
  color: var(--text-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  white-space: nowrap;
  cursor: default;
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
  padding: 1.5rem 1.5rem 1rem;
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

.assistant-message.major-response {
  border-left: 3px solid #4ade80;
  background: linear-gradient(90deg, rgba(74, 222, 128, 0.06) 0%, var(--bg-card) 40%);
  box-shadow: -2px 0 8px rgba(74, 222, 128, 0.08);
}

/* Structured message cards */
.structured-card {
  padding: 1rem;
}

.structured-card .message-meta {
  margin-top: 0.75rem;
  margin-bottom: 0;
}

.structured-title {
  font-size: 1.1rem;
  font-weight: 700;
  margin-bottom: 0.5rem;
  color: #e2e8f0;
}

.structured-summary {
  margin-bottom: 0.5rem;
}

.structured-summary :deep(p) {
  margin: 0.25rem 0;
}

.structured-images {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  margin: 0.5rem 0;
}

.structured-img {
  max-width: 300px;
  max-height: 225px;
  border-radius: 6px;
  cursor: zoom-in;
  transition: transform 0.15s;
}

.structured-img:hover {
  transform: scale(1.05);
}

.structured-details {
  margin: 0.5rem 0;
  border: 1px solid rgba(255,255,255,0.1);
  border-radius: 6px;
  overflow: hidden;
}

.structured-details > summary {
  padding: 0.4rem 0.75rem;
  cursor: pointer;
  font-size: 0.85rem;
  color: #94a3b8;
  background: rgba(255,255,255,0.03);
  user-select: none;
}

.structured-details > summary:hover {
  color: #e2e8f0;
  background: rgba(255,255,255,0.06);
}

.structured-details-body {
  padding: 0.75rem;
  font-size: 0.9rem;
  border-top: 1px solid rgba(255,255,255,0.05);
}

.structured-details-actions {
  display: flex;
  gap: 0.5rem;
  padding: 0.5rem 0.75rem;
  border-top: 1px solid rgba(255,255,255,0.05);
}

.btn-sm {
  font-size: 0.75rem;
  padding: 0.25rem 0.6rem;
  border: 1px solid rgba(255,255,255,0.15);
  border-radius: 4px;
  background: rgba(255,255,255,0.05);
  color: #94a3b8;
  cursor: pointer;
}

.btn-sm:hover {
  background: rgba(255,255,255,0.1);
  color: #e2e8f0;
}

.structured-actions {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  margin: 0.5rem 0;
}

.structured-action {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.3rem;
}

.action-img {
  max-width: 180px;
  max-height: 135px;
  border-radius: 4px;
  cursor: zoom-in;
}

.action-btn {
  padding: 0.4rem 1rem;
  border: 1px solid #4ade80;
  border-radius: 6px;
  background: rgba(74, 222, 128, 0.1);
  color: #4ade80;
  cursor: pointer;
  font-size: 0.85rem;
  transition: all 0.15s;
}

.action-btn:hover {
  background: rgba(74, 222, 128, 0.2);
  box-shadow: 0 0 8px rgba(74, 222, 128, 0.2);
}

.segment-response {
  margin: 0.4rem 0;
  padding: 0.5rem 0.75rem;
  font-size: 0.9rem;
  opacity: 0.85;
  max-width: 100%;
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

/* Mermaid diagrams */
.message-body :deep(.mermaid) {
  background: var(--bg-main);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0.75rem;
  margin: 0 0 0.5rem;
  overflow-x: auto;
  text-align: center;
  line-height: normal;
}
.message-body :deep(.mermaid svg) {
  max-width: 100%;
  height: auto;
}
.message-body :deep(.mermaid:not([data-processed])) {
  white-space: pre;
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.8rem;
  text-align: left;
  color: var(--text-secondary);
}

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
  padding: 1rem 1.5rem;
  padding-bottom: max(1rem, env(safe-area-inset-bottom, 0));
  border-top: 1px solid var(--border);
  align-items: flex-end;
  max-width: 100%;
  box-sizing: border-box;
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
  overflow: hidden;
}

/* ─── Document workspace (chat ↔ artifact split) ─── */
.chat-body {
  flex: 1;
  display: flex;
  min-height: 0;
  min-width: 0;
  overflow: hidden;
}
.chat-body.no-doc { flex-direction: column; }
.chat-body.horizontal { flex-direction: row; }
.chat-body.vertical { flex-direction: column; }
.chat-body.mobile-portrait { flex-direction: column-reverse; }

.chat-body > .chat-container { flex: 1 1 auto; min-width: 0; min-height: 0; }
.chat-body.no-doc > .chat-container { flex-basis: auto; }

.outer-doc {
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
}

.outer-divider {
  flex: 0 0 auto;
  background: var(--border, #333);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 5;
}
.outer-divider.horizontal { width: 6px; cursor: col-resize; }
.outer-divider.vertical { height: 6px; cursor: row-resize; }
.outer-divider:hover { background: var(--accent, #6c8cff); }
.outer-divider .divider-grip { background: var(--bg-card, #2a2a32); border-radius: 3px; }
.outer-divider.horizontal .divider-grip { width: 2px; height: 28px; }
.outer-divider.vertical .divider-grip { height: 2px; width: 28px; }

/* On mobile portrait the split is a fixed doc-on-top stack (no drag handle). */
.chat-body.mobile-portrait > .chat-container { flex: 1 1 55%; }
.chat-body.mobile-portrait > .outer-doc { flex: 1 1 45%; }
.chat-body.mobile-portrait > .outer-divider { display: none; }

/* Header document controls */
.doc-controls { display: flex; align-items: center; gap: 0.3rem; position: relative; }
.doc-open-btn, .doc-orient-btn {
  background: var(--bg-card, #1e1e24);
  border: 1px solid var(--border, #333);
  color: inherit;
  border-radius: 6px;
  padding: 0.25rem 0.5rem;
  cursor: pointer;
  font-size: 0.95rem;
}
.doc-open-btn:hover, .doc-orient-btn:hover { border-color: var(--accent, #6c8cff); }
.doc-menu-wrap { position: relative; }
.doc-menu {
  position: absolute;
  right: 0;
  top: calc(100% + 4px);
  min-width: 220px;
  max-width: 320px;
  max-height: 50vh;
  overflow: auto;
  background: var(--bg-card, #1e1e24);
  border: 1px solid var(--border, #333);
  border-radius: 8px;
  box-shadow: 0 6px 20px rgba(0,0,0,0.4);
  z-index: 50;
  padding: 0.25rem;
}
.doc-menu-empty { padding: 0.5rem 0.6rem; font-size: 0.85rem; }
.doc-menu-item {
  display: block;
  width: 100%;
  text-align: left;
  background: none;
  border: none;
  color: inherit;
  padding: 0.4rem 0.6rem;
  border-radius: 5px;
  cursor: pointer;
  font-size: 0.85rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.doc-menu-item:hover { background: var(--bg, #121216); }
.doc-menu-item.active { color: var(--accent, #6c8cff); }

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
  padding-left: 0.5rem;
  margin-left: 0.25rem;
}

.live-event {
  margin-bottom: 0.15rem;
}

.thinking-block {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
}

.tool-block {
  overflow: hidden;
}

.file-send-preview {
  padding: 0.25rem 0;
}

.file-send-block {
  margin: 0.25rem 0;
}

.file-send-label {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  font-size: 0.75rem;
  color: var(--text-muted);
  margin-bottom: 0.2rem;
}

.file-send-img {
  max-width: 450px;
  max-height: 300px;
  border-radius: var(--radius-sm);
  cursor: zoom-in;
}

.event-header {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  padding: 0.15rem 0.4rem;
  cursor: pointer;
  font-size: 0.72rem;
  color: var(--text-muted);
  user-select: none;
}

.event-header:hover {
  color: var(--text-secondary);
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
  font-size: 0.6rem;
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
  .chat-view { height: calc(100dvh - 3.5rem); }
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

/* Jump to latest button */
.jump-to-latest {
  position: absolute;
  bottom: 5rem;
  left: 50%;
  transform: translateX(-50%);
  background: var(--accent, #63b3ed);
  color: #fff;
  border: none;
  border-radius: 20px;
  padding: 0.5rem 1.2rem;
  font-size: 0.8rem;
  font-weight: 500;
  cursor: pointer;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
  z-index: 10;
  transition: opacity 0.2s, transform 0.2s;
}
.jump-to-latest:hover {
  transform: translateX(-50%) scale(1.05);
}
.fade-enter-active, .fade-leave-active { transition: opacity 0.2s; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

/* Collapsed live events summary */
.live-events-collapsed {
  margin-bottom: 0.5rem;
}
.collapsed-summary {
  cursor: pointer;
  font-size: 0.78rem;
  color: var(--text-muted, #888);
  user-select: none;
  padding: 0.2rem 0;
}
.collapsed-summary:hover {
  color: var(--text-primary, #ccc);
}

/* Chat container needs relative positioning for jump button */

/* Clickable images */
.clickable-img {
  cursor: zoom-in;
}
.message-body :deep(img) {
  cursor: zoom-in;
  max-width: 100%;
  border-radius: var(--radius-sm, 4px);
}

/* Lightbox gallery overlay (not scoped — teleported to body) */
</style>

<style>
.lightbox-overlay {
  position: fixed;
  inset: 0;
  z-index: 9999;
  background: rgba(0, 0, 0, 0.92);
  display: flex;
  align-items: center;
  justify-content: center;
  backdrop-filter: blur(4px);
}

.lightbox-img {
  max-width: 90vw;
  max-height: 90vh;
  object-fit: contain;
  border-radius: 4px;
  box-shadow: 0 4px 30px rgba(0, 0, 0, 0.5);
  user-select: none;
}

.lightbox-close {
  position: absolute;
  top: 1rem;
  right: 1rem;
  background: rgba(255, 255, 255, 0.15);
  border: none;
  color: #fff;
  font-size: 1.5rem;
  width: 40px;
  height: 40px;
  border-radius: 50%;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.2s;
}
.lightbox-close:hover {
  background: rgba(255, 255, 255, 0.3);
}

.lightbox-nav {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  background: rgba(255, 255, 255, 0.12);
  border: none;
  color: #fff;
  font-size: 2.5rem;
  width: 50px;
  height: 70px;
  border-radius: 8px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.2s;
  user-select: none;
}
.lightbox-nav:hover {
  background: rgba(255, 255, 255, 0.25);
}
.lightbox-prev { left: 1rem; }
.lightbox-next { right: 1rem; }

.lightbox-counter {
  position: absolute;
  bottom: 1.5rem;
  left: 50%;
  transform: translateX(-50%);
  color: rgba(255, 255, 255, 0.7);
  font-size: 0.85rem;
  background: rgba(0, 0, 0, 0.5);
  padding: 0.25rem 0.75rem;
  border-radius: 12px;
}

.lightbox-fade-enter-active { transition: opacity 0.2s ease; }
.lightbox-fade-leave-active { transition: opacity 0.15s ease; }
.lightbox-fade-enter-from, .lightbox-fade-leave-to { opacity: 0; }
</style>
