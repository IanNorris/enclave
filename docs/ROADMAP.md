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

1. **MCP (Model Context Protocol) Server** ✅ — Expose Enclave as an MCP
   server so external tools (VS Code, other agents) can interact.
   Module: `src/enclave/orchestrator/mcp_server.py`, `enclavectl mcp`.
2. **Multi-Model Support** — Allow switching between LLM providers
   (Anthropic, OpenAI, local via Ollama). Currently tied to Copilot SDK.
3. **Memory Containers** ✅ — Shared SQLite memory per user, cross-session
   learning, key memories in every system prompt, auto-dreaming.
4. **Management Dashboard** — Web UI with LDAP auth, live container
   monitoring, disk usage, session management, Matrix room cleanup.
5. **Cost/Token Tracking** ✅ — Track LLM usage per session/project.
   Set budgets and alerts. Module: `src/enclave/common/cost_tracker.py`.
6. **Audit Log** ✅ — Structured JSONL log of all agent actions (commands,
   permissions, tool calls) for security review.
   Module: `src/enclave/common/audit.py`.
7. **Plugin System** ✅ — User-defined tools via drop-in Python files
   in `{workspace}/.enclave/plugins/` or `~/.config/enclave/plugins/`.
   Module: `src/enclave/agent/plugins.py`.
8. **System Status Dashboard** ✅ — Agent tool for host system info
   (uptime, CPU, memory, disk, services, updates).

---

## Backlog

### Host Mode — Agent Execution ✅

**Priority:** High | **Effort:** Medium | **Status:** Implemented

Agents can run directly on the host when the "host" profile is selected
(image=""). The container manager spawns a subprocess instead of a
podman container, with Landlock kernel sandboxing applied automatically.
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

### Management Dashboard

**Priority:** High | **Effort:** High

Web-based management UI for Enclave administrators. Provides real-time
visibility into running agents, resource usage, and session lifecycle
management. Complements Matrix chat — chat is for agent interaction,
the dashboard is for operations.

**Authentication — LDAP:**
- Users login with their actual system username/password
- LDAP bind against the host's LDAP/AD server (configurable)
- Maps LDAP user → existing `UserMapping` in enclave config
- Session cookies (JWT or signed cookie) with configurable expiry
- Config:
  ```yaml
  dashboard:
    enabled: true
    bind: "0.0.0.0:8443"
    tls_cert: "/etc/enclave/dashboard.crt"  # or auto-generate self-signed
    tls_key: "/etc/enclave/dashboard.key"
    auth:
      method: "ldap"          # ldap | local | none
      ldap_url: "ldap://localhost:389"
      ldap_base_dn: "dc=example,dc=com"
      ldap_user_filter: "(uid={username})"
  ```
- Fallback: `local` auth using PAM or config-file credentials
- `none` for trusted networks (development only)

**Dashboard Views:**

1. **Sessions Overview** — All active/stopped sessions
   - Status (running/stopped/crashed), profile, user, created_at
   - Real-time resource usage: CPU%, memory, disk per container
   - Quick actions: stop, restart, remove, view logs
   - Data source: `ContainerManager.list_sessions()` +
     `podman stats --no-stream --format json`

2. **Session Detail** — Deep dive into one session
   - Live container stats (top-like: CPU, memory, PID tree)
   - Recent agent activity (last N messages/tool calls)
   - Disk usage breakdown (workspace size, state dir, container layers)
   - Permission history (grants, denials, pending)
   - Logs tail (podman logs --follow, streamed via WebSocket)

3. **System Overview** — Host-level health
   - CPU, memory, disk, network utilisation
   - Total containers, total disk, Enclave uptime
   - Scheduler status (active crons, pending timers)

4. **Matrix Room Cleanup** — Manage chat room lifecycle
   - List all rooms the bot is in (via matrix-nio `joined_rooms`)
   - Show which rooms have active sessions vs archived/stopped
   - Bulk actions: leave room, archive (leave + mark in DB)
   - Auto-archive policy: rooms for sessions stopped > N days
   - Adds `room_leave()` wrapper to EnclaveMatrixClient
   - Option to tombstone rooms (Matrix `m.room.tombstone` event)

**Architecture:**

