<template>
  <div class="document-pane">
    <div class="doc-header">
      <div class="doc-title" :title="filename">
        <span class="doc-icon">📄</span>
        <span class="doc-name">{{ filename }}</span>
        <span v-if="version" class="version-badge">v{{ version }}</span>
        <span v-if="dirty" class="dirty-dot" title="Unsaved changes">●</span>
      </div>

      <div class="doc-modes">
        <!-- Desktop: full mode toggle. Mobile: tabs (no Combined). -->
        <button
          v-if="isMarkdown"
          class="mode-btn" :class="{ active: mode === 'view' }"
          @click="setMode('view')"
        >View</button>
        <button
          class="mode-btn" :class="{ active: mode === 'edit' }"
          @click="setMode('edit')"
        >Edit</button>
        <button
          v-if="isMarkdown && !mobile"
          class="mode-btn" :class="{ active: mode === 'combined' }"
          @click="setMode('combined')"
        >Combined</button>
        <button
          class="mode-btn" :class="{ active: mode === 'diff' }"
          @click="setMode('diff')"
        >Diff</button>

        <!-- Inner orientation toggle (Combined only) -->
        <button
          v-if="mode === 'combined'"
          class="icon-btn"
          :title="innerOrientation === 'horizontal' ? 'Switch to stacked' : 'Switch to side-by-side'"
          @click="toggleInnerOrientation"
        >{{ innerOrientation === 'horizontal' ? '⬌' : '⬍' }}</button>
      </div>

      <div class="doc-actions">
        <button class="btn-sm save-btn" :disabled="!dirty || saving" @click="save">
          {{ saving ? 'Saving…' : 'Save' }}
        </button>
        <button class="icon-btn" title="Close document" @click="$emit('close')">✕</button>
      </div>
    </div>

    <!-- Conflict banner -->
    <div v-if="conflict" class="doc-conflict">
      <span>This artifact changed (now v{{ conflict.current_version }}). </span>
      <button class="btn-sm" @click="reload">Reload</button>
      <button class="btn-sm danger" @click="overwrite">Overwrite</button>
    </div>
    <!-- Agent updated the file while we have unsaved edits -->
    <div v-if="externalUpdate" class="doc-external">
      <span>The agent updated this document (v{{ externalUpdate.version }}). </span>
      <button class="btn-sm" @click="acceptExternal">Load theirs</button>
      <button class="btn-sm" @click="dismissExternal">Keep mine</button>
    </div>
    <div v-if="error" class="doc-error">{{ error }}</div>

    <div class="doc-body">
      <div v-if="loading" class="doc-loading muted">Loading…</div>

      <template v-else>
        <!-- View -->
        <MarkdownViewer v-if="mode === 'view'" :source="content" />

        <!-- Edit -->
        <CodeEditor
          v-else-if="mode === 'edit'"
          v-model="editContent"
          :language="language"
        />

        <!-- Combined: editor + viewer -->
        <SplitPane
          v-else-if="mode === 'combined'"
          :orientation="innerOrientation"
          :storage-key="'enclave_doc_inner_' + innerOrientation"
        >
          <template #a>
            <CodeEditor v-model="editContent" :language="language" />
          </template>
          <template #b>
            <MarkdownViewer :source="editContent" />
          </template>
        </SplitPane>

        <!-- Diff -->
        <div v-else-if="mode === 'diff'" class="doc-diff">
          <div class="diff-controls">
            <label>Compare</label>
            <select v-model.number="diffV1">
              <option v-for="v in versionOptions" :key="'a'+v" :value="v">v{{ v }}</option>
            </select>
            <span>→</span>
            <select v-model.number="diffV2">
              <option v-for="v in versionOptions" :key="'b'+v" :value="v">v{{ v }}{{ v === version ? ' (current)' : '' }}</option>
            </select>
            <button class="btn-sm" @click="loadDiff" :disabled="diffLoading">Refresh</button>
          </div>
          <div v-if="diffLoading" class="muted">Loading diff…</div>
          <div v-else-if="diffText === '' " class="muted">No differences between these versions.</div>
          <pre v-else class="diff-output"><template v-for="(ln, i) in diffLines" :key="i"><span :class="ln.cls">{{ ln.text }}</span>
</template></pre>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { api } from '../api.js'
import SplitPane from './SplitPane.vue'
import CodeEditor from './CodeEditor.vue'
import MarkdownViewer from './MarkdownViewer.vue'

const props = defineProps({
  session: { type: String, required: true },
  filename: { type: String, required: true },
  mobile: { type: Boolean, default: false },
  // Bumped by the parent on agent activity so we can re-check for external edits.
  refreshTick: { type: Number, default: 0 },
})
defineEmits(['close'])

