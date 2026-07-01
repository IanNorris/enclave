# Design

## Placement

`decisions` rides on the existing `structured_response` mechanism, parallel to
`actions`. Cards may have `actions`, `decisions`, or both; single-action buttons
keep firing immediately, while a decisions block batches.

## Schema

```json
{
  "type": "structured_response",
  "title": "…",
  "summary": "…",
  "decisions": [
    { "id": "regate_on_edit",
      "question": "Re-gate on post-approval edits?",
      "options": [{"id":"yes","label":"Yes"},{"id":"no","label":"No"}],
      "allowFreeText": true }
  ]
}
```

Reply (one message, delivered through a structured channel where available
rather than a stringified chat blob):

```json
{
  "type": "decision_response",
  "in_reply_to": "<card_id>",
  "decisions": [
    {"id":"regate_on_edit","selected":"yes","comment":""}
  ]
}
```

## Rendering

Each decision is one option group (radio/segmented) + an optional free-text box,
with a **single submit for the whole card**. Unanswered decisions submit as
`{selected:null, comment:""}` = "no preference". No required flags, validation,
or conditional branching in v1.

## Reliability note

Round-tripping LLM-generated JSON through the free-text chat transcript is
fragile. If the SDK/UI exposes a structured event or tool-message channel, send
the batched reply through that instead of parsing a stringified reply from the
transcript. This is the main piece worth care when this change is built.
