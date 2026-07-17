<template>
  <div class="chat-view" @dragover.prevent="onDragOver" @dragleave="onDragLeave" @drop.prevent="onDrop">
    <div v-if="selectedSession" class="chat-body" :class="bodyClass">
      <div class="chat-container" :style="chatPaneStyle">
      <!-- Floating agent status (non-scrolling) -->
      <div v-if="agentState !== 'unknown'" class="agent-status-float" :class="agentStateClass">
        <span class="status-indicator"></span>
        <span class="status-label">{{ agentStateLabel }}</span>
      </div>

      <!-- Artifacts grip handle (right edge on desktop, top edge on mobile) -->
      <div class="doc-tools" :class="{ 'mobile-portrait': isMobilePortrait }">
        <button
          v-if="docPanelOpen && !isMobilePortrait"
          class="doc-orient-btn"
          :title="outerOrientation === 'horizontal' ? 'Stack vertically' : 'Place side by side'"
          @click="toggleOuterOrientation"
        >{{ outerOrientation === 'horizontal' ? '⬍' : '⬌' }}</button>
        <button
          class="doc-grip"
          :class="{ open: docPanelOpen }"
          :title="docPanelOpen ? 'Close document panel' : 'Open documents'"
          @click="toggleDocPanel"
        ><span class="grip-dots"></span></button>
      </div>

      <!-- Drop overlay -->
      <div v-if="dragging" class="drop-overlay">
        <div class="drop-label">Drop files to attach</div>
      </div>

      <!-- Messages -->
      <div class="messages" ref="messagesEl" @scroll="onMessagesScroll" @click="onMessagesClick" @mouseover="showBugHover" @mouseout="hideBugHover">
        <!-- Load earlier button -->
        <div v-if="hasMore" class="load-earlier">
          <button class="secondary" @click="loadEarlier" :disabled="loadingMore">
            {{ loadingMore ? 'Loading…' : '↑ Load earlier messages' }}
          </button>
        </div>

        <!-- Completed turns from SQLite -->
        <div v-for="(turn, idx) in turns" :key="turn.turn_index ?? `m-${idx}`" class="turn">
          <div v-if="turn.user_message || (turn.user_images && turn.user_images.length)" class="message user-message">
            <div class="message-meta">
              <span class="sender">User</span>
              <span v-if="turn.source === 'queued'" class="queued-badge">queued</span>
              <span class="time">{{ formatTime(turn.timestamp) }}</span>
            </div>
            <div v-if="turn.user_images && turn.user_images.length" class="user-images">
              <img v-for="(img, ii) in turn.user_images" :key="ii"
                   :src="userImageUrl(img)"
                   class="user-img clickable-img" @click="openLightbox(userImageUrl(img))" />
            </div>
            <div v-if="turn.user_message" class="message-body" v-html="renderMarkdown(turn.user_message)"></div>
          </div>

          <!-- Persisted event segments for this turn (tool calls interleaved with responses) -->
          <template v-if="turnEvents[turn.turn_index]?.length">
            <!-- File attachments always visible (not collapsed with tool calls) -->
            <template v-for="(seg, si) in turnEvents[turn.turn_index]" :key="'fs-'+si">
              <template v-for="(evt, ei) in seg.tools" :key="'fs-'+si+'-'+ei">
                <div v-if="evt.type === 'file_send'" class="file-send-block">
                  <div class="file-send-label">
                    <span class="event-icon">📎</span>
                    <span>{{ evt.data?.filename || 'file' }}</span>
                  </div>
                  <div v-if="evt.data?.mimetype?.startsWith('image/')" class="file-send-preview">
                    <img v-if="evt.data?.file_path" :src="workspaceFileUrl(evt.data.file_path)" class="file-send-img clickable-img" @click="openLightbox(workspaceFileUrl(evt.data.file_path))" />
                    <img v-else-if="evt.data?.mxc_url" :src="mediaUrl(evt.data.mxc_url)" class="file-send-img clickable-img" @click="openLightbox(mediaUrl(evt.data.mxc_url))" />
                  </div>
                </div>
              </template>
            </template>

            <div class="turn-events" :class="{ collapsed: !expandedTurns[turn.turn_index] }">
              <div class="events-toggle" @click="expandedTurns[turn.turn_index] = !expandedTurns[turn.turn_index]">
                <span class="expand-toggle">{{ expandedTurns[turn.turn_index] ? '▼' : '▶' }}</span>
                <span class="events-summary">{{ countSegmentEvents(turnEvents[turn.turn_index]) }} events</span>
              </div>
              <template v-if="expandedTurns[turn.turn_index]">
                <template v-for="(seg, si) in turnEvents[turn.turn_index]" :key="si">
                  <!-- Tool calls in this segment -->
                  <div v-for="(evt, ei) in seg.tools" :key="`${si}-${ei}`" class="live-event" :class="evt.type">
                    <div v-if="evt.type === 'thinking'" class="thinking-block collapsed">
                      <div class="event-header" @click="evt.expanded = !evt.expanded">
                        <span class="event-icon">🤔</span>
                        <span class="event-label">Thinking</span>
                        <span class="expand-toggle">{{ evt.expanded ? '▼' : '▶' }}</span>
                      </div>
                      <div v-if="evt.expanded" class="event-content thinking-content">{{ evt.data?.content }}</div>
                    </div>
                    <div v-else-if="evt.type === 'tool_start' || evt.type === 'tool_complete'" class="tool-block collapsed">
                      <div class="event-header">
                        <span class="event-icon">{{ TOOL_ICONS_MAP[evt.data?.name] || '🔧' }}</span>
                        <span class="event-label">{{ evt.data?.detail || evt.data?.name || 'tool' }}</span>
                        <span v-if="evt.type === 'tool_complete'" class="tool-status" :class="evt.data?.success !== false ? 'success' : 'fail'">
                          {{ evt.data?.success !== false ? '✅' : '❌' }}
                        </span>
                      </div>
                    </div>
                  </div>
                  <!-- Intermediate response after this batch of tool calls -->
                  <div v-if="seg.response" class="message assistant-message segment-response">
                    <div class="message-meta">
                      <span class="sender">Agent</span>
                      <span v-if="seg.responseTimestamp" class="time">{{ formatTime(seg.responseTimestamp) }}</span>
                    </div>
                    <div class="message-body" v-html="renderMarkdown(seg.response)"></div>
                  </div>
                </template>
              </template>
            </div>
          </template>

          <!-- Structured response card -->
          <div v-if="turn.structured" class="message assistant-message structured-card major-response">
            <div class="structured-title" v-if="turn.structured.title">{{ turn.structured.title }}</div>
            <div class="structured-summary" v-html="renderMarkdown(turn.structured.summary)"></div>
            <div v-if="turn.structured.images?.length" class="structured-images">
              <img v-for="(img, ii) in turn.structured.images" :key="ii"
                   :src="workspaceFileUrl(img)"
                   class="structured-img clickable-img" @click="openLightbox(workspaceFileUrl(img))" />
            </div>
            <details v-if="turn.structured.details" class="structured-details">
              <summary>Details</summary>
              <div class="structured-details-body" v-html="renderMarkdown(turn.structured.details)"></div>
              <div class="structured-details-actions">
                <button class="btn-sm" @click="downloadMarkdown(turn.structured)">📥 Download as Markdown</button>
              </div>
            </details>
            <div v-if="turn.structured.actions?.length" class="structured-actions">
              <div v-for="(action, ai) in turn.structured.actions" :key="ai" class="structured-action">
                <img v-if="action.image"
                     :src="workspaceFileUrl(action.image)"
                     class="action-img clickable-img" @click="openLightbox(workspaceFileUrl(action.image))" />
                <button class="action-btn" @click="sendActionReply(action.label)">{{ action.label }}</button>
              </div>
            </div>
            <!-- Decision fork menu: stacked independent decisions, batched submit -->
            <div v-if="turn.structured.decisions?.length" class="decision-menu">
              <div v-for="(dec, di) in turn.structured.decisions" :key="dec.id || di" class="decision">
                <div class="decision-q">{{ dec.question }}</div>
                <div v-if="dec.options?.length" class="decision-opts">
                  <button
                    v-for="(opt, oi) in dec.options" :key="opt.id || oi"
                    class="decision-opt"
                    :class="{ selected: decisionAnswers[decKey(turn)][dec.id]?.selected === (opt.id || opt.label) }"
                    @click="pickOption(turn, dec, opt)"
                  >{{ opt.label }}</button>
                </div>
                <input
                  v-if="dec.allowFreeText"
                  class="decision-freetext"
                  :placeholder="dec.options?.length ? 'or comment…' : 'your answer…'"
                  :value="decisionAnswers[decKey(turn)][dec.id]?.comment || ''"
                  @input="setComment(turn, dec, $event.target.value)"
                />
              </div>
              <button
                class="decision-submit"
                :disabled="decisionSubmitted[decKey(turn)] || !decisionAnyAnswered(turn)"
                @click="submitDecisions(turn)"
              >{{ decisionSubmitted[decKey(turn)] ? 'Submitted ✓' : 'Submit decisions' }}</button>
            </div>
            <div class="message-meta">
              <span class="sender">Agent</span>
              <span class="time">{{ formatTime(turn.timestamp) }}</span>
              <ComplexityBadge
                v-if="turnComplexity(turn)"
                class="meta-cx"
                :score="turnComplexity(turn).score"
                :tier="turnComplexity(turn).tier"
                :reason="turnComplexity(turn).reason"
              />
            </div>
          </div>

          <!-- Regular response (non-structured) -->
          <div v-else-if="turn.assistant_response" class="message assistant-message" :class="{ 'major-response': turn.is_major || (!turn.user_message && turn.assistant_response) }">
            <div class="message-meta">
              <span class="sender">Agent</span>
              <span class="time">{{ formatTime(turn.timestamp) }}</span>
              <ComplexityBadge
                v-if="turnComplexity(turn)"
                class="meta-cx"
                :score="turnComplexity(turn).score"
                :tier="turnComplexity(turn).tier"
                :reason="turnComplexity(turn).reason"
              />
            </div>
            <div class="message-body" v-html="renderMarkdown(turn.assistant_response)"></div>
          </div>

          <!-- Auto Fusion model traces for this turn: tap to see each model's
               outcome + the judge's decision that produced the answer. -->
          <div
            v-for="(run, ri) in turnFusions(turn)"
            :key="'fz-' + ri"
            class="fusion-block card-fusion"
          >
            <div class="event-header" @click="toggleCardFusion(turn.turn_index, ri)">
              <span class="event-icon">⚡</span>
              <span class="event-label">Fusion · {{ run.preset }}</span>
              <span class="fusion-models">{{ (run.models || []).join(' + ') }}</span>
              <span class="expand-toggle">{{ isCardFusionOpen(turn.turn_index, ri) ? '▼' : '▶' }}</span>
            </div>
            <div v-if="isCardFusionOpen(turn.turn_index, ri)" class="fusion-trace">
              <div class="fusion-trace-meta">
                judge: {{ run.judge_model }} · synthesizer: {{ run.synthesizer_model }}
              </div>
              <details class="fusion-section" open>
                <summary>Judge analysis &amp; decision</summary>
                <div class="fusion-content" v-html="renderMarkdown(run.judge_analysis)"></div>
              </details>
              <details v-for="(p, pi) in run.participants" :key="pi" class="fusion-section">
                <summary>{{ p.model }}</summary>
                <div class="fusion-content" v-html="renderMarkdown(p.response)"></div>
              </details>
            </div>
          </div>
        </div>

        <!-- Live streaming section -->
        <div v-if="liveEvents.length || streamingText" class="turn live-turn">
          <!-- Collapsed tool summary when many events -->
          <div v-if="collapsedLiveCount > 0" class="live-events-collapsed">
            <span class="collapsed-summary" @click="liveEventsExpanded = !liveEventsExpanded">
              {{ liveEventsExpanded ? '▼' : '▶' }} {{ collapsedLiveCount }} earlier tool calls
            </span>
            <template v-if="liveEventsExpanded">
              <div v-for="(evt, i) in collapsedLiveEvents" :key="'c-'+i" class="live-event" :class="evt.type">
                <div v-if="evt.type === 'thinking'" class="thinking-block collapsed">
                  <div class="event-header" @click="evt.collapsed = !evt.collapsed">
                    <span class="event-icon">🤔</span>
                    <span class="event-label">Thinking</span>
                    <span class="expand-toggle">{{ evt.collapsed ? '▶' : '▼' }}</span>
                  </div>
                  <div v-if="!evt.collapsed" class="event-content thinking-content">{{ evt.content }}</div>
                </div>
                <div v-if="evt.type === 'tool'" class="tool-block collapsed">
                  <div class="event-header" @click="evt.collapsed = !evt.collapsed">
                    <span class="event-icon">{{ evt.icon }}</span>
                    <span class="event-label">{{ evt.detail || evt.name }}</span>
                    <span v-if="evt.done" class="tool-status" :class="evt.success ? 'success' : 'fail'">
                      {{ evt.success ? '✅' : '❌' }}
                    </span>
                    <span v-else class="tool-spinner">⏳</span>
                  </div>
                </div>
                <div v-if="evt.type === 'file_send'" class="tool-block">
                  <div class="event-header">
                    <span class="event-icon">📎</span>
                    <span class="event-label">{{ evt.filename }}</span>
                  </div>
                </div>
              </div>
            </template>
          </div>

          <!-- Visible (recent) events -->
          <div v-for="(evt, i) in visibleLiveEvents" :key="'v-'+i" class="live-event" :class="evt.type">
            <!-- Thinking block -->
            <div v-if="evt.type === 'thinking'" class="thinking-block" :class="{ collapsed: evt.collapsed }">
              <div class="event-header" @click="evt.collapsed = !evt.collapsed">
                <span class="event-icon">🤔</span>
                <span class="event-label">Thinking</span>
                <span class="expand-toggle">{{ evt.collapsed ? '▶' : '▼' }}</span>
              </div>
              <div v-if="!evt.collapsed" class="event-content thinking-content">{{ evt.content }}</div>
            </div>

            <!-- Tool call -->
            <div v-if="evt.type === 'tool'" class="tool-block" :class="{ collapsed: evt.collapsed }">
              <div class="event-header" @click="evt.collapsed = !evt.collapsed">
                <span class="event-icon">{{ evt.icon }}</span>
                <span class="event-label">{{ evt.detail || evt.name }}</span>
                <span v-if="evt.done" class="tool-status" :class="evt.success ? 'success' : 'fail'">
                  {{ evt.success ? '✅' : '❌' }}
                </span>
                <span v-else class="tool-spinner">⏳</span>
                <span class="expand-toggle">{{ evt.collapsed ? '▶' : '▼' }}</span>
              </div>
            </div>

            <!-- File sent by agent -->
            <div v-if="evt.type === 'file_send'" class="tool-block">
              <div class="event-header">
                <span class="event-icon">📎</span>
                <span class="event-label">{{ evt.filename }}</span>
              </div>
              <div v-if="evt.mimetype?.startsWith('image/')" class="file-send-preview">
                <img v-if="evt.filePath" :src="workspaceFileUrl(evt.filePath)" class="file-send-img clickable-img" @click="openLightbox(workspaceFileUrl(evt.filePath))" />
                <img v-else-if="evt.mxcUrl" :src="mediaUrl(evt.mxcUrl)" class="file-send-img clickable-img" @click="openLightbox(mediaUrl(evt.mxcUrl))" />
              </div>
            </div>

            <!-- Fusion run: model combo + tappable trace -->
            <div v-if="evt.type === 'fusion'" class="fusion-block">
              <div class="event-header" @click="evt.expanded = !evt.expanded">
                <span class="event-icon">⚡</span>
                <span class="event-label">Fusion · {{ evt.preset }}</span>
                <span class="fusion-models">{{ (evt.models || []).join(' + ') }}</span>
                <span class="expand-toggle">{{ evt.expanded ? '▼' : '▶' }}</span>
              </div>
              <div v-if="evt.expanded" class="fusion-trace">
                <div class="fusion-trace-meta">
                  judge: {{ evt.judge_model }} · synthesizer: {{ evt.synthesizer_model }}
                </div>
                <details class="fusion-section" open>
                  <summary>Judge analysis</summary>
                  <div class="fusion-content" v-html="renderMarkdown(evt.judge_analysis)"></div>
                </details>
                <details v-for="(p, pi) in evt.participants" :key="pi" class="fusion-section">
                  <summary>{{ p.model }}</summary>
                  <div class="fusion-content" v-html="renderMarkdown(p.response)"></div>
                </details>
              </div>
            </div>
          </div>

          <!-- Streaming text with cursor -->
          <div v-if="streamingText" class="message assistant-message streaming">
            <div class="message-meta">
              <span class="sender">Agent</span>
              <span class="streaming-badge">streaming</span>
            </div>
            <div class="message-body" v-html="renderMarkdown(streamingText + ' ▍')"></div>
          </div>
        </div>

        <!-- Activity status -->
        <div v-if="activityText" class="activity-line">
          {{ activityText }}
        </div>

        <!-- Ask user prompt -->
        <div v-if="askUserPrompt" class="message assistant-message ask-user-block">
          <div class="message-meta">
            <span class="sender">Agent</span>
            <span class="ask-badge">question</span>
          </div>
          <div class="ask-question">{{ askUserPrompt.question }}</div>
          <div v-if="askUserPrompt.choices.length" class="ask-choices">
            <button
              v-for="(choice, i) in askUserPrompt.choices"
              :key="i"
              class="secondary ask-choice-btn"
              @click="answerAskUser(choice)"
            >{{ choice }}</button>
          </div>
          <div class="ask-freeform">
            <input
              v-model="askUserAnswer"
              placeholder="Type your answer…"
              @keydown.enter.prevent="answerAskUser(askUserAnswer)"
            />
            <button class="primary" @click="answerAskUser(askUserAnswer)" :disabled="!askUserAnswer.trim()">Reply</button>
          </div>
        </div>

        <!-- Agent permission requests (host-mode approvals) -->
        <div v-for="req in permissionRequests" :key="req.request_id" class="message assistant-message permission-block">
          <div class="message-meta">
            <span class="sender">Agent</span>
            <span class="perm-badge">permission</span>
          </div>
          <div class="perm-target">
            <span class="perm-icon">{{ permTypeIcon(req.perm_type) }}</span>
            <span class="perm-type">{{ req.perm_type }}</span>
            <code class="perm-code">{{ req.target }}</code>
          </div>
          <div v-if="req.reason" class="perm-reason">💬 {{ req.reason }}</div>
          <div class="perm-actions">
            <button class="secondary perm-approve" :disabled="req.answering" @click="respondPermission(req, 'approve_once')">✅ Approve once</button>
            <button class="secondary perm-approve" :disabled="req.answering" @click="respondPermission(req, 'approve_project')">✅ Approve for project</button>
            <button v-if="req.allow_pattern && req.pattern" class="secondary perm-approve" :disabled="req.answering" @click="respondPermission(req, 'approve_pattern')" :title="'Pattern: ' + req.pattern">✅ Approve pattern</button>
            <button class="secondary perm-deny" :disabled="req.answering" @click="respondPermission(req, 'deny_once')">❌ Deny</button>
          </div>
        </div>

        <!-- Agent working indicator (driven by agent state, not the send
             round-trip, so it persists for the whole turn and the composer
             stays usable for queuing a follow-up). -->
        <div v-if="agentWorking && !streamingText && !activityText" class="message assistant-message typing">
          <span class="typing-indicator">●●●</span>
        </div>
      </div>

      <!-- Jump to latest button -->
      <transition name="fade">
        <button v-if="hasNewContent && !isScrollPinned" class="jump-to-latest" @click="jumpToLatest">
          ↓ Jump to latest
        </button>
      </transition>

      <!-- Pending files -->
      <div v-if="pendingFiles.length" class="pending-files">
        <div v-for="(f, i) in pendingFiles" :key="i" class="file-chip">
          <img v-if="f.preview" :src="f.preview" class="file-thumb" />
          <span class="file-name">{{ f.file.name }}</span>
          <span class="file-size">{{ formatSize(f.file.size) }}</span>
          <button class="chip-remove" @click="removeFile(i)">✕</button>
        </div>
      </div>

      <!-- Input -->
      <div class="input-bar">
        <button class="secondary attach-btn" @click="$refs.chatFile.click()" title="Attach files">📎</button>
        <input type="file" ref="chatFile" style="display:none" @change="attachFiles" multiple accept="image/*,application/pdf,text/*" />
        <textarea
          v-model="draft"
          :placeholder="composerPlaceholder"
          @keydown.enter.exact="onEnterKey"
          @keydown.shift.enter.exact=""
          @input="autoGrow"
          @paste="onPaste"
          rows="1"
          ref="inputEl"
        ></textarea>
        <button class="primary" @click="send" :disabled="(!draft.trim() && !pendingFiles.length) || sending">Send</button>
      </div>
      </div>
      <template v-if="docPanelOpen">
        <div
          class="outer-divider"
          :class="outerOrientation"
          @pointerdown="startOuterDrag"
        ><div class="divider-grip"></div></div>
        <div v-if="pinnedSpec" class="outer-doc spec-pin-panel" :style="docPaneStyle">
          <div class="doc-list-head">
            <span>📌 {{ pinnedSpec.title }}</span>
            <button class="doc-panel-close" title="Unpin" @click="unpinSpec">✕</button>
          </div>
          <div class="spec-pin-body md" v-html="pinnedSpecHtml"></div>
        </div>
        <DocumentPane
          v-else-if="openDoc"
          class="outer-doc"
          :style="docPaneStyle"
          :session="selectedSession"
          :filename="openDoc"
          :mobile="isMobilePortrait"
          :refresh-tick="docRefreshTick"
          @close="closeDoc"
        />
        <div v-else class="outer-doc doc-list-panel" :style="docPaneStyle">
          <div class="doc-list-head">
            <span>Documents</span>
            <button class="doc-panel-close" title="Close" @click="closeDocPanel">✕</button>
          </div>
          <div v-if="!docList.length" class="doc-list-empty muted">No editable documents in this session.</div>
          <button
            v-for="d in docList"
            :key="d.filename"
            class="doc-list-item"
            @click="openDocument(d.filename)"
          >
            <span class="doc-list-title">{{ d.title || d.filename }}</span>
            <span v-if="d.title" class="doc-list-file">{{ d.filename }}</span>
          </button>
        </div>
      </template>
    </div>
    <div v-else class="empty-state">
      <p class="muted">Select a session to start chatting.</p>
    </div>

    <!-- Lightbox gallery overlay -->
    <teleport to="body">
      <transition name="lightbox-fade">
        <div v-if="lightboxImage" class="lightbox-overlay" @click.self="lightboxImage = null">
          <button class="lightbox-close" @click="lightboxImage = null" title="Close">✕</button>
          <button v-if="lightboxIndex > 0" class="lightbox-nav lightbox-prev" @click="lightboxPrev" title="Previous">‹</button>
          <img :src="lightboxImageUrl" class="lightbox-img" @click.stop />
          <button v-if="lightboxIndex < galleryImages.length - 1" class="lightbox-nav lightbox-next" @click="lightboxNext" title="Next">›</button>
          <div class="lightbox-counter" v-if="galleryImages.length > 1">
            {{ lightboxIndex + 1 }} / {{ galleryImages.length }}
          </div>
        </div>
      </transition>
    </teleport>

    <!-- Bug reference preview card (hover on desktop, tap on mobile) -->
    <teleport to="body">
      <transition name="bugcard-fade">
        <div
          v-if="bugCard"
          class="bug-card"
          :class="{ pinned: bugCard.pinned }"
          :style="{ left: bugCard.x + 'px', top: bugCard.y + 'px' }"
          @mouseover.stop
          @mouseout.stop
        >
          <div class="bug-card-head">
            <span class="bug-card-id">{{ bugCard.id }}</span>
            <span class="bug-card-badges">
              <span class="bug-badge status" :class="bugCard.status">{{ bugCard.status }}</span>
              <span class="bug-badge sev" :class="bugCard.severity">{{ bugCard.severity }}</span>
            </span>
            <button v-if="bugCard.pinned" class="bug-card-close" @click="closeBugCard" title="Close">✕</button>
          </div>
          <div class="bug-card-title">{{ bugCard.title }}</div>
          <div v-if="bugCard.preview" class="bug-card-preview">{{ bugCard.preview }}</div>
          <button class="bug-card-link" @click="openBugDetail(bugCard.id)">View full bug →</button>
        </div>
      </transition>
    </teleport>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted, nextTick, watch, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api.js'
