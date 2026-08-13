<template>
  <div class="plan-bar" v-if="planInfo">
    <div class="plan-content">
      <span class="plan-label">Plan:</span>
      <span class="plan-detail">{{ planInfo.monthly_cap }} {{ planInfo.currency }}/mo</span>
    </div>
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
  position: fixed;
  bottom: 0;
  right: 0;
  background: var(--bg-sidebar);
  border-left: 1px solid var(--border);
  border-top: 1px solid var(--border);
  padding: 0.5rem 0.75rem;
  font-size: 0.75rem;
  color: var(--text-secondary);
  z-index: 10;
}

.plan-content {
  display: flex;
  align-items: center;
  gap: 0.3rem;
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

@media (max-width: 768px) {
  .plan-bar {
    padding: 0.4rem 0.6rem;
    font-size: 0.65rem;
  }
}
</style>
