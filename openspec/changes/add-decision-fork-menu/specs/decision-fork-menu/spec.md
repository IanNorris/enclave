# Decision fork menu capability

## ADDED Requirements

### Requirement: Batched multi-decision structured response

A structured response SHALL be able to present multiple independent decisions
resolved and submitted together.

#### Scenario: Resolve several decisions at once

- **WHEN** the agent sends a structured response containing a set of decisions
- **THEN** the user can select an option or leave a free-text comment on each
- **AND** submitting sends one batched reply keyed by decision id

#### Scenario: Partial answers

- **WHEN** the user answers some decisions and leaves others blank
- **THEN** the unanswered decisions are reported as "no preference"
