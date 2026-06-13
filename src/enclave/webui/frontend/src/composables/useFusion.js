import { ref, computed } from 'vue'

// Shared Auto Fusion state for the *selected* session, lifted out of Chat.vue
// so the global session tab bar can show the live complexity grade and the
// model combination currently in use while Chat keeps feeding updates over its
// websocket. Mirrors the pattern used by useModels for AI credits.

const liveComplexity = ref(null) // { score, tier, reason } | null
const liveModels = ref('')       // model combo of the most recent fusion run

const complexityLabel = computed(() => {
  const c = liveComplexity.value
  if (!c || !c.score) return ''
  return `Complexity ${c.score}/5`
})

function applyComplexity(msg) {
  liveComplexity.value = {
    score: msg.score,
    tier: msg.tier,
    reason: msg.reason || '',
  }
}

function applyFusion(msg) {
  const models = msg.models || []
  if (models.length) liveModels.value = models.join(' + ')
}

function resetFusion() {
  liveComplexity.value = null
  liveModels.value = ''
}

export function useFusion() {
  return {
    liveComplexity,
    liveModels,
    complexityLabel,
    applyComplexity,
    applyFusion,
    resetFusion,
  }
}
