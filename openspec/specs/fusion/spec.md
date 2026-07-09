# fusion Specification

## Purpose
Requirements for Enclave's Fusion compound-model: how the judge and synthesizer
treat participant responses. Fusion fans a prompt to several models, a judge
extracts structure, and a synthesizer writes the final answer. These rules keep
that process honest — blind to model identity, and treating agreement as signal
rather than proof.
## Requirements
### Requirement: Judge and synthesizer are blind to model identity

The Fusion judge and synthesizer SHALL NOT be told which model produced each
participant response, so a judge cannot self-prefer its own model family.

#### Scenario: Judge prompt omits model attribution

- **WHEN** the judge prompt is built from participant responses
- **THEN** each response is labelled only by position (e.g. "Response 1")
- **AND** the response's model id does not appear in its label

#### Scenario: Synthesizer prompt omits model attribution

- **WHEN** the synthesizer prompt is built
- **THEN** participant responses are labelled only by position, with no model id

#### Scenario: The human reviewer is not blinded

- **WHEN** a completed fusion run is shown in the UI trace
- **THEN** each participant's model name is still displayed to the user

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

#### Scenario: Findings are ordered by priority, not origin

- **WHEN** the synthesizer prompt is built
- **THEN** it instructs ordering points by priority/severity
- **AND** it does not instruct grouping by which response raised a point or
  leading by agreement-bucket (consensus-first)

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

