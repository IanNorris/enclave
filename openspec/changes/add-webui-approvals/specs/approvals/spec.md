# Approvals

## ADDED Requirements

### Requirement: Agent permission requests are answerable from the web UI

An agent permission request SHALL be surfaced in the web UI and resolvable there,
independently of Matrix.

#### Scenario: Approve from the browser

- **WHEN** a host-mode agent requests permission for a restricted operation
- **THEN** an approve/deny card appears in that session's web UI chat
- **AND** answering it resolves the agent's request

#### Scenario: Resolution clears the card everywhere

- **WHEN** a request is answered (web UI or Matrix) or expires
- **THEN** the web UI card for that request is cleared

#### Scenario: No channel fails closed

- **WHEN** a permission request has neither a web UI channel nor a Matrix room
- **THEN** it is denied immediately rather than blocking for the timeout
