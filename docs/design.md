# Copilot Agent System — High-Level Design

## Vision
A system where AI agent instances run on a Linux PC, controlled via Matrix/Element,
powered by the GitHub Copilot SDK (Python). Each agent is sandboxed in a podman
container with explicitly approved permissions. A host-level orchestrator manages
the agents, handles Matrix communication, and brokers privilege escalation.

## Constraints & Decisions
- **Language:** Python (Copilot SDK support, Linux ecosystem, user familiarity)
- **AI Engine:** GitHub Copilot SDK (`pip install github-copilot-sdk`)
- **Chat UI:** Matrix + Element (E2EE, threads, self-hostable)
- **Sandboxing:** Podman (rootless containers)
- **Host OS:** Arch Linux, Hyprland/Wayland

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│                  Element (phone/web)             │
└──────────────────────┬──────────────────────────┘
                       │ E2EE
                       ▼
┌─────────────────────────────────────────────────┐
│            Matrix Homeserver (Conduit)           │
└──────────────────────┬──────────────────────────┘
                       │ E2EE
                       ▼
┌─────────────────────────────────────────────────┐
│              ORCHESTRATOR (host)                 │
│  Python · matrix-nio · runs as `aeon` user      │
│                                                  │
│  ┌─────────────┐ ┌───────────┐ ┌─────────────┐ │
│  │ Matrix      │ │ Container │ │ Display     │ │
│  │ Bridge      │ │ Manager   │ │ Manager     │ │
│  └─────────────┘ └───────────┘ └─────────────┘ │
│  ┌─────────────┐ ┌───────────┐                  │
│  │ Permission  │ │ Session   │                  │
│  │ Controller  │ │ Store     │                  │
│  └─────────────┘ └───────────┘                  │
└───────┬──────────────┬──────────────┬───────────┘
        │              │              │
   Unix Socket    Unix Socket    Unix Socket
        │              │              │
        ▼              ▼              ▼
┌──────────┐    ┌──────────┐    ┌──────────────┐
│ Agent    │    │ Agent    │    │ Priv Broker  │
│ Pod #1   │    │ Pod #2   │    │ (root svc)   │
│ Project A│    │ Project B│    │ systemd unit │
│ (podman) │    │ (podman) │    └──────────────┘
└──────────┘    └──────────┘
```

### The Three Trust Zones

1. **User Zone** — Element client, E2EE, full trust
2. **Orchestrator Zone** — Runs on host as unprivileged `aeon` user
   - Manages Matrix connection
   - Spawns/destroys agent containers
   - Routes messages
   - Controls file access (what containers can see)
3. **Agent Zone** — Podman containers, sandboxed
   - Has Copilot SDK + custom tools
   - Can ONLY see files explicitly mounted by orchestrator
   - Communicates with orchestrator via Unix socket
4. **Privileged Zone** — Root broker daemon (systemd)
   - Only acts on orchestrator requests + user approval via Matrix

---

## Component Detail

### 1. Orchestrator (host process)

The central coordinator. Runs as a systemd user service.

**Responsibilities:**
- Matrix E2EE client (matrix-nio)
- Spawns podman containers for agent sessions
- Routes Matrix messages ↔ agent containers
- Manages the "workspace" directories (what files each agent can see)
- Forwards privilege requests to the root broker
- Detects Hyprland session for GUI app launching

**NOT responsible for:** AI reasoning (that's the Copilot SDK inside the container)

### 2. Agent Container (podman)

Each agent instance is a podman container running:
- Python + Copilot SDK
- Custom MCP tools (file ops, shell, web, etc.)
- A thin IPC client that talks to the orchestrator

**Container image:** Pre-built with Copilot CLI + SDK + common tools.

**What it sees:**
- `/workspace` — bind-mounted project directory (read-write, approved paths only)
- `/socket/orchestrator.sock` — Unix socket to request permissions, send output
- `/tmp` — ephemeral scratch space
- No network by default (or restricted to specific endpoints)
- No access to host filesystem beyond /workspace

**Lifecycle:**
```
User: "new session for project-foo"
  → Orchestrator creates /home/aeon/workspaces/project-foo/
  → Orchestrator runs: podman run --userns=keep-id \
      -v /home/aeon/workspaces/project-foo:/workspace:Z \
      -v /run/aeon/agent-abc.sock:/socket/orchestrator.sock \
      --network=none \
      aeon-agent:latest
  → Agent starts, loads Copilot SDK, waits for messages
  → User chats → orchestrator forwards → agent processes → response
