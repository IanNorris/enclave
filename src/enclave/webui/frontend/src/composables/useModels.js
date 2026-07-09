import { ref, computed } from 'vue'
import { api } from '../api.js'

// Shared, singleton model + AI-credits state for the *selected* session.
// Lifted out of Chat.vue so the global session tab bar can own the picker and
// the credits label while Chat keeps feeding live updates over its websocket.

const models = ref({ current: null, available: [], preferences: [] })
const currentModel = ref('')
const modelsRefreshing = ref(false)
const aiCredits = ref(null)
// Tracks which session `models` currently reflects, so a merge is only applied
// on same-session reloads (not on a switch, which must replace outright).
let _lastModelsSession = null

// Real (non-fusion) models from a models payload.
function realModels(data) {
  const fusion = new Set(data?.fusion_models || [])
  return (data?.available || []).filter(m => !fusion.has(m))
}

// Merge a freshly-fetched models payload over the current one WITHOUT letting a
// transient empty result collapse the picker to fusion-only. If the new payload
// has no real models but we already had some, keep the known-good real list and
// only adopt the new fusion ids + current. (A crashed/busy/fresh session can
// briefly report no real models; that must not wipe the list — regression for
// the blog session showing only fusion models.)
function mergeModels(prev, next) {
  if (realModels(next).length > 0 || realModels(prev).length === 0) return next
  const nextFusion = (next.available || []).filter(m => (new Set(next.fusion_models || [])).has(m))
  return { ...next, available: [...nextFusion, ...realModels(prev)] }
}

// Pick the account "AI Credits" snapshot (premium request quota) from the SDK's
// quota snapshots, tolerating naming differences across SDK versions.
function pickCreditsSnapshot(snapshots) {
  if (!snapshots) return null
  const keys = Object.keys(snapshots)
  if (!keys.length) return null
  const preferred = keys.find(k => /premium/i.test(k))
    || keys.find(k => /credit/i.test(k))
    || keys.find(k => /interaction/i.test(k))
    || keys[0]
  return { key: preferred, ...snapshots[preferred] }
}

const creditsLabel = computed(() => {
  const c = aiCredits.value
  // Consumed AI Units ("AI Credits") for this session — the primary figure,
  // mirroring the Copilot CLI's "AI Credits" indicator.
  const aiu = Number(c?.session?.aiu)
  if (Number.isFinite(aiu) && aiu > 0) {
    const shown = aiu >= 100 ? Math.round(aiu) : Math.round(aiu * 10) / 10
    return `AI Credits: ${shown.toLocaleString()}`
  }
  // Fall back to the account entitlement snapshot (e.g. "Unlimited") until the
  // session has consumed anything.
  const snap = pickCreditsSnapshot(c?.snapshots)
  if (!snap) return ''
  if (snap.is_unlimited) return 'AI Credits: Unlimited'
  const ent = Number(snap.entitlement)
  const used = Number(snap.used)
  if (Number.isFinite(ent) && Number.isFinite(used)) {
    const remaining = Math.max(ent - used, 0)
    const rounded = Math.round(remaining * 10) / 10
    return `AI Credits: ${rounded}/${ent}`
  }
  if (Number.isFinite(Number(snap.remaining_percentage))) {
    return `AI Credits: ${Math.round(Number(snap.remaining_percentage))}%`
  }
  return ''
})

const creditsTitle = computed(() => {
  const c = aiCredits.value
  const parts = []
  const sess = c?.session
  if (sess && Number.isFinite(Number(sess.aiu))) {
    parts.push(`${Number(sess.aiu).toLocaleString()} AI Units consumed this session`)
    if (sess.requests) parts.push(`${sess.requests} requests`)
  }
  const snap = pickCreditsSnapshot(c?.snapshots)
  if (snap) {
    if (snap.is_unlimited) parts.push('Entitlement: Unlimited')
    if (snap.reset_date) parts.push(`Resets: ${new Date(snap.reset_date).toLocaleString()}`)
  }
  if (c?.model) parts.push(`Last model: ${c.model}`)
  if (c?.ts) parts.push(`Updated: ${new Date(c.ts).toLocaleString()}`)
  return parts.join(' · ')
})

async function loadModels(sessionId) {
  if (!sessionId) {
    models.value = { current: null, available: [], preferences: [] }
    currentModel.value = ''
    _lastModelsSession = null
    return
  }
  try {
    const data = await api.getModels(sessionId)
    // Merge only when reloading the SAME session (guards against a transient
    // empty list wiping the picker). On a session switch, replace outright so
    // one session's models can't bleed into another.
    models.value = sessionId === _lastModelsSession ? mergeModels(models.value, data) : data
    _lastModelsSession = sessionId
    currentModel.value = data.current || ''
  } catch (e) {
    console.error('Failed to load models:', e)
  }
}

async function refreshModels(sessionId) {
  if (!sessionId) return
  modelsRefreshing.value = true
  try {
    const data = await api.getModels(sessionId, true)
    // Same-session refresh: never let a transient empty result collapse the
    // picker to fusion-only.
    models.value = sessionId === _lastModelsSession ? mergeModels(models.value, data) : data
    _lastModelsSession = sessionId
    currentModel.value = data.current || currentModel.value || ''
  } catch (e) {
    console.error('Failed to refresh models:', e)
  } finally {
    modelsRefreshing.value = false
  }
}

async function changeModel(sessionId) {
  if (!currentModel.value || !sessionId) return
  try {
    await api.setModel(sessionId, currentModel.value)
  } catch (e) {
    console.error('Failed to change model:', e)
  }
}

async function loadCredits(sessionId) {
  if (!sessionId) { aiCredits.value = null; return }
  try {
    const data = await api.getCredits(sessionId)
    const hasSnapshots = data && data.snapshots && Object.keys(data.snapshots).length
    const hasSession = data && data.session && Object.keys(data.session).length
    if (hasSnapshots || hasSession) {
      aiCredits.value = data
    }
  } catch (e) {
    console.error('Failed to load AI credits:', e)
  }
}

// Apply a live "credits" websocket message (forwarded from Chat's stream).
function applyCreditsUpdate(msg) {
  if ((msg.snapshots && Object.keys(msg.snapshots).length) ||
      (msg.session && Object.keys(msg.session).length)) {
    aiCredits.value = {
      snapshots: msg.snapshots || aiCredits.value?.snapshots || {},
      session: msg.session || aiCredits.value?.session || {},
      ts: msg.ts || new Date().toISOString(),
      last_cost: msg.last_cost || 0,
      model: msg.model || '',
    }
  }
}

export function useModels() {
  return {
    models,
    currentModel,
    modelsRefreshing,
    aiCredits,
    creditsLabel,
    creditsTitle,
    loadModels,
    refreshModels,
    changeModel,
    loadCredits,
    applyCreditsUpdate,
  }
}
