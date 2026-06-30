# Design

## Read root (Phase A)

A single resolver `_openspec_root(session_id)` returns the directory whose
`openspec/` is read. For Phase A it returns the Enclave repo root
(`ENCLAVE_OPENSPEC_ROOT` env override, else the repo root derived from the
package location). Flipping to per-session (Phase B) later is a one-function
change: return the session workspace instead. All file reads are bounded to
`<root>/openspec/` with path-traversal checks mirroring `routes/sessions.py`.

## Backend endpoints (`routes/openspec.py`)

- `GET /api/sessions/{sid}/openspec/changes`
  Scans `<root>/openspec/changes/` (excluding `archive/`). For each change
  returns id, artifact paths that exist, `taskProgress {done,total}` parsed from
  `tasks.md`, and `review {state, note, at}` from `.enclave-review.json` if
  present. Returns `{exists:false, changes:[]}` when there is no `openspec/`.

- `GET /api/sessions/{sid}/openspec/changes/{name}`
  Returns the raw markdown of proposal/design/tasks plus a map of spec files
  (relative path -> content).

- `POST /api/sessions/{sid}/openspec/changes/{name}/review`
  Body `{state, note}`. Writes `.enclave-review.json`
  (`{state, note, by, at}`). The frontend separately sends the agent a tagged
  chat message; the endpoint only persists the durable badge.

`sid` is accepted for URL symmetry and future Phase B; in Phase A all sessions
resolve to the same repo root.

## Task progress parsing

`tasks.md` uses markdown checkboxes. Count `- [x]` (done, case-insensitive) and
`- [ ]` (open); `total = done + open`, `percent = done/total`. The current task
is the first unchecked item; the next task is the second unchecked item.

## Frontend

- `views/Specs.vue`: left = change list (title, progress bar, review badge);
  right = selected change rendered as collapsible Proposal / Design / Tasks /
  Specs sections via the existing `markdown-it` + mermaid renderer. Controls:
  Pin, Approve, Request changes (with note).
- A **Specs** entry is added to `SessionTabBar.vue` and a route to `router.js`.
- `components/SpecProgressBar.vue`: a thin bar fixed under the top bar in the
  main layout (`App.vue`), bound to the active change's progress, with a hover
  overlay showing current + next task. It polls the changes endpoint lightly (or
  refreshes on Specs navigation) and hides when there is no active change.
- Pin reuses the chat side panel: pinning sets
  `localStorage["enclave:<sid>:pinnedSpec"]` and opens the spec markdown in the
  pull-out panel; `Chat.vue` restores it on load.

## Feedback message format

Plain language the agent will read (not slash commands, which Copilot CLI does
not handle):

```
[OpenSpec feedback on change '<name>'] APPROVE
[OpenSpec feedback on change '<name>'] REQUEST CHANGES: <note>
```

Sent via the existing `sendActionReply`/`send` path so it enters the normal
chat + event log.

## Security

Reads are bounded to `<root>/openspec/`; reject paths that escape via
`relative_to`. The review write is confined to a change's own directory. No CLI
execution from the web process.
