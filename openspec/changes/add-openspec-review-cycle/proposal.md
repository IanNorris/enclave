# Add OpenSpec review cycle (inline comments, processing, re-approval, edit highlighting)

## Why

The first OpenSpec UI (`add-openspec-ui`) shipped coarse Approve / Request-changes
buttons. Reviewing through that UI, Ian asked for a richer review loop:

- Inline, per-block commenting so feedback attaches to the exact paragraph or
  bullet it concerns, with the block's text captured automatically (manual
  copying is tedious, especially on mobile).
- Feedback captured **verbatim**, but also **processed** into an actionable form
  attached to the individual spec block it addresses.
- A proper **re-approval cycle**: once the agent applies changes, the change
  returns for approval rather than staying silently "changes requested".
- **Automatic highlighting** of the sections the agent edited, so the reviewer
  sees exactly what changed before re-approving.

This change is itself specified and reviewed through the OpenSpec UI — the
feedback that drove it is recorded in this change's review history.

## What changes

- **One append-only event log per change** (`.enclave-review.json`) with two
  event types: `review` (user feedback, verbatim) and `agent_revision` (the
  agent's response, with per-comment resolutions). Change state and per-comment
  status are **derived** from the log, never mutated in place.
- **Inline comments**: every rendered markdown block carries its source line
  (via a markdown-it core rule injecting `data-line`); tapping/hovering a block
  reveals a comment affordance; the block's markdown is frozen as context.
  Comments accumulate as a draft (localStorage); a sticky "Submit review (N)"
  bar bundles them plus an overall note into one server-persisted review event
  and one tagged chat message to the agent.
- **Processing + attachment**: the verbatim comment is immutable; the agent
  writes a per-comment `resolution` (actionable intent + how addressed/declined
  + files changed) on its `agent_revision`. A derived per-comment view joins the
  two so each spec block shows the raw comment and the agent's response.
- **Re-approval state machine** (derived): `none → commented → changes_requested
  → revised_pending_review → approved`, with post-approval agent edits
  re-gating to `revised_pending_review`. Approval is always explicit and
  human-only.
- **Edit highlighting**: the revision-log tool snapshots changed files
  (content-by-hash) on both review and revision events; the UI line-diffs the
  reviewed baseline against the latest revision and marks changed `data-line`
  blocks. Default-on while a change is pending re-approval, with a toggle.
- **New agent tool** `openspec_revision_log` gains `resolutions[]` and writes
  file snapshots itself (reading disk, never trusting agent-pasted content).

## Decisions to confirm (recommended defaults)

- **Re-gate on post-approval edits: YES** — any agent file edit after approval
  returns the change to `revised_pending_review`.
- **Approval is human-only** — "all comments addressed" never auto-approves.
- **Highlight baseline = since the user's last review**, not just the last edit.

## Impact

- Backend: `review_state.py` (derive functions), new state/diff endpoints,
  atomic append to the event log, snapshot capture.
- Frontend: inline-comment decoration over the existing markdown render, draft
  composable, history/activity timeline, state badge + dynamic CTA, changed-block
  highlighting.
- Agent: extended `openspec_revision_log` tool + one prompt instruction.
- The review log files are gitignored so they do not pollute spec diffs.

## Non-goals

- Word/phrase-level highlighting (MVP is line/block-level; word-level deferred).
- Threaded replies, editing/deleting sent comments, comment re-opening on
  regression, multi-reviewer identity, per-file approval scope, server-side
  draft sync.

## Deferred consumers (phase 2)

- **Panel/fusion consult as a review document:** attach the agent's panel
  consult (synthesized doc + judge analysis) to a change as a review artifact,
  and have the agent surface trajectory-altering decision forks. The "surface
  decision forks" part is a consumer of `add-decision-fork-menu` (its own
  change); the "attach consult doc" part extends this change's event log +
  `publish_artifact`. Neither is a standalone spec.