import { useSessionStore } from '../stores/session.js'
import { useModels } from '../composables/useModels.js'
import { useFusion } from '../composables/useFusion.js'
import DocumentPane from '../components/DocumentPane.vue'
import ComplexityBadge from '../components/ComplexityBadge.vue'
import MarkdownIt from 'markdown-it'
import mathPlugin from '../lib/mathPlugin.js'
import bugRefPlugin from '../lib/bugRefPlugin.js'

// Live map of this session's bugs (id -> {title,status,severity,body}), used to
// resolve BRO-123-style ids into hoverable/clickable references and to populate
// the preview card. Populated by loadBugs() on session change.
const bugMap = ref({})
function resolveBug(id) { return bugMap.value[id] || null }

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
  .use(mathPlugin)
  .use(bugRefPlugin, { resolve: resolveBug })


// Render ```mermaid fenced code blocks as diagram containers; everything else
// falls through to the default fenced-code renderer. The raw source is HTML
// escaped so textContent recovers it for mermaid to parse.
const defaultFence = md.renderer.rules.fence?.bind(md.renderer.rules) ||
  ((tokens, idx, opts, env, self) => self.renderToken(tokens, idx, opts))
md.renderer.rules.fence = (tokens, idx, options, env, self) => {
  const info = (tokens[idx].info || '').trim().toLowerCase()
  if (info === 'mermaid') {
    return `<div class="mermaid">${md.utils.escapeHtml(tokens[idx].content)}</div>`
  }
  return defaultFence(tokens, idx, options, env, self)
}

// Lazily load mermaid only when a diagram is actually present (it is a large
// dependency, so keep it out of the initial bundle).
let _mermaidPromise = null
function ensureMermaid() {
  if (!_mermaidPromise) {
    _mermaidPromise = import('mermaid').then(({ default: mermaid }) => {
      mermaid.initialize({
        startOnLoad: false,
        theme: 'dark',
        securityLevel: 'strict',
      })
      return mermaid
    })
  }
  return _mermaidPromise
}

const TOOL_ICONS = {
  bash: '🖥️', read_bash: '📖', write_bash: '⌨️', stop_bash: '⏹️',
  view: '📄', edit: '✏️', create: '📝', grep: '🔍', glob: '📁',
  web_fetch: '🌐', web_search: '🔎', task: '🤖', read_agent: '📨',
  sql: '🗃️', ask_user: '❓', list_bash: '📋',
}

const { selectedSessionId } = useSessionStore()
const selectedSession = computed(() => selectedSessionId.value)
const route = useRoute()
const router = useRouter()

