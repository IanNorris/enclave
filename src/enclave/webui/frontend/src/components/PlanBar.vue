<template>
  <div class="plan-bar-container" v-if="planInfo">
    <div class="plan-bar" :title="tooltipText">
      <div class="bar-wrapper">
        <div class="progress-bar">
          <div class="progress-fill" :style="{ width: progressPercent + '%' }"></div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, computed } from 'vue'
import { api } from '../api.js'

const planInfo = ref(null)

const usedCredits = computed(() => {
  if (!planInfo.value) return 0
  return Math.round((planInfo.value.used || 0) * 100) / 100
})

const totalCredits = computed(() => {
  return planInfo.value?.monthly_cap || 7000
})

const progressPercent = computed(() => {
  if (!totalCredits.value) return 0
  return Math.min(100, (usedCredits.value / totalCredits.value) * 100)
})

const tooltipText = computed(() => {
  if (!planInfo.value) return ''
  const used = usedCredits.value
  const total = totalCredits.value
  const remaining = Math.max(0, total - used)
  return `Used: ${used} / ${total} AI credits\nRemaining: ${remaining}\nResets monthly`
})

onMounted(async () => {
  try {
    planInfo.value = await api.getPlan()
  } catch (err) {
    console.warn('Failed to fetch plan info:', err)
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
  cursor: help;
}

.bar-wrapper {
  display: flex;
  align-items: center;
  gap: 0.3rem;
}

.progress-bar {
  flex: 1;
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
</style>
