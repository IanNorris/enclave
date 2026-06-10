<template>
  <div ref="rootEl" class="split-pane" :class="orientation">
    <div class="split-a" :style="paneAStyle">
      <slot name="a" />
    </div>
    <div
      class="split-divider"
      :class="{ dragging }"
      @pointerdown="onPointerDown"
      role="separator"
      :aria-orientation="orientation === 'horizontal' ? 'vertical' : 'horizontal'"
    >
      <div class="divider-grip"></div>
    </div>
    <div class="split-b" :style="paneBStyle">
      <slot name="b" />
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onBeforeUnmount } from 'vue'

const props = defineProps({
  // 'horizontal' = side-by-side (A left / B right), 'vertical' = stacked (A top / B bottom)
  orientation: { type: String, default: 'horizontal' },
  // Percentage (0-100) of the container allotted to pane A.
  modelValue: { type: Number, default: 50 },
  // localStorage key to persist the split size across sessions.
  storageKey: { type: String, default: '' },
  // Minimum percentage for either pane.
  min: { type: Number, default: 12 },
})
const emit = defineEmits(['update:modelValue'])

const rootEl = ref(null)
const dragging = ref(false)

function clamp(v) {
  return Math.min(100 - props.min, Math.max(props.min, v))
}

function initialSize() {
  if (props.storageKey) {
    const saved = parseFloat(localStorage.getItem(props.storageKey))
    if (!Number.isNaN(saved)) return clamp(saved)
  }
  return clamp(props.modelValue)
}

const size = ref(initialSize())

watch(() => props.modelValue, (v) => {
  const c = clamp(v)
  if (Math.abs(c - size.value) > 0.01) size.value = c
})

watch(size, (v) => {
  emit('update:modelValue', v)
  if (props.storageKey) localStorage.setItem(props.storageKey, String(v))
})

const paneAStyle = computed(() => ({ flexBasis: `${size.value}%` }))
const paneBStyle = computed(() => ({ flexBasis: `${100 - size.value}%` }))

function onPointerMove(e) {
  const el = rootEl.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  let pct
  if (props.orientation === 'horizontal') {
    pct = ((e.clientX - rect.left) / rect.width) * 100
  } else {
    pct = ((e.clientY - rect.top) / rect.height) * 100
  }
  size.value = clamp(pct)
}

function endDrag() {
  if (!dragging.value) return
  dragging.value = false
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('pointerup', endDrag)
  document.body.style.userSelect = ''
  document.body.style.cursor = ''
}

function onPointerDown() {
  dragging.value = true
  window.addEventListener('pointermove', onPointerMove)
  window.addEventListener('pointerup', endDrag)
  document.body.style.userSelect = 'none'
  document.body.style.cursor = props.orientation === 'horizontal' ? 'col-resize' : 'row-resize'
}

onBeforeUnmount(endDrag)
</script>

<style scoped>
.split-pane {
  display: flex;
  width: 100%;
  height: 100%;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}
.split-pane.horizontal { flex-direction: row; }
.split-pane.vertical { flex-direction: column; }

.split-a, .split-b {
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.split-divider {
  flex: 0 0 auto;
  background: var(--border, #333);
  position: relative;
  z-index: 5;
  display: flex;
  align-items: center;
  justify-content: center;
}
.split-pane.horizontal > .split-divider {
  width: 6px;
  cursor: col-resize;
}
.split-pane.vertical > .split-divider {
  height: 6px;
  cursor: row-resize;
}
.split-divider:hover, .split-divider.dragging {
  background: var(--accent, #6c8cff);
}
.divider-grip {
  background: var(--bg-card, #2a2a32);
  border-radius: 3px;
}
.split-pane.horizontal .divider-grip { width: 2px; height: 28px; }
.split-pane.vertical .divider-grip { height: 2px; width: 28px; }
</style>
