<template>
  <span
    class="complexity-badge"
    :class="'cx-' + score + (tier === 'fusion' ? ' cx-fusion' : '')"
    :title="reason || `Auto Fusion complexity ${score}/5`"
  >
    <span class="cx-dots">
      <span v-for="n in 5" :key="n" class="cx-dot" :class="{ on: n <= score }"></span>
    </span>
    <span class="cx-num">{{ score }}/5</span>
    <span v-if="tier === 'fusion'" class="cx-tier">⚡</span>
  </span>
</template>

<script setup>
defineProps({
  score: { type: Number, default: 0 },
  tier: { type: String, default: '' },
  reason: { type: String, default: '' },
})
</script>

<style scoped>
.complexity-badge {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  font-size: 0.7rem;
  color: var(--text-secondary);
  padding: 0.1rem 0.4rem;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--bg-sidebar);
  white-space: nowrap;
  vertical-align: middle;
}
.complexity-badge.cx-fusion {
  border-color: var(--accent);
  color: var(--text-primary);
}
.cx-dots { display: inline-flex; gap: 2px; }
.cx-dot { width: 5px; height: 5px; border-radius: 50%; background: var(--border); }
.cx-1 .cx-dot.on, .cx-2 .cx-dot.on { background: #4caf7a; }
.cx-3 .cx-dot.on { background: #e8a838; }
.cx-4 .cx-dot.on, .cx-5 .cx-dot.on { background: #e05555; }
.cx-num { font-variant-numeric: tabular-nums; }
.cx-tier { color: var(--accent); font-weight: 600; }
</style>