// ─── Document workspace (side-by-side artifact editing) ───
const EDITABLE_DOC_RE = /\.(md|txt|json|yaml|yml|csv|log)$/i
const openDoc = ref('')
const docList = ref([])
const docPanelOpen = ref(false)
const outerOrientation = ref(localStorage.getItem('enclave_outer_orientation') || 'horizontal')
const outerSize = ref(parseFloat(localStorage.getItem('enclave_outer_size')) || 58) // % for chat pane
const isMobilePortrait = ref(false)
// Touch / on-screen-keyboard devices: Enter inserts a newline (the Send button
// sends) since there's no easy Shift+Enter. Also drives the smaller line cap.
const isCoarsePointer = ref(false)
const isMobile = computed(() => isMobilePortrait.value || isCoarsePointer.value)
const composerPlaceholder = computed(() =>
  isMobile.value
    ? 'Send a message…'
    : 'Send a message… (Enter to send, Shift+Enter for newline)')
const docRefreshTick = ref(0) // bumped on agent activity → DocumentPane re-checks for external changes
let _mqlPortrait = null

const bodyClass = computed(() => {
  if (isMobilePortrait.value && docPanelOpen.value) return 'mobile-portrait'
  return docPanelOpen.value ? outerOrientation.value : 'no-doc'
})
const chatPaneStyle = computed(() => {
  if (!docPanelOpen.value || isMobilePortrait.value) return {}
  return { flexBasis: `${outerSize.value}%` }
})
const docPaneStyle = computed(() => {
  if (!docPanelOpen.value || isMobilePortrait.value) return {}
  return { flexBasis: `${100 - outerSize.value}%` }
})

function docStorageKey(id) { return `enclave_open_doc_${id}` }

async function loadDocList() {
  if (!selectedSession.value) { docList.value = []; return }
  try {
    const arts = await api.getArtifacts(selectedSession.value)
    docList.value = (arts || []).filter(a => EDITABLE_DOC_RE.test(a.filename))
  } catch { docList.value = [] }
}
function toggleDocPanel() {
  docPanelOpen.value = !docPanelOpen.value
  if (docPanelOpen.value) loadDocList()
}
function openDocument(filename) {
  openDoc.value = filename
  docPanelOpen.value = true
  if (selectedSession.value) localStorage.setItem(docStorageKey(selectedSession.value), filename)
}
function closeDoc() {
  // Return to the document list within the open panel.
  openDoc.value = ''
  if (selectedSession.value) localStorage.removeItem(docStorageKey(selectedSession.value))
  loadDocList()
}
function closeDocPanel() {
  docPanelOpen.value = false
  openDoc.value = ''
  if (selectedSession.value) localStorage.removeItem(docStorageKey(selectedSession.value))
}

// ─── Pinned OpenSpec spec (shown in the same pull-out panel) ───
const pinnedSpec = ref(null)        // { changeId, path, title }
const pinnedSpecHtml = ref('')
function pinnedSpecKey() { return `enclave:${selectedSession.value}:pinnedSpec` }
async function restorePinnedSpec() {
  pinnedSpec.value = null
  pinnedSpecHtml.value = ''
  if (!selectedSession.value) return
  let saved = null
  try { saved = JSON.parse(localStorage.getItem(pinnedSpecKey()) || 'null') } catch { saved = null }
  if (!saved || !saved.changeId) return
  pinnedSpec.value = saved
  docPanelOpen.value = true
  try {
    const d = await api.getOpenSpecChange(selectedSession.value, saved.changeId)
    // Show the proposal by default; fall back to whatever exists.
    const body = d.proposal || d.design || d.tasks || ''
    pinnedSpecHtml.value = renderMarkdown(body)
  } catch { pinnedSpecHtml.value = '<p class="muted">Could not load spec.</p>' }
}
function unpinSpec() {
  if (selectedSession.value) localStorage.removeItem(pinnedSpecKey())
  pinnedSpec.value = null
  pinnedSpecHtml.value = ''
  if (!openDoc.value) docPanelOpen.value = false
}

// ─── Pending OpenSpec feedback handoff (from the Specs tab) ───
function flushPendingFeedback() {
  if (!selectedSession.value) return
  const key = `enclave:${selectedSession.value}:pendingFeedback`
  const msg = localStorage.getItem(key)
  if (!msg) return
  localStorage.removeItem(key)
  draft.value = msg
  send()
}
function toggleOuterOrientation() {
  outerOrientation.value = outerOrientation.value === 'horizontal' ? 'vertical' : 'horizontal'
  localStorage.setItem('enclave_outer_orientation', outerOrientation.value)
}

function startOuterDrag(e) {
  const body = e.currentTarget.parentElement
  if (!body) return
  const rect = body.getBoundingClientRect()
  const horizontal = outerOrientation.value === 'horizontal'
  function move(ev) {
    let pct = horizontal
      ? ((ev.clientX - rect.left) / rect.width) * 100
      : ((ev.clientY - rect.top) / rect.height) * 100
    outerSize.value = Math.min(85, Math.max(15, pct))
  }
  function up() {
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', up)
    document.body.style.userSelect = ''
    localStorage.setItem('enclave_outer_size', String(outerSize.value))
  }
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', up)
  document.body.style.userSelect = 'none'
}

function restoreOpenDoc() {
  // Deep-link (?doc=) takes priority, else the last-open doc for this session.
  const q = route.query.doc
  if (typeof q === 'string' && q) { openDoc.value = q; docPanelOpen.value = true; return }
  if (selectedSession.value) {
    openDoc.value = localStorage.getItem(docStorageKey(selectedSession.value)) || ''
  } else {
    openDoc.value = ''
  }
  if (openDoc.value) docPanelOpen.value = true
}

watch(() => route.query.doc, (d) => {
  if (typeof d === 'string' && d) openDocument(d)
})

const turns = ref([])
const hasMore = ref(false)
const loadingMore = ref(false)
const INITIAL_LIMIT = 120
const draft = ref('')
const sending = ref(false)
const messagesEl = ref(null)
const inputEl = ref(null)

// ─── Per-session draft persistence ───
// Keep an unsent message around if the user navigates away and returns.
const DRAFT_PREFIX = 'enclave_chat_draft_'
function draftKey(id) { return DRAFT_PREFIX + id }
function loadDraft(id) {
  draft.value = id ? (localStorage.getItem(draftKey(id)) || '') : ''
}
watch(draft, (v) => {
  const id = selectedSession.value
  nextTick(autoGrow)
  if (!id) return
  if (v) localStorage.setItem(draftKey(id), v)
  else localStorage.removeItem(draftKey(id))
})

// ─── Mermaid diagram rendering ───
let _mermaidObserver = null
let _mermaidTimer = null
async function processMermaid() {
  const root = messagesEl.value
  if (!root) return
  const nodes = root.querySelectorAll('div.mermaid:not([data-processed])')
  if (!nodes.length) return
  try {
    const mermaid = await ensureMermaid()
    await mermaid.run({ nodes: Array.from(nodes), suppressErrors: true })
  } catch { /* incomplete/invalid diagram — ignore */ }
}
function scheduleMermaid() {
  if (_mermaidTimer) clearTimeout(_mermaidTimer)
  // Debounce so streaming deltas don't trigger renders on every frame.
  _mermaidTimer = setTimeout(processMermaid, 300)
}

const pendingFiles = ref([])
const dragging = ref(false)
const { loadModels, loadCredits, applyCreditsUpdate } = useModels()

// Live streaming state
const liveEvents = ref([])
const streamingText = ref('')
const activityText = ref('')
const askUserPrompt = ref(null)
const askUserAnswer = ref('')
// Pending agent permission requests (host-mode approvals), keyed by request_id.
// Rendered as approve/deny cards; answered via POST /permission.
const permissionRequests = ref([])
// Auto Fusion: latest complexity grade (1-5) + recommended tier, shown live.
const complexity = ref(null)
const { applyComplexity, applyFusion, resetFusion } = useFusion()
// Per-turn Auto Fusion metadata (turn_index → { complexity, fusions: [] }), so
// each message card can show its complexity badge + tappable fusion trace.
const turnMeta = ref({})
const expandedFusion = ref({})
// Accumulates the complexity grade + fusion runs for the in-progress turn until
// it finalizes, at which point they're attached to turnMeta[turn_index].
let pendingComplexity = null
let pendingFusions = []

// Carry the Auto Fusion trace (complexity grade + fusion runs) accumulated this
// turn onto the first agent output bubble. The live WS flow has no durable
// `turn` event to populate turnMeta, so without this the trace is cleared with
// liveEvents when the response lands and only returns on a full reload. Cleared
// after attaching so later bubbles in the same turn don't duplicate it; the
// reload path rebinds via turnMeta by timestamp.
function attachPendingMeta(liveTurn) {
  if (pendingFusions.length) { liveTurn.fusions = pendingFusions.slice(); pendingFusions = [] }
  if (pendingComplexity) { liveTurn.complexity = pendingComplexity; pendingComplexity = null }
}
let currentThinkingIdx = -1
const liveEventsExpanded = ref(false)
const LIVE_EVENTS_VISIBLE = 5

const collapsedLiveCount = computed(() => Math.max(0, liveEvents.value.length - LIVE_EVENTS_VISIBLE))
const collapsedLiveEvents = computed(() => liveEvents.value.slice(0, collapsedLiveCount.value))
const visibleLiveEvents = computed(() => {
  if (liveEvents.value.length <= LIVE_EVENTS_VISIBLE) return liveEvents.value
  return liveEvents.value.slice(-LIVE_EVENTS_VISIBLE)
})

// Agent state tracking
const agentState = ref('unknown') // 'idle' | 'thinking' | 'tool' | 'responding' | 'waiting_user' | 'unknown'
const agentToolName = ref('')
const agentLastUpdate = ref(null)
let stateIdleTimer = null

function setAgentState(state, toolName = '') {
  agentState.value = state
  agentToolName.value = toolName
  agentLastUpdate.value = new Date()
  // Clear any pending idle timer
  if (stateIdleTimer) { clearTimeout(stateIdleTimer); stateIdleTimer = null }
}

const agentStateLabel = computed(() => {
  switch (agentState.value) {
    case 'thinking': return '🤔 Thinking…'
    case 'tool': return `⚙️ Running ${agentToolName.value || 'tool'}…`
    case 'responding': return '💬 Responding…'
    case 'waiting_user': return '❓ Waiting for input'
    case 'idle': return '😴 Idle'
    default: return ''
  }
})

const agentStateClass = computed(() => agentState.value)

// The agent is actively working this turn (show the ●●● indicator). Also true
// briefly while a send is in flight, so the indicator appears immediately on
// send before the first agent event arrives. Excludes idle/waiting_user/unknown.
const agentWorking = computed(() =>
  sending.value || ['thinking', 'tool', 'responding'].includes(agentState.value)
)

// Persisted events per turn (turn_index → events array)
const turnEvents = ref({})
const expandedTurns = ref({})
const TOOL_ICONS_MAP = TOOL_ICONS

// Stream events that indicate the agent has resumed work after an ask_user
// (used to auto-dismiss a lingering question card).
const AGENT_PROGRESS_EVENTS = new Set([
  'turn_start', 'delta', 'thinking', 'tool_start', 'tool_complete',
  'structured_response', 'turn', 'turn_end',
])

// Events after which an open artifact may have changed on disk.
const DOC_REFRESH_EVENTS = new Set([
  'tool_complete', 'response', 'structured_response', 'turn_end', 'file_send',
])

function countSegmentEvents(segments) {
  if (!segments) return 0
  return segments.reduce((sum, seg) => sum + seg.tools.length + (seg.response ? 1 : 0), 0)
}

let ws = null
let dragCounter = 0

onMounted(async () => {
  // Track mobile-portrait to force the doc-on-top stacked layout with tabs.
  _mqlPortrait = window.matchMedia('(max-width: 820px) and (orientation: portrait)')
  const applyPortrait = (e) => { isMobilePortrait.value = e.matches }
  applyPortrait(_mqlPortrait)
  _mqlPortrait.addEventListener('change', applyPortrait)
  _mqlPortrait._handler = applyPortrait

  // Touch / on-screen-keyboard devices use Enter-for-newline.
  try { isCoarsePointer.value = window.matchMedia('(pointer: coarse)').matches } catch { /* ignore */ }

  restoreOpenDoc()
  restorePinnedSpec()
  if (selectedSession.value) {
    loadHistory()
    loadDocList()
    seedAgentState(selectedSession.value)
    flushPendingFeedback()
  }
  loadDraft(selectedSession.value)
  nextTick(autoGrow)
  window.addEventListener('keydown', onLightboxKey)
  window.addEventListener('keydown', onTypeToFocus)
  window.addEventListener('click', onOutsideBugCard, true)
  window.addEventListener('keydown', onBugCardEsc)
})

// Dismiss a pinned bug card on an outside click or Escape.
function onOutsideBugCard(e) {
  if (!bugCard.value || !bugCard.value.pinned) return
  if (e.target.closest('.bug-card') || e.target.closest('.bug-ref')) return
  bugCard.value = null
}
function onBugCardEsc(e) {
  if (e.key === 'Escape' && bugCard.value) bugCard.value = null
}

// Start typing anywhere (no field focused) to jump straight into the composer,
// so the keystroke lands in the message box without a manual click.
function onTypeToFocus(e) {
  if (e.ctrlKey || e.metaKey || e.altKey) return
  if (e.key == null || e.key.length !== 1) return  // ignore Enter, arrows, F-keys…
  if (!selectedSession.value) return
  if (lightboxImage.value) return  // don't steal keys from the image viewer
  const ae = document.activeElement
  const tag = ae && ae.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (ae && ae.isContentEditable)) return
  const el = inputEl.value
  if (!el) return
  el.focus()  // focus during keydown so the character is inserted by the browser
}

