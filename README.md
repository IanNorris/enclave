# Enclave

AI agent instances running on Linux, controlled via Matrix/Element, powered by
the GitHub Copilot SDK. Each agent is sandboxed in a podman container with
explicitly approved permissions.

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
    ├── Agent Pod #2 (podman, sandboxed)
    └── Priv Broker (root, systemd)
```

**Three trust zones:**
- **Orchestrator** — manages Matrix, spawns containers, controls file access
- **Agent containers** — Copilot SDK + custom tools, sandboxed, no host access
- **Privilege broker** — Rust daemon, approval-based root operations via Matrix

See [docs/design.md](docs/design.md) for the full design document.

## Status

🚧 **Active development** — core architecture is functional.
See [docs/ROADMAP.md](docs/ROADMAP.md) for planned features.

## Features

- **Sandboxed agents** — each runs in a podman container with explicit permissions
- **Profile system** — dev (Nix), light (minimal), host (direct) profiles
- **Privilege escalation** — sudo via Matrix approval polls, with pattern-based grants
- **Dynamic mounts** — request host directories at runtime, approved via chat
- **Scheduling** — cron-like recurring callbacks and one-shot timers
- **Session persistence** — conversation history survives container restarts
- **User identity** — agents know your name and pronouns

## Tech Stack

- **Python 3.12+** — orchestrator + agent
- **github-copilot-sdk** — AI agent runtime
- **matrix-nio[e2ee]** — Matrix E2EE client
- **podman** — rootless container sandboxing
- **Rust** — privilege broker
- **Conduit** — Matrix homeserver
- **systemd** — service management

## License

TBD
