<template>
  <div class="session-tabbar" v-if="selectedSessionId">
    <!-- Left: tabs (desktop) / dropdown (mobile) -->
    <div class="tabbar-left">
      <button class="tabbar-hamburger" @click="$emit('toggle-sidebar')" title="Menu">☰</button>
      <nav class="tabs" role="tablist">
        <router-link
          v-for="t in tabs"
          :key="t.key"
          :to="t.to"
          class="tab"
          :class="{ active: activeKey === t.key }"
        >
          <span class="tab-icon">{{ t.icon }}</span>
          <span class="tab-label">{{ t.label }}</span>
        </router-link>
      </nav>

      <!-- Mobile dropdown replaces the tab strip -->
      <div class="tabs-mobile">
        <select :value="activeKey" @change="onMobileNav($event)" class="tabs-mobile-select">
          <option v-for="t in tabs" :key="t.key" :value="t.key">
            {{ t.icon }} {{ t.label }}
          </option>
        </select>
      </div>
    </div>

    <!-- Right: AI credits + model picker -->
    <div class="tabbar-right">
      <span v-if="creditsLabel" class="ai-credits" :title="creditsTitle">{{ creditsLabel }}</span>
      <div class="model-picker" v-if="models.available.length">
        <select v-model="currentModel" @change="changeModel(selectedSessionId)" class="model-select">
          <option v-for="m in models.available" :key="m" :value="m">{{ m }}</option>
        </select>
        <button
          class="model-refresh"
          @click="refreshModels(selectedSessionId)"
          :disabled="modelsRefreshing"
          title="Refresh model list"
        >{{ modelsRefreshing ? '⟳' : '↻' }}</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSessionStore } from '../stores/session.js'
import { useModels } from '../composables/useModels.js'

defineEmits(['toggle-sidebar'])

const route = useRoute()
const router = useRouter()
const { selectedSessionId } = useSessionStore()
const {
  models, currentModel, modelsRefreshing,
  creditsLabel, creditsTitle,
  loadModels, loadCredits, refreshModels, changeModel,
} = useModels()

const tabs = [
  { key: 'chat', label: 'Chat', icon: '💬', to: '/chat', names: ['chat'] },
  { key: 'bugs', label: 'Bugs', icon: '🐛', to: '/bugs', names: ['bugs', 'bug-detail'] },
  { key: 'artifacts', label: 'Artifacts', icon: '📎', to: '/artifacts', names: ['artifacts', 'artifact-preview'] },
  { key: 'timeline', label: 'Timeline', icon: '📅', to: '/timeline', names: ['timeline'] },
  { key: 'settings', label: 'Session Settings', icon: '⚙', to: '/session/settings', names: ['session-settings'] },
]

const activeKey = computed(() => {
  const name = route.name
  const t = tabs.find(t => t.names.includes(name))
  return t ? t.key : ''
})

function onMobileNav(e) {
  const t = tabs.find(t => t.key === e.target.value)
  if (t) router.push(t.to)
}

// Load models + credits whenever the selected session changes.
watch(selectedSessionId, (id) => {
  loadModels(id)
  loadCredits(id)
}, { immediate: true })
</script>

<style scoped>
.session-tabbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 0.4rem 1rem;
  background: var(--bg-sidebar);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}

.tabbar-left {
  display: flex;
  align-items: center;
  gap: 1rem;
  min-width: 0;
}

.tabs {
  display: flex;
  align-items: stretch;
  gap: 0.25rem;
}

.tab {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.4rem 0.75rem;
  font-size: 0.85rem;
  color: var(--text-secondary);
  text-decoration: none;
  border-radius: var(--radius-sm, 4px);
  white-space: nowrap;
  transition: background 0.15s, color 0.15s;
}

.tab:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.tab.active {
  background: var(--bg-active);
  color: var(--accent);
  font-weight: 600;
  box-shadow: inset 0 -2px 0 var(--accent);
}

.tab-icon {
  font-size: 1rem;
}

.tabbar-right {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  flex-shrink: 0;
  margin-left: auto;
}

.tabs-mobile {
  display: none;
}

.tabs-mobile-select {
  font-size: 0.85rem;
  padding: 0.35rem 0.5rem;
  background: var(--bg-main);
  color: var(--text-primary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
}

.ai-credits {
  font-size: 0.75rem;
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.model-picker {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  flex-shrink: 0;
}

.model-select {
  font-size: 0.8rem;
  padding: 0.35rem 0.5rem;
  background: var(--bg-main);
  color: var(--text-primary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  max-width: 220px;
}

.model-refresh {
  background: var(--bg-main);
  color: var(--text-primary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  cursor: pointer;
  width: 2rem;
  height: 2rem;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.9rem;
  line-height: 1;
  padding: 0;
  flex-shrink: 0;
}

.model-refresh:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--accent);
}

.model-refresh:disabled {
  opacity: 0.6;
  cursor: default;
}

.tabbar-hamburger {
  display: none;
  background: none;
  border: none;
  color: var(--text-primary);
  font-size: 1.3rem;
  cursor: pointer;
  padding: 0 0.25rem;
  flex-shrink: 0;
}

@media (max-width: 768px) {
  .tabbar-hamburger {
    display: inline-flex;
  }
  .tabs {
    display: none;
  }
  .tabs-mobile {
    display: block;
  }
  .ai-credits {
    display: inline;
    font-size: 0.7rem;
    max-width: 8rem;
  }
  .model-select {
    max-width: 110px;
  }
}
</style>