const content = ref('')        // last-saved content
const editContent = ref('')    // working copy
const version = ref(null)
const baseVersion = ref(null)
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const conflict = ref(null)
const externalUpdate = ref(null) // {version} when agent changed the file while we have unsaved edits
let _checkTimer = null
let _pollTimer = null

const MD_RE = /\.md$/i
const JSON_RE = /\.json$/i
const isMarkdown = computed(() => MD_RE.test(props.filename))
const language = computed(() => {
  if (JSON_RE.test(props.filename)) return 'json'
  if (isMarkdown.value) return 'markdown'
  return 'text'
})

const dirty = computed(() => editContent.value !== content.value)

const STORAGE_MODE = 'enclave_doc_mode'
const STORAGE_INNER = 'enclave_doc_inner_orientation'

function defaultMode() {
  const saved = localStorage.getItem(STORAGE_MODE)
  const allowed = isMarkdown.value
    ? (props.mobile ? ['view', 'edit', 'diff'] : ['view', 'edit', 'combined', 'diff'])
    : ['edit', 'diff']
  if (saved && allowed.includes(saved)) return saved
  return isMarkdown.value ? 'view' : 'edit'
}
const mode = ref(defaultMode())
const innerOrientation = ref(localStorage.getItem(STORAGE_INNER) || 'horizontal')

function setMode(m) {
  mode.value = m
  localStorage.setItem(STORAGE_MODE, m)
  if (m === 'diff') loadDiff()
}
function toggleInnerOrientation() {
  innerOrientation.value = innerOrientation.value === 'horizontal' ? 'vertical' : 'horizontal'
  localStorage.setItem(STORAGE_INNER, innerOrientation.value)
}

async function load() {
  loading.value = true
  error.value = ''
  conflict.value = null
  externalUpdate.value = null
  try {
    const data = await api.getArtifactContent(props.session, props.filename)
    content.value = data.content || ''
    editContent.value = content.value
    try {
      const vinfo = await api.getArtifactVersions(props.session, props.filename)
      version.value = vinfo.current_version || 1
    } catch { version.value = 1 }
    baseVersion.value = version.value
  } catch (e) {
    error.value = `Failed to load: ${e.message}`
  } finally {
    loading.value = false
  }
}

async function doSave(useBase) {
  saving.value = true
  error.value = ''
  conflict.value = null
  try {
    const res = await api.saveArtifactContent(
      props.session, props.filename, editContent.value,
      useBase ? baseVersion.value : null,
    )
    content.value = editContent.value
    version.value = res.version
    baseVersion.value = res.version
    externalUpdate.value = null
  } catch (e) {
    if (e.status === 409) {
      conflict.value = { current_version: e.detail?.current_version }
    } else {
      error.value = `Save failed: ${e.message}`
    }
  } finally {
    saving.value = false
  }
}
function save() { doSave(true) }
function overwrite() { doSave(false) }

async function reload() {
  conflict.value = null
  await load()
}

// Diff state
const diffV1 = ref(1)
const diffV2 = ref(0)
const diffText = ref('')
const diffLoading = ref(false)

const versionOptions = computed(() => {
  const cur = version.value || 1
  return Array.from({ length: cur }, (_, i) => i + 1)
})
const diffLines = computed(() =>
  diffText.value.split('\n').map((text) => {
    let cls = 'diff-ctx'
    if (text.startsWith('+++') || text.startsWith('---')) cls = 'diff-file'
    else if (text.startsWith('@@')) cls = 'diff-hunk'
    else if (text.startsWith('+')) cls = 'diff-add'
    else if (text.startsWith('-')) cls = 'diff-del'
    return { text, cls }
  })
)

async function loadDiff() {
  diffLoading.value = true
  diffText.value = ''
  try {
    const cur = version.value || 1
    if (!diffV1.value) diffV1.value = Math.max(1, cur - 1)
    if (!diffV2.value) diffV2.value = cur
    const data = await api.getArtifactDiff(
      props.session, props.filename, diffV1.value,
      diffV2.value === cur ? 0 : diffV2.value,
    )
    diffText.value = data.diff || ''
  } catch (e) {
    diffText.value = ''
    error.value = `Diff failed: ${e.message}`
  } finally {
    diffLoading.value = false
  }
}

watch(() => props.filename, () => {
  mode.value = defaultMode()
  diffV1.value = 1
  diffV2.value = 0
  externalUpdate.value = null
  load()
})

