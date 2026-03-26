# Enclave Roadmap

## Implemented

### Landlock Kernel Sandboxing ✅

Kernel-level filesystem restrictions for host-mode agents using Linux
Landlock LSM. Defence-in-depth against prompt injection — even compiled
binaries can't escape the sandbox.

- Module: `src/enclave/orchestrator/landlock.py`
- `apply_sandbox(scratch_dir, readonly_paths)` — one call to lock down
- `classify_path()` — pure-Python path classification for UI hints
- `is_supported()` / `get_abi_version()` — runtime detection
- Scratch dir: full RW, system paths: read-only, everything else: denied
- 23 tests including live subprocess sandbox verification

**Integration:** Call `apply_sandbox()` before exec'ing host-mode agent
subprocess. The permission handler in the agent provides the UX layer
(explains why access is denied), Landlock provides the enforcement layer.

### Display/UI Integration ✅

Desktop interaction via Wayland/Hyprland for GUI operations.

- `launch_gui` tool — launches apps on user's display (requires approval)
- `screenshot` tool — captures screen to workspace (auto-approved)
- DisplayManager with Hyprland socket detection, tmux fallback
- Approval flow mirrors sudo (user must approve in chat)

### Cron Scheduling & Timers ✅

Agents can schedule recurring callbacks and one-shot wake alarms.

- `schedule_cron` tool — min 1h interval, persistent across restarts
- `set_timer` tool — relative or absolute one-shot wake
- Scheduler auto-restores sleeping containers for callbacks

### Message Timestamps ✅

All agent messages include ISO 8601 timestamps for time-aware reasoning.

---

## Feature Gap Analysis

Compared against: Cline, Aider, Open Interpreter, Claude Code, Cursor.
Evaluated for three target workflows.

### Workflow 1: Local Dev with Agent on Shared Project

Developer and agent both working on the same codebase, committing code,
running tests, reviewing changes.

**What Enclave has:** Container isolation, dynamic mounts, permission
system, scheduling, sudo approval, nix dev environment.

**Missing features (ranked by impact):**

1. **Git Workstream Management** — Agent works on a branch, creates PRs,
   handles merge conflicts. Currently no built-in git workflow.
2. **Test Runner Integration** — Auto-detect and run project tests,
   report results in chat. Agent can run tests manually but no framework.
3. **File Change Notifications** — inotify/fanotify watcher that alerts
   the agent when the developer modifies files in the shared project.
4. **Diff Preview** — Show the agent's proposed changes as a diff in
   chat before applying, with approve/reject buttons.
5. **Project Context Indexing** — Build a searchable index of the
   codebase (symbols, dependencies) so the agent understands structure.

### Workflow 2: Remote User Interacting with PC

User is away from machine, interacting via Matrix on phone/laptop.
Machine may or may not have an active desktop session.

**What Enclave has:** Matrix chat, scheduling, timers, display/UI
(when session active), systemd auto-start, user identity.

**Missing features (ranked by impact):**

1. **System Status Dashboard** — On-demand report: uptime, CPU, memory,
   disk, network, running services, pending updates.
2. **File Browser/Transfer** — Browse host filesystem, upload/download
   files via Matrix. Currently requires manual mount + cat.
3. **Notification Forwarding** — Forward desktop notifications,
   important system events, or service alerts to Matrix.
4. **Session Resumption on Reboot** — Auto-restore agent sessions after
   system reboot (not just enclave restart). Currently requires manual
   reconnection if the container was running pre-reboot.
5. **Wake-on-LAN / Remote Power** — If the machine is off, trigger WoL
   from a secondary always-on device or cloud relay.

### Workflow 3: Headless Server Managing Services

Enclave runs on a server managing containers, services, backups.
No desktop. Interaction purely via Matrix.

**What Enclave has:** Container management, scheduling, permission
system, Landlock sandboxing, host-mode security.

**Missing features (ranked by impact):**

1. **Service Monitoring** — Watch systemd units, podman containers,
   Docker services. Alert on failures, auto-restart with approval.
2. **Log Aggregation** — Tail and search journalctl/container logs,
   summarize errors, alert on patterns.
3. **Automated Runbooks** — Predefined incident response playbooks
   the agent can execute (with approval) when conditions are met.
4. **Resource Monitoring** — CPU/memory/disk trending with alerts.
   Agent can take corrective action (clear logs, restart services).
5. **Backup Management** — Schedule and verify backups, test restores,
   alert on failures.

### Cross-Cutting Features (All Workflows)

Ranked by overall value:

1. **MCP (Model Context Protocol) Server** — Expose Enclave as an MCP
   server so external tools (VS Code, other agents) can interact.
   Biggest interop win.
2. **Multi-Model Support** — Allow switching between LLM providers
   (Anthropic, OpenAI, local via Ollama). Currently tied to Copilot SDK.
3. **Memory Containers** — Shared SQLite memory per user, cross-session
   learning, key memories in every system prompt, auto-dreaming.
   Inspired by Claude Code's memory model but container-aware.
