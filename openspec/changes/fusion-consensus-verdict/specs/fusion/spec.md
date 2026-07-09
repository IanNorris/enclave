# Fusion

## ADDED Requirements

### Requirement: Consensus is treated as signal, not confidence

The Fusion judge SHALL NOT instruct that model agreement is high-confidence, and
the synthesizer SHALL NOT lead with consensus as high-confidence content,
because correlated models can agree on wrong answers.

#### Scenario: Judge does not equate agreement with correctness

- **WHEN** the judge prompt is built
- **THEN** the consensus section is framed as points of agreement
- **AND** it does not instruct treating agreement as high-confidence

#### Scenario: Disagreement is elevated

- **WHEN** the judge prompt is built
- **THEN** it contains a section that treats disagreement between strong models
  as the highest-signal part of the analysis

### Requirement: Fusion emits a verdict that can escalate to a human

The judge SHALL produce a verdict of settled, contested, or unresolved, and the
synthesizer SHALL flag unresolved verdicts for human judgement rather than
producing a falsely confident answer.

#### Scenario: Unresolved verdict escalates

- **WHEN** the panel's answer is genuinely unsettled
- **THEN** the judge marks the verdict unresolved
- **AND** the synthesizer presents the contested options and the safest course
  and states that human judgement is needed

#### Scenario: Settled verdict answers directly

- **WHEN** the panel's answer is well supported and not meaningfully contested
- **THEN** the judge marks the verdict settled
- **AND** the synthesizer gives the direct answer
