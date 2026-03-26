# Enclave Roadmap

## Backlog

### Host Mode — Landlock Kernel Sandboxing

**Priority:** Medium | **Effort:** Medium

Enforce filesystem access restrictions at the kernel level for host-mode
agents using Linux Landlock LSM. This provides defence-in-depth against
prompt injection attacks — even if an agent compiles and runs a malicious
binary, the kernel prevents access outside the approved paths.

**Architecture:**
- Orchestrator applies `landlock_restrict_self()` before exec'ing the agent
- Scratch directory gets full RW access
- `/usr`, `/nix`, system libs get read-only access
- All other paths denied (including `~/`)
- Dynamic mounts: orchestrator bind-mounts approved paths into scratch
  (agent already has access to scratch, so new mounts "just work")

**Why Landlock over SELinux/AppArmor:**
- No root required — process self-restricts
- No system-wide configuration or policy files
- Inherited by all child processes (compiled binaries can't escape)
- Available since Linux 5.13

**Implementation notes:**
- Python bindings via `ctypes` or `landlock` PyPI package
- One-way ratchet: can only tighten, never loosen after `restrict_self()`
- SDK permission handler becomes the UX layer (explains restrictions)
- Landlock becomes the enforcement layer (actually prevents access)
- Option A (recommended): proxy bind-mounts into scratch for dynamic access
- Option B (simpler): pre-authorize broad read-only access, gate writes

### Host Mode — Agent Execution

**Priority:** High | **Effort:** Medium

Wire up the orchestrator to run agents directly on the host when the
"host" profile is selected (image=""). Currently only config and prompts
exist — the container manager needs to handle this case by spawning a
subprocess instead of a podman container.
