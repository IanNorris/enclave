# Tasks

## 1. Event log + derived state (backend)

- [x] 1.1 Define the event-log schema and an atomic append helper for `.enclave-review.json`
- [x] 1.2 Add `review_state.py` with `derive_state(events)` and `derive_comment_statuses(events)`
- [x] 1.3 `GET .../openspec/changes/{name}/state` (state + per-comment status view)
- [x] 1.4 Extend the review POST to append a `review` event + capture `snapshot_at_review`

## 2. Inline commenting (frontend)

- [x] 2.1 markdown-it `data-line` core rule (incl. fence/table) + normalization
- [x] 2.2 Floating comment affordance over `v-html` (hover desktop / tap mobile) via event delegation
- [x] 2.3 Capture frozen block context; accumulate draft in `localStorage` with count badges
- [x] 2.4 Sticky "Submit review (N)" bar → POST review event + send one tagged chat message

## 3. Processing + re-approval

- [ ] 3.1 Extend `openspec_revision_log` with `resolutions[]`; snapshot files from disk; idempotent
- [ ] 3.2 Prompt instruction: call the tool with one resolution per comment after applying feedback
- [ ] 3.3 State badge + dynamic CTA (Review changes / Approve / Request more) from derived state
- [ ] 3.4 Per-comment view: verbatim comment + agent resolution + addressed/stale indicators

## 4. Edit highlighting

- [ ] 4.1 Snapshot diff endpoint (`diff?since=review`) using `jsdiff diffLines` on normalized markdown
- [ ] 4.2 Post-render decoration: `.changed` class on intersecting `data-line` blocks + CSS
- [ ] 4.3 "Show changes since I last reviewed" toggle; default-on when pending re-approval

## 5. History timeline

- [ ] 5.1 History/Activity section rendering `review` + `agent_revision` events reverse-chronologically

## 6. Wrap up

- [ ] 6.1 Gitignore the review-log files
- [ ] 6.2 Build the frontend and restart the web UI
- [ ] 6.3 Verify end-to-end: comment → submit → agent revision + resolutions → highlight → re-approve
