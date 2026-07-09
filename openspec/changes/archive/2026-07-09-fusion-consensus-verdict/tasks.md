# Tasks

- [x] 1.1 Judge prompt: relabel Consensus as agreement (not "high-confidence")
- [x] 1.2 Judge prompt: add a **Disagreement (highest-signal)** section
- [x] 1.3 Judge prompt: replace the Confidence line with **Verdict: settled | contested | unresolved**, escalate-to-human when unresolved
- [x] 1.4 Synthesizer prompt: stop leading with consensus; surface disagreement + verdict; on unresolved, give options + safest course + flag for human
- [x] 1.5 Judge + synthesizer: order findings by priority/severity, not by origin (agreement-bucket or response order) — review feedback
- [x] 1.6 No external citation/statistic baked into the prompt text
- [x] 1.7 Unit test: rendered judge prompt contains "Verdict" and no "high-confidence"; synthesizer no longer instructs leading with consensus
