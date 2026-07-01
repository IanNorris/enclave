# Design

## Resolver (layered, per-session)

`_openspec_root(request, session_id)`:
1. `ENCLAVE_OPENSPEC_ROOT` env — global escape hatch.
2. `<workspace>/.openspec_root` pointer file → external project dir (host-mode).
3. Default: `workspace_base/<session_id>` (the session workspace).

Returns the dir CONTAINING `openspec/`. ~6 callers thread `request` (all are
route handlers, so it's in scope). No request-supplied root is ever trusted.

## Path safety

`_safe_change_dir` bounds the user-supplied change name to
`<root>/openspec/changes/` via resolve() + relative_to. `session_id` comes from
the authenticated route, not user input.

## Migration

None. The Enclave repo's 3 done changes stay in the repo. New project sessions
start empty until the agent runs `openspec init`.