// Attach a MutationObserver to the messages container so mermaid diagrams are
// rendered whenever new content is injected via v-html (history, live stream,
// structured responses). Re-attaches if the container is recreated.
watch(messagesEl, (el) => {
  if (_mermaidObserver) { _mermaidObserver.disconnect(); _mermaidObserver = null }
  if (el) {
    _mermaidObserver = new MutationObserver(scheduleMermaid)
    _mermaidObserver.observe(el, { childList: true, subtree: true })
    scheduleMermaid()
  }
})

onUnmounted(() => {
  if (ws) ws.close()
  if (_mermaidObserver) { _mermaidObserver.disconnect(); _mermaidObserver = null }
  if (_mermaidTimer) clearTimeout(_mermaidTimer)
  pendingFiles.value.forEach(f => { if (f.preview) URL.revokeObjectURL(f.preview) })
  window.removeEventListener('keydown', onLightboxKey)
  window.removeEventListener('keydown', onTypeToFocus)
  window.removeEventListener('click', onOutsideBugCard, true)
  window.removeEventListener('keydown', onBugCardEsc)
  if (_mqlPortrait && _mqlPortrait._handler) {
    _mqlPortrait.removeEventListener('change', _mqlPortrait._handler)
    _mqlPortrait = null
  }
})

watch(selectedSession, (newVal) => {
  if (ws) { ws.close(); ws = null }
  clearLiveState()
  // Per-turn Auto Fusion metadata is session-specific; reset it (and the shared
  // tab-bar indicator) on switch so it doesn't leak across sessions.
  turnMeta.value = {}
  expandedFusion.value = {}
  pendingComplexity = null
  pendingFusions = []
  resetFusion()
  // Reset live activity so the previous session's state doesn't leak into the
  // newly selected one (the indicator must be session-specific).
  setAgentState('unknown')
  sending.value = false
  docPanelOpen.value = false
  restoreOpenDoc()
  restorePinnedSpec()
  loadDraft(newVal)
  if (newVal) { loadHistory(); loadDocList(); seedAgentState(newVal); flushPendingFeedback() }
})

// Seed the live status float from the orchestrator's current activity snapshot
// so opening a session that is mid-tool-call (e.g. a multi-minute fusion run
// that emits one tool_start then goes quiet) shows a working indicator instead
// of appearing idle until the next streamed event.
async function seedAgentState(sessionId) {
  try {
    const resp = await api.getActivity()
    if (sessionId !== selectedSession.value) return  // session changed mid-fetch
    const state = resp?.states?.[sessionId]
    if (state && state !== 'idle' && agentState.value === 'unknown') {
      setAgentState(state)
    }
  } catch { /* ignore */ }
}

function clearLiveState() {
  liveEvents.value = []
  streamingText.value = ''
  activityText.value = ''
  askUserPrompt.value = null
  askUserAnswer.value = ''
  complexity.value = null
  currentThinkingIdx = -1
  turnEvents.value = {}
  expandedTurns.value = {}
}

async function loadHistory() {
  if (!selectedSession.value) { turns.value = []; return }
  try {
    const data = await api.getChatHistory(selectedSession.value, INITIAL_LIMIT)
    turns.value = data.turns || []
    // If we got exactly INITIAL_LIMIT turns, there may be more
    hasMore.value = turns.value.length >= INITIAL_LIMIT
    // Load persisted events (tool calls, thinking, etc.)
    await loadEvents()
    await nextTick()
    scrollToBottom(true)
    connectWebSocket()
    loadModels(selectedSession.value)
    loadCredits(selectedSession.value)
    loadBugs(selectedSession.value)
  } catch (e) {
    console.error('Failed to load history:', e)
  }
}

// Load this session's bugs into bugMap so bug ids in messages resolve to
// hoverable/clickable references. Refreshed on each history load; a failure
// just leaves ids as plain text.
async function loadBugs(sessionId) {
  if (!sessionId) { bugMap.value = {}; return }
  try {
    const bugs = await api.getBugs(sessionId)
    const map = {}
    for (const b of (bugs || [])) {
      if (b && b.id) map[b.id] = b
    }
    bugMap.value = map
  } catch {
    bugMap.value = {}
  }
}

async function loadEvents() {
  if (!selectedSession.value) return
  try {
    // Only load events for the displayed turn range
    const firstTs = turns.value.length > 0 ? turns.value[0].timestamp : null
    const data = await api.getChatEvents(selectedSession.value, {
      limit: 5000,
      sinceTimestamp: firstTs || undefined,
    })
    const events = data.events || []
    // Tool calls (and file sends) are collapsed under their turn. Agent
    // responses and structured cards are NOT grouped here: history
    // reconstruction now surfaces every response as its own bubble and marks
    // structured cards on dedicated turns, so grouping them here would
    // duplicate (responses) or double-map (cards) them.
    const grouped = {}
    const meta = {}
    for (const evt of events) {
      // Auto Fusion grades + fusion runs bind to their turn for the per-message
      // complexity badge and the tappable model-outcome trace.
      if (evt.type === 'complexity' || evt.type === 'fusion') {
        let bestTurn = null
        for (const t of turns.value) {
          if (t.turn_index == null) continue
          if (t.timestamp && t.timestamp <= evt.timestamp) bestTurn = t.turn_index
        }
        if (bestTurn == null) continue
        if (!meta[bestTurn]) meta[bestTurn] = { complexity: null, fusions: [] }
        // The /events API nests the payload under evt.data (unlike the flattened
        // WS stream), so read fusion/complexity fields from there.
        const d = evt.data || {}
        if (evt.type === 'complexity') {
          meta[bestTurn].complexity = { score: d.score, tier: d.tier, reason: d.reason || '' }
        } else {
          meta[bestTurn].fusions.push({
            preset: d.preset_name || d.preset || 'fusion',
            models: d.models || [],
            judge_model: d.judge_model || '',
            synthesizer_model: d.synthesizer_model || '',
            participants: d.participants || [],
            judge_analysis: d.judge_analysis || '',
            final: d.final || '',
          })
        }
        continue
      }
      if (!['tool_start', 'tool_complete', 'file_send', 'thinking'].includes(evt.type)) continue
      // Find the best matching turn (latest turn that started before this event)
      let bestTurn = null
      for (const t of turns.value) {
        const ti = t.turn_index
        if (ti == null) continue
        if (t.timestamp && t.timestamp <= evt.timestamp) {
          bestTurn = ti
        }
      }
      if (bestTurn == null) continue
      if (!grouped[bestTurn]) grouped[bestTurn] = [{ tools: [], response: null }]
      grouped[bestTurn][0].tools.push(evt)
    }
    turnEvents.value = grouped
    // Merge (don't clobber) so live-attached meta from the current turn survives.
    turnMeta.value = { ...meta, ...turnMeta.value }
  } catch (e) {
    console.error('Failed to load events:', e)
  }
}

async function loadEarlier() {
  if (loadingMore.value || !selectedSession.value) return
  loadingMore.value = true
  try {
    const data = await api.getChatHistory(selectedSession.value, INITIAL_LIMIT, turns.value.length)
    const older = data.turns || []
    if (older.length) {
      // Prepend older turns, preserve scroll position
      const el = messagesEl.value
      const prevHeight = el ? el.scrollHeight : 0
      turns.value = [...older, ...turns.value]
      hasMore.value = older.length >= INITIAL_LIMIT
      await nextTick()
      if (el) el.scrollTop = el.scrollHeight - prevHeight
    } else {
      hasMore.value = false
    }
  } catch (e) {
    console.error('Failed to load earlier messages:', e)
  } finally {
    loadingMore.value = false
  }
}

function connectWebSocket() {
  if (ws) ws.close()
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const token = localStorage.getItem('enclave_token')
  ws = new WebSocket(`${proto}//${location.host}/api/chat/${selectedSession.value}/stream?token=${token}`)

  ws.onopen = () => { seedAgentState(selectedSession.value) }

  ws.onmessage = async (event) => {
    const msg = JSON.parse(event.data)
    handleStreamEvent(msg)
    await nextTick()
    scrollToBottom()
  }

  ws.onclose = () => {
    setTimeout(() => {
      if (selectedSession.value) connectWebSocket()
    }, 3000)
  }
}

function handleStreamEvent(msg) {
  const type = msg.type

  // Any sign of the agent resuming forward progress means a pending ask_user
  // question has been answered (via the composer, Matrix, or another client),
  // so clear the lingering question card. ask_user blocks the turn, so these
  // events only arrive after the answer was delivered.
  if (askUserPrompt.value && AGENT_PROGRESS_EVENTS.has(type)) {
    askUserPrompt.value = null
    askUserAnswer.value = ''
  }

  // After the agent does work it may have rewritten an open artifact (via
  // publish_artifact or a direct file edit). Nudge the document pane to re-check
  // for external changes on these completion-ish events.
  if (openDoc.value && DOC_REFRESH_EVENTS.has(type)) {
    docRefreshTick.value++
  }

  if (type === 'credits') {
    // Live "AI Credits" update from the orchestrator: account entitlement
    // snapshot + per-session consumed AI Units. Routed through the shared
    // composable so the global tab bar reflects it too.
    applyCreditsUpdate(msg)
    return
  }

  if (type === 'complexity') {
    // Auto Fusion: live task-complexity grade (1-5) + recommended tier.
    complexity.value = { score: msg.score, tier: msg.tier, reason: msg.reason || '' }
    applyComplexity(msg)
    pendingComplexity = { score: msg.score, tier: msg.tier, reason: msg.reason || '' }
    return
  }

  if (type === 'fusion') {
    // A completed fusion run — render it as a tappable card in the live turn.
    const run = {
      type: 'fusion',
      preset: msg.preset_name || msg.preset || 'fusion',
      models: msg.models || [],
      judge_model: msg.judge_model || '',
      synthesizer_model: msg.synthesizer_model || '',
      participants: msg.participants || [],
      judge_analysis: msg.judge_analysis || '',
      final: msg.final || '',
      expanded: false,
    }
    liveEvents.value.push(run)
    applyFusion(msg)
    pendingFusions.push(run)
    return
  }

  if (type === 'turn') {
    // Completed turn from SQLite — merge into turns list
    const idx = turns.value.findIndex(t => t.turn_index === msg.turn_index)
    if (idx >= 0) {
      turns.value[idx] = msg
    } else {
      // Remove any queued message that matches this turn's user_message, and
      // carry its image previews onto the reconciled turn so the picture the
      // user sent stays visible (the SDK turn carries no image metadata).
      // Image-only sends still arrive with a placeholder body, so user_message
      // is truthy here in every user-initiated turn.
      if (msg.user_message) {
        let qIdx = turns.value.findIndex(t =>
          t.source === 'queued' && (
            t.user_message === msg.user_message ||
            msg.user_message.includes(t.user_message)
          )
        )
        if (qIdx < 0) {
          // Fallback: the most recent queued message (covers image-only sends,
          // whose placeholder body won't match the queued empty text).
          for (let i = turns.value.length - 1; i >= 0; i--) {
            if (turns.value[i].source === 'queued') { qIdx = i; break }
          }
        }
        if (qIdx >= 0) {
          const q = turns.value[qIdx]
          if (q.user_images && q.user_images.length &&
              !(msg.user_images && msg.user_images.length)) {
            msg.user_images = q.user_images
          }
          turns.value.splice(qIdx, 1)
        }
      }
      // Remove any live-cache entry that matches this turn's assistant_response
      if (msg.assistant_response) {
        const lIdx = turns.value.findIndex(t => t.source === 'live' && t.assistant_response === msg.assistant_response)
        if (lIdx >= 0) {
          // Preserve is_major and structured data from the live turn
          if (turns.value[lIdx].is_major) msg.is_major = true
          if (turns.value[lIdx].structured) msg.structured = turns.value[lIdx].structured
          turns.value.splice(lIdx, 1)
        }
      }
      turns.value.push(msg)
    }
    // Attach any Auto Fusion metadata accumulated during this turn so the
    // message card can show its complexity badge + tappable fusion trace.
    if (msg.turn_index != null && (pendingComplexity || pendingFusions.length)) {
      turnMeta.value[msg.turn_index] = {
        complexity: pendingComplexity,
        fusions: pendingFusions,
      }
    }
    pendingComplexity = null
    pendingFusions = []
    // Only clear live state if this turn has an assistant response
    if (msg.assistant_response) {
      clearLiveState()
    }
    sending.value = false
    return
  }

  if (type === 'turn_start') {
    // New turn — reset streaming text but keep accumulated live events
    streamingText.value = ''
    activityText.value = ''
    currentThinkingIdx = -1
    // Drop any Auto Fusion meta left over from an incomplete previous turn so it
    // doesn't get misattributed to this one.
    pendingComplexity = null
    pendingFusions = []
    // Clear the live complexity indicator so it reflects only the *current*
    // message: a grade of 5 on a previous turn must not linger over later
    // messages that weren't graded. It re-populates if this turn grades.
    complexity.value = null
    resetFusion()
    setAgentState('thinking')
    return
  }

  if (type === 'delta') {
    streamingText.value = msg.content || ''
    activityText.value = ''
    sending.value = false
    setAgentState('responding')
    return
  }

  if (type === 'thinking') {
    const phase = msg.phase || 'delta'
    setAgentState('thinking')
    if (phase === 'end') {
      // Finalize thinking block — auto-collapse
      if (currentThinkingIdx >= 0 && currentThinkingIdx < liveEvents.value.length) {
        liveEvents.value[currentThinkingIdx].content = msg.content || liveEvents.value[currentThinkingIdx].content
        liveEvents.value[currentThinkingIdx].collapsed = true
      }
      currentThinkingIdx = -1
    } else {
      // Start or delta
      if (currentThinkingIdx < 0 || currentThinkingIdx >= liveEvents.value.length) {
        // Create new thinking block
        liveEvents.value.push(reactive({
          type: 'thinking',
          content: msg.content || '',
          collapsed: false,
        }))
        currentThinkingIdx = liveEvents.value.length - 1
      } else {
        liveEvents.value[currentThinkingIdx].content = msg.content || ''
      }
    }
    return
  }

  if (type === 'tool_start') {
    currentThinkingIdx = -1  // End any open thinking block
    const icon = TOOL_ICONS[msg.name] || '🔧'
    setAgentState('tool', msg.detail || msg.name || 'tool')
    liveEvents.value.push(reactive({
      type: 'tool',
      name: msg.name || 'unknown',
      detail: msg.detail || '',
      icon,
      done: false,
      success: true,
      collapsed: false,
    }))
    activityText.value = ''
    return
  }

  if (type === 'tool_complete') {
    setAgentState('thinking') // Back to thinking after tool completes
    // Find the last tool event with this name that isn't done
    for (let i = liveEvents.value.length - 1; i >= 0; i--) {
      const evt = liveEvents.value[i]
      if (evt.type === 'tool' && evt.name === msg.name && !evt.done) {
        evt.done = true
        evt.success = msg.success !== false
        evt.collapsed = true
        break
      }
    }
    return
  }

  if (type === 'activity') {
    activityText.value = msg.text || ''
    return
  }

  if (type === 'ask_user') {
    setAgentState('waiting_user')
    askUserPrompt.value = {
      question: msg.question || '',
      choices: msg.choices || [],
    }
    askUserAnswer.value = ''
    return
  }

  if (type === 'permission_request') {
    // Agent is blocked awaiting approval for a restricted op. Show a card.
    if (!permissionRequests.value.some(p => p.request_id === msg.request_id)) {
      permissionRequests.value.push({
        request_id: msg.request_id,
        perm_type: msg.perm_type || '',
        target: msg.target || '',
        reason: msg.reason || '',
        pattern: msg.pattern || '',
        allow_pattern: !!msg.allow_pattern,
        answering: false,
      })
    }
    setAgentState('waiting_user')
    nextTick(() => scrollToBottom())
    return
  }

  if (type === 'permission_resolved') {
    // Answered here or elsewhere (Matrix / another client) or expired — clear it.
    permissionRequests.value = permissionRequests.value.filter(p => p.request_id !== msg.request_id)
    return
  }

  if (type === 'file_send') {
    // Agent sent a file — show it as a live event
    const filename = msg.filename || 'file'
    const mxcUrl = msg.mxc_url || ''
    liveEvents.value.push(reactive({
      type: 'file_send',
      filename,
      filePath: msg.file_path || '',
      mxcUrl,
      mimetype: msg.mimetype || '',
      collapsed: false,
    }))
    nextTick(() => scrollToBottom())
    return
  }

  if (type === 'response') {
    setAgentState('responding')
    // Final response — promote to a synthetic turn immediately so it
    // survives live state clears (turn_start, next response, etc.).
    // The SQLite poll will eventually replace it with the real turn.
    if (msg.content) {
      // Remove any existing live-source turn with the same content
      const existIdx = turns.value.findIndex(t => t.source === 'live' && t.assistant_response === msg.content)
      if (existIdx < 0) {
        const liveTurn = {
          turn_index: null,
          user_message: null,
          assistant_response: msg.content,
          timestamp: new Date().toISOString(),
          source: 'live',
          is_major: true,
        }
        attachPendingMeta(liveTurn)
        turns.value.push(liveTurn)
      }
      streamingText.value = ''
    }
    activityText.value = ''
    // Flush the live tool/thinking blocks — they belong to the segment we just
    // finalized into a turn above. Keep file_send events: they're durable/major
    // artifacts (they also route to Matrix) and are otherwise wiped here, only
    // reappearing on reload when rebuilt from the event store.
    liveEvents.value = liveEvents.value.filter(e => e.type === 'file_send')
    currentThinkingIdx = -1
    nextTick(() => scrollToBottom())
    return
  }

  if (type === 'structured_response') {
    setAgentState('responding')
    const summary = msg.summary || ''
    if (summary) {
      const liveTurn = {
        turn_index: null,
        user_message: null,
        assistant_response: summary,
        timestamp: new Date().toISOString(),
        source: 'live',
        is_major: true,
        structured: {
          title: msg.title || '',
          summary: msg.summary || '',
          details: msg.details || '',
          actions: msg.actions || [],
          images: msg.images || [],
        },
      }
      attachPendingMeta(liveTurn)
      turns.value.push(liveTurn)
      streamingText.value = ''
    }
    activityText.value = ''
    // Keep file_send events (durable/major); flush ephemeral tool/thinking.
    liveEvents.value = liveEvents.value.filter(e => e.type === 'file_send')
    currentThinkingIdx = -1
    nextTick(() => scrollToBottom())
    return
  }

  if (type === 'turn_end') {
    sending.value = false
    activityText.value = ''
    // Set idle after a short delay (agent may start a new turn immediately)
    if (stateIdleTimer) clearTimeout(stateIdleTimer)
    stateIdleTimer = setTimeout(() => setAgentState('idle'), 3000)
    return
  }
}

