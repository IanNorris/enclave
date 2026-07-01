# Per-project OpenSpec (Phase B)

## Why

The Specs tab read a single global `openspec/` (the Enclave repo) regardless of
session — so every session showed the same specs. Specs should be per project:
each session reads its own workspace's `openspec/`.

## What changes

- The web read-root resolver becomes per-session (it was built swappable for
  exactly this). Priority: `ENCLAVE_OPENSPEC_ROOT` env → a `.openspec_root`
  pointer file in the session workspace → the session workspace itself.
- Container sessions naturally read `workspace/openspec/` (the bind-mounted
  project). New sessions with no `openspec/` show a graceful empty state.
- Host-mode sessions whose project is an external repo (e.g. the Enclave dev
  session) drop a `.openspec_root` file naming that repo, so they keep seeing
  the repo's specs.

## Decision: pointer file now, config field later

Host-mode override uses a workspace `.openspec_root` file — ships today, fully
reversible (delete the file), no schema change. The resolver is layered so a
future `session.project_root` config field can supersede it without touching
callers.

## Impact

- Backend: `_openspec_root` takes `request`, resolves per session; ~6 callers
  threaded; path-safety bounds reads to `<root>/openspec/changes/`.
- No data migration: the Enclave repo's own changes stay in the repo (its
  project IS the repo).

## Non-goals

- A `session.project_root` config field + UI (deferred; the layered resolver
  reserves a slot for it).
- Inferring a host agent's project dir from shell cwd.
