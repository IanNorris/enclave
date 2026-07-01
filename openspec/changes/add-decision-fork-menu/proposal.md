# Add a decision fork menu (batched multi-decision structured responses)

## Why

The agent already sends `structured_response` cards whose `actions` render as
buttons — but each button fires immediately and sends one message. Many real
moments need the opposite: several **independent** decisions the user wants to
resolve **together** in one pass (e.g. "confirm these three defaults", "pick an
option on each of these five forks"). Today that means a back-and-forth per
decision, or the user hand-writing a combined reply.

A **decision fork menu** presents a stacked set of independent decisions, each
resolvable by picking an option OR leaving a free-text comment, all submitted as
one batched reply. It is a generic `structured_response` capability — useful
anywhere the agent faces the user with multiple choices, not just OpenSpec.

## Status: BUILT

Implemented as a generic `structured_response.decisions` field with a batched
multi-decision form in the web UI. The OpenSpec review flow (and any agent
facing the user with several choices) is a consumer.

## What changes

- Extend `structured_response` with an optional `decisions` field parallel to
  `actions`:
  `decisions: [{id, question, options:[{id,label}], allowFreeText}]`.
- The web UI renders each decision as an option group + optional free-text box,
  with **one submit button for the whole card** (batch, not fire-on-click).
- The reply is a single structured `decision_response`
  (`{in_reply_to, decisions:[{id, selected, comment}]}`) delivered through a
  structured channel where possible rather than a stringified chat message.
- The agent reads answers keyed by `decision.id`; unanswered = no preference.

## Consumers (why this is generic)

- OpenSpec review: "surface decision forks that meaningfully alter trajectory"
  becomes a use of this menu (the review-cycle change consumes it).
- Panel/fusion consults: present the panel's decision forks as a selectable menu.
- Any structured response needing batched multi-choice input.

## Non-goals

- Required fields, validation rules, conditional/branching decisions, nesting.
- Replacing single-action `actions` (they coexist).
