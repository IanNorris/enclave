# Design

## Invariant

The per-change `.enclave-review.json` is an **append-only event log**. Change
state and per-comment status are **derived** by a pure function over the events;
no past event is rewritten. This keeps history truthful and makes the UI
reconstructable after any reload.

## Event log schema

Top-level keeps the existing badge fields (`state`, `by`, `at`) for backward
compatibility, plus an `events[]` array:

- `review` (user):
  `{id:"rev_<ts>", type:"review", at, by, state, overall_note,
    snapshot_at_review:{<path>:<content_hash>},
    comments:[{id:"c_<..>", section, path, start_line, end_line,
               block_text, block_hash, section_hash, comment}]}`
- `agent_revision` (agent):
  `{id:"arev_<ts>", type:"agent_revision", at, by:"agent",
    in_response_to:"rev_<ts>"|null, base_event_id, summary, why,
    files_changed:[...], snapshot_after:{<path>:<content_hash>},
    related_comment_ids:[...],
    resolutions:[{comment_id, actionable_intent, resolution_note,
                  files_changed:[...]}]}`

File snapshots are stored content-by-hash (store-once, reference by hash) to
dedup across cycles.

## Derived state machine (`review_state.py`)

States: `none`, `commented`, `changes_requested`, `revised_pending_review`,
`approved`.

```
derive_state(events):
  reviews   = sorted reviews; revisions = agent_revisions
  if no reviews: return "none"
  latest = reviews[-1]
  if latest.state == "approved":
      # re-gate: an approval certifies a document state; a later agent edit
      # invalidates it.
      if any(rv.at > latest.at for rv in revisions): return "revised_pending_review"
      return "approved"
  if latest.state == "commented": return "commented"
  if latest.state == "changes_requested":
      answered = any(rv.in_response_to == latest.id for rv in revisions)
      return "revised_pending_review" if answered else "changes_requested"
  return "none"
```

Primary trigger for `revised_pending_review` is explicit `in_response_to`
linkage; timestamp ordering is the fallback only for the post-approval re-gate.
`derive()` is idempotent over duplicate revision events.

## Per-comment status (derived)

A comment is `addressed` iff its id appears in a later revision's
`related_comment_ids`; else `open`. Partial address still advances the change to
`revised_pending_review` (the user is the final arbiter and may "Request more").
A declined comment is expressed via its `resolution_note`. Approval is never
auto-implied from addressed counts.

## Inline comments

- A markdown-it `core.ruler` rule injects `data-line="<startLine>"` on
  `paragraph_open`, `heading_open`, `list_item_open`, `blockquote_open`,
  `fence`, and `table_open` tokens (0-based source lines, normalized).
- The rendered markdown is inert `v-html`; affordances are overlays, never Vue
  mounted inside it. One floating comment button follows the active block
  (`closest('[data-line]')`) on hover (desktop) / tap (mobile).
- Tapping captures `{section, path, start_line, end_line, block_text}` where
  `block_text` is the **frozen** raw-markdown slice from this block's start line
  to the next block's start line. A `block_hash` detects later staleness.
- Draft comments live in `localStorage` keyed per change; commented blocks get a
  count badge via a post-render decoration pass (re-applied on every render).
- Submit: POST the `review` event (server captures `snapshot_at_review`), THEN
  send one tagged chat message via the existing pendingFeedback → Chat transport.

## Submit message format

```
[OpenSpec review on change '<name>'] CHANGES REQUESTED  (review_id: rev_<ts>)

Overall: <overall note>

Inline comments (N):

1. [proposal.md:12]
   > <frozen block quote>
   Comment: <user comment>
...

NOTE: locate each block by its QUOTED TEXT (line numbers may have shifted).
After applying, call openspec_revision_log(change, summary, why,
in_response_to='rev_<ts>', resolutions=[...]).
```

## Edit highlighting

- Source of truth: backend **file snapshots** captured by the revision-log tool
  (reads disk itself). Diff baseline = content at the originating review
  (`snapshot_at_review`) → latest revision (`snapshot_after`); union across
  multiple revisions answering one review.
- Granularity (MVP): **line/block-level** via `jsdiff diffLines` on normalized
  raw markdown (trim trailing whitespace, collapse blank-line runs to avoid
  reflow false-positives). Word-level `<mark>` deferred.
- Rendering: after markdown-it renders the **latest** version, add a `.changed`
  class to every `[data-line]` block whose source range intersects a changed
  line. CSS = left accent bar + faint tint. Recomputed each render (survives
  re-render because it keys off `data-line`).
- Critical: the DOM carrying `data-line` MUST be rendered from exactly the
  snapshot the diff's "after" side used, or highlights land on wrong blocks.
- Control: default-on when state is `revised_pending_review`; phone-friendly
  "Show changes since I last reviewed" toggle. No side-by-side on mobile.

## Endpoints / tool deltas

- `GET .../openspec/changes/{name}/state` → `{state, comments:[{id,status,
  actionable_intent,resolution_note,resolved_by,is_stale}]}`.
- `GET .../openspec/changes/{name}/diff?since=review|prev` → changed line ranges
  per file from snapshots.
- Review POST: capture `snapshot_at_review`; atomic append (temp-file rename).
- `openspec_revision_log`: add `resolutions[]`; handler reads file content from
  disk, writes `snapshot_after` + `base_event_id`; idempotent on `arev_` id.

## Security / robustness

- Reads bounded to `openspec/`; traversal-guarded (existing helper).
- Atomic write of the log to avoid read-during-write corruption.
- The tool never trusts agent-pasted file content — it snapshots from disk.
