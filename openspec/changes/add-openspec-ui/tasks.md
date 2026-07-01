# Tasks

## 1. Backend

- [x] 1.1 Add `routes/openspec.py` with the read-root resolver and path-safety helpers
- [x] 1.2 Implement `GET .../openspec/changes` (list + task progress + review state)
- [x] 1.3 Implement `GET .../openspec/changes/{name}` (markdown + specs map)
- [x] 1.4 Implement `POST .../openspec/changes/{name}/review` (write `.enclave-review.json`)
- [x] 1.5 Mount the router in the web app and add api.js client methods

## 2. Specs view

- [x] 2.1 Add `views/Specs.vue` (change list + collapsible detail sections)
- [x] 2.2 Add the Specs tab to `SessionTabBar.vue` and a route to `router.js`
- [x] 2.3 Render proposal/design/tasks/specs via the shared markdown renderer

## 3. Feedback + pinning

- [x] 3.1 Approve / Request-changes controls that dual-write review + send a tagged agent message
- [x] 3.2 Pin a spec into the chat side panel, persisted per session in `localStorage`

## 4. Global progress bar

- [x] 4.1 Add `components/SpecProgressBar.vue` (thin bar + current/next hover overlay)
- [x] 4.2 Mount it in `App.vue`; bind to the active change; hide when none

## 5. Wrap up

- [x] 5.1 Reference OpenSpec in the README as used
- [x] 5.2 Build the frontend and restart the web UI
- [x] 5.3 Verify the dogfood change renders, progress bar tracks, feedback + pin work