```

### 3. Permission Controller (the sandboxing secret sauce)

**Validated approach: Shared mount propagation + bind mounts.**

The workspace directory is mounted into the container with `bind-propagation=shared`.
The orchestrator can then `mount --bind` paths into the workspace at runtime, and
`umount` to revoke — all without restarting the container.

**How it works:**
1. On host: `sudo mount --bind <workspace> <workspace> && sudo mount --make-shared <workspace>`
2. Container starts with: `--mount type=bind,src=<workspace>,dst=/workspace,bind-propagation=shared`
3. To grant access: `sudo mount --bind /home/ian/projects/foo <workspace>/foo`
4. To revoke: `sudo umount <workspace>/foo`
5. Changes are **instant** — container sees new mounts appear/disappear in real time

**Flow:**
- Agent requests access via socket: `{"request": "mount", "path": "/home/ian/projects/foo"}`
- Orchestrator posts reaction-based approval to Matrix
- User approves → orchestrator asks **priv broker** to do the bind mount (requires root)
- Agent can now see it at `/workspace/foo`
- Revocation: orchestrator asks priv broker to umount — files vanish instantly

**Why this is better than the original approaches:**
- No container restart needed (unlike Approach C)
- No FUSE complexity (unlike Approach B)
- Real file access, not copies — changes reflect both ways
- Revocation is instant and complete
- Uses the priv broker we're already building

**Critical design principle:** All permissions are managed by the **orchestrator**
(external to the container), never by the agent itself. The orchestrator maintains
a permission database per-session with entries that can be added, revoked, or
modified at any time without restarting the container.

**Approval UI — Reaction-based**:
Polls (MSC3381) are unreliable in E2EE rooms. Instead, use reactions:

```
🔒 Permission Request

Project Hello World wants to access:
cdn.github.com/bla/bla/bla

