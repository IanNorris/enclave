<template>
  <div class="timeline-view">
    <div class="timeline-header">
      <h2>📅 Timeline</h2>
      <div class="breadcrumb" v-if="breadcrumb.length">
        <span
          v-for="(crumb, i) in breadcrumb"
          :key="i"
          class="crumb"
          :class="{ clickable: i < breadcrumb.length - 1 }"
          @click="navigateTo(crumb)"
        >
          {{ crumb.label }}
          <span v-if="i < breadcrumb.length - 1" class="sep">›</span>
        </span>
      </div>
    </div>

    <div v-if="loading" class="loading">Loading…</div>
    <div v-else-if="error" class="error card">{{ error }}</div>
    <div v-else-if="!sessionId" class="empty card">
      <p>Select a session from the sidebar to view its timeline.</p>
    </div>

    <!-- Level 1: Day cards -->
    <div v-else-if="level === 'days'" class="day-list">
      <div v-if="!days.length" class="empty card">No activity recorded yet.</div>
      <div
        v-for="day in days"
        :key="day.date"
        class="day-card card"
        @click="drillToDay(day.date)"
      >
        <div class="day-date">
          <span class="day-name">{{ formatDayName(day.date) }}</span>
          <span class="day-full">{{ day.date }}</span>
        </div>
        <div class="day-stats">
          <span class="stat" v-if="day.event_counts.user_message">
            💬 {{ day.event_counts.user_message }} messages
          </span>
          <span class="stat" v-if="day.event_counts.response">
            🤖 {{ day.event_counts.response }} responses
          </span>
          <span class="stat" v-if="day.event_counts.tool_start">
            🔧 {{ day.event_counts.tool_start }} tool calls
          </span>
          <span class="stat" v-if="day.event_counts.file_send">
            📁 {{ day.event_counts.file_send }} files
          </span>
          <span class="stat total">{{ day.total }} events</span>
        </div>
        <div class="day-time" v-if="day.first_activity">
          {{ formatTime(day.first_activity) }} – {{ formatTime(day.last_activity) }}
        </div>
      </div>
    </div>

    <!-- Level 2: Hour breakdown -->
    <div v-else-if="level === 'hours'" class="hour-list">
      <div v-if="!hours.length" class="empty card">No activity on this date.</div>
      <div
        v-for="hour in hours"
        :key="hour.hour"
        class="hour-card card"
        @click="drillToHour(selectedDate, hour.hour)"
      >
        <div class="hour-label">
          <span class="hour-time">{{ hour.hour }}:00 – {{ hour.hour }}:59</span>
          <span class="hour-total">{{ hour.total }} events</span>
        </div>
        <div class="hour-bar">
          <div class="bar-segment tool" :style="barStyle(hour, 'tool_start')" title="Tool calls"></div>
          <div class="bar-segment response" :style="barStyle(hour, 'response')" title="Responses"></div>
          <div class="bar-segment user" :style="barStyle(hour, 'user_message')" title="User messages"></div>
        </div>
        <div class="hour-highlights" v-if="hour.highlights && hour.highlights.length">
          <div
            v-for="(h, i) in hour.highlights.slice(0, 3)"
            :key="i"
            class="highlight"
          >
            <span class="highlight-icon">{{ eventIcon(h.type) }}</span>
            <span class="highlight-time">{{ formatTime(h.timestamp) }}</span>
            <span class="highlight-text">{{ h.preview }}</span>
          </div>
          <div v-if="hour.highlights.length > 3" class="more-highlights">
            +{{ hour.highlights.length - 3 }} more
          </div>
        </div>
      </div>
    </div>

    <!-- Level 3: Event stream -->
    <div v-else-if="level === 'events'" class="event-stream">
      <div v-if="!events.length" class="empty card">No events in this period.</div>
      <div
        v-for="event in events"
        :key="event.id"
        class="event-item"
        :class="'event-' + event.type"
      >
        <div class="event-time">{{ formatTime(event.timestamp) }}</div>
        <div class="event-icon">{{ eventIcon(event.type) }}</div>
        <div class="event-body">
          <div class="event-type-label">{{ eventLabel(event.type) }}</div>
          <div class="event-content" v-if="eventContent(event)">
            {{ eventContent(event) }}
          </div>
          <div class="event-detail" v-if="eventDetail(event)">
            <code>{{ eventDetail(event) }}</code>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { useSessionStore } from '../stores/session.js'
