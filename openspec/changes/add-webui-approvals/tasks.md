# Tasks

- [x] 1.1 ApprovalManager: `on_ui_request` callback + `resolve_external(request_id, answer_id, sender)`
- [x] 1.2 `request_permission` surfaces to web UI always; Matrix poll only if room present; fail-closed if no channel
- [x] 1.3 Per-request pattern map so a request_id-keyed response can grant a pattern
- [x] 2.1 control.py: `emit_permission_request` / `emit_permission_resolved` + `permission_respond` action
- [x] 2.2 router.py: wire `on_ui_request` → control emits; `resolve_web_approval` bridge
- [x] 3.1 webui: `POST /chat/{session}/permission` relays via control socket
- [x] 3.2 Chat.vue: permission_request card (approve/deny) + api.respondPermission + clear on permission_resolved
- [x] 4.1 Live test: control socket permission_respond wired + ApprovalManager web path unit-tested (approve/deny/fail-closed)
- [ ] 4.2 Regression: Matrix poll still posted + still resolves when Matrix on
