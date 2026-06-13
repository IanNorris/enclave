<template>
  <div class="fusion-view">
    <div class="fusion-header">
      <h2>✦ Fusion</h2>
      <div class="header-actions">
        <button class="secondary" :disabled="loading || saving" @click="load">Reload</button>
        <button class="primary" :disabled="loading || saving" @click="save">
          {{ saving ? 'Saving…' : 'Save changes' }}
        </button>
      </div>
    </div>

    <p class="intro">
      Fusion fans a prompt out to a panel of <strong>diverse models</strong> in
      parallel, a <strong>judge</strong> extracts the structure of their answers,
      and a <strong>synthesizer</strong> writes the final answer. These presets
      power the agent's <code>fusion</code> tool and the pickable Fusion entries
      in the model list. Model ids are stored on the host only, never committed.
    </p>

    <p v-if="message" class="notice" :class="{ error: isError }">{{ message }}</p>

    <div v-if="loading" class="empty">Loading…</div>

    <div v-else>
      <!-- Auto Fusion routing -->
      <div class="card routing">
        <h3>Auto Fusion routing</h3>
        <p class="card-intro">
          When a session uses <strong>Auto Fusion</strong>, the agent grades each
          unit of work 1–5 and escalates to a Fusion preset once it hits the
          threshold. Below that, it answers on the cheaper base model.
        </p>
        <div class="routing-row">
          <div class="routing-field">
            <label class="field-label">Base model (cheap default)</label>
            <input
              v-model="baseModel"
              class="models-input"
              list="known-models"
              placeholder="claude-sonnet-4.6"
            />
          </div>
          <div class="routing-field">
            <label class="field-label">Escalation threshold: <strong>{{ autoThreshold }}/5</strong></label>
            <input type="range" min="1" max="5" step="1" v-model.number="autoThreshold" class="slider" />
            <div class="threshold-scale">
              <span v-for="n in 5" :key="n" :class="{ hot: n >= autoThreshold }">{{ n }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Complexity distribution graph -->
      <div class="card graph-card">
        <div class="graph-head">
          <h3>Complexity scores</h3>
          <select v-model="graphScope" @change="loadComplexity" class="scope-select">
            <option value="">All sessions</option>
            <option v-for="s in sessionIds" :key="s" :value="s">{{ s }}</option>
          </select>
        </div>
        <div v-if="!complexityScores.length" class="graph-empty">
          No complexity grades recorded yet.
        </div>
        <div v-else class="histogram">
          <div v-for="bar in histogram" :key="bar.score" class="hist-col">
            <div class="hist-bar-wrap">
              <span class="hist-count">{{ bar.count }}</span>
              <div class="hist-bar" :class="{ fusion: bar.score >= autoThreshold }"
                   :style="{ height: bar.pct + '%' }"></div>
            </div>
            <div class="hist-label">{{ bar.score }}</div>
          </div>
        </div>
        <p v-if="complexityScores.length" class="graph-foot">
          {{ complexityScores.length }} grades · mean {{ meanScore }} ·
          {{ fusionPct }}% escalated to Fusion
        </p>
      </div>

      <!-- Presets -->
      <h3 class="section-title">Presets</h3>
      <div v-for="(p, pi) in presets" :key="p._key" class="card preset" :class="{ disabled: !p.enabled }">
        <div class="preset-top">
          <input v-model="p.name" class="preset-name" placeholder="Preset name (e.g. Frontier)" />
          <label class="toggle">
            <input type="checkbox" v-model="p.enabled" />
            <span>{{ p.enabled ? 'Enabled' : 'Disabled' }}</span>
          </label>
          <button class="remove" title="Remove preset" @click="removePreset(pi)">✕</button>
        </div>

        <label class="field-label">Description</label>
        <input v-model="p.description" class="models-input" placeholder="When to use this preset…" />

        <label class="field-label">
          Participants — each is one panel member; list fallback models in order
        </label>
        <div v-for="(part, parti) in p.participants" :key="parti" class="participant-row">
          <input
            v-model="part.text"
            class="models-input"
            list="known-models"
            placeholder="claude-opus-4.8-max, claude-opus-4.8"
          />
          <button class="remove small" title="Remove participant" @click="removeParticipant(p, parti)">✕</button>
        </div>
        <button class="add small" @click="addParticipant(p)">＋ Add participant</button>

        <label class="field-label">Judge (priority order)</label>
        <input v-model="p.judgeText" class="models-input" list="known-models" placeholder="gpt-5.5, claude-opus-4.8" />

        <label class="field-label">Synthesizer (priority order)</label>
        <input v-model="p.synthText" class="models-input" list="known-models" placeholder="claude-opus-4.8-max" />
      </div>

      <datalist id="known-models">
        <option v-for="mid in knownModels" :key="mid" :value="mid" />
      </datalist>

      <button class="add" @click="addPreset">＋ Add preset</button>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api.js'

const presets = ref([])
const baseModel = ref('')
const autoThreshold = ref(4)
const knownModels = ref([])
const loading = ref(true)
const saving = ref(false)
const message = ref('')
const isError = ref(false)
let keyCounter = 0

const complexityScores = ref([])
const graphScope = ref('')
const sessionIds = ref([])

function listToText(list) {
  return (list || []).join(', ')
}
function textToList(text) {
  return (text || '').split(/[\n,]/).map((s) => s.trim()).filter(Boolean)
}

function toEditable(p) {
  return {
    _key: keyCounter++,
    id: p.id || '',
    name: p.name || '',
    description: p.description || '',
    enabled: p.enabled !== false,
    participants: (p.participants || []).map((grp) => ({ text: listToText(grp) })),
    judgeText: listToText(p.judge),
    synthText: listToText(p.synthesizer),
  }
}

function fromEditable(p) {
  return {
    id: p.id,
    name: p.name,
    description: p.description,
    enabled: p.enabled,
    participants: p.participants.map((part) => textToList(part.text)).filter((g) => g.length),
    judge: textToList(p.judgeText),
    synthesizer: textToList(p.synthText),
  }
}

async function load() {
  loading.value = true
  message.value = ''
  try {
    const data = await api.getFusion()
    presets.value = (data.presets || []).map(toEditable)
    baseModel.value = data.base_model || ''
    autoThreshold.value = data.auto_threshold || 4
  } catch (e) {
    isError.value = true
    message.value = `Failed to load fusion: ${e.message}`
  } finally {
    loading.value = false
  }
  try {
    const md = await api.getFusionModels()
    knownModels.value = md.models || []
  } catch { /* suggestions optional */ }
  loadComplexity()
}

async function loadComplexity() {
  try {
    const data = await api.getComplexityHistory(graphScope.value)
    complexityScores.value = data.scores || []
    if (!graphScope.value) {
      const ids = new Set()
      for (const s of complexityScores.value) if (s.session_id) ids.add(s.session_id)
      sessionIds.value = [...ids].sort()
    }
  } catch { /* graph optional */ }
}

const histogram = computed(() => {
  const counts = [0, 0, 0, 0, 0]
  for (const s of complexityScores.value) {
    const v = Math.round(Number(s.score))
    if (v >= 1 && v <= 5) counts[v - 1] += 1
  }
  const max = Math.max(1, ...counts)
  return counts.map((c, i) => ({ score: i + 1, count: c, pct: Math.round((c / max) * 100) }))
})

const meanScore = computed(() => {
  if (!complexityScores.value.length) return '0'
  const sum = complexityScores.value.reduce((a, s) => a + Number(s.score || 0), 0)
  return (sum / complexityScores.value.length).toFixed(1)
})

const fusionPct = computed(() => {
  if (!complexityScores.value.length) return 0
  const esc = complexityScores.value.filter((s) => Number(s.score) >= autoThreshold.value).length
  return Math.round((esc / complexityScores.value.length) * 100)
})

function addPreset() {
  presets.value.push(toEditable({ name: '', enabled: true, participants: [[], []] }))
}
function removePreset(i) { presets.value.splice(i, 1) }
function addParticipant(p) { p.participants.push({ text: '' }) }
function removeParticipant(p, i) { p.participants.splice(i, 1) }

async function save() {
  saving.value = true
  message.value = ''
  isError.value = false
  try {
    const doc = {
      presets: presets.value.map(fromEditable),
      base_model: baseModel.value.trim(),
      auto_threshold: autoThreshold.value,
    }
    const data = await api.updateFusion(doc)
    presets.value = (data.fusion?.presets || []).map(toEditable)
    baseModel.value = data.fusion?.base_model || ''
    autoThreshold.value = data.fusion?.auto_threshold || 4
    const pushed = data.pushed || 0
    message.value = `Saved. Applied to ${pushed} active session${pushed === 1 ? '' : 's'}.`
  } catch (e) {
    isError.value = true
    message.value = `Failed to save: ${e.message}`
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.fusion-view { max-width: 880px; }

.fusion-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
}
.fusion-header h2 { margin: 0; }
.header-actions { display: flex; gap: 0.5rem; }

.intro {
  color: var(--text-secondary);
  font-size: 0.9rem;
  line-height: 1.5;
  margin: 0.5rem 0 1rem;
}
.intro code {
  background: var(--bg-sidebar);
  padding: 0.1rem 0.3rem;
  border-radius: 4px;
}

.notice {
  font-size: 0.85rem;
  padding: 0.5rem 0.75rem;
  border-radius: var(--radius-sm, 4px);
  background: var(--bg-active);
  color: var(--text-primary);
}
.notice.error { background: #7f1d1d; color: #fff; }

.card {
  background: var(--bg-sidebar);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 1rem;
  margin-bottom: 1rem;
}
.card h3 { margin: 0 0 0.5rem; font-size: 1rem; }
.card-intro { color: var(--text-secondary); font-size: 0.82rem; margin: 0 0 0.75rem; line-height: 1.45; }

.section-title { margin: 1.25rem 0 0.75rem; }

.routing-row { display: flex; gap: 1.5rem; flex-wrap: wrap; }
.routing-field { flex: 1; min-width: 220px; }
.slider { width: 100%; }
.threshold-scale { display: flex; justify-content: space-between; font-size: 0.75rem; color: var(--text-secondary); margin-top: 0.2rem; }
.threshold-scale .hot { color: var(--accent); font-weight: 700; }

/* Histogram */
.graph-head { display: flex; align-items: center; justify-content: space-between; gap: 1rem; }
.scope-select {
  font-size: 0.8rem;
  padding: 0.3rem 0.5rem;
  background: var(--bg-main);
  color: var(--text-primary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  max-width: 240px;
}
.graph-empty { color: var(--text-secondary); font-size: 0.85rem; padding: 1rem 0; }
.histogram { display: flex; align-items: flex-end; gap: 0.75rem; height: 140px; padding: 0.5rem 0 0; }
.hist-col { flex: 1; display: flex; flex-direction: column; align-items: center; height: 100%; }
.hist-bar-wrap { flex: 1; width: 100%; display: flex; flex-direction: column; justify-content: flex-end; align-items: center; }
.hist-count { font-size: 0.7rem; color: var(--text-secondary); margin-bottom: 0.2rem; }
.hist-bar {
  width: 70%;
  min-height: 2px;
  background: var(--text-secondary);
  border-radius: 3px 3px 0 0;
  transition: height 0.2s;
}
.hist-bar.fusion { background: var(--accent, #7c5cff); }
.hist-label { font-size: 0.8rem; color: var(--text-primary); margin-top: 0.3rem; }
.graph-foot { font-size: 0.78rem; color: var(--text-secondary); margin: 0.6rem 0 0; }

/* Presets */
.card.preset.disabled { opacity: 0.6; }
.preset-top { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.5rem; }
.preset-name {
  flex: 1;
  font-size: 1rem;
  font-weight: 600;
  padding: 0.4rem 0.5rem;
  background: var(--bg-main);
  color: var(--text-primary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
}
.toggle { display: flex; align-items: center; gap: 0.35rem; font-size: 0.8rem; color: var(--text-secondary); white-space: nowrap; cursor: pointer; }

.participant-row { display: flex; gap: 0.5rem; align-items: center; margin-bottom: 0.4rem; }
.participant-row .models-input { flex: 1; }

.field-label { display: block; font-size: 0.75rem; color: var(--text-secondary); margin: 0.6rem 0 0.25rem; }

.models-input {
  width: 100%;
  box-sizing: border-box;
  font-size: 0.85rem;
  font-family: inherit;
  padding: 0.45rem 0.5rem;
  background: var(--bg-main);
  color: var(--text-primary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
}

.remove {
  flex-shrink: 0;
  width: 1.9rem;
  height: 1.9rem;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-main);
  color: var(--text-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  cursor: pointer;
}
.remove.small { width: 1.6rem; height: 1.6rem; font-size: 0.75rem; }
.remove:hover { color: #ef4444; border-color: #ef4444; }

.add {
  padding: 0.5rem 1rem;
  font-size: 0.9rem;
  background: var(--bg-sidebar);
  color: var(--text-primary);
  border: 1px dashed var(--border);
  border-radius: var(--radius-sm, 4px);
  cursor: pointer;
}
.add.small { padding: 0.3rem 0.7rem; font-size: 0.8rem; margin-bottom: 0.3rem; }
.add:hover { color: var(--accent); border-color: var(--accent); }

.empty { color: var(--text-secondary); padding: 2rem 0; }

button.primary {
  padding: 0.45rem 1rem;
  font-size: 0.85rem;
  background: var(--accent);
  color: #fff;
  border: 1px solid var(--accent);
  border-radius: var(--radius-sm, 4px);
  cursor: pointer;
}
button.secondary {
  padding: 0.45rem 1rem;
  font-size: 0.85rem;
  background: var(--bg-main);
  color: var(--text-primary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  cursor: pointer;
}
button:disabled { opacity: 0.6; cursor: not-allowed; }
</style>
