# Tasks

- [ ] 1.1 Judge prompt: relabel Consensus as agreement (not "high-confidence")
- [ ] 1.2 Judge prompt: add a **Disagreement (highest-signal)** section
- [ ] 1.3 Judge prompt: replace the Confidence line with **Verdict: settled | contested | unresolved**, escalate-to-human when unresolved
- [ ] 1.4 Synthesizer prompt: stop leading with consensus; surface disagreement + verdict; on unresolved, give options + safest course + flag for human
- [ ] 1.5 No external citation/statistic baked into the prompt text
- [ ] 1.6 Unit test: rendered judge prompt contains "Verdict" and no "high-confidence"; synthesizer no longer instructs leading with consensus
