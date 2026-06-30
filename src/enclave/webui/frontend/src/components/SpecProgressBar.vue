<template>
  <div v-if="active" class="spec-progress" @mouseenter="hover = true" @mouseleave="hover = false">
    <div class="spec-bar">
      <div class="spec-fill" :style="{ width: active.taskProgress.percent + '%' }"></div>
    </div>
    <transition name="ovl">
      <div v-if="hover" class="spec-overlay">
        <div class="ovl-head">
          <span class="ovl-name">📋 {{ active.id }}</span>
          <span class="ovl-pct">{{ active.taskProgress.done }}/{{ active.taskProgress.total }} · {{ active.taskProgress.percent }}%</span>
        </div>
        <div class="ovl-row" v-if="active.taskProgress.current">
          <span class="ovl-lbl now">Now</span><span class="ovl-task">{{ active.taskProgress.current }}</span>
        </div>
        <div class="ovl-row" v-if="active.taskProgress.next">
          <span class="ovl-lbl next">Next</span><span class="ovl-task">{{ active.taskProgress.next }}</span>
        </div>
        <div class="ovl-row" v-if="!active.taskProgress.current">
          <span class="ovl-task done">✓ All tasks complete</span>
        </div>
      </div>
    </transition>
  </div>
</template>

<script setup>
import { ref, watch, onMounted, onUnmounted } from 'vue'
import { useSessionStore } from '../stores/session.js'
import { api } from '../api.js'

const { selectedSessionId } = useSessionStore()
const active = ref(null)   // the change whose progress is shown (most in-progress)
const hover = ref(false)
let timer = null

function pickActive(changes) {
  if (!changes || !changes.length) return null
  // Prefer a change that's started but not finished; else the first.
  const inProgress = changes.filter(c => c.taskProgress.total > 0 && c.taskProgress.done < c.taskProgress.total)
  const pool = inProgress.length ? inProgress : changes
  // Most-progressed in-progress change feels most "active".
  return pool.slice().sort((a, b) => b.taskProgress.done - a.taskProgress.done)[0]
}

async function refresh() {
  if (!selectedSessionId.value) { active.value = null; return }
  try {
    const data = await api.getOpenSpecChanges(selectedSessionId.value)
    active.value = pickActive(data.changes)
  } catch { active.value = null }
}

onMounted(() => {
  refresh()
  timer = setInterval(refresh, 15000)  // light poll; specs change slowly
})
onUnmounted(() => { if (timer) clearInterval(timer) })
watch(selectedSessionId, refresh)
</script>

<style scoped>
.spec-progress { position: relative; height: 3px; width: 100%; cursor: default; }
.spec-bar { height: 3px; width: 100%; background: var(--bg-main, #15151a); overflow: hidden; }
.spec-fill { height: 100%; background: linear-gradient(90deg, var(--accent, #7c9eff), #4ade80); transition: width 0.6s cubic-bezier(.2,.8,.2,1); }
.spec-progress:hover .spec-bar { height: 5px; }
.spec-overlay {
  position: absolute; top: 6px; left: 12px; z-index: 50;
  background: var(--bg-card, #1a1a22); border: 1px solid var(--border, #333);
  border-radius: 8px; padding: 0.55rem 0.7rem; min-width: 280px; max-width: 440px;
  box-shadow: 0 6px 24px rgba(0,0,0,.4);
}
.ovl-head { display: flex; justify-content: space-between; gap: 0.6rem; margin-bottom: 0.4rem; font-size: 0.78rem; }
.ovl-name { font-family: monospace; color: var(--accent, #7c9eff); }
.ovl-pct { color: var(--text-muted, #999); }
.ovl-row { display: flex; gap: 0.5rem; align-items: baseline; font-size: 0.78rem; margin-top: 0.2rem; }
.ovl-lbl { font-size: 0.6rem; text-transform: uppercase; letter-spacing: .05em; padding: 1px 5px; border-radius: 8px; flex-shrink: 0; }
.ovl-lbl.now { background: #14532d; color: #4ade80; }
.ovl-lbl.next { background: #1e3a5f; color: #7c9eff; }
.ovl-task { color: var(--text, #e8e8f0); }
.ovl-task.done { color: #4ade80; }
.ovl-enter-active, .ovl-leave-active { transition: opacity .12s, transform .12s; }
.ovl-enter-from, .ovl-leave-to { opacity: 0; transform: translateY(-3px); }
</style>
