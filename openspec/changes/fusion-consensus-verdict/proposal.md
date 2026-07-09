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