import { api } from '../api.js'

const { selectedSessionId } = useSessionStore()
const sessionId = computed(() => selectedSessionId.value)

const loading = ref(false)
const error = ref('')
const level = ref('days')
const days = ref([])
const hours = ref([])
const events = ref([])
const selectedDate = ref('')
const selectedHour = ref('')
const maxHourTotal = ref(1)

const breadcrumb = computed(() => {
  const crumbs = [{ label: 'All days', level: 'days', date: null }]
  if (level.value === 'hours' || level.value === 'events') {
    crumbs.push({ label: selectedDate.value, level: 'hours', date: selectedDate.value })
  }
  if (level.value === 'events') {
    crumbs.push({ label: `${selectedHour.value}:00`, level: 'events', date: `${selectedDate.value}T${selectedHour.value}` })
  }
  return crumbs
})

function navigateTo(crumb) {
  if (crumb.level === 'days') {
    loadDays()
  } else if (crumb.level === 'hours') {
    drillToDay(crumb.date)
  }
}

async function loadDays() {
  if (!sessionId.value) return
  loading.value = true
  error.value = ''
  level.value = 'days'
  selectedDate.value = ''
  selectedHour.value = ''
  try {
    const data = await api.getTimeline(sessionId.value)
    days.value = data.days || []
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function drillToDay(date) {
  if (!sessionId.value) return
  loading.value = true
  error.value = ''
  level.value = 'hours'
  selectedDate.value = date
  selectedHour.value = ''
  try {
    const data = await api.getTimeline(sessionId.value, date)
    hours.value = data.hours || []
    maxHourTotal.value = Math.max(1, ...hours.value.map(h => h.total))
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function drillToHour(date, hour) {
  if (!sessionId.value) return
  loading.value = true
  error.value = ''
  level.value = 'events'
  selectedDate.value = date
  selectedHour.value = hour
  try {
    const data = await api.getTimeline(sessionId.value, `${date}T${hour}`)
    events.value = data.events || []
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

function barStyle(hour, type) {
  const count = hour.event_counts[type] || 0
  const pct = (count / maxHourTotal.value) * 100
  return { width: `${pct}%` }
}

function formatDayName(dateStr) {
  try {
    const d = new Date(dateStr + 'T12:00:00')
    return d.toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'short' })
  } catch { return dateStr }
}

function formatTime(ts) {
  if (!ts) return ''
  return ts.substring(11, 19)
}

function eventIcon(type) {
  const icons = {
    user_message: '💬',
    response: '🤖',
    tool_start: '🔧',
    tool_complete: '✅',
    file_send: '📁',
    ask_user: '❓',
    ask_user_response: '💬',
    structured_response: '📋',
    turn_start: '▶️',
    turn_end: '⏹️',
    thinking: '🧠',
  }
  return icons[type] || '•'
}

function eventLabel(type) {
  const labels = {
    user_message: 'User message',
    response: 'Agent response',
    tool_start: 'Tool call',
    tool_complete: 'Tool result',
    file_send: 'File sent',
    ask_user: 'Question',
    ask_user_response: 'Answer',
    structured_response: 'Structured response',
    turn_start: 'Turn started',
    turn_end: 'Turn ended',
    thinking: 'Thinking',
  }
  return labels[type] || type
}

function eventContent(event) {
  const d = event.data || {}
  if (event.type === 'user_message') return d.content?.substring(0, 300)
  if (event.type === 'response') return d.content?.substring(0, 300)
  if (event.type === 'ask_user') return d.question
  if (event.type === 'structured_response') return d.summary || d.title
  if (event.type === 'file_send') return d.filename || d.file_path
  return null
}

function eventDetail(event) {
  const d = event.data || {}
  if (event.type === 'tool_start') return d.name ? `${d.name}${d.detail ? ': ' + d.detail.substring(0, 120) : ''}` : null
  if (event.type === 'tool_complete') return d.name || null
  return null
}

watch(sessionId, () => { loadDays() })
onMounted(() => { if (sessionId.value) loadDays() })
</script>

<style scoped>
.timeline-view {
  max-width: 900px;
  margin: 0 auto;
}

.timeline-header {
  display: flex;
  align-items: baseline;
  gap: 1rem;
  margin-bottom: 1.5rem;
  flex-wrap: wrap;
}

.timeline-header h2 {
  margin: 0;
  font-size: 1.5rem;
}

.breadcrumb {
  display: flex;
  align-items: center;
  gap: 0.25rem;
  font-size: 0.9rem;
  color: var(--text-secondary);
}

.crumb.clickable {
  cursor: pointer;
  color: var(--accent);
}

.crumb.clickable:hover {
  text-decoration: underline;
}

.sep {
  margin: 0 0.25rem;
  color: var(--text-tertiary, #666);
}

.loading {
  color: var(--text-secondary);
  padding: 2rem;
  text-align: center;
}

.empty {
  padding: 2rem;
  text-align: center;
  color: var(--text-secondary);
}

.error {
  color: #ef4444;
  padding: 1rem;
}

/* Day cards */
.day-list {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.day-card {
  padding: 1rem 1.25rem;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.day-card:hover {
  background: var(--bg-hover);
  border-color: var(--accent);
}

.day-date {
  display: flex;
  align-items: baseline;
  gap: 0.75rem;
}

.day-name {
  font-weight: 600;
  font-size: 1.1rem;
  color: var(--text-primary);
}

.day-full {
  font-size: 0.8rem;
  color: var(--text-tertiary, #888);
}

.day-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem 1rem;
}

.stat {
  font-size: 0.85rem;
  color: var(--text-secondary);
}

.stat.total {
  margin-left: auto;
  font-weight: 500;
  color: var(--text-tertiary, #888);
}

.day-time {
  font-size: 0.8rem;
  color: var(--text-tertiary, #888);
}

/* Hour cards */
.hour-list {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.hour-card {
  padding: 0.75rem 1rem;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
}

.hour-card:hover {
  background: var(--bg-hover);
  border-color: var(--accent);
}

.hour-label {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.4rem;
}

.hour-time {
  font-weight: 500;
  font-size: 0.95rem;
}

.hour-total {
  font-size: 0.8rem;
  color: var(--text-tertiary, #888);
}

.hour-bar {
  display: flex;
  height: 6px;
  border-radius: 3px;
  background: var(--bg-main);
  overflow: hidden;
  margin-bottom: 0.5rem;
}

.bar-segment {
  height: 100%;
  min-width: 0;
  transition: width 0.3s ease;
}

.bar-segment.tool {
  background: #6366f1;
}

.bar-segment.response {
  background: #22c55e;
}

.bar-segment.user {
  background: #3b82f6;
}

.hour-highlights {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.highlight {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  font-size: 0.8rem;
  color: var(--text-secondary);
  overflow: hidden;
}

.highlight-icon {
  flex-shrink: 0;
}

.highlight-time {
  flex-shrink: 0;
  font-family: monospace;
  font-size: 0.75rem;
  color: var(--text-tertiary, #888);
}

.highlight-text {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.more-highlights {
  font-size: 0.75rem;
  color: var(--text-tertiary, #888);
  padding-left: 1.5rem;
}

/* Event stream */
.event-stream {
  display: flex;
  flex-direction: column;
}

.event-item {
  display: grid;
  grid-template-columns: 70px 28px 1fr;
  gap: 0.5rem;
  padding: 0.5rem 0;
  border-bottom: 1px solid var(--border);
  align-items: start;
}

.event-item:last-child {
  border-bottom: none;
}

.event-time {
  font-family: monospace;
  font-size: 0.78rem;
  color: var(--text-tertiary, #888);
  padding-top: 0.15rem;
}

.event-icon {
  text-align: center;
  font-size: 0.95rem;
}

.event-body {
  min-width: 0;
}

.event-type-label {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.03em;
  margin-bottom: 0.15rem;
}

.event-content {
  font-size: 0.88rem;
  color: var(--text-primary);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 120px;
  overflow: hidden;
}

.event-detail code {
  font-size: 0.8rem;
  color: var(--text-secondary);
  background: var(--bg-code, rgba(255,255,255,0.05));
  padding: 0.1rem 0.3rem;
  border-radius: 3px;
  word-break: break-all;
}

/* Subtle coloring by event type */
.event-tool_start { opacity: 0.7; }
.event-tool_complete { opacity: 0.6; }
.event-turn_start, .event-turn_end { opacity: 0.5; }

.event-user_message .event-content {
  color: var(--accent, #3b82f6);
}

.event-response .event-content {
  color: var(--text-primary);
}
</style>
