<template>
  <div ref="editorEl" class="code-editor"></div>
</template>

<script setup>
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
import { EditorState } from '@codemirror/state'
import { EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter } from '@codemirror/view'
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands'
import { markdown } from '@codemirror/lang-markdown'
import { json } from '@codemirror/lang-json'
import { oneDark } from '@codemirror/theme-one-dark'

const props = defineProps({
  modelValue: { type: String, default: '' },
  language: { type: String, default: 'markdown' },
})
const emit = defineEmits(['update:modelValue'])

const editorEl = ref(null)
let view = null
let applyingExternal = false

function langExtension(lang) {
  if (lang === 'json') return [json()]
  if (lang === 'markdown') return [markdown()]
  return []
}

function buildState(doc) {
  const updateListener = EditorView.updateListener.of((u) => {
    if (u.docChanged && !applyingExternal) {
      emit('update:modelValue', u.state.doc.toString())
    }
  })
  return EditorState.create({
    doc,
    extensions: [
      lineNumbers(),
      highlightActiveLine(),
      highlightActiveLineGutter(),
      history(),
      keymap.of([...defaultKeymap, ...historyKeymap, indentWithTab]),
      EditorView.lineWrapping,
      oneDark,
      ...langExtension(props.language),
      updateListener,
    ],
  })
}

onMounted(() => {
  view = new EditorView({
    state: buildState(props.modelValue || ''),
    parent: editorEl.value,
  })
})

// External content changes (load / reload / overwrite) — replace doc without
// echoing back through the update listener.
watch(() => props.modelValue, (val) => {
  if (!view) return
  const current = view.state.doc.toString()
  if (val === current) return
  applyingExternal = true
  view.dispatch({
    changes: { from: 0, to: current.length, insert: val ?? '' },
  })
  applyingExternal = false
})

// Language switch rebuilds the editor state.
watch(() => props.language, () => {
  if (!view) return
  view.setState(buildState(view.state.doc.toString()))
})

onBeforeUnmount(() => {
  if (view) { view.destroy(); view = null }
})
</script>

<style scoped>
.code-editor {
  height: 100%;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.code-editor :deep(.cm-editor) {
  height: 100%;
  font-size: 0.9rem;
}
.code-editor :deep(.cm-scroller) { overflow: auto; }
</style>
