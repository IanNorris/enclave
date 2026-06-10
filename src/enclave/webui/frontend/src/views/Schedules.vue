<template>
  <div class="schedules-view">
    <div class="schedules-header">
      <h2>⏰ Schedules</h2>
      <div class="header-actions">
        <button class="secondary" :disabled="loading" @click="load">Reload</button>
      </div>
    </div>

    <p class="intro">
      Recurring tasks run by the orchestrator. A schedule can message an existing
      session, run on the always-on <strong>Concierge</strong>, or spawn a fresh
      worker session each time it fires. The minimum interval is 1 hour.
    </p>

    <p v-if="message" class="notice" :class="{ error: isError }">{{ message }}</p>

    <!-- Create form -->
    <div class="card create-card">
      <h3>New schedule</h3>
      <div class="form-row">
        <label>Runs on</label>
        <select v-model="form.target">
          <option value="session">A session</option>
          <option value="concierge">The Concierge</option>
          <option value="spawn">A fresh worker (spawn each time)</option>
        </select>
      </div>

      <div class="form-row" v-if="form.target === 'session'">
        <label>Session</label>
        <select v-model="form.session_id">
          <option value="">Select a session…</option>
          <option v-for="s in sessionOptions" :key="s.id" :value="s.id">
            {{ s.name }}
          </option>
        </select>
      </div>

      <div class="form-row">
        <label>Every</label>
        <div class="interval-input">
          <input type="number" min="1" v-model.number="form.intervalValue" />
          <select v-model="form.intervalUnit">
            <option value="3600">hours</option>
            <option value="86400">days</option>
          </select>
        </div>
      </div>

      <div class="form-row">
        <label>Task</label>
        <textarea v-model="form.reason" rows="2"
          placeholder="What should happen when this fires? (e.g. 'Summarise overnight activity')"></textarea>
      </div>

      <div class="form-row" v-if="form.target === 'spawn'">
        <label>Worker brief</label>
        <textarea v-model="form.spawn_brief" rows="2"
          placeholder="Initial instruction for the spawned worker session…"></textarea>
      </div>

      <div class="form-actions">
        <button class="primary" :disabled="creating || !canCreate" @click="create">
          {{ creating ? 'Creating…' : 'Create schedule' }}
        </button>
      </div>
    </div>

    <div v-if="loading" class="empty">Loading…</div>

    <div v-else>
      <h3 v-if="schedules.length">Recurring</h3>
      <div v-for="s in schedules" :key="s.id" class="card sched">
        <div class="sched-main">
          <div class="sched-reason">{{ s.reason }}</div>
          <div class="sched-meta">
            <span class="badge">{{ targetLabel(s) }}</span>
            <span>every {{ formatInterval(s.interval_seconds) }}</span>
            <span class="muted">next: {{ formatTime(s.next_fire) }}</span>
          </div>
          <div v-if="s.spawn_brief" class="sched-brief">brief: {{ s.spawn_brief }}</div>
        </div>
        <button class="remove" title="Cancel" @click="cancel(s.id)">✕</button>
      </div>

      <h3 v-if="timers.length">One-shot timers</h3>
      <div v-for="t in timers" :key="t.id" class="card sched">
        <div class="sched-main">
          <div class="sched-reason">{{ t.reason }}</div>
          <div class="sched-meta">
            <span class="badge">{{ targetLabel(t) }}</span>
            <span class="muted">fires: {{ formatTime(t.fire_at) }}</span>
          </div>
        </div>
        <button class="remove" title="Cancel" @click="cancel(t.id)">✕</button>
      </div>

      <p v-if="!schedules.length && !timers.length" class="empty">
        No schedules yet. Create one above.
      </p>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { api } from '../api.js'
import { useSessionStore } from '../stores/session.js'

const { sessions, loadSessions } = useSessionStore()

const schedules = ref([])
const timers = ref([])
const loading = ref(false)
const creating = ref(false)
const message = ref('')
const isError = ref(false)

const form = reactive({
  target: 'concierge',
  session_id: '',
  intervalValue: 1,
  intervalUnit: '86400',
  reason: '',
  spawn_brief: '',
})

const sessionOptions = computed(() =>
  sessions.value.filter(s => !s.archived && !s.concierge)
)

