# Blind the Fusion judge and synthesizer to model identity

## Why

`build_judge_prompt` and `build_synthesizer_prompt` label each participant
response `### Response {i} (from {label})` (`fusion.py:408`, `:437`), where
`label` is the model id. The frontier preset's judge is opus-primary
(`["claude-opus-4.8", "gpt-5.5"]`) over a panel that includes
`claude-opus-4.8`, so the judge can see which response is from its own family
and self-prefer it. With only three model families, all of them in-panel,
blinding the judge is the only available self-preference defence.

This is a P0 fix from the review-council spec, independent of the roster work.

## What changes

- Drop the model attribution from the judge and synthesizer prompts: label
  responses neutrally (`### Response {i}`), in a stable but anonymous order.
- Nothing else about ordering or content changes.

## Impact

- `common/fusion.py`: `build_judge_prompt` and `build_synthesizer_prompt` only.
  Prompt construction, no signature or data-flow change.
- **The human is not blinded.** The UI trace still shows participant model names
  from `participants[].model`; only the judge/synthesizer prompts lose the
  attribution. Users still see who said what.

## Non-goals

- Adding a fourth model family (the real fix for the independence ceiling; out
  of scope here).
- Randomising response order (anonymising the label is sufficient; stable order
  keeps the trace readable).