// Enter handling: on desktop Enter sends (Shift+Enter makes a newline). On
// touch devices there's no easy Shift+Enter, so Enter inserts a newline and the
// Send button is the only way to send.
function onEnterKey(e) {
  if (isMobile.value) return  // allow the default newline
  e.preventDefault()
  send()
}

// Auto-grow the composer to fit its content, capped at 8 lines (4 on mobile),
// after which it scrolls. Runs on input, paste, draft restore and after send.
function autoGrow() {
  const el = inputEl.value
  if (!el) return
  el.style.height = 'auto'
  const cs = getComputedStyle(el)
  const lh = parseFloat(cs.lineHeight) || 20
  const padTop = parseFloat(cs.paddingTop) || 0
  const padBottom = parseFloat(cs.paddingBottom) || 0
  const borderTop = parseFloat(cs.borderTopWidth) || 0
  const borderBottom = parseFloat(cs.borderBottomWidth) || 0
  const maxLines = isMobile.value ? 4 : 8
  const maxH = lh * maxLines + padTop + padBottom + borderTop + borderBottom
  const newH = Math.min(el.scrollHeight + borderTop + borderBottom, maxH)
  el.style.height = newH + 'px'
  el.style.overflowY = el.scrollHeight + borderTop + borderBottom > maxH ? 'auto' : 'hidden'
}

