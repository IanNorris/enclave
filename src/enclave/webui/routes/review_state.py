"""Derived state for OpenSpec review — pure functions over an append-only event log.

The per-change ``.enclave-review.json`` is an append-only log of two event
types: ``review`` (user feedback) and ``agent_revision`` (the agent's response).
Change state and per-comment status are DERIVED here, never mutated in place, so
history stays truthful and the UI is reconstructable after any reload.

States: none | commented | changes_requested | revised_pending_review | approved

Confirmed defaults (Ian, 2026-06-30):
- Post-approval agent edits RE-GATE the change back to revised_pending_review.
- Approval is explicit and human-only; addressed-counts never auto-approve.
"""

from __future__ import annotations


def _reviews(events: list[dict]) -> list[dict]:
    return sorted(
        (e for e in events if e.get("type") == "review"),
        key=lambda e: e.get("at", ""),
    )


def _revisions(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("type") == "agent_revision"]


def derive_state(events: list[dict]) -> str:
    """Derive the current review state from the event log."""
    reviews = _reviews(events)
    revisions = _revisions(events)
    if not reviews:
        return "none"
    latest = reviews[-1]
    state = latest.get("state")

    if state == "approved":
        # Re-gate: an approval certifies a specific document state; the agent is
        # the sole file-mutator, so any later revision invalidates the approval.
        latest_at = latest.get("at", "")
        if any(rv.get("at", "") > latest_at for rv in revisions):
            return "revised_pending_review"
        return "approved"

    if state == "commented":
        return "commented"

    if state == "changes_requested":
        answered = any(rv.get("in_response_to") == latest.get("id") for rv in revisions)
        return "revised_pending_review" if answered else "changes_requested"

    return "none"


def derive_comment_statuses(events: list[dict]) -> dict[str, str]:
    """Map each review comment id to 'open' or 'addressed'.

    A comment is addressed once its id appears in any later agent_revision's
    ``related_comment_ids``. Partial address is fine — the change still advances
    to revised_pending_review; the user remains the final arbiter.
    """
    addressed: set[str] = set()
    for rv in _revisions(events):
        for cid in rv.get("related_comment_ids", []) or []:
            addressed.add(cid)
    statuses: dict[str, str] = {}
    for r in _reviews(events):
        for c in r.get("comments", []) or []:
            cid = c.get("id")
            if cid:
                statuses[cid] = "addressed" if cid in addressed else "open"
    return statuses