4. **Cost/Token Tracking** — Track LLM usage per session/project.
   Set budgets and alerts.
5. **Audit Log** — Structured log of all agent actions (commands run,
   files modified, permissions granted) for security review.
6. **Plugin System** — User-defined tools/extensions without modifying
   core code. Drop a Python file in a plugins dir.
7. **Web Dashboard** — Simple web UI for session management, logs,
   permissions — complement to Matrix.

---

## Backlog

### Host Mode — Agent Execution

**Priority:** High | **Effort:** Medium

Wire up the orchestrator to run agents directly on the host when the
"host" profile is selected (image=""). Currently only config and prompts
exist — the container manager needs to handle this case by spawning a
subprocess instead of a podman container.

### Git Workstream Management

**Priority:** High | **Effort:** Medium

Agent creates/manages branches for its work. Creates PRs for review.
Detects and reports merge conflicts. Integrates with GitHub/GitLab APIs.

### System Status Dashboard

**Priority:** High | **Effort:** Low

Agent tool that gathers system info (uptime, resources, services,
updates) and presents a formatted report. Foundation for monitoring.

### Service Monitoring

**Priority:** High | **Effort:** Medium

Watch systemd units and container health. Alert on failures. Offer
auto-restart with approval. Foundation for server management workflow.

### MCP Server

**Priority:** Medium | **Effort:** High

Expose Enclave capabilities via Model Context Protocol so external
tools can use Enclave-managed agents as tools.

### Memory Containers

**Priority:** High | **Effort:** High

Shared cross-session memory per user, stored in SQLite. Agents
accumulate knowledge across sessions — user preferences, project
conventions, personal facts, debugging insights. Inspired by
[Claude Code's memory model](https://code.claude.com/docs/en/memory)
but adapted for Enclave's multi-container architecture.

**Core concepts:**

- **Memory database** — SQLite file per user at
  `{data_dir}/memory/{matrix_user_id}.db`. Tables: `memories`
  (id, category, content, source_session, created_at, last_accessed,
  access_count, is_key_memory). Shared across all that user's sessions.

- **Key memories** — Memories flagged `is_key_memory=true` are injected
  into every session's system prompt via SystemMessageAppendConfig.
  Limited to first ~200 lines (like Claude Code). Agent sees them as
  persistent context. Examples: user's name, coding preferences,
  project architecture decisions.

- **Auto-dreaming** — Automatic memory extraction. The agent's context
  window is scanned for noteworthy information during two events:
  1. **Consolidation** — before context window overflow / summarization
  2. **Session end** — after idle timeout, before container shutdown

  The scan uses a focused prompt asking the LLM to extract:
  - Personal facts (name, family, preferences)
  - Technical preferences (languages, frameworks, style)
  - Project-specific knowledge (architecture, conventions)
  - Debugging insights and solutions
  - Workflow patterns

  Extracted memories are deduplicated against existing entries and
  stored with source session attribution.

- **Configuration:**
  ```yaml
  memory:
    auto_memory: true      # enable memory persistence
    auto_dreaming: true    # enable automatic extraction
    key_memory_limit: 200  # max lines in system prompt
  ```
  Both settings off by default. Auto-dreaming requires auto_memory.

**Architecture:**

```
┌──────────────┐    IPC     ┌──────────────┐
│   Agent A    │◄──────────►│              │
│  (session 1) │            │  Orchestrator │
└──────────────┘            │              │
                            │  ┌─────────┐ │
┌──────────────┐    IPC     │  │ Memory  │ │
│   Agent B    │◄──────────►│  │ Store   │ │
│  (session 2) │            │  │ (SQLite)│ │
└──────────────┘            │  └─────────┘ │
                            └──────────────┘
```

- Orchestrator owns the database (single-writer, avoids corruption)
- Agents interact via IPC: MEMORY_STORE, MEMORY_QUERY, MEMORY_LIST
- Key memories loaded by orchestrator and injected into prompt at
  session creation time
- Dreaming happens orchestrator-side: receives context dump from agent,
  runs extraction, stores results

**IPC messages (new):**
- `MEMORY_STORE` — agent stores a memory (content, category, is_key)
- `MEMORY_QUERY` — agent searches memories (keyword/category)
- `MEMORY_LIST` — list key memories or recent memories
- `MEMORY_DELETE` — remove a memory by ID
- `DREAM_REQUEST` — agent sends context for dreaming extraction
- `DREAM_COMPLETE` — orchestrator returns extracted memories

**Agent tools:**
- `remember(content, category, is_key)` — store a memory manually
- `recall(query, category)` — search memories
- `forget(memory_id)` — delete a memory
- Auto-dreaming is transparent — no tool call needed

**Categories:** personal, technical, project, workflow, debug, other

### File Change Notifications

**Priority:** Medium | **Effort:** Low

inotify-based watcher that alerts agents when files in mounted
directories change. Enables reactive workflows.

### Audit Log

**Priority:** Medium | **Effort:** Low

Structured JSON log of all agent actions, permission requests,
and approvals. Essential for security review and compliance.
