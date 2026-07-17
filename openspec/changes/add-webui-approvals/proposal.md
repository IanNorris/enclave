# Web UI approval path for agent permission requests

## Why

Agent permission requests (host-mode restricted ops: system commands, out-of-
scratch file access) were **Matrix-poll-only**. The web UI — the operator's
primary interface — had no way to see or answer them, so a host-mode agent
would block the full timeout and then be denied with no chance to approve. This
also blocks making Matrix optional: with Matrix off there was no approval
channel at all.

Add a Matrix-independent approval path: surface each request in the web UI chat
and let the operator approve/deny in the browser. This is useful on its own
(approve from the browser even with Matrix on) and is the prerequisite for
web-UI-only operation.

## What changes

- `ApprovalManager` gains an `on_ui_request` callback (surface + clear) and a
  `resolve_external(request_id, answer_id, sender)` method that resolves a
  waiting request by id — the browser's response path, mirroring the Matrix
  poll path but not tied to a poll event.
- `request_permission` now registers the waiter, surfaces the request to the web
  UI **always**, and posts a Matrix poll **only when a room is available**.
  Either channel can resolve it. If neither channel exists, it fails closed
  immediately instead of blocking for the timeout.
- The control socket gains `emit_permission_request` / `emit_permission_resolved`
  push events (which flow to the browser over the existing chat stream) and a
  `permission_respond` action.
- Web UI: a `POST /chat/{session}/permission` route relays the response, and the
  chat view renders an approve/deny card (Approve once / Approve for project /
  Approve pattern / Deny) that clears on resolution from any channel.

## Impact

- `orchestrator/approval.py`, `orchestrator/router.py`, `orchestrator/control.py`
- `webui/routes/chat.py` (+ `api.js`, `Chat.vue`)
- Matrix approval behavior is unchanged when a room is present (the poll is still
  posted); the web UI card is simply an additional, always-available channel.

## Non-goals

- Custom-pattern text entry in the web UI (Matrix keeps it; the web card offers
  the suggested pattern only).
- Changing the approval timeout or the deny-on-timeout default.
