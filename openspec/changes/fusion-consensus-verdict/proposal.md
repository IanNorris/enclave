# Reframe Fusion consensus as signal, not confidence

## Why

The judge prompt tells the model that consensus is high-confidence
("**Consensus** — points (most/all) agree on; treat as high-confidence",
`fusion.py:417`) and the synthesizer is told to "Lead with the consensus
(high-confidence) content" (`:443`). But correlated models agree on wrong
answers: with only ~2-3 effectively-independent families, agreement is weak
evidence of correctness. Treating consensus as confidence launders correlated
error into a confident final answer, and the highest-signal part of a panel —
where good models *disagree* — is buried.

This is a P0 fix from the review-council spec, independent of the roster work
and of `fusion-blind-judge`.

## What changes

- Judge prompt: stop equating agreement with correctness. Keep a consensus
  section but label it as agreement (not confidence). Elevate a
  **Disagreement (highest-signal)** section. Replace the "Confidence
  (low/med/high)" line with a **Verdict: settled | contested | unresolved**
  that instructs escalation to a human when unresolved.
- Synthesizer prompt: stop leading with "consensus (high-confidence)". Surface
  the disagreement and the verdict; when the verdict is unresolved, present the
  contested options and the safest course, and state plainly that it needs human
  judgement rather than manufacturing a confident answer.
- Order findings by **priority/severity, not by origin** (review feedback): the
  synthesizer presents points in order of importance, not grouped by
  agreement-bucket (consensus-first) or by which response raised them, and the
  judge's disagreement points are ordered strongest/highest-stakes first. This
  is the natural corollary of "consensus is not confidence": the most important
  finding leads, whether or not the panel agreed on it.

## Impact

- `common/fusion.py`: `build_judge_prompt` (sections list) and
  `build_synthesizer_prompt` (guidance bullets) only. Prompt-only; no signature
  or data-flow change.
- The judge output gains a machine-readable-ish `Verdict:` line the UI can
  surface; the trace renders judge markdown as-is, so no frontend change is
  required for it to appear.

## Non-goals

- Parsing the verdict into a structured field / auto-routing unresolved runs to
  an `ask_user` (a good follow-up, but out of scope; this change is prompt-only).
- Citing a specific external study or statistic in the prompt (the design stands
  on its own reasoning; unverified citations must not be baked into prompts).
- **Archetype-blinding (review feedback):** the reviewer also asked to "blind the
  model to the archetype." Fusion has no archetypes — every participant gets the
  same neutral prompt, and blinding the judge to participant *model identity* is
  already handled by the approved `fusion-blind-judge` change. Archetypes exist
  only in `consult_panel` / the review council, so blinding the panel
  synthesizer to *which archetype* raised each point (so a role's prestige, e.g.
  "the Architect", doesn't outweigh the strength of the point) belongs to the
  council work, not here. Captured there via the same origin-blind + order-by-
  priority principle this change establishes for Fusion.