React to choose:
1️⃣ Approve once
2️⃣ Approve for this project
3️⃣ Approve regex: *.github.com
❌ Deny
```

Bot seeds the reaction emojis so user just taps. For regex approval,
bot suggests a pattern based on the URL; user can override by replying
with a custom pattern.

**Permission types (stored in orchestrator DB):**
- `once` — single-use, auto-revoked after request completes
- `session` — valid for the current session only
- `project` — persisted across sessions for this project
- `pattern` — regex-based rule (e.g., `*.github.com`, `/home/ian/projects/*`)

**Management interface** (control room commands):
- `!perms <project>` — list active permissions for a project
- `!revoke <id>` — revoke a specific permission
- `!rules` — list all persistent permission rules
- `!rules add <pattern> <scope>` — add a rule without a request
- `!rules rm <id>` — remove a rule

### 4. Privilege Broker (root daemon)

Separate systemd service running as root.

```
[Unit]
Description=Aeon Privilege Broker
After=network.target

[Service]
ExecStart=/usr/local/bin/aeon-priv-broker
User=root
RuntimeDirectory=aeon-priv

[Install]
WantedBy=multi-user.target
```

**Flow:**
1. Agent (in container) → orchestrator socket: "I need to run `pacman -Syu`"
2. Orchestrator → priv broker (Unix socket at `/run/aeon-priv/broker.sock`)
3. Broker → Matrix (via its own socket to orchestrator): approval request
4. User approves in Element
5. Broker executes, returns stdout/stderr/exit code
6. Result flows back through to the agent

**Security:**
- Socket permissions: only `aeon` user can connect
- Command allowlist/denylist in `/etc/aeon/priv-broker.conf`
- All executions logged to journald with full context
- 5-minute approval timeout
- Rate limiting

### 5. Display Manager

Runs inside the orchestrator. Detects and bridges to the desktop.

**When Hyprland is active:**
- Discovers session via `/run/user/1000/hypr/` or `HYPRLAND_INSTANCE_SIGNATURE`
- Launches GUI apps via `hyprctl dispatch exec`
- Takes screenshots via `grim`, sends to Matrix
- Clipboard bridge (wl-copy/wl-paste)

**When headless:**
- Terminal apps run inside the agent's container
- Or in a tmux session managed by the orchestrator
- Optional: headless Wayland compositor (cage/weston) for apps requiring a display

### 6. Matrix Room Model

**Bot-managed rooms** — the bot owns the room lifecycle. On first startup,
bot creates a Space and control room, invites the user. Project rooms are
created on demand via commands.

Using **Spaces + Rooms + Threads**:

```
📁 Space: "Enclave"
   ├── 🔧 #control          — commands only (agent spawning, system mgmt)
   ├── 🔐 #approvals        — permission/privilege requests with reactions
   ├── 📂 #project-foo      — agent session for project foo
   │     ├── Thread: "refactor auth module"
   │     └── Thread: "debug CI pipeline"
   ├── 📂 #project-bar      — agent session for project bar
   └── 📊 #monitoring       — system stats, alerts
```

**Room creation flow:**
1. User types `project Test Project` (or `!project Test Project`) in control room
2. Bot creates encrypted room "Test Project" in the Space
3. Bot invites user, spins up podman container
4. User accepts invite, starts chatting with the agent

**Command format:** Both `!command` and bare `command` are accepted
(strip `!` prefix if present, then match command word). This makes mobile
usage less painful.

**Control room commands:**
- `help` — list available commands
- `project <name>` — create a new project session
- `sessions` — list active sessions
- `kill <id>` — stop a session
- `status` — system info
- `perms <project>` — list active permissions
- `revoke <id>` — revoke a permission
- `rules` — list persistent permission rules

**Per-user homeserver:** Bot config supports different Matrix servers per user.
Federation handles cross-server messaging natively. No need to tie all users
to a single homeserver.

**Mapping:**
- 1 Room = 1 Project
- 1 Thread = 1 task/conversation (maps to a Copilot SDK session)
- Control room for meta-operations (spawn agents, system commands)

---

## Copilot SDK Integration

The SDK runs INSIDE each agent container:

```python
from copilot import CopilotClient

client = CopilotClient()
await client.start()

session = await client.create_session({
    "model": "claude-sonnet-4",  # or any available model
    "system_message": agent_system_prompt,
    "tools": custom_tools,  # file ops, shell, web, etc.
})

# Messages from Matrix are forwarded here:
response = await session.prompt(user_message)
# Response sent back to Matrix via orchestrator socket
```

**Custom tools exposed to the agent:**
- `read_file`, `write_file`, `list_directory` — scoped to /workspace
- `shell_exec` — runs commands inside the container
- `request_permission` — asks orchestrator for file/directory access
- `request_privilege` — asks for root execution via broker
- `launch_gui` — requests GUI app launch on desktop
- `screenshot` — requests desktop screenshot
- `send_file` — sends a file to the Matrix room

**MCP servers:** The SDK supports MCP, so you could also expose tools via MCP servers running alongside the agent.

---

## Prior Art & What We're Borrowing

| Project | What we take from it |
|---------|---------------------|
| **Opsdroid** | Concept of chat-triggered skills; we use Copilot SDK instead |
| **Maubot** | Plugin architecture idea; our "tools" serve the same role |
| **matrix-commander** | Reference for matrix-nio E2EE patterns |
| **XMPP_Shell_Bot** | Validates the concept; we improve with sandboxing + approval |
| **polkit** | Inspiration for the priv broker approval model |
| **Copilot CLI** | Proven agentic runtime; we embed it via SDK |

Nothing existing combines: **AI agent + chat control + sandboxed containers + approval-based permissions + desktop integration**. This is novel.

---

## Tech Stack Summary

- **Python 3.12+** — everything except priv broker
- **github-copilot-sdk** — AI agent runtime
- **matrix-nio[e2ee]** — Matrix E2EE client
- **podman** — rootless container sandboxing
- **systemd** — service management (orchestrator + priv broker)
- **Conduit** — lightweight Matrix homeserver (Rust)
- **SQLite** — session persistence, permission state
- **hyprctl / grim / wl-clipboard** — desktop integration

---

## Resolved Decisions

1. **Priv broker:** Rust (safer for a root daemon, smaller attack surface)
2. **Container networking:** Denied by default, request-based. Web search is always
   allowed but runs in a **separate search agent** to prevent prompt injection
   (see Search Isolation below).
3. **Multi-user:** Designed in from the start, single-user first pass.
4. **Homeserver:** Using Synapse at `matrix.iostream.uk`. Currently in a VM, will
   transplant to a more powerful local machine later.
5. **Session persistence:** Always persisted until manually deleted.
   Conversation history + workspace state saved to disk.
6. **MCP servers:** Host-level pass-through per user + local to container.
   Usage expected to be minimal.
7. **Priv broker approval:** Per-user. If user has sudo, they can approve
   their own requests.
8. **Permission requests:** Per-user. One Matrix user = one Linux user.
9. **Container image strategy:** Hybrid — base image + optional per-project
   layers (see Container Image Strategy below).
10. **Approval UI:** Reaction-based (not polls). Bot seeds emoji options,
    user taps to choose. Polls (MSC3381) unreliable in E2EE rooms.
11. **Room lifecycle:** Bot-managed. Bot creates Space + rooms, invites users.
    No manual room setup required.
12. **Command prefix:** Both `!command` and bare `command` accepted.
    Control room is commands-only.
13. **Permission storage:** External to container. Orchestrator maintains
    permission DB per-session. Can add/revoke at any time without container restart.
14. **Per-user homeserver:** Bot supports different Matrix servers per user
    via federation.

---

## Sub-Agent Threading

When a main agent spawns a sub-agent (e.g., for research, code review, testing):

1. Main agent sends request via orchestrator socket
2. Orchestrator **automatically creates a Matrix thread** in the project room
   - Thread root message: "🤖 Sub-agent: *researching auth patterns*"
3. Orchestrator spawns a new podman container for the sub-agent
4. Sub-agent output streams to the thread (user can follow along live)
5. When sub-agent completes, summary goes back to the main agent
6. Thread gets a completion message: "✅ Done — summary sent to main agent"

**Why threads?** User can choose to follow the sub-agent's work in real time
or ignore it and just see the result in the main conversation.

---

## Search Isolation (Prompt Injection Defense)

Web search is always allowed, but never done by the main agent directly:

```
Main Agent (no network) → "search for X" → Orchestrator
   → Search Agent (has network, no workspace access)
   → Fetches results, summarizes in plain text
   → Returns summary to Orchestrator
   → Orchestrator forwards text-only summary to Main Agent
```

The search agent:
- Has network access but **no workspace mount** (can't read/write project files)
- Uses Copilot SDK with a constrained system prompt focused on summarization
- Returns **plain text only** — no tool calls, no code blocks that could be
  interpreted as instructions
- Is ephemeral: spun up per search, destroyed after

This means even if a malicious website injects "ignore previous instructions..."
into its content, it only affects the search agent (which has no tools to abuse)
and gets filtered into a plain text summary before reaching the main agent.

---

## Multi-User Model

**Principle:** An agent should only have access to resources that the
underlying Linux user also has.

**Solution: Per-user podman via systemd-run**

```
Matrix user "alice" → mapped to Linux user "alice"
   → Orchestrator uses systemd-run to launch podman AS alice
   → Container runs with alice's UID (--userns=keep-id)
   → Can only access files alice can access on the host
   → alice's own subuid/subgid range for namespace isolation
```

**Setup per user:**
1. Linux user exists with home directory
2. `/etc/subuid` and `/etc/subgid` entries (non-overlapping ranges)
3. `loginctl enable-linger <user>` for persistent services
4. Matrix account linked to Linux user in orchestrator config

**Orchestrator runs as a system service** (not as any individual user).
It uses `systemd-run --property=User=<username>` to spawn containers
in the correct user context. This means:
- Containers inherit the user's file permissions naturally
- No need for complex permission mapping
- cgroup isolation per user is handled by systemd
- Each user's containers are invisible to other users

**Config mapping (example):**
```yaml
users:
  "@ian:aeon.local":
    linux_user: ian
    allowed_rooms: ["#project-*:aeon.local", "#control:aeon.local"]
    max_sessions: 5
    can_approve_privilege: true
  "@alice:aeon.local":
    linux_user: alice
    allowed_rooms: ["#project-alice-*:aeon.local"]
    max_sessions: 3
    can_approve_privilege: false  # only ian can approve root ops
```

---

## Container Image Strategy (Hybrid)

**Base image** — one image maintained for all projects:
```
aeon-agent:base
├── Python 3.12+
├── Copilot CLI + SDK
├── Common tools (git, ripgrep, jq, curl, etc.)
├── IPC client (talks to orchestrator)
└── ~500MB, cached, starts in <1s
```

**Per-project layers** (optional, extends base):
```dockerfile
FROM aeon-agent:base
RUN pip install django pytest  # project-specific deps
COPY .tool-versions /etc/      # or whatever
```

**When to use each:**
- Most sessions → just use base image (fast, simple)
- Project needs specific toolchain (e.g., Node 20, Rust nightly) → per-project layer
- User can define a `Containerfile` in their project root → orchestrator auto-builds

**Persistent volumes** for each session preserve installed packages across restarts:
```
/home/aeon/sessions/<id>/workspace/   → /workspace     (project files)
/home/aeon/sessions/<id>/state/       → /state         (conversation history, SDK state)
/home/aeon/sessions/<id>/packages/    → /usr/local/    (pip installs etc, overlay)
```

This means even with the base image, a session can `pip install` things and
they survive container restarts (via the packages volume). Best of both worlds.

---

## UX: Container Selection on Project Start

When a user starts a new project/session, the orchestrator presents a **poll in Matrix**
letting them choose which container image to use:
- Base image (default, fastest)
- Per-project image (if a `Containerfile` exists in the project root)
- Custom image (user specifies)

This keeps the user in control of the environment without needing to configure files manually.

---

## Open Questions (Remaining)

1. **MCP integration detail:** How to expose host MCP servers securely into containers?
2. **Session restore:** Replay conversation history into Copilot SDK, or serialize
   SDK session state directly?
3. **Search agent model:** Same Copilot model as main agent, or a smaller/cheaper one?
