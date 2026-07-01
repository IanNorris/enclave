# Per-project OpenSpec

## ADDED Requirements

### Requirement: Per-session spec root

The Specs tab SHALL read each session's own `openspec/`, not a global one.

#### Scenario: Container session reads its workspace

- **WHEN** a session has `openspec/` in its workspace
- **THEN** the Specs tab shows that session's changes only

#### Scenario: Empty state

- **WHEN** a session has no `openspec/`
- **THEN** the Specs tab shows a graceful empty state

#### Scenario: Host-mode external repo

- **WHEN** a host-mode session has a `.openspec_root` pointer to an external repo
- **THEN** the Specs tab reads that repo's `openspec/`
