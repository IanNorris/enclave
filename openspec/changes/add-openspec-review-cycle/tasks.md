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

- [x] 3.1 Extend `openspec_revision_log` with `resolutions[]`; snapshot files from disk; idempotent
- [x] 3.2 Prompt instruction: call the tool with one resolution per comment after applying feedback
- [x] 3.3 State badge + dynamic CTA (Review changes / Approve / Request more) from derived state
- [x] 3.4 Per-comment view: verbatim comment + agent resolution + addressed/stale indicators

## 4. Edit highlighting

- [x] 4.1 Snapshot diff endpoint (`diff?since=review`) using `jsdiff diffLines` on normalized markdown
- [x] 4.2 Post-render decoration: `.changed` class on intersecting `data-line` blocks + CSS
- [x] 4.3 "Show changes since I last reviewed" toggle; default-on when pending re-approval

## 5. History timeline

- [x] 5.1 History/Activity section rendering `review` + `agent_revision` events reverse-chronologically

## 6. Wrap up

- [x] 6.1 Gitignore the review-log files
- [x] 6.2 Build the frontend and restart the web UI
- [x] 6.3 Verify end-to-end: comment → submit → agent revision + resolutions → highlight → re-approve

> Note (added per review comment c_mr1ce8oeat0g): edit highlighting is now
> implemented — this very note is the edit whose highlight you should see, shown
> as a green marker on the changed lines when "Show changes" is on.
