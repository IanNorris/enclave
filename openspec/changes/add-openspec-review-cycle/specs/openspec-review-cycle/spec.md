# OpenSpec review cycle capability

## ADDED Requirements

### Requirement: Inline per-block comments

The UI SHALL let a reviewer attach a comment to a specific rendered block,
capturing that block's source text automatically.

#### Scenario: Comment on a block

- **WHEN** the reviewer activates the comment affordance on a paragraph or bullet
- **THEN** a comment box opens showing that block's text as frozen context
- **AND** the reviewer's comment is added to a draft without manual copying

#### Scenario: Submit accumulated review

- **WHEN** the reviewer submits the review
- **THEN** all draft comments plus an overall note are persisted as one review event
- **AND** the agent receives a single tagged message bundling every comment with its block context

### Requirement: Verbatim capture with processed attachment

The system SHALL keep each comment verbatim and immutable while attaching a
processed, actionable resolution to the spec block it concerns.

#### Scenario: Agent resolves a comment

- **WHEN** the agent applies feedback and records a revision
- **THEN** each addressed comment gains a resolution (actionable intent + how addressed)
- **AND** the original comment text is preserved unchanged

### Requirement: Re-approval cycle

The change SHALL return for approval after the agent revises it, and approval
SHALL be explicit and human-only.

#### Scenario: Revised change awaits re-approval

- **WHEN** the agent revises a change that had changes requested
- **THEN** the change enters a "revised, pending review" state
- **AND** the reviewer is offered Approve or Request-more actions

#### Scenario: Post-approval edit re-gates

- **WHEN** the agent edits a change's files after it was approved
- **THEN** the change returns to "revised, pending review"

#### Scenario: No auto-approval

- **WHEN** every comment on a change is marked addressed
- **THEN** the change does NOT become approved without an explicit human approval

### Requirement: Highlight edited sections

The UI SHALL highlight the parts of a change edited since the reviewer's last
review.

#### Scenario: Show changes before re-approval

- **WHEN** the reviewer opens a change that is pending re-approval
- **THEN** the blocks changed since their last review are visually marked
- **AND** the reviewer can toggle the highlighting
