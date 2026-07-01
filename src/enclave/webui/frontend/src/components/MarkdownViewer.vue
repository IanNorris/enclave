<template>
  <div ref="renderedEl" class="markdown-viewer" v-html="renderedHtml"></div>
</template>

<script setup>
import { ref, computed, watch, onMounted, nextTick } from 'vue'
import MarkdownIt from 'markdown-it'

const props = defineProps({
  source: { type: String, default: '' },
  // When rendering a workspace document, these let relative image paths
  // (e.g. ![](pic.png) in an artifact) resolve to the file-proxy URL instead
  // of the app origin, where they'd 404. Omitted for chat message bodies.
  session: { type: String, default: '' },
  baseDir: { type: String, default: '' },
})

const renderedEl = ref(null)

const md = new MarkdownIt({ html: true, linkify: true, breaks: true })

// Resolve a possibly-relative image src to something the browser can load.
// Absolute URLs, root-relative paths and data: URIs pass through untouched;
// bare relative paths are rewritten to the session file proxy, resolved
// against the document's directory.
function resolveImageSrc(src) {
  if (!src) return src
  if (/^(https?:|data:|blob:|\/)/i.test(src)) return src
  if (!props.session) return src
  const rel = src.replace(/^\.\//, '')
  const base = props.baseDir ? props.baseDir.replace(/\/+$/, '') + '/' : ''
  const full = base + rel
  const encoded = full.split('/').map(encodeURIComponent).join('/')
  const token = localStorage.getItem('enclave_token') || ''
  return `/api/chat/${props.session}/file/${encoded}?token=${encodeURIComponent(token)}`
}

const defaultImage = md.renderer.rules.image?.bind(md.renderer.rules) ||
  ((tokens, idx, opts, env, self) => self.renderToken(tokens, idx, opts))
md.renderer.rules.image = (tokens, idx, options, env, self) => {
  const token = tokens[idx]
  const srcIdx = token.attrIndex('src')
  if (srcIdx >= 0) {
    token.attrs[srcIdx][1] = resolveImageSrc(token.attrs[srcIdx][1])
  }
  return defaultImage(tokens, idx, options, env, self)
}

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

const renderedHtml = computed(() => {
  // Touch session/baseDir so the rewrite re-runs if the document context changes.
  void props.session; void props.baseDir
  return md.render(props.source || '')
})

watch(renderedHtml, () => nextTick(processMermaid))
onMounted(() => nextTick(processMermaid))
</script>

<style scoped>
.markdown-viewer {
  padding: 1rem 1.25rem;
  overflow: auto;
  height: 100%;
  line-height: 1.6;
  word-wrap: break-word;
}
.markdown-viewer :deep(h1),
.markdown-viewer :deep(h2),
.markdown-viewer :deep(h3) { margin-top: 1.2em; }
.markdown-viewer :deep(pre) {
  background: var(--bg-code, #15151a);
  padding: 0.75rem 1rem;
  border-radius: 6px;
  overflow-x: auto;
}
.markdown-viewer :deep(code) {
  background: var(--bg-code, #15151a);
  padding: 0.1em 0.35em;
  border-radius: 4px;
}
.markdown-viewer :deep(pre) code { background: none; padding: 0; }
.markdown-viewer :deep(table) { border-collapse: collapse; }
.markdown-viewer :deep(th),
.markdown-viewer :deep(td) {
  border: 1px solid var(--border, #333);
  padding: 0.35rem 0.6rem;
}
.markdown-viewer :deep(blockquote) {
  border-left: 3px solid var(--border, #333);
  margin: 0.5em 0;
  padding-left: 1em;
  color: var(--text-muted, #aaa);
}
.markdown-viewer :deep(img) { max-width: 100%; }
</style>