// ─── Auto-refresh when the agent updates the file ───
async function checkForUpdate() {
  if (loading.value || saving.value || conflict.value) return
  let data
  try {
    data = await api.getArtifactContent(props.session, props.filename)
  } catch { return }
  const serverContent = data.content || ''
  if (serverContent === content.value) return // nothing changed on disk

  let serverVersion = version.value
  try {
    const vinfo = await api.getArtifactVersions(props.session, props.filename)
    serverVersion = vinfo.current_version || serverVersion
  } catch { /* keep prior */ }

  if (dirty.value) {
    // User has unsaved edits — never clobber. Offer a non-destructive choice.
    externalUpdate.value = { version: serverVersion }
    return
  }
  // No local edits — adopt the agent's version live.
  content.value = serverContent
  editContent.value = serverContent
  version.value = serverVersion
  baseVersion.value = serverVersion
  if (mode.value === 'diff') loadDiff()
}

function acceptExternal() {
  externalUpdate.value = null
  load()
}
function dismissExternal() {
  externalUpdate.value = null
}

watch(() => props.refreshTick, () => {
  // Debounce bursts of agent events into a single check.
  if (_checkTimer) clearTimeout(_checkTimer)
  _checkTimer = setTimeout(checkForUpdate, 600)
})

onMounted(() => {
  // Initialise diff defaults once version is known.
  load().then(() => {
    const cur = version.value || 1
    diffV1.value = Math.max(1, cur - 1)
    diffV2.value = cur
  })
  // Safety-net poll for changes not coupled to a streamed event (e.g. direct
  // file edits mid-turn), only while the tab is visible.
  _pollTimer = setInterval(() => {
    if (document.visibilityState === 'visible') checkForUpdate()
  }, 12000)
})

onBeforeUnmount(() => {
  if (_checkTimer) clearTimeout(_checkTimer)
  if (_pollTimer) clearInterval(_pollTimer)
})
</script>

<style scoped>
.document-pane {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: var(--bg, #121216);
  border-left: 1px solid var(--border, #333);
}
.doc-header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.4rem 0.6rem;
  border-bottom: 1px solid var(--border, #333);
  flex-wrap: wrap;
}
.doc-title {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  min-width: 0;
  flex: 1 1 auto;
  font-size: 0.9rem;
}
.doc-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.version-badge {
  background: var(--bg-card, #2a2a32);
  border-radius: 4px;
  padding: 0 0.35rem;
  font-size: 0.7rem;
  color: var(--text-muted, #aaa);
}
.dirty-dot { color: var(--accent, #6c8cff); }
.doc-modes { display: flex; gap: 0.2rem; align-items: center; }
.mode-btn, .icon-btn, .save-btn {
  background: var(--bg-card, #1e1e24);
  border: 1px solid var(--border, #333);
  color: inherit;
  border-radius: 5px;
  padding: 0.2rem 0.5rem;
  font-size: 0.8rem;
  cursor: pointer;
}
.mode-btn.active { background: var(--accent, #6c8cff); color: #fff; border-color: var(--accent, #6c8cff); }
.save-btn:disabled { opacity: 0.5; cursor: default; }
.save-btn:not(:disabled) { border-color: var(--accent, #6c8cff); }
.doc-actions { display: flex; gap: 0.3rem; align-items: center; }
.doc-conflict, .doc-error {
  padding: 0.4rem 0.6rem;
  font-size: 0.85rem;
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
.doc-conflict { background: #3a2e12; border-bottom: 1px solid #5a4a1a; }
.doc-external { background: #12303a; border-bottom: 1px solid #1a4a5a; }
.doc-conflict, .doc-external {
  padding: 0.4rem 0.6rem;
  font-size: 0.85rem;
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
.doc-error { background: #3a1212; border-bottom: 1px solid #5a1a1a; }
.btn-sm.danger { border-color: #c0392b; }
.doc-body { flex: 1 1 auto; min-height: 0; overflow: hidden; display: flex; flex-direction: column; }
.doc-loading { padding: 1rem; }
.doc-diff { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.diff-controls {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.4rem 0.6rem;
  border-bottom: 1px solid var(--border, #333);
  font-size: 0.85rem;
  flex-wrap: wrap;
}
.diff-output {
  flex: 1 1 auto;
  overflow: auto;
  margin: 0;
  padding: 0.5rem 0.75rem;
  font-size: 0.82rem;
  line-height: 1.45;
}
.diff-output .diff-add { color: #7ee787; display: block; background: rgba(46,160,67,0.15); }
.diff-output .diff-del { color: #ff7b72; display: block; background: rgba(248,81,73,0.15); }
.diff-output .diff-hunk { color: #6c8cff; display: block; }
.diff-output .diff-file { color: var(--text-muted, #aaa); display: block; }
.diff-output .diff-ctx { display: block; }
</style>
