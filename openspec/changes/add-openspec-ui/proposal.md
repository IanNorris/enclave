# Add OpenSpec review UI

## Why

Enclave has adopted OpenSpec for spec-driven development, but the specs live as
markdown files under `openspec/` that are invisible in the web UI. A user
reviewing what an agent is building has no way to read a change proposal, track
implementation progress, give structured feedback, or keep a spec in view while
chatting.

This change adds a UI surface so the user can read OpenSpec changes, see live
task progress, approve or request changes (with that feedback flowing back to
the agent), and pin a spec so it stays visible during a conversation.

Fittingly, this feature is itself specified with OpenSpec — the proposal you are
reading is the first artifact the UI renders.

## What changes

- A backend reads the repo's `openspec/` markdown directly (no `openspec` CLI in
  the web process) via a single configurable read-root, defaulting to the
  Enclave repo (Phase A dogfood).
- A new per-session **Specs** surface lists changes with a task-progress bar and
  review state, and renders a selected change (proposal / design / tasks /
  specs) using the existing markdown renderer.
- The existing chat pull-out side panel is reused to **pin** a spec so it stays
  visible while chatting; the pin persists per session in `localStorage`.
- A **thin global progress bar** in the main UI reflects the active change's
  task completion, with a hover overlay showing the current and next task parsed
  from `tasks.md`.
- **Approve / Request-changes** controls dual-write a durable review record
  (`.enclave-review.json`) and send a plain-language tagged message to the agent
  so the agent (the sole state-mutator) acts on the feedback.

## Impact

- New backend route module `routes/openspec.py`; mounted in the web app.
- Frontend: new Specs view/panel, a global progress bar component, and reuse of
  the existing side panel + markdown renderer.
- No change to the agent loop beyond receiving tagged feedback messages.
- Deploying requires a shared web UI rebuild + restart (affects all sessions).

## Non-goals

- Running `openspec validate` / `status` inside the web process (deferred; the
  agent runs the CLI and can emit results later).
- In-UI editing of specs, archive browsing, diff/version views, inline
  section-anchored comments, and live auto-refresh — all deferred to phase 2.
