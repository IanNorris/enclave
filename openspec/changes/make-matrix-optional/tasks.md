# Tasks

## 1. Config
- [ ] 1.1 Add `MatrixConfig.enabled: bool` (default derived, see 1.2)
- [ ] 1.2 Resolution: omitted+creds→true; omitted+no-creds→false+warn; true+no-creds→hard-fail; false→off
- [ ] 1.3 `ENCLAVE_MATRIX_ENABLED` env override, parsed as a real boolean
- [ ] 1.4 Make the `matrix-nio` import + credential validation conditional on `enabled`
- [ ] 1.5 Update `enclave.yaml.example` (write `enabled: false` in newly-generated/example config)

## 2. Null client
- [ ] 2.1 Define a shared `Protocol`/ABC over the ~28 public methods
- [ ] 2.2 `NullMatrixClient`: no-op fire-and-forget; fail-fast (not fake-success) for polls/create_room/event-id/media
- [ ] 2.3 `EnclaveMatrixClient` conforms to the same Protocol (drift guard)

## 3. Startup
- [ ] 3.1 main.py: construct `NullMatrixClient` when disabled; skip login gate + control-room resolve
- [ ] 3.2 Gate `create_task(sync_forever())` — track `Task | None`, cancel/await only if present
- [ ] 3.3 No `CancelledError` swallowing anywhere in the null path

## 4. Synthetic room id
- [ ] 4.1 Non-Matrix scheme (`local:<uuid>`) at session creation when Matrix off
- [ ] 4.2 `is_synthetic_room(room_id)` helper; call at every real-room assumption site
- [ ] 4.3 Web UI Matrix-fallback send path fails loud under Matrix-off (no silent no-op)
- [ ] 4.4 Restart/recovery skips synthetic ids; SQLite authoritative for session state

## 5. Approval policy
- [ ] 5.1 `matrix.off_approval_policy: deny|approve` (default deny)
- [ ] 5.2 `request_permission` short-circuits to the policy when Matrix off (no poll, no 300s wait)
- [ ] 5.3 Log the auto-resolution explicitly (never a silent auto-grant)

## 6. Verify
- [ ] 6.1 Orchestrator starts web-UI-only with no Matrix creds (no sys.exit)
- [ ] 6.2 Web UI: send + stream + history all work with Matrix off
- [ ] 6.3 A host-mode restricted-op approval resolves immediately per policy (not a 300s hang)
- [ ] 6.4 Existing Matrix-on deployment behavior unchanged (regression)
- [ ] 6.5 Clean shutdown with Matrix off (no perpetually-pending sync task)