```
┌──────────┐    HTTPS    ┌──────────────┐
│  Browser  │◄──────────►│  Dashboard   │
│  (admin)  │            │  (FastAPI)   │
└──────────┘    WSS      │              │
     │       ◄──────────►│  WebSocket   │
     │                   │  (live logs) │
     │                   └──────┬───────┘
     │                          │ internal API
     │                   ┌──────▼───────┐
     │                   │ Orchestrator  │
     │                   │ (existing)    │
     │                   └──────────────┘
```

- FastAPI app running as a separate thread/process in the orchestrator
- Or standalone service sharing the same config + session store
- WebSocket endpoint for live log streaming and stats updates
- REST API: /api/sessions, /api/sessions/{id}/stats,
  /api/sessions/{id}/logs, /api/system, /api/rooms
- Frontend: lightweight (htmx + alpine.js or similar), no heavy SPA

**Implementation phases:**
1. REST API + LDAP auth (no frontend) — immediately useful for scripts
2. Basic dashboard (sessions list, system overview)
3. Live stats + log streaming via WebSocket
4. Matrix room cleanup UI + auto-archive policy

### File Change Notifications

**Priority:** Medium | **Effort:** Low

inotify-based watcher that alerts agents when files in mounted
directories change. Enables reactive workflows.

### Specialized Sub-Agent Containers

**Priority:** High | **Effort:** High

Enable agents to spawn isolated child containers for specific tasks.
Infrastructure exists (SubAgentManager, thread tracking, SDK events)
but the agent-facing request mechanism isn't wired up.

**What exists:**
- `SubAgentManager` class in `sub_agents.py` — spawns containers,
  tracks parent-child relationships, manages threads
- SDK `SUBAGENT_STARTED`/`COMPLETED` events flow through IPC
- Thread routing: sub-agent activity posts to its own Matrix thread
- `_subagent_threads` tracking in router

**What's missing:**
- `SUB_AGENT_REQUEST` handler in router (defined, not dispatched)
- Agent tool to request sub-agent spawn with parameters
- Specialized container profiles (network-only, workspace-isolated)

**First use case: Web Research Agent**
- Network access, no workspace mount
- Can only produce a single markdown file as output
- Parent agent consumes the result after completion
- Result is scanned for prompt injection by a second specialized
  agent before being passed to the parent
- Container profile: `research` with `has_network=true`,
  `has_workspace=false`, restricted output

**Prompt injection scanner:**
- Separate lightweight container or inline LLM call
- Scans sub-agent output for hidden instructions, encoded payloads,
  social engineering attempts
- Flags suspicious content for human review before passing to parent
- Defence-in-depth: even if the research agent is compromised by a
  malicious website, the injection can't reach the parent agent

**Architecture:**
```
Parent Agent
    │
    ├──► Research Agent (network, no workspace)
    │         │
    │         └── markdown result
    │                │
    │         Injection Scanner
    │                │
    │         ◄── clean result
    │
    └── continues with verified data
```

### Nix Packaging

**Priority:** High | **Effort:** Medium

Package Enclave for Nix users. Enables declarative installation,
reproducible builds, and integration with NixOS/home-manager.

**Phase 1: Personal flake (dev)**
- `flake.nix` at repo root
- `buildPythonApplication` for the orchestrator
- Dev shell with all dependencies (`nix develop`)
- Overlay for easy pinning from external flakes
- Install: `nix profile install github:IanNorris/enclave`

**Phase 2: NixOS module**
- Declarative systemd service (`services.enclave.enable = true`)
- Configuration via Nix attributes → generates enclave.yaml
- Automatic user/group creation, state directory setup
- Podman integration (ensures podman is available)
- Example:
  ```nix
  services.enclave = {
    enable = true;
    matrix.homeserver = "https://matrix.example.com";
    matrix.userId = "@bot:example.com";
    memory.autoMemory = true;
    idleTimeout = 7200;
  };
  ```

**Phase 3: Container images via Nix**
- Build agent container images with `pkgs.dockerTools.buildImage`
- Reproducible, layered, minimal images
- No Dockerfile needed — Nix handles dependency resolution
- Publish to a container registry or build locally

**Phase 4: Home-manager module**
- User-level installation without root
- `systemd.user.services.enclave` integration
- Personal config in `~/.config/enclave/`

**Approach for now:**
Start with Phase 1 — a `flake.nix` that you can reference from
your system config. The orchestrator, container images, and systemd
unit are all buildable from the flake.

### Audit Log

**Priority:** Medium | **Effort:** Low

Structured JSON log of all agent actions, permission requests,
and approvals. Essential for security review and compliance.
