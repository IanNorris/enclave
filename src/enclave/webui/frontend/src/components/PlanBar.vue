<template>
  <div class="plan-bar" v-if="planInfo">
    <span class="plan-label">Plan:</span>
    <span class="plan-detail">{{ planInfo.monthly_cap }} {{ planInfo.currency }}/mo</span>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api.js'

const planInfo = ref(null)

onMounted(async () => {
  try {
    planInfo.value = await api.getPlan()
  } catch (err) {
    console.warn('Failed to fetch plan info:', err)
  }
})
</script>

<style scoped>
.plan-bar {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  padding: 0.5rem 0.6rem;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  font-size: 0.75rem;
  color: var(--text-secondary);
  white-space: nowrap;
}

.plan-label {
  font-weight: 500;
  opacity: 0.7;
}

.plan-detail {
  color: var(--text-primary);
  font-weight: 500;
}
</style>
