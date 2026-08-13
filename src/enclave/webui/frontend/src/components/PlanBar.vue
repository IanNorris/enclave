<template>
  <div v-if="plan" class="plan-bar-container">
    <div
      class="plan-bar"
      tabindex="0"
      role="progressbar"
      :aria-label="tooltipText"
      :aria-valuenow="usedCredits"
      :aria-valuemin="0"
      :aria-valuemax="totalCredits"
    >
      <div class="progress-bar" aria-hidden="true">
        <div class="progress-fill" :style="{ width: `${progressPercent}%` }"></div>
      </div>
      <div class="plan-tooltip" role="tooltip">
        <strong>{{ usedCredits }} / {{ totalCredits }} AI credits</strong>
        <span>{{ remainingCredits }} remaining</span>
        <span>Resets {{ resetDate }}</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, computed } from 'vue'
import { api } from '../api.js'

const credits = ref(null)

const plan = computed(() => credits.value?.snapshots?.premium_interactions || null)

const usedCredits = computed(() => plan.value?.used ?? 0)
const totalCredits = computed(() => plan.value?.entitlement ?? 0)
const remainingCredits = computed(() => Math.max(0, totalCredits.value - usedCredits.value))

const progressPercent = computed(() => {
  if (!totalCredits.value) return 0
  return Math.min(100, (usedCredits.value / totalCredits.value) * 100)
})

const resetDate = computed(() => {
  if (!plan.value?.reset_date) return 'at the next billing period'
  return new Intl.DateTimeFormat(undefined, {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  }).format(new Date(plan.value.reset_date))
})

const tooltipText = computed(() => {
  if (!plan.value) return ''
  return `${usedCredits.value} / ${totalCredits.value} AI credits. ` +
    `${remainingCredits.value} remaining. Resets ${resetDate.value}.`
})

onMounted(async () => {
  try {
    credits.value = await api.getCredits()
  } catch (err) {
    console.warn('Failed to fetch credit plan:', err)
  }
})
</script>

<style scoped>
.plan-bar-container {
  padding: 0.5rem 0.6rem;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  font-size: 0.7rem;
  color: var(--text-secondary);
}

.plan-bar {
  position: relative;
  cursor: help;
}

.progress-bar {
  height: 6px;
  background: var(--bg-hover);
  border-radius: 3px;
  overflow: hidden;
  border: 1px solid var(--border);
}

.progress-fill {
  height: 100%;
  background: linear-gradient(90deg, #3b82f6, #8b5cf6);
  transition: width 0.3s ease;
}

.plan-tooltip {
  position: absolute;
  left: 0;
  bottom: calc(100% + 0.5rem);
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  width: max-content;
  max-width: 220px;
  padding: 0.55rem 0.65rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  background: var(--bg-main);
  box-shadow: 0 4px 12px rgb(0 0 0 / 20%);
  color: var(--text-primary);
  font-size: 0.75rem;
  line-height: 1.25;
  opacity: 0;
  pointer-events: none;
  transform: translateY(0.2rem);
  transition: opacity 0.15s ease, transform 0.15s ease;
}

.plan-bar:hover .plan-tooltip,
.plan-bar:focus-visible .plan-tooltip {
  opacity: 1;
  transform: translateY(0);
}
</style>
