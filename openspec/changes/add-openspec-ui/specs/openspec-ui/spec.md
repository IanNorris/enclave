# OpenSpec UI capability

## ADDED Requirements

### Requirement: Read OpenSpec changes in the web UI

The web UI SHALL present OpenSpec changes from the configured read-root so a
user can read a change without leaving the browser.

#### Scenario: List changes

- **WHEN** the user opens the Specs surface for a session
- **THEN** every change under `openspec/changes/` (excluding `archive/`) is
  listed with its title, task-progress, and review state
- **AND** a session with no `openspec/` directory shows a graceful empty state

#### Scenario: Read a change

- **WHEN** the user selects a change
- **THEN** its proposal, design, tasks, and specs render as markdown

### Requirement: Track implementation progress

The UI SHALL show task completion parsed from `tasks.md`.

#### Scenario: Progress bar reflects checkboxes

- **WHEN** a change's `tasks.md` has checked and unchecked items
- **THEN** a thin global progress bar shows the completion ratio
- **AND** a hover overlay shows the current and next unchecked task

### Requirement: Give feedback that reaches the agent

The UI SHALL let the user approve or request changes, persisting a durable
review record and notifying the agent.

#### Scenario: Approve a change

- **WHEN** the user clicks Approve on a change
- **THEN** a review record is written to the change's `.enclave-review.json`
- **AND** a tagged feedback message is sent to the agent in the session chat

#### Scenario: Request changes with a note

- **WHEN** the user requests changes and provides a note
- **THEN** the note is persisted in the review record
- **AND** the agent receives a tagged request-changes message including the note

### Requirement: Pin a spec for reference

The UI SHALL let the user pin a spec so it stays visible while chatting.

#### Scenario: Pin persists across reloads

- **WHEN** the user pins a change's spec
- **THEN** the spec opens in the chat side panel
- **AND** the pin is restored after a page reload within the same session
