<template>
  <div class="panel-view">
    <div class="panel-header">
      <h2>Consult Panel</h2>
      <div class="header-actions">
        <button class="secondary" :disabled="loading || saving" @click="load">Reload</button>
        <button class="primary" :disabled="loading || saving" @click="save">
          {{ saving ? 'Saving…' : 'Save changes' }}
        </button>
      </div>
    </div>

    <p class="intro">
      These panelists power the agent's <code>consult_panel</code> tool. Edit their
      prompts and models, toggle them on or off, or add your own. Models are tried
      in order — the first one available to the session wins. Model ids you enter
      here are stored on the host only, never committed to the repository.
    </p>

    <p v-if="message" class="notice" :class="{ error: isError }">{{ message }}</p>

    <div v-if="loading" class="empty">Loading…</div>

    <div v-else>
      <div v-for="(m, i) in members" :key="m._key" class="card member" :class="{ disabled: !m.enabled }">
        <div class="member-top">
          <input v-model="m.name" class="member-name" placeholder="Panelist name (e.g. The Architect)" />
          <label class="toggle">
            <input type="checkbox" v-model="m.enabled" />
            <span>{{ m.enabled ? 'Enabled' : 'Disabled' }}</span>
          </label>
          <button class="remove" title="Remove panelist" @click="remove(i)">✕</button>
        </div>

        <label class="field-label">Voice — the character / attitude</label>
        <textarea v-model="m.voice" rows="3" placeholder="How this panelist thinks and speaks…"></textarea>

        <label class="field-label">Focus — what they look for</label>
        <textarea v-model="m.focus" rows="3" placeholder="The concerns this panelist surfaces…"></textarea>

        <label class="field-label">Models (in priority order, comma or newline separated)</label>
        <input
          v-model="m.modelsText"
          class="models-input"
          list="known-models"
          placeholder="claude-opus-4.8, gpt-5.5, …"
        />
      </div>

      <datalist id="known-models">
        <option v-for="mid in knownModels" :key="mid" :value="mid" />
      </datalist>

      <button class="add" @click="addMember">＋ Add panelist</button>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api.js'

const members = ref([])
const knownModels = ref([])
const loading = ref(true)
const saving = ref(false)
const message = ref('')
const isError = ref(false)
let keyCounter = 0

function toEditable(m) {
  return {
    _key: keyCounter++,
    id: m.id || '',
    name: m.name || '',
    voice: m.voice || '',
    focus: m.focus || '',
    enabled: m.enabled !== false,
    modelsText: (m.models || []).join(', '),
  }
}

function fromEditable(m) {
  return {
    id: m.id,
    name: m.name,
    voice: m.voice,
    focus: m.focus,
    enabled: m.enabled,
    models: m.modelsText
      .split(/[\n,]/)
      .map((s) => s.trim())
      .filter(Boolean),
  }
}

async function load() {
  loading.value = true
  message.value = ''
  try {
    const data = await api.getPanel()
    members.value = (data.members || []).map(toEditable)
  } catch (e) {
    isError.value = true
    message.value = `Failed to load panel: ${e.message}`
  } finally {
    loading.value = false
  }
  try {
    const md = await api.getPanelModels()
    knownModels.value = md.models || []
  } catch {
    /* suggestions are optional */
  }
}

function addMember() {
  members.value.push(toEditable({ name: '', enabled: true, models: [] }))
}

function remove(i) {
  members.value.splice(i, 1)
}

async function save() {
  saving.value = true
  message.value = ''
  isError.value = false
  try {
    const payload = members.value.map(fromEditable)
    const data = await api.updatePanel(payload)
    members.value = (data.panel?.members || []).map(toEditable)
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
.panel-view {
  max-width: 880px;
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
}

.panel-header h2 {
  margin: 0;
}

.header-actions {
  display: flex;
  gap: 0.5rem;
}

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

.notice.error {
  background: #7f1d1d;
  color: #fff;
}

.card.member {
  background: var(--bg-sidebar);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 1rem;
  margin-bottom: 1rem;
}

.card.member.disabled {
  opacity: 0.6;
}

.member-top {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-bottom: 0.75rem;
}

.member-name {
  flex: 1;
  font-size: 1rem;
  font-weight: 600;
  padding: 0.4rem 0.5rem;
  background: var(--bg-main);
  color: var(--text-primary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
}

.toggle {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.8rem;
  color: var(--text-secondary);
  white-space: nowrap;
  cursor: pointer;
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

.remove:hover {
  color: #ef4444;
  border-color: #ef4444;
}

.field-label {
  display: block;
  font-size: 0.75rem;
  color: var(--text-secondary);
  margin: 0.6rem 0 0.25rem;
}

textarea,
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
  resize: vertical;
}

.add {
  padding: 0.5rem 1rem;
  font-size: 0.9rem;
  background: var(--bg-sidebar);
  color: var(--text-primary);
  border: 1px dashed var(--border);
  border-radius: var(--radius-sm, 4px);
  cursor: pointer;
}

.add:hover {
  color: var(--accent);
  border-color: var(--accent);
}

.empty {
  color: var(--text-secondary);
  padding: 2rem 0;
}

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

button:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
</style>
