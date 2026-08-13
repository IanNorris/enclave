<template>
  <div v-if="plan" class="plan-bar-container">
    <div
      class="plan-bar"
      tabindex="0"
    >
      <div
        class="progress-bar"
        role="progressbar"
        :aria-label="tooltipText"
        :aria-valuenow="usedCredits"
        :aria-valuemin="0"
        :aria-valuemax="totalCredits"
      >
        <div class="progress-fill" :style="{ width: `${progressPercent}%` }"></div>
      </div>
      <button
        class="plan-refresh"
        type="button"
        :disabled="creditsRefreshing"
        title="Fetch the current Copilot account quota"
        @click.stop="refreshCredits(selectedSessionId)"
      >{{ creditsRefreshing ? '⟳' : '↻' }}</button>
      <div class="plan-tooltip" role="tooltip">
        <strong>{{ usedCredits }} / {{ totalCredits }} AI credits</strong>
        <span>{{ remainingCredits }} remaining</span>
        <span>Resets {{ resetDate }}</span>
        <span class="plan-tooltip-note">Refresh fetches the current Copilot quota.</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import { useSessionStore } from '../stores/session.js'
import { useModels } from '../composables/useModels.js'

const { selectedSessionId } = useSessionStore()
const { premiumCredits: plan, creditsRefreshing, loadCredits, refreshCredits } = useModels()

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
  await loadCredits()
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
  margin-right: 1.75rem;
  height: 6px;
  background: var(--bg-hover);
  border-radius: 3px;
  overflow: hidden;
  border: 1px solid var(--border);
}

.plan-refresh {
  position: absolute;
  right: 0;
  top: 50%;
  width: 1.4rem;
  height: 1.4rem;
  padding: 0;
  border: 0;
  border-radius: var(--radius-sm, 4px);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 1rem;
  line-height: 1;
  transform: translateY(-50%);
}

.plan-refresh:hover:not(:disabled),
.plan-refresh:focus-visible {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.plan-refresh:disabled {
  cursor: wait;
  opacity: 0.6;
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

.plan-tooltip-note {
  margin-top: 0.15rem;
  color: var(--text-secondary);
  font-size: 0.7rem;
}
</style>
