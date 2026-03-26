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

## Backlog

### Host Mode — Agent Execution

**Priority:** High | **Effort:** Medium

Wire up the orchestrator to run agents directly on the host when the
"host" profile is selected (image=""). Currently only config and prompts
exist — the container manager needs to handle this case by spawning a
subprocess instead of a podman container.
