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

🚧 **Early development** — proving out core architecture via isolated spikes.

## Spikes

1. `spikes/spike1_copilot_in_podman/` — Copilot SDK running inside a podman container
2. `spikes/spike2_matrix_e2ee/` — Matrix E2EE bot with thread creation
3. `spikes/spike3_ipc_bridge/` — Unix socket IPC between host and container

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