const canCreate = computed(() => {
  if (!form.reason.trim()) return false
  if (form.target === 'session' && !form.session_id) return false
  return true
})

function notify(msg, err = false) {
  message.value = msg
  isError.value = err
  if (!err) setTimeout(() => { message.value = '' }, 4000)
}

function targetLabel(s) {
  if (s.target === 'concierge') return '🛎️ Concierge'
  if (s.target === 'spawn') return '✨ Spawn worker'
  return `▶ ${s.session_name || s.session_id}`
}

function formatInterval(seconds) {
  if (seconds % 86400 === 0) {
    const d = seconds / 86400
    return d === 1 ? '1 day' : `${d} days`
  }
  const h = seconds / 3600
  return h === 1 ? '1 hour' : `${h} hours`
}

function formatTime(ts) {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString()
}

async function load() {
  loading.value = true
  try {
    const data = await api.getSchedules()
    schedules.value = data.schedules || []
    timers.value = data.timers || []
  } catch (e) {
    notify(`Failed to load schedules: ${e.message}`, true)
  } finally {
    loading.value = false
  }
}

async function create() {
  creating.value = true
  try {
    const payload = {
      target: form.target,
      session_id: form.target === 'session' ? form.session_id : '',
      reason: form.reason.trim(),
      interval_seconds: form.intervalValue * Number(form.intervalUnit),
      spawn_brief: form.target === 'spawn' ? form.spawn_brief.trim() : '',
    }
    await api.createSchedule(payload)
    notify('Schedule created.')
    form.reason = ''
    form.spawn_brief = ''
    await load()
  } catch (e) {
    notify(`Failed to create schedule: ${e.message}`, true)
  } finally {
    creating.value = false
  }
}

async function cancel(id) {
  if (!confirm('Cancel this schedule?')) return
  try {
    await api.cancelSchedule(id)
    await load()
  } catch (e) {
    notify(`Failed to cancel: ${e.message}`, true)
  }
}

onMounted(async () => {
  await loadSessions()
  await load()
})
</script>

<style scoped>
.schedules-view { max-width: 800px; margin: 0 auto; padding: 1rem; }
.schedules-header { display: flex; justify-content: space-between; align-items: center; }
.schedules-header h2 { margin: 0; }
.intro { color: var(--text-muted, #888); font-size: 0.9rem; }
.notice { padding: 0.5rem 0.75rem; border-radius: 6px; background: #2a3a2a; }
.notice.error { background: #3a2a2a; }
.card { background: var(--card-bg, #1e1e24); border: 1px solid var(--border, #333); border-radius: 8px; padding: 1rem; margin-bottom: 0.75rem; }
.create-card h3 { margin-top: 0; }
.form-row { display: flex; flex-direction: column; gap: 0.25rem; margin-bottom: 0.75rem; }
.form-row label { font-size: 0.85rem; color: var(--text-muted, #999); }
.form-row select, .form-row input, .form-row textarea { width: 100%; padding: 0.4rem; border-radius: 6px; border: 1px solid var(--border, #444); background: var(--input-bg, #15151a); color: inherit; }
.interval-input { display: flex; gap: 0.5rem; }
.interval-input input { width: 5rem; }
.form-actions { text-align: right; }
.sched { display: flex; justify-content: space-between; align-items: flex-start; gap: 0.5rem; }
.sched-main { flex: 1; }
.sched-reason { font-weight: 500; }
.sched-meta { display: flex; gap: 0.75rem; flex-wrap: wrap; font-size: 0.85rem; margin-top: 0.25rem; }
.sched-brief { font-size: 0.8rem; color: var(--text-muted, #888); margin-top: 0.25rem; }
.badge { background: #2a2a3a; padding: 0.1rem 0.4rem; border-radius: 4px; }
.muted { color: var(--text-muted, #888); }
.remove { background: transparent; border: none; color: #c66; cursor: pointer; font-size: 1.1rem; }
.empty { color: var(--text-muted, #888); padding: 1rem 0; }
button.primary { background: var(--accent, #4a7); color: #fff; border: none; padding: 0.4rem 0.9rem; border-radius: 6px; cursor: pointer; }
button.secondary { background: transparent; border: 1px solid var(--border, #444); color: inherit; padding: 0.4rem 0.9rem; border-radius: 6px; cursor: pointer; }
button:disabled { opacity: 0.5; cursor: default; }
</style>
