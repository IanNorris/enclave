# Tasks

## 1. Lifecycle (backend) — shipped

- [x] 1.1 Derive `lifecycle` (active | needs_approval | done | archived) on change summaries
- [x] 1.2 Surface archived changes in the list; resolve archived paths in get_change

## 2. Lifecycle (frontend) — shipped

- [x] 2.1 Lifecycle badges (Done / Needs approval / Archived)
- [x] 2.2 Collapsed Done group + collapsed read-only Archived section
- [x] 2.3 Auto-select an active change (not a done/archived one)

## 3. Approve the legacy change

- [x] 3.1 One-time in-UI Approve of add-openspec-ui (it then derives Done)

## 4. Archive action (rides the container rebuild)

- [x] 4.1 Add the OpenSpec CLI to the agent container image
- [x] 4.2 `openspec_archive` agent tool running `openspec archive <name> --json -y`, surfacing validation failures
- [x] 4.3 "Archive" button on done changes, triggered via the agent path; UI refreshes on completion

## 5. Wrap up

- [x] 5.1 Verify: finished change shows Done, archive moves it to the Archived section
