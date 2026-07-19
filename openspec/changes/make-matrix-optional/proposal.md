# Make Matrix optional

## Why

The operator has stopped using Matrix and wants to run Enclave web-UI-only.
Today Matrix is a hard dependency: the orchestrator `sys.exit(1)`s if Matrix
login or control-room creation fails, and `self.matrix.<method>()` is called
~81 times in `router.py` with only one call guarded. But the web UI is already
Matrix-independent for messaging — it drives agents entirely through the
control socket (Unix `control.sock`), and conversation history lives in
per-session SQLite, not Matrix. So agents fully function with Matrix off; the
work is decoupling the orchestrator's startup and the ~28-method Matrix surface
cleanly, without scattering 80 guards.

## What changes

### 1. Null-object Matrix client (not 80 scattered guards)
Introduce a `NullMatrixClient` implementing the same method surface as no-ops,
selected at construction when Matrix is disabled. `self.matrix` is then always a
valid object, so the ~81 call sites stay unchanged. Both clients are backed by a
shared `Protocol`/ABC so the null object can't silently drift when a method is
added.

**Fail-fast, not fake-success, for awaited/correlated results.** No-op is safe
for fire-and-forget (typing, reactions, notifications, callback registration,
`cleanup_room`, `close`). For anything whose return value is later awaited,
edited, redacted, joined, or correlated (polls, `create_room` ids, event ids,
media), the null client must NOT fabricate a plausible success — see the
approval and room-id sections.

### 2. Config: `enabled` with credential-derived default
Add `MatrixConfig.enabled`. Resolution rule (explicit wins; absence of creds
implies off; presence implies on):
- `enabled` omitted + creds present → `true` (upgrade-safe, no behavior change).
- `enabled` omitted + creds absent → `false` with a loud warning (fixes the
  current fresh-deploy `sys.exit(1)`), instead of hard-failing.
- `enabled: true` + creds missing → still hard-fail (operator asked for it).
- `enabled: false` → web-UI-only regardless of creds.
- New `ENCLAVE_MATRIX_ENABLED` env override, parsed as a real boolean (guard the
  `bool("false") is True` trap).
- The `matrix-nio` import and credential validation must be conditional on
  `enabled` — otherwise "optional" is a lie at import time.

### 3. Startup: gate Matrix work, don't sleep-forever
In `main.py`, when Matrix is disabled: skip the login gate, skip control-room
resolve/create, and do NOT create the `sync_forever` task at all (track it as
`Task | None`; cancel/await only if present). This makes "Matrix off = no Matrix
background work" literally true and keeps shutdown clean.

### 4. Synthetic room id: unmistakably non-Matrix + boundary helper
Sessions still need a `room_id` key. Use a scheme that can never be confused with
a real Matrix room (`local:<uuid>`, NOT a `!...:server` look-alike) so if it ever
reaches nio it fails loud and early. Add one `is_synthetic_room(room_id)` helper
used at every site that assumes a real room:
- The web UI's Matrix **fallback** send path must detect Matrix-off and fail
  loudly (or be hidden), never silently no-op the operator's message.
- Restart/session-recovery reconciliation must skip synthetic ids; SQLite is
  authoritative for session state, not joined-room state.

### 5. Approval flow when Matrix is off (the real product decision)
Approvals are Matrix-poll-only with no web-UI path. Container agents auto-approve
(unaffected). Host-mode agents requesting a restricted op would, with a null
client, post a no-op poll and block the full 300s timeout, then get EXPIRED
(deny). That's a 5-minute hang per request, not a deadlock, but unacceptable.

With Matrix disabled, `request_permission` must short-circuit immediately to an
operator-chosen policy instead of posting a poll and waiting:
- `matrix.off_approval_policy: deny` (default — the safe, surfaced choice; the
  agent's tool call is rejected gracefully and it continues) or `approve`.
- The decision must be explicit and logged, never an implicit auto-grant.

A proper web-UI approval path is the correct long-term fix but is out of scope
for this change (see Non-goals).

## Impact

- `common/config.py` (`MatrixConfig.enabled` + derivation + env), `enclave.yaml`
  example.
- `orchestrator/matrix_client.py` (new `NullMatrixClient` + shared `Protocol`).
- `orchestrator/main.py` (conditional startup, gate `sync_forever`).
- `orchestrator/router.py` (synthetic room id at session creation; the ~81 call
  sites are unchanged by design).
- `orchestrator/approval.py` (short-circuit to policy when Matrix off).
- `webui/routes/chat.py` (fallback send fails loud under Matrix-off).
- Deployment: orchestrator can start with no Matrix env; document web-UI-only.

## Non-goals

- A web-UI approval UI for host-mode permission requests (follow-up; this change
  only stops the 300s hang via an explicit policy).
- A generic `ChatTransport` interface for other chat backends (Slack/Discord) —
  premature with one operator and no second transport.
- Removing Matrix code. This makes it optional and off, not deleted.
- Chat-driven concierge fleet management with Matrix off — the concierge already
  guards on `control_room_id` and simply isn't created; scheduled *spawn* tasks
  still run, session-targeted-to-concierge schedules degrade (documented).
