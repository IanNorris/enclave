<template>
  <div class="preview-view">
    <div class="preview-header">
      <button class="back" @click="goBack">← Back</button>
      <h2 class="title">{{ filename }}</h2>
      <a :href="rawUrl" target="_blank" class="raw-link">Raw ↗</a>
    </div>

    <div v-if="loading" class="muted">Loading…</div>
    <div v-else-if="error" class="error">{{ error }}</div>

    <template v-else>
      <div
        v-if="isMarkdown"
        ref="renderedEl"
        class="rendered"
        v-html="renderedHtml"
      ></div>
      <pre v-else class="plain">{{ content }}</pre>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api.js'
import MarkdownIt from 'markdown-it'
import mathPlugin from '../lib/mathPlugin.js'

const route = useRoute()
const router = useRouter()

const session = computed(() => route.params.session)
const filename = computed(() => route.params.filename)

const content = ref('')
const loading = ref(true)
const error = ref('')
const renderedEl = ref(null)

const TEXT_RE = /\.(md|txt|json|yaml|yml|csv|log)$/i
const isMarkdown = computed(() => /\.md$/i.test(filename.value))
const isText = computed(() => TEXT_RE.test(filename.value))

const rawUrl = computed(() => {
  const token = localStorage.getItem('enclave_token')
  return `${api.rawArtifactUrl(session.value, filename.value)}&token=${encodeURIComponent(token)}`
})

const md = new MarkdownIt({ html: true, linkify: true, breaks: true }).use(mathPlugin)
const defaultFence = md.renderer.rules.fence?.bind(md.renderer.rules) ||
  ((tokens, idx, opts, env, self) => self.renderToken(tokens, idx, opts))
md.renderer.rules.fence = (tokens, idx, options, env, self) => {
  const info = (tokens[idx].info || '').trim().toLowerCase()
  if (info === 'mermaid') {
    return `<div class="mermaid">${md.utils.escapeHtml(tokens[idx].content)}</div>`
  }
  return defaultFence(tokens, idx, options, env, self)
}

let _mermaidPromise = null
function ensureMermaid() {
  if (!_mermaidPromise) {
    _mermaidPromise = import('mermaid').then(({ default: mermaid }) => {
      mermaid.initialize({ startOnLoad: false, theme: 'dark', securityLevel: 'strict' })
      return mermaid
    })
  }
  return _mermaidPromise
}
async function processMermaid() {
  const root = renderedEl.value
  if (!root) return
  const nodes = root.querySelectorAll('div.mermaid:not([data-processed])')
  if (!nodes.length) return
  try {
    const mermaid = await ensureMermaid()
    await mermaid.run({ nodes: Array.from(nodes), suppressErrors: true })
  } catch { /* ignore */ }
}

const renderedHtml = computed(() => md.render(content.value || ''))

function goBack() {
  if (window.history.length > 1) router.back()
  else router.push('/artifacts')
}

onMounted(async () => {
  try {
    if (isText.value) {
      const data = await api.getArtifactContent(session.value, filename.value)
      content.value = data.content || ''
      if (isMarkdown.value) nextTick(processMermaid)
    } else {
      // Non-text artifact — just open the raw file.
      window.location.replace(rawUrl.value)
      return
    }
  } catch (e) {
    error.value = `Failed to load artifact: ${e.message}`
  } finally {
    loading.value = false
  }
})
</script>

<style scoped>
.preview-view { max-width: 900px; margin: 0 auto; padding: 1rem; }
.preview-header {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-bottom: 1rem;
  flex-wrap: wrap;
}
.back {
  background: var(--bg-card, #1e1e24);
  border: 1px solid var(--border, #333);
  color: inherit;
  padding: 0.3rem 0.7rem;
  border-radius: var(--radius-sm, 6px);
  cursor: pointer;
}
.title {
  margin: 0;
  font-size: 1rem;
  font-family: monospace;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.raw-link {
  font-size: 0.85rem;
  text-decoration: none;
  color: var(--accent, #4a7);
}
.muted { color: var(--text-muted, #888); }
.error { color: #ef4444; }
.plain {
  font-size: 0.85rem;
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--bg-card, #1e1e24);
  border: 1px solid var(--border, #333);
  border-radius: var(--radius-sm, 6px);
  padding: 1rem;
  margin: 0;
}
.rendered { font-size: 0.95rem; line-height: 1.6; }
.rendered :deep(h1) { font-size: 1.5rem; margin: 0.6rem 0; }
.rendered :deep(h2) { font-size: 1.25rem; margin: 0.6rem 0; }
.rendered :deep(h3) { font-size: 1.05rem; margin: 0.6rem 0; }
.rendered :deep(p) { margin: 0 0 0.6rem; }
.rendered :deep(pre) {
  background: var(--bg-main, #15151a);
  border: 1px solid var(--border, #333);
  border-radius: var(--radius-sm, 6px);
  padding: 0.75rem;
  overflow-x: auto;
  font-size: 0.85rem;
}
.rendered :deep(code) { font-family: 'JetBrains Mono', monospace; font-size: 0.85em; }
.rendered :deep(table) { border-collapse: collapse; margin: 0 0 0.6rem; }
.rendered :deep(th), .rendered :deep(td) {
  border: 1px solid var(--border, #333);
  padding: 0.3rem 0.6rem;
}
.rendered :deep(a) { color: var(--accent, #4a7); }
.rendered :deep(blockquote) {
  border-left: 3px solid var(--border, #444);
  margin: 0 0 0.6rem;
  padding-left: 0.75rem;
  color: var(--text-muted, #999);
}
.rendered :deep(.mermaid) {
  background: var(--bg-main, #15151a);
  border: 1px solid var(--border, #333);
  border-radius: var(--radius-sm, 6px);
  padding: 0.75rem;
  margin: 0 0 0.6rem;
  overflow-x: auto;
  text-align: center;
}
.rendered :deep(.mermaid svg) { max-width: 100%; height: auto; }
.rendered :deep(img) { max-width: 100%; border-radius: var(--radius-sm, 6px); }
</style>
