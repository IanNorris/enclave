# Fusion

## ADDED Requirements

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
