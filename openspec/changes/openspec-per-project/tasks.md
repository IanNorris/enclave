# Tasks

- [x] 1.1 Make `_openspec_root` per-session (env → .openspec_root pointer → workspace)
- [x] 1.2 Thread `request` through the ~6 callers
- [x] 1.3 Path-safety bounds reads to <root>/openspec/changes/
- [x] 1.4 Drop `.openspec_root` in the Enclave dev session workspace (points at the repo)
- [x] 1.5 Verify: this session sees repo specs; a fresh session is isolated/empty
- [x] 1.6 Build + restart the shared web UI