async function send() {
  if ((!draft.value.trim() && !pendingFiles.value.length) || !selectedSession.value) return
  const content = draft.value.trim()
  draft.value = ''
  sending.value = true
  nextTick(autoGrow)

  // Sending a message answers any pending ask_user question — dismiss its card.
  if (askUserPrompt.value) {
    askUserPrompt.value = null
    askUserAnswer.value = ''
  }

  // Immediately show the message as "queued" (with any image previews) so the
  // user sees what they sent before the upload round-trips. The queued turn
  // takes ownership of the preview blob URLs; they're revoked when it's
  // reconciled away or on unmount, not by clearPendingFiles below.
  const queuedPreviews = pendingFiles.value.filter(f => f.preview).map(f => f.preview)
  if (content || queuedPreviews.length) {
    const ts = new Date().toISOString()
    turns.value.push({
      turn_index: null,
      user_message: content,
      user_images: queuedPreviews,
      assistant_response: null,
      timestamp: ts,
      source: 'queued',
    })
    await nextTick()
    scrollToBottom()
  }

  try {
    const token = localStorage.getItem('enclave_token')
    const files = [...pendingFiles.value]

    if (files.length > 0) {
      for (let i = 0; i < files.length; i++) {
        const form = new FormData()
        form.append('file', files[i].file)
        if (i === files.length - 1 && content) {
          form.append('message', content)
        }
        await fetch(`/api/chat/${selectedSession.value}/upload`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${token}` },
          body: form,
        })
      }
      // Clear the picker without revoking the preview URLs: the queued turn now
      // owns them for display until it's reconciled away (or the view unmounts).
      pendingFiles.value = []
    } else {
      await api.sendChatMessage(selectedSession.value, content)
    }
  } catch (e) {
    console.error('Send failed:', e)
    draft.value = content
    // Remove the queued message on failure
    const idx = turns.value.findIndex(t => t.source === 'queued' && t.user_message === content)
    if (idx >= 0) turns.value.splice(idx, 1)
  } finally {
    // `sending` guards only the delivery round-trip (upload/POST), NOT the
    // agent's turn — the agent buffers messages sent while it's busy
    // (pending_messages / check_messages), so the composer must re-enable as
    // soon as delivery completes to allow queuing a follow-up. The "agent is
    // working" indicator is driven by agentState, not by `sending`.
    sending.value = false
  }
}

async function answerAskUser(answer) {
  if (!answer?.trim() || !selectedSession.value) return
  const text = answer.trim()
  askUserPrompt.value = null
  askUserAnswer.value = ''
  // Show as queued user message
  turns.value.push({
    turn_index: null,
    user_message: text,
    assistant_response: null,
    timestamp: new Date().toISOString(),
    source: 'queued',
  })
  await nextTick()
  scrollToBottom(true)
  try {
    await api.sendChatMessage(selectedSession.value, text)
  } catch (e) {
    console.error('Failed to send answer:', e)
  }
}

// Answer an agent permission request. answerId is one of the ANSWER_* ids the
// orchestrator's ApprovalManager understands.
async function respondPermission(req, answerId) {
  if (!selectedSession.value || req.answering) return
  req.answering = true
  try {
    await api.respondPermission(selectedSession.value, req.request_id, answerId)
    // Optimistically clear; the permission_resolved event also clears it.
    permissionRequests.value = permissionRequests.value.filter(p => p.request_id !== req.request_id)
  } catch (e) {
    console.error('Failed to respond to permission:', e)
    req.answering = false
  }
}

function permTypeIcon(t) {
  if (t === 'filesystem') return '📂'
  if (t === 'network') return '🌐'
  return '❓'
}

function addFiles(fileList) {
  for (const file of fileList) {
    const entry = { file, preview: null }
    if (file.type.startsWith('image/')) {
      entry.preview = URL.createObjectURL(file)
    }
    pendingFiles.value.push(entry)
  }
}

function removeFile(index) {
  const f = pendingFiles.value[index]
  if (f.preview) URL.revokeObjectURL(f.preview)
  pendingFiles.value.splice(index, 1)
}

function clearPendingFiles() {
  pendingFiles.value.forEach(f => { if (f.preview) URL.revokeObjectURL(f.preview) })
  pendingFiles.value = []
}

function attachFiles(event) {
  const files = event.target.files
  if (files?.length) addFiles(files)
  event.target.value = ''
}

function onPaste(event) {
  const items = event.clipboardData?.items
  if (!items) return
  const imageFiles = []
  for (const item of items) {
    if (item.type.startsWith('image/')) {
      const file = item.getAsFile()
      if (file) imageFiles.push(file)
    }
  }
  if (imageFiles.length) {
    event.preventDefault()
    addFiles(imageFiles)
  }
}

function onDragOver(event) {
  dragCounter++
  dragging.value = true
}

function onDragLeave() {
  dragCounter--
  if (dragCounter <= 0) {
    dragging.value = false
    dragCounter = 0
  }
}

function onDrop(event) {
  dragging.value = false
  dragCounter = 0
  const files = event.dataTransfer?.files
  if (files?.length) addFiles(files)
}

function onMessagesClick(event) {
  // A bug reference (BRO-123) was tapped/clicked → open the preview popup.
  // Works on mobile where hover is unavailable; desktop also gets a hover card.
  const bugEl = event.target.closest('.bug-ref')
  if (bugEl) {
    event.preventDefault()
    openBugCard(bugEl.getAttribute('data-bug-id'), bugEl)
    return
  }
  // Delegate click on any <img> inside rendered markdown to open lightbox
  const img = event.target.closest('.message-body img')
  if (img && img.src) {
    event.preventDefault()
    openLightbox(img.src)
  }
}

// ─── Bug reference preview card ──────────────────────────────────────────────
// Hover (desktop) or click/tap (all, esp. mobile) shows a small card with the
// bug's title, status, severity, and a description preview + a link to the full
// bug detail page.
const bugCard = ref(null) // { id, title, status, severity, preview, x, y } | null

function bugPreviewText(body) {
  if (!body) return ''
  // Strip the markdown section headers / frontmatter noise; take the first
  // meaningful lines of the description.
  const cleaned = body
    .replace(/^#+\s.*$/gm, '')
    .replace(/^\s*[-*]\s/gm, '')
    .trim()
  return cleaned.length > 240 ? cleaned.slice(0, 240) + '…' : cleaned
}

function openBugCard(id, anchorEl) {
  const bug = bugMap.value[id]
  if (!bug) return
  const rect = anchorEl.getBoundingClientRect()
  bugCard.value = {
    id,
    title: bug.title || id,
    status: bug.status || 'open',
    severity: bug.severity || 'medium',
    preview: bugPreviewText(bug.body),
    // Position below the ref, clamped to the viewport in the template style.
    x: Math.min(rect.left, window.innerWidth - 340),
    y: rect.bottom + 6,
    pinned: true, // opened by click — stays until dismissed
  }
}

function showBugHover(event) {
  // Desktop hover: only when not already pinned by a click.
  const bugEl = event.target.closest?.('.bug-ref')
  if (!bugEl || (bugCard.value && bugCard.value.pinned)) return
  const id = bugEl.getAttribute('data-bug-id')
  const bug = bugMap.value[id]
  if (!bug) return
  const rect = bugEl.getBoundingClientRect()
  bugCard.value = {
    id,
    title: bug.title || id,
    status: bug.status || 'open',
    severity: bug.severity || 'medium',
    preview: bugPreviewText(bug.body),
    x: Math.min(rect.left, window.innerWidth - 340),
    y: rect.bottom + 6,
    pinned: false,
  }
}

function hideBugHover(event) {
  // Leave a pinned (clicked) card open; dismiss a hover card.
  if (bugCard.value && !bugCard.value.pinned) bugCard.value = null
}

function closeBugCard() { bugCard.value = null }

function openBugDetail(id) {
  closeBugCard()
  router.push(`/bugs/${selectedSession.value}/${id}`)
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// Auto-scroll pinning: only scroll down if user is already at the bottom
const isScrollPinned = ref(true)
const hasNewContent = ref(false)

function onMessagesScroll() {
  if (!messagesEl.value) return
  const el = messagesEl.value
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60
  isScrollPinned.value = atBottom
  if (atBottom) hasNewContent.value = false
}

function scrollToBottom(force = false) {
  if (!messagesEl.value) return
  if (force || isScrollPinned.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
    hasNewContent.value = false
  } else {
    hasNewContent.value = true
  }
}

function jumpToLatest() {
  isScrollPinned.value = true
  hasNewContent.value = false
  if (messagesEl.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
  }
}

function renderMarkdown(text) {
  if (!text) return ''
  // Strip <current_datetime>...</current_datetime> tags injected by the system
  let cleaned = text.replace(/<current_datetime>[^<]*<\/current_datetime>/g, '')
  // Trim excessive whitespace left behind
  cleaned = cleaned.replace(/\n{3,}/g, '\n\n').trim()
  // Convert mxc:// URLs to proxied URLs for images (with auth token)
  const authToken = encodeURIComponent(localStorage.getItem('enclave_token') || '')
  cleaned = cleaned.replace(/mxc:\/\/([^/\s]+)\/([^)\s]+)/g,
    (_, server, id) => `/api/chat/media/${server}/${id}?token=${authToken}`)
  // Rewrite /workspace/ paths to proxy URLs so embedded images work
  if (selectedSession.value) {
    cleaned = cleaned.replace(/\/workspace\/([^)\s"']+)/g, (_, p) =>
      `/api/chat/${selectedSession.value}/file/${p.split('/').map(encodeURIComponent).join('/')}?token=${authToken}`)
  }
  const display = cleaned.length > 10000 ? cleaned.slice(0, 10000) + '\n\n…(truncated)' : cleaned
  return md.render(display)
}

function downloadMarkdown(structured) {
  const parts = []
  if (structured.title) parts.push(`# ${structured.title}\n`)
  if (structured.summary) parts.push(structured.summary + '\n')
  if (structured.details) parts.push('---\n\n' + structured.details + '\n')
  if (structured.actions?.length) {
    parts.push('## Options\n')
    structured.actions.forEach((a, i) => parts.push(`${i + 1}. ${a.label}\n`))
  }
  const blob = new Blob([parts.join('\n')], { type: 'text/markdown' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = (structured.title || 'response').replace(/[^a-zA-Z0-9_-]/g, '_') + '.md'
  a.click()
  URL.revokeObjectURL(url)
}

function sendActionReply(label) {
  if (!selectedSession.value) return
  draft.value = label
  send()
}

// ─── Decision fork menu: batched multi-decision answers ───
// Answers are keyed per-card (turn) then per-decision-id. A Proxy-backed default
// ensures decisionAnswers[cardKey] always exists for the template.
const decisionAnswers = reactive({})
const decisionSubmitted = reactive({})
function decKey(turn) {
  const k = String(turn.turn_index ?? turn.timestamp ?? 'live')
  if (!decisionAnswers[k]) decisionAnswers[k] = {}
  return k
}
function pickOption(turn, dec, opt) {
  const k = decKey(turn)
  const val = opt.id || opt.label
  const cur = decisionAnswers[k][dec.id]
  // Toggle off if re-picking the same option.
  decisionAnswers[k][dec.id] = { ...(cur || {}), selected: cur?.selected === val ? null : val }
}
function setComment(turn, dec, text) {
  const k = decKey(turn)
  decisionAnswers[k][dec.id] = { ...(decisionAnswers[k][dec.id] || {}), comment: text }
}
function decisionAnyAnswered(turn) {
  const ans = decisionAnswers[decKey(turn)] || {}
  return Object.values(ans).some(a => a && (a.selected || (a.comment && a.comment.trim())))
}
function submitDecisions(turn) {
  const k = decKey(turn)
  if (decisionSubmitted[k]) return
  const decs = turn.structured?.decisions || []
  const ans = decisionAnswers[k] || {}
  const lines = ['[Decision responses]', '']
  for (const d of decs) {
    const a = ans[d.id] || {}
    const label = optLabel(d, a.selected)
    const parts = []
    if (label) parts.push(label)
    if (a.comment && a.comment.trim()) parts.push(`"${a.comment.trim()}"`)
    lines.push(`- ${d.question} → ${parts.length ? parts.join(' — ') : '(no preference)'}`)
  }
  decisionSubmitted[k] = true
  draft.value = lines.join('\n')
  send()
}
function optLabel(dec, selected) {
  if (!selected) return ''
  const o = (dec.options || []).find(o => (o.id || o.label) === selected)
  return o ? o.label : selected
}

function workspaceFileUrl(filePath) {
  // Strip leading /workspace/ prefix if present, then build the proxy URL
  // Each path segment is individually encoded to preserve slashes
  let rel = filePath
  if (rel.startsWith('/workspace/')) rel = rel.slice('/workspace/'.length)
  else if (rel.startsWith('/')) rel = rel.slice(1)
  const encoded = rel.split('/').map(encodeURIComponent).join('/')
  const token = localStorage.getItem('enclave_token') || ''
  return `/api/chat/${selectedSession.value}/file/${encoded}?token=${encodeURIComponent(token)}`
}

// Render a user-attached image. Locally-queued sends carry a blob:/data: preview
// URL (used as-is); persisted/reloaded turns carry a workspace path served via
// the file proxy.
function userImageUrl(img) {
  if (/^(blob:|data:|https?:)/i.test(img)) return img
  return workspaceFileUrl(img)
}

function mediaUrl(mxcUrl) {
  // Convert mxc://server/mediaId to proxied URL with auth
  if (!mxcUrl || !mxcUrl.startsWith('mxc://')) return ''
  const parts = mxcUrl.slice('mxc://'.length).split('/')
  if (parts.length < 2) return ''
  const token = localStorage.getItem('enclave_token') || ''
  return `/api/chat/media/${parts[0]}/${parts[1]}?token=${encodeURIComponent(token)}`
}

// Gallery / lightbox
const lightboxImage = ref(null) // stores the URL of the currently viewed image

const galleryImages = computed(() => {
  const imgs = []
  // Collect from turns
  for (const turn of turns.value) {
    if (turn.structured?.images?.length) {
      for (const img of turn.structured.images) imgs.push(workspaceFileUrl(img))
    }
    if (turn.structured?.actions?.length) {
      for (const a of turn.structured.actions) {
        if (a.image) imgs.push(workspaceFileUrl(a.image))
      }
    }
    // Persisted events
    const evts = turnEvents.value[turn.turn_index]
    if (evts) {
      for (const seg of evts) {
        for (const evt of (seg.events || [])) {
          if (evt.type === 'file_send' && evt.data?.mimetype?.startsWith('image/')) {
            if (evt.data.file_path) imgs.push(workspaceFileUrl(evt.data.file_path))
            else if (evt.data.mxc_url) imgs.push(mediaUrl(evt.data.mxc_url))
          }
        }
      }
    }
  }
  // Collect from live events
  for (const evt of liveEvents.value) {
    if (evt.type === 'file_send' && evt.mimetype?.startsWith('image/')) {
      if (evt.filePath) imgs.push(workspaceFileUrl(evt.filePath))
      else if (evt.mxcUrl) imgs.push(mediaUrl(evt.mxcUrl))
    }
  }
  return imgs
})

const lightboxIndex = computed(() => {
  if (!lightboxImage.value) return -1
  const idx = galleryImages.value.indexOf(lightboxImage.value)
  return idx >= 0 ? idx : 0
})

const lightboxImageUrl = computed(() => lightboxImage.value || '')

function openLightbox(url) {
  lightboxImage.value = url
}

function lightboxPrev() {
  const idx = lightboxIndex.value
  if (idx > 0) lightboxImage.value = galleryImages.value[idx - 1]
}

function lightboxNext() {
  const idx = lightboxIndex.value
  if (idx < galleryImages.value.length - 1) lightboxImage.value = galleryImages.value[idx + 1]
}

function onLightboxKey(e) {
  if (!lightboxImage.value) return
  if (e.key === 'Escape') lightboxImage.value = null
  else if (e.key === 'ArrowLeft') lightboxPrev()
  else if (e.key === 'ArrowRight') lightboxNext()
}

function formatTime(ts) {
  if (!ts) return ''
  const d = new Date(ts)
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

// Per-turn fusion trace expand state, keyed "<turn_index>:<run_index>".
function isCardFusionOpen(ti, ri) {
  return !!expandedFusion.value[`${ti}:${ri}`]
}
function toggleCardFusion(ti, ri) {
  const k = `${ti}:${ri}`
  expandedFusion.value[k] = !expandedFusion.value[k]
}

// Auto Fusion trace for a rendered turn. The durable reload path binds it into
// turnMeta[turn_index] (by timestamp); the live WS flow has no `turn` event, so
// it's carried inline on the response bubble (turn.fusions / turn.complexity).
// Prefer turnMeta when present so a reload's canonical binding wins.
function turnFusions(turn) {
  return turnMeta.value[turn?.turn_index]?.fusions || turn?.fusions || []
}
function turnComplexity(turn) {
  return turnMeta.value[turn?.turn_index]?.complexity || turn?.complexity || null
}
</script>

<style scoped>
.chat-view {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  overflow-x: hidden;
}

/* Floating agent status pill (sits above the messages, does not scroll). */
.agent-status-float {
  position: absolute;
  top: 0.5rem;
  left: 50%;
  transform: translateX(-50%);
  z-index: 5;
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.78rem;
  color: #999;
  padding: 0.2rem 0.7rem;
  border-radius: 12px;
  background: var(--bg-sidebar);
  border: 1px solid var(--border);
  pointer-events: none;
}

/* Per-message Auto Fusion complexity badge: pushed to the far right of the
   timestamp row. The badge itself is styled in ComplexityBadge.vue. */
.meta-cx { margin-left: auto; }

/* Fusion trace attached to a message card (model combo + tappable outcomes). */
.fusion-block.card-fusion { margin: 0.15rem 0 0.5rem; }

/* Fusion run card (model combo + tappable trace). */
.fusion-block {
  border: 1px solid var(--accent);
  border-radius: var(--radius-sm);
  background: var(--bg-card, #1e1e24);
  margin: 0.25rem 0;
}
.fusion-block .event-header {
  display: flex; align-items: center; gap: 0.5rem;
  padding: 0.5rem 0.7rem; cursor: pointer;
}
.fusion-models {
  font-size: 0.78rem; color: var(--text-secondary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  flex: 1; min-width: 0;
}
.fusion-trace { padding: 0 0.7rem 0.6rem; }
.fusion-trace-meta {
  font-size: 0.72rem; color: var(--text-muted, #5c6078);
  margin: 0.25rem 0 0.5rem;
}
.fusion-section { margin: 0.3rem 0; }
.fusion-section summary {
  cursor: pointer; font-size: 0.82rem; color: var(--text-secondary);
  padding: 0.2rem 0;
}
.fusion-content {
  font-size: 0.85rem; padding: 0.3rem 0 0.3rem 0.8rem;
  border-left: 2px solid var(--border);
}

.agent-status-float .status-indicator {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #666;
}

/* Artifacts handle on the right edge of the chat pane. */
.doc-tools {
  position: absolute;
  top: 50%;
  right: 0;
  transform: translateY(-50%);
  z-index: 6;
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  align-items: flex-end;
}

.doc-grip {
  background: var(--bg-sidebar);
  border: 1px solid var(--border);
  border-right: none;
  border-radius: 8px 0 0 8px;
  cursor: pointer;
  padding: 0.7rem 0.45rem;
  line-height: 0;
  box-shadow: -1px 0 4px rgba(0,0,0,0.15);
  display: flex;
  align-items: center;
  justify-content: center;
}

.doc-grip:hover {
  background: var(--bg-hover);
}

/* The grip glyph: two columns of dots, like a drag handle. */
.grip-dots {
  width: 6px;
  height: 22px;
  background-image: radial-gradient(var(--text-secondary) 1px, transparent 1.2px);
  background-size: 3px 5px;
  background-repeat: repeat;
  opacity: 0.8;
}

.doc-grip.open .grip-dots {
  background-image: radial-gradient(var(--accent) 1px, transparent 1.2px);
}

/* Mobile portrait: the panel pulls down from the top, so the grip sits at the
   top-center and is a larger, easier tap target with a horizontal grip glyph. */
.doc-tools.mobile-portrait {
  top: 0;
  right: auto;
  left: 50%;
  transform: translateX(-50%);
  flex-direction: row;
  align-items: flex-start;
}

.doc-tools.mobile-portrait .doc-grip {
  border: 1px solid var(--border);
  border-top: none;
  border-radius: 0 0 10px 10px;
  padding: 0.7rem 1.4rem;
  box-shadow: 0 1px 4px rgba(0,0,0,0.15);
}

.doc-tools.mobile-portrait .grip-dots {
  width: 28px;
  height: 6px;
  background-size: 5px 3px;
}

.doc-tools.mobile-portrait .doc-orient-btn {
  display: none;
}

.doc-orient-btn {
  background: var(--bg-sidebar);
  border: 1px solid var(--border);
  border-right: none;
  border-radius: 8px 0 0 8px;
  cursor: pointer;
  padding: 0.5rem 0.4rem;
  font-size: 1rem;
  line-height: 1;
}

/* Document list shown inside the side panel before a doc is picked. */
.doc-list-panel {
  display: flex;
  flex-direction: column;
  background: var(--bg-card, #1e1e24);
  border-left: 1px solid var(--border);
  overflow-y: auto;
  min-width: 0;
  min-height: 0;
}

.spec-pin-panel {
  display: flex;
  flex-direction: column;
  background: var(--bg-card, #1e1e24);
  border-left: 1px solid var(--border);
  min-width: 0;
  min-height: 0;
}
.spec-pin-body {
  overflow-y: auto;
  padding: 0.85rem 1rem;
  font-size: 0.86rem;
  line-height: 1.6;
}
.spec-pin-body :deep(h1) { font-size: 1.15rem; }
.spec-pin-body :deep(h2) { font-size: 1.02rem; }
.spec-pin-body :deep(h3) { font-size: 0.92rem; }
.spec-pin-body :deep(pre) {
  background: var(--bg-main, #15151a);
  border: 1px solid var(--border, #333);
  border-radius: 6px;
  padding: 0.6rem;
  overflow-x: auto;
}
.spec-pin-body :deep(code) { font-family: monospace; font-size: 0.85em; }

.doc-list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.75rem 1rem;
  border-bottom: 1px solid var(--border);
  font-weight: 600;
  font-size: 0.9rem;
}

.doc-panel-close {
  background: none;
  border: none;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 0.95rem;
}

.doc-panel-close:hover { color: var(--text-primary); }

.doc-list-empty {
  padding: 1rem;
  font-size: 0.85rem;
}

.doc-list-item {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  text-align: left;
  background: none;
  border: none;
  border-bottom: 1px solid var(--border);
  color: inherit;
  cursor: pointer;
  padding: 0.7rem 1rem;
}

.doc-list-item:hover { background: var(--bg-hover); }

.doc-list-title { font-size: 0.9rem; }

.doc-list-file {
  font-size: 0.72rem;
  color: var(--text-secondary);
}

.chat-header {
  display: flex;
  align-items: center;
  gap: 1rem;
  margin-bottom: 1rem;
}

.chat-header h2 { margin: 0; }

/* Agent status indicator */
.agent-status {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.8rem;
  color: #999;
  padding: 0.2rem 0.6rem;
  border-radius: 12px;
  background: rgba(255,255,255,0.05);
}

.agent-status .status-indicator {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #666;
}

.agent-status.thinking .status-indicator {
  background: #f59e0b;
  animation: pulse 1.5s ease-in-out infinite;
}

.agent-status.tool .status-indicator {
  background: #3b82f6;
  animation: spin-dot 1s linear infinite;
  border-radius: 2px;
  width: 8px;
  height: 8px;
}

.agent-status.responding .status-indicator {
  background: #4ade80;
  animation: pulse 0.8s ease-in-out infinite;
}

.agent-status.waiting_user .status-indicator {
  background: #f97316;
  animation: pulse 2s ease-in-out infinite;
}

.agent-status.idle .status-indicator {
  background: #666;
}

@keyframes pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.4; transform: scale(0.8); }
}

@keyframes spin-dot {
  0% { transform: rotate(0deg); }
  100% { transform: rotate(360deg); }
}

.model-picker {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  margin-left: auto;
}

.ai-credits {
  font-size: 0.78rem;
  padding: 0.3rem 0.55rem;
  background: var(--bg-main);
  color: var(--text-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  white-space: nowrap;
  cursor: default;
}

.model-select {
  width: auto;
  max-width: 260px;
  font-size: 0.8rem;
  padding: 0.4rem 0.5rem;
  background: var(--bg-main);
  color: var(--text-primary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
}

.model-refresh {
  font-size: 1rem;
  padding: 0.25rem 0.4rem;
  background: none;
  border: 1px solid var(--border);
  color: var(--text-secondary);
  border-radius: var(--radius-sm, 4px);
  cursor: pointer;
  line-height: 1;
}
.model-refresh:hover { color: var(--text-primary); background: var(--bg-hover); }
.model-refresh:disabled { opacity: 0.5; cursor: wait; }

.messages {
  flex: 1;
  overflow-y: auto;
  padding: 1.5rem 1.5rem 1rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.turn {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.message {
  max-width: 85%;
  min-width: 0;
  border-radius: var(--radius);
  padding: 0.75rem 1rem;
}

.user-message {
  align-self: flex-end;
  background: #1a2a4a;
  border: 1px solid #2a3a5a;
}

.assistant-message {
  align-self: flex-start;
  background: var(--bg-card);
  border: 1px solid var(--border);
}

.assistant-message.major-response {
  border-left: 3px solid #4ade80;
  background: linear-gradient(90deg, rgba(74, 222, 128, 0.06) 0%, var(--bg-card) 40%);
  box-shadow: -2px 0 8px rgba(74, 222, 128, 0.08);
}

/* Structured message cards */
.structured-card {
  padding: 1rem;
}

.structured-card .message-meta {
  margin-top: 0.75rem;
  margin-bottom: 0;
}

.structured-title {
  font-size: 1.1rem;
  font-weight: 700;
  margin-bottom: 0.5rem;
  color: #e2e8f0;
}

.structured-summary {
  margin-bottom: 0.5rem;
}

.structured-summary :deep(p) {
  margin: 0.25rem 0;
}

.structured-images {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  margin: 0.5rem 0;
}

.structured-img {
  max-width: 300px;
  max-height: 225px;
  border-radius: 6px;
  cursor: zoom-in;
  transition: transform 0.15s;
}

.structured-img:hover {
  transform: scale(1.05);
}

.structured-details {
  margin: 0.5rem 0;
  border: 1px solid rgba(255,255,255,0.1);
  border-radius: 6px;
  overflow: hidden;
}

.structured-details > summary {
  padding: 0.4rem 0.75rem;
  cursor: pointer;
  font-size: 0.85rem;
  color: #94a3b8;
  background: rgba(255,255,255,0.03);
  user-select: none;
}

.structured-details > summary:hover {
  color: #e2e8f0;
  background: rgba(255,255,255,0.06);
}

.structured-details-body {
  padding: 0.75rem;
  font-size: 0.9rem;
  border-top: 1px solid rgba(255,255,255,0.05);
}

.structured-details-actions {
  display: flex;
  gap: 0.5rem;
  padding: 0.5rem 0.75rem;
  border-top: 1px solid rgba(255,255,255,0.05);
}

.btn-sm {
  font-size: 0.75rem;
  padding: 0.25rem 0.6rem;
  border: 1px solid rgba(255,255,255,0.15);
  border-radius: 4px;
  background: rgba(255,255,255,0.05);
  color: #94a3b8;
  cursor: pointer;
}

.btn-sm:hover {
  background: rgba(255,255,255,0.1);
  color: #e2e8f0;
}

.structured-actions {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  margin: 0.5rem 0;
}

.structured-action {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.3rem;
}

.action-img {
  max-width: 180px;
  max-height: 135px;
  border-radius: 4px;
  cursor: zoom-in;
}

.action-btn {
  padding: 0.4rem 1rem;
  border: 1px solid #4ade80;
  border-radius: 6px;
  background: rgba(74, 222, 128, 0.1);
  color: #4ade80;
  cursor: pointer;
  font-size: 0.85rem;
  transition: all 0.15s;
}

.action-btn:hover {
  background: rgba(74, 222, 128, 0.2);
  box-shadow: 0 0 8px rgba(74, 222, 128, 0.2);
}

/* Decision fork menu */
.decision-menu {
  margin-top: 0.6rem;
  border-top: 1px solid var(--border, #333);
  padding-top: 0.6rem;
  display: flex;
  flex-direction: column;
  gap: 0.7rem;
}
.decision-q { font-size: 0.88rem; font-weight: 600; margin-bottom: 0.35rem; }
.decision-opts { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.35rem; }
.decision-opt {
  background: var(--bg-main, #15151a);
  border: 1px solid var(--border, #333);
  color: inherit;
  padding: 0.35rem 0.8rem;
  border-radius: 16px;
  cursor: pointer;
  font-size: 0.82rem;
  transition: all 0.12s;
}
.decision-opt:hover { border-color: var(--accent, #7c9eff); }
.decision-opt.selected {
  background: var(--accent, #7c9eff);
  color: #0f0f14;
  border-color: var(--accent, #7c9eff);
  font-weight: 600;
}
.decision-freetext {
  width: 100%;
  background: var(--bg-main, #15151a);
  border: 1px solid var(--border, #333);
  border-radius: 6px;
  color: inherit;
  padding: 0.4rem 0.6rem;
  font-family: inherit;
  font-size: 0.82rem;
  box-sizing: border-box;
}
.decision-submit {
  align-self: flex-start;
  background: rgba(124, 158, 255, 0.15);
  border: 1px solid var(--accent, #7c9eff);
  color: var(--accent, #7c9eff);
  padding: 0.45rem 1.1rem;
  border-radius: 8px;
  cursor: pointer;
  font-size: 0.85rem;
  font-weight: 600;
}
.decision-submit:disabled { opacity: 0.5; cursor: default; }
.decision-submit:not(:disabled):hover { background: rgba(124, 158, 255, 0.28); }

.segment-response {
  margin: 0.4rem 0;
  padding: 0.5rem 0.75rem;
  font-size: 0.9rem;
  opacity: 0.85;
  max-width: 100%;
}

.message-meta {
  display: flex;
  gap: 0.75rem;
  margin-bottom: 0.4rem;
  font-size: 0.75rem;
}

.sender {
  font-weight: 600;
  color: var(--text-secondary);
}

.time {
  color: var(--text-muted);
}

.message-body {
  font-size: 0.9rem;
  line-height: 1.6;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.message-body :deep(p) { margin: 0 0 0.5rem; }
.message-body :deep(p:last-child) { margin: 0; }
.message-body :deep(pre) {
  background: var(--bg-main);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0.75rem;
  overflow-x: auto;
  font-size: 0.8rem;
}
.message-body :deep(code) {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.85em;
}
.message-body :deep(a) { color: var(--accent); }

/* Mermaid diagrams */
.message-body :deep(.mermaid) {
  background: var(--bg-main);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0.75rem;
  margin: 0 0 0.5rem;
  overflow-x: auto;
  text-align: center;
  line-height: normal;
}
.message-body :deep(.mermaid svg) {
  max-width: 100%;
  height: auto;
}
.message-body :deep(.mermaid:not([data-processed])) {
  white-space: pre;
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.8rem;
  text-align: left;
  color: var(--text-secondary);
}

.typing-indicator {
  animation: blink 1.2s infinite;
  color: var(--text-muted);
  font-size: 1.2rem;
  letter-spacing: 2px;
}

@keyframes blink {
  0%, 100% { opacity: 0.3; }
  50% { opacity: 1; }
}

.input-bar {
  display: flex;
  gap: 0.75rem;
  padding: 1rem 1.5rem;
  padding-bottom: max(1rem, env(safe-area-inset-bottom, 0));
  border-top: 1px solid var(--border);
  align-items: flex-end;
  max-width: 100%;
  box-sizing: border-box;
}

.input-bar .attach-btn {
  padding: 0.5rem 0.7rem;
  font-size: 1.1rem;
  cursor: pointer;
}

.input-bar textarea {
  flex: 1;
  resize: none;
  min-height: 42px;
  line-height: 1.4;
  overflow-y: hidden;
  font-family: inherit;
}

.input-bar button {
  align-self: flex-end;
  padding: 0.6rem 1.5rem;
}
/* Visible disabled state for the composer buttons (Send + attach) so a briefly
   disabled Send during delivery, or an empty draft, reads as inactive rather
   than looking clickable-but-inert. */
.input-bar button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.pending-files {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  padding: 0.5rem 0;
}

.file-chip {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0.3rem 0.5rem;
  font-size: 0.8rem;
}

.file-thumb {
  width: 32px;
  height: 32px;
  object-fit: cover;
  border-radius: 3px;
}

.file-name {
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-size {
  color: var(--text-muted);
  font-size: 0.75rem;
}

.chip-remove {
  background: none;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  padding: 0 0.2rem;
  font-size: 0.9rem;
}

.chip-remove:hover { color: var(--text-primary); }

.drop-overlay {
  position: absolute;
  inset: 0;
  background: rgba(26, 42, 74, 0.85);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 50;
  border-radius: var(--radius);
  border: 2px dashed var(--accent);
}

.drop-label {
  font-size: 1.2rem;
  color: var(--accent);
  font-weight: 600;
}

.chat-container {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  position: relative;
  overflow: hidden;
}

/* ─── Document workspace (chat ↔ artifact split) ─── */
.chat-body {
  flex: 1;
  display: flex;
  min-height: 0;
  min-width: 0;
  overflow: hidden;
}
.chat-body.no-doc { flex-direction: column; }
.chat-body.horizontal { flex-direction: row; }
.chat-body.vertical { flex-direction: column; }
.chat-body.mobile-portrait { flex-direction: column-reverse; }

.chat-body > .chat-container { flex: 1 1 auto; min-width: 0; min-height: 0; }
.chat-body.no-doc > .chat-container { flex-basis: auto; }

.outer-doc {
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
}

.outer-divider {
  flex: 0 0 auto;
  background: var(--border, #333);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 5;
}
.outer-divider.horizontal { width: 6px; cursor: col-resize; }
.outer-divider.vertical { height: 6px; cursor: row-resize; }
.outer-divider:hover { background: var(--accent, #6c8cff); }
.outer-divider .divider-grip { background: var(--bg-card, #2a2a32); border-radius: 3px; }
.outer-divider.horizontal .divider-grip { width: 2px; height: 28px; }
.outer-divider.vertical .divider-grip { height: 2px; width: 28px; }

/* On mobile portrait the split is a fixed doc-on-top stack (no drag handle). */
.chat-body.mobile-portrait > .chat-container { flex: 1 1 55%; }
.chat-body.mobile-portrait > .outer-doc { flex: 1 1 45%; }
.chat-body.mobile-portrait > .outer-divider { display: none; }

/* Header document controls */
.doc-controls { display: flex; align-items: center; gap: 0.3rem; position: relative; }
.doc-open-btn, .doc-orient-btn {
  background: var(--bg-card, #1e1e24);
  border: 1px solid var(--border, #333);
  color: inherit;
  border-radius: 6px;
  padding: 0.25rem 0.5rem;
  cursor: pointer;
  font-size: 0.95rem;
}
.doc-open-btn:hover, .doc-orient-btn:hover { border-color: var(--accent, #6c8cff); }
.doc-menu-wrap { position: relative; }
.doc-menu {
  position: absolute;
  right: 0;
  top: calc(100% + 4px);
  min-width: 220px;
  max-width: 320px;
  max-height: 50vh;
  overflow: auto;
  background: var(--bg-card, #1e1e24);
  border: 1px solid var(--border, #333);
  border-radius: 8px;
  box-shadow: 0 6px 20px rgba(0,0,0,0.4);
  z-index: 50;
  padding: 0.25rem;
}
.doc-menu-empty { padding: 0.5rem 0.6rem; font-size: 0.85rem; }
.doc-menu-item {
  display: block;
  width: 100%;
  text-align: left;
  background: none;
  border: none;
  color: inherit;
  padding: 0.4rem 0.6rem;
  border-radius: 5px;
  cursor: pointer;
  font-size: 0.85rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.doc-menu-item:hover { background: var(--bg, #121216); }
.doc-menu-item.active { color: var(--accent, #6c8cff); }

.empty-state {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}

.muted { color: var(--text-muted); }

.load-earlier {
  text-align: center;
  padding: 0.5rem 0;
}

.load-earlier button {
  font-size: 0.8rem;
}

.queued-badge {
  font-size: 0.65rem;
  color: #e8a735;
  background: rgba(232, 167, 53, 0.15);
  padding: 0.1rem 0.4rem;
  border-radius: 3px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.ask-user-block {
  max-width: 90%;
}

.permission-block {
  max-width: 90%;
  border: 1px solid rgba(232, 167, 53, 0.5);
  border-radius: var(--radius-sm, 6px);
  padding: 0.6rem 0.75rem;
}
.perm-badge {
  font-size: 0.65rem;
  color: #e8a735;
  background: rgba(232, 167, 53, 0.15);
  padding: 0.1rem 0.4rem;
  border-radius: 3px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.perm-target {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  margin: 0.4rem 0;
  flex-wrap: wrap;
}
.perm-type { font-size: 0.8rem; text-transform: capitalize; color: var(--text-muted, #adbac7); }
.perm-code {
  font-family: monospace;
  font-size: 0.82rem;
  background: rgba(255,255,255,0.06);
  padding: 1px 6px;
  border-radius: 4px;
  word-break: break-all;
}
.perm-reason { font-size: 0.82rem; color: var(--text-muted, #adbac7); margin-bottom: 0.5rem; }
.perm-actions { display: flex; flex-wrap: wrap; gap: 0.4rem; }
.perm-approve { border-color: rgba(74, 222, 128, 0.5) !important; color: #4ade80 !important; }
.perm-deny { border-color: rgba(248, 113, 113, 0.5) !important; color: #f87171 !important; }
.perm-actions button:disabled { opacity: 0.5; cursor: wait; }

.ask-badge {
  font-size: 0.65rem;
  color: #e8a735;
  background: rgba(232, 167, 53, 0.15);
  padding: 0.1rem 0.4rem;
  border-radius: 3px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.ask-question {
  font-size: 0.9rem;
  margin-bottom: 0.75rem;
  line-height: 1.5;
}

.ask-choices {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-bottom: 0.75rem;
}

.ask-choice-btn {
  font-size: 0.8rem;
  padding: 0.4rem 0.8rem;
}

.ask-freeform {
  display: flex;
  gap: 0.5rem;
}

.ask-freeform input {
  flex: 1;
  font-size: 0.85rem;
  padding: 0.4rem 0.6rem;
}

.ask-freeform button {
  padding: 0.4rem 0.8rem;
  font-size: 0.8rem;
}

/* Live streaming styles */
.live-turn {
  border-left: 2px solid var(--accent);
  padding-left: 0.5rem;
  margin-left: 0.25rem;
}

.live-event {
  margin-bottom: 0.15rem;
}

.thinking-block {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
}

.tool-block {
  overflow: hidden;
}

.file-send-preview {
  padding: 0.25rem 0;
}

.file-send-block {
  margin: 0.25rem 0;
}

.file-send-label {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  font-size: 0.75rem;
  color: var(--text-muted);
  margin-bottom: 0.2rem;
}

.file-send-img {
  max-width: 450px;
  max-height: 300px;
  border-radius: var(--radius-sm);
  cursor: zoom-in;
}

.user-images {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin-bottom: 0.4rem;
}
.user-img {
  max-width: 320px;
  max-height: 240px;
  border-radius: var(--radius-sm);
  cursor: zoom-in;
}

.event-header {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  padding: 0.15rem 0.4rem;
  cursor: pointer;
  font-size: 0.72rem;
  color: var(--text-muted);
  user-select: none;
}

.event-header:hover {
  color: var(--text-secondary);
}

.event-icon {
  flex-shrink: 0;
}

.event-label {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.expand-toggle {
  flex-shrink: 0;
  font-size: 0.6rem;
  color: var(--text-muted);
}

.thinking-content {
  padding: 0.5rem 0.6rem;
  font-size: 0.78rem;
  font-style: italic;
  color: var(--text-muted);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 200px;
  overflow-y: auto;
  border-top: 1px solid var(--border);
}

.tool-status { flex-shrink: 0; }
.tool-status.success { color: var(--success); }
.tool-status.fail { color: var(--danger); }

.tool-spinner {
  flex-shrink: 0;
  animation: pulse 1.5s ease-in-out infinite;
}

.streaming-badge {
  font-size: 0.65rem;
  color: var(--accent);
  background: rgba(99, 179, 237, 0.15);
  padding: 0.1rem 0.4rem;
  border-radius: 3px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.streaming .message-body {
  opacity: 0.95;
}

.activity-line {
  font-size: 0.78rem;
  color: var(--text-muted);
  padding: 0.25rem 0;
  font-style: italic;
  animation: fadeInOut 2s ease-in-out infinite;
}

@keyframes fadeInOut {
  0%, 100% { opacity: 0.6; }
  50% { opacity: 1; }
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

@media (max-width: 768px) {
  .chat-view { height: calc(100dvh - 3.5rem); }
  .chat-header { flex-direction: column; align-items: stretch; gap: 0.5rem; }
  .chat-header select { max-width: 100%; }
  .message { max-width: 95%; }
  .input-bar { gap: 0.4rem; }
  .input-bar button { padding: 0.5rem 1rem; }
  .file-chip .file-name { max-width: 80px; }
}

/* Persisted turn events */
.turn-events {
  margin: 0.25rem 0;
  padding-left: 0.5rem;
  border-left: 2px solid var(--border-color, #333);
}
.events-toggle {
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.2rem 0;
  font-size: 0.8rem;
  color: var(--text-secondary, #888);
  user-select: none;
}
.events-toggle:hover {
  color: var(--text-primary, #ccc);
}
.events-summary {
  font-style: italic;
}

/* Jump to latest button */
.jump-to-latest {
  position: absolute;
  bottom: 5rem;
  left: 50%;
  transform: translateX(-50%);
  background: var(--accent, #63b3ed);
  color: #fff;
  border: none;
  border-radius: 20px;
  padding: 0.5rem 1.2rem;
  font-size: 0.8rem;
  font-weight: 500;
  cursor: pointer;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
  z-index: 10;
  transition: opacity 0.2s, transform 0.2s;
}
.jump-to-latest:hover {
  transform: translateX(-50%) scale(1.05);
}
.fade-enter-active, .fade-leave-active { transition: opacity 0.2s; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

/* Collapsed live events summary */
.live-events-collapsed {
  margin-bottom: 0.5rem;
}
.collapsed-summary {
  cursor: pointer;
  font-size: 0.78rem;
  color: var(--text-muted, #888);
  user-select: none;
  padding: 0.2rem 0;
}
.collapsed-summary:hover {
  color: var(--text-primary, #ccc);
}

/* Chat container needs relative positioning for jump button */

/* Clickable images */
.clickable-img {
  cursor: zoom-in;
}
.message-body :deep(img) {
  cursor: zoom-in;
  max-width: 100%;
  border-radius: var(--radius-sm, 4px);
}

/* Lightbox gallery overlay (not scoped — teleported to body) */
</style>

<style>
/* Bug reference chips (injected via v-html; needs unscoped styles) */
.bug-ref {
  color: var(--accent, #7c9eff);
  background: rgba(124, 158, 255, 0.12);
  border: 1px solid rgba(124, 158, 255, 0.35);
  border-radius: 4px;
  padding: 0 4px;
  font-weight: 600;
  font-size: 0.92em;
  cursor: pointer;
  white-space: nowrap;
}
.bug-ref:hover { background: rgba(124, 158, 255, 0.22); }

/* Bug preview card (teleported to body) */
.bug-card {
  position: fixed;
  z-index: 10000;
  width: 320px;
  max-width: calc(100vw - 20px);
  background: #161b22;
  border: 1px solid #30363d;
  border-radius: 8px;
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.5);
  padding: 10px 12px;
  color: #e6edf3;
  font-size: 0.85rem;
}
.bug-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.bug-card-id { font-weight: 700; color: var(--accent, #7c9eff); font-family: monospace; }
.bug-card-badges { display: flex; gap: 5px; margin-left: auto; }
.bug-card-close {
  background: none; border: none; color: #8b949e; cursor: pointer;
  font-size: 0.9rem; padding: 0 2px; line-height: 1;
}
.bug-card-close:hover { color: #e6edf3; }
.bug-badge {
  font-size: 0.68rem; padding: 1px 6px; border-radius: 9px;
  text-transform: capitalize; white-space: nowrap;
}
.bug-badge.status { background: #1e3a5f; color: #7c9eff; }
.bug-badge.status.resolved, .bug-badge.status.wontfix { background: #14532d; color: #4ade80; }
.bug-badge.status.in_progress { background: #5a3a14; color: #fbbf24; }
.bug-badge.sev { background: #30363d; color: #adbac7; }
.bug-badge.sev.high, .bug-badge.sev.critical { background: #5a1e1e; color: #f87171; }
.bug-badge.sev.low { background: #21402b; color: #6ee7a8; }
.bug-card-title { font-weight: 600; margin-bottom: 5px; line-height: 1.3; }
.bug-card-preview {
  color: #adbac7; line-height: 1.4; margin-bottom: 8px;
  white-space: pre-wrap; word-break: break-word;
  max-height: 6.5em; overflow: hidden;
}
.bug-card-link {
  background: none; border: none; color: var(--accent, #7c9eff);
  cursor: pointer; padding: 0; font-size: 0.82rem; font-weight: 600;
}
.bug-card-link:hover { text-decoration: underline; }
.bugcard-fade-enter-active { transition: opacity 0.12s ease; }
.bugcard-fade-leave-active { transition: opacity 0.1s ease; }
.bugcard-fade-enter-from, .bugcard-fade-leave-to { opacity: 0; }

.lightbox-overlay {
  position: fixed;
  inset: 0;
  z-index: 9999;
  background: rgba(0, 0, 0, 0.92);
  display: flex;
  align-items: center;
  justify-content: center;
  backdrop-filter: blur(4px);
}

.lightbox-img {
  max-width: 90vw;
  max-height: 90vh;
  object-fit: contain;
  border-radius: 4px;
  box-shadow: 0 4px 30px rgba(0, 0, 0, 0.5);
  user-select: none;
}

.lightbox-close {
  position: absolute;
  top: 1rem;
  right: 1rem;
  background: rgba(255, 255, 255, 0.15);
  border: none;
  color: #fff;
  font-size: 1.5rem;
  width: 40px;
  height: 40px;
  border-radius: 50%;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.2s;
}
.lightbox-close:hover {
  background: rgba(255, 255, 255, 0.3);
}

.lightbox-nav {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  background: rgba(255, 255, 255, 0.12);
  border: none;
  color: #fff;
  font-size: 2.5rem;
  width: 50px;
  height: 70px;
  border-radius: 8px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.2s;
  user-select: none;
}
.lightbox-nav:hover {
  background: rgba(255, 255, 255, 0.25);
}
.lightbox-prev { left: 1rem; }
.lightbox-next { right: 1rem; }

.lightbox-counter {
  position: absolute;
  bottom: 1.5rem;
  left: 50%;
  transform: translateX(-50%);
  color: rgba(255, 255, 255, 0.7);
  font-size: 0.85rem;
  background: rgba(0, 0, 0, 0.5);
  padding: 0.25rem 0.75rem;
  border-radius: 12px;
}

.lightbox-fade-enter-active { transition: opacity 0.2s ease; }
.lightbox-fade-leave-active { transition: opacity 0.15s ease; }
.lightbox-fade-enter-from, .lightbox-fade-leave-to { opacity: 0; }
</style>
