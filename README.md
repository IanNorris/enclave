<p align="center">
  <img src="assets/logo.png" alt="Enclave" width="200">
</p>

<h1 align="center">Enclave</h1>

<p align="center">
AI agent instances running on Linux, controlled via Matrix/Element, powered by
the GitHub Copilot SDK. Each agent is sandboxed in a podman container with
explicitly approved permissions.
</p>

## Architecture

```
Element (phone/web)
    │ E2EE
    ▼
Matrix Homeserver (Conduit)
    │ E2EE
    ▼
Orchestrator (host, unprivileged)
    │ Unix sockets
    ├── Agent Pod #1 (podman, sandboxed)
    └── Agent Pod #2 (podman, sandboxed)
```

**Two trust zones:**
- **Orchestrator** — manages Matrix, spawns containers, controls file access
- **Agent containers** — Copilot SDK + custom tools, sandboxed, no host access

See [docs/design.md](docs/design.md) for the full design document.

## Status

🚧 **Active development** — core architecture is functional.
See [docs/ROADMAP.md](docs/ROADMAP.md) for planned features.

## Features

### Interfaces

- **Matrix/Element control** — drive agents from any Matrix client, end-to-end encrypted
- **Web UI** — full-featured web dashboard over HTTPS with token auth: live chat,
  three-level Timeline drill-down, Artifacts panel, Bug tracker, Memories browser,
  and an Asks inbox. Renders images inline (gallery/lightbox) and Mermaid diagrams
- **Management CLI + TUI** — `enclavectl` commands plus an interactive terminal dashboard
- **MCP server** — expose Enclave to external tools via Model Context Protocol

### Sandboxing & permissions

- **Sandboxed agents** — each runs in a podman container with explicit permissions
- **Profile system** — dev (Nix), light (minimal), host (direct) profiles
- **Landlock** — kernel-level filesystem sandboxing for host-mode agents
- **Dynamic mounts** — request host directories at runtime, approved via chat
- **Multi-user** — each Matrix user maps to a Linux user with their own permissions
- **Audit log** — structured JSONL record of every agent action for security review

### Agent capabilities

- **Web search & extraction** — optional Kagi helpers (`kagi_search`, `kagi_extract`)
  for ranked primary sources and clean-markdown page content
- **Document conversion** — `markitdown` turns local PDF/Word/PowerPoint/Excel/etc.
  into Markdown the agent can read
- **Structured responses** — rich scannable cards with embedded images and action buttons
- **Deferred questions** — non-blocking `ask_deferred` questions collected in the Asks inbox
- **File sharing** — `send_file` delivers files to chat; images preview inline
- **Port mapping** — `request_port` exposes a container service on a host port
- **Bug tracker** — workspace-local bug tracking, mirrored to the web UI
- **Artifacts** — versioned report/document artifacts shown in the web UI
- **Display/UI** — launch GUI apps and capture screenshots on Wayland
- **Sub-agents** — spawn child agents for parallel tasks
- **Remote agents (ACP)** — connect external agents over the Agent Client Protocol (TCP)

### Memory & lifecycle

- **Memory** — persistent cross-session memory with auto-dreaming (plus the Mimir backend)
- **Session persistence** — conversation history survives container restarts via a durable event log
- **Scheduling** — cron-like recurring callbacks and one-shot timers
- **Cost & AI-credit tracking** — token usage and premium-request quota per session
- **Plugin system** — drop-in Python tools per workspace or user
- **User identity** — agents know your name and pronouns
- **Room cleanup** — clean up Matrix rooms for stopped sessions

### Management CLI

<p align="center">
  <img src="assets/screenshot-cli.png" alt="enclavectl status" width="700">
</p>

### Interactive TUI

<p align="center">
  <img src="assets/screenshot-tui.png" alt="enclavectl tui" width="700">
</p>

## Tech Stack

- **Python 3.12+** — orchestrator + agent
- **github-copilot-sdk** — AI agent runtime
- **matrix-nio[e2ee]** — Matrix E2EE client
- **podman** — rootless container sandboxing
- **FastAPI + Vue 3** — web UI (HTTPS, token auth)
- **Conduit** — Matrix homeserver
- **systemd** — service management
- **Nix** — reproducible builds and dev environment

## Prerequisites

- **Python 3.12+**
- **Podman** (or Docker)
- **Nix** (optional but recommended — `nix develop` provides everything above)

## Installation

```bash
git clone https://github.com/icstatic/enclave.git
cd enclave

# Edit config before starting
cp enclave.yaml.example ~/.config/enclave/enclave.yaml
nano ~/.config/enclave/enclave.yaml

# Install orchestrator (user-level) — builds, installs, and enables the service
./install.sh orchestrator

# Build container images (main + light agent images)
./install.sh image
```

## Web UI

Enclave ships a web dashboard (`enclave-webui`) alongside the Matrix interface.
It serves chat, the Timeline view, Artifacts, the Bug tracker, Memories, and the
Asks inbox over HTTPS with token-based auth.

```bash
# Create a login (first run)
enclave-webui --create-user <name>

# Run it (defaults to https://0.0.0.0:8430)
enclave-webui
```

Or enable the bundled user service so it starts with the orchestrator:

```bash
cp services/enclave-webui.service ~/.config/systemd/user/
systemctl --user enable --now enclave-webui
```

TLS certificates are loaded from the configured cert/key (self-signed by default);
pass `--no-tls` to serve plain HTTP on a trusted network.

### NixOS

On NixOS, install the orchestrator as a user service:

```bash
./install.sh orchestrator
```

Optionally, add the flake to your system configuration for development tooling:

```nix
# flake.nix
{
  inputs.enclave.url = "github:icstatic/enclave";
}
```

The orchestrator is installed as a user service via `./install.sh orchestrator`.

## Development (Nix)

```bash
# Development shell with all dependencies
nix develop github:icstatic/enclave

# Or clone and develop locally
git clone https://github.com/icstatic/enclave.git
cd enclave
nix develop
pip install --user github-copilot-sdk 'matrix-nio[e2ee]'
python3 -m pytest tests/unit/
```

## License

TBD
