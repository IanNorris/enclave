# Tasks

## 1. Config
- [x] 1.1 Add `MatrixConfig.enabled: bool` (default derived, see 1.2)
- [x] 1.2 Resolution: omitted+creds→true; omitted+no-creds→false+warn; true+no-creds→hard-fail; false→off
- [x] 1.3 `ENCLAVE_MATRIX_ENABLED` env override, parsed as a real boolean
- [x] 1.4 Make the `matrix-nio` import + credential validation conditional on `enabled`
- [x] 1.5 Update `enclave.yaml.example` (write `enabled: false` in newly-generated/example config)

## 2. Null client
- [x] 2.1 Drift guard over the ~28 public methods (implemented as an introspective test
      asserting the null covers the real public surface, rather than a formal ABC —
      lighter, catches the same drift)
- [x] 2.2 `NullMatrixClient`: no-op fire-and-forget; fail-fast (not fake-success) for polls/create_room/event-id/media
- [x] 2.3 `EnclaveMatrixClient` conforms to the same surface (drift-guard test)

## 3. Startup
- [x] 3.1 main.py: construct `NullMatrixClient` when disabled; skip login gate + control-room resolve
- [x] 3.2 Gate `create_task(sync_forever())` — track `Task | None`, cancel/await only if present
- [x] 3.3 No `CancelledError` swallowing anywhere in the null path

## 4. Synthetic room id
- [x] 4.1 Non-Matrix scheme (`local:<uuid>`) at session creation when Matrix off
- [x] 4.2 `is_synthetic_room(room_id)` helper (used by the web UI fallback guard)
- [x] 4.3 Web UI Matrix-fallback send path fails loud under Matrix-off (no silent no-op)
- [x] 4.4 Restart/recovery unaffected: SQLite is authoritative, and every
      `self.matrix.<method>(room_id)` is a null no-op under Matrix-off, so a synthetic
      id never reaches nio during reconciliation.

## 5. Approval policy — SUPERSEDED, intentionally not implemented
- [~] 5.1 `matrix.off_approval_policy: deny|approve` — NOT added.
- [~] 5.2 short-circuit — NOT added.
- [~] 5.3 log auto-resolution — N/A.
  Rationale: this section predates the web-UI approval path (`add-webui-approvals`,
  PR #25). `request_permission` is now Matrix-independent: it surfaces every request
  to the web UI card and only posts a Matrix poll if a room exists, failing closed
  fast otherwise. The 300s hang this guarded against no longer occurs, and a
  deny/approve short-circuit would REMOVE the operator's ability to answer host
  approvals in-browser. Deliberately omitted; host approvals remain answerable via
  the web UI card.

## 6. Verify
- [x] 6.1 Starts web-UI-only with no creds (no sys.exit) — config-derivation tests + lazy-import branch.
- [x] 6.2 Web UI send + stream + history work with Matrix off — control socket + SQLite are
      Matrix-independent; file_send decoupled from the Matrix event_id.
- [x] 6.3 Host-mode approval resolves via the web UI card (no 300s hang) — via add-webui-approvals.
- [x] 6.4 Matrix-on behavior unchanged — enabled path is the original code under `matrix.enabled`.
- [x] 6.5 Clean shutdown with Matrix off — sync task never created (`Task | None`).
