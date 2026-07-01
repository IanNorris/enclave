# OpenSpec change lifecycle + archive

## ADDED Requirements

### Requirement: Derived terminal states

A change SHALL derive a terminal presentation state so finished work is not
shown as merely "Open".

#### Scenario: Done

- **WHEN** a change has all tasks complete and its latest review is approved
- **THEN** it is shown as Done and grouped under a collapsed Done section

#### Scenario: Needs approval

- **WHEN** a change has all tasks complete but no approved review
- **THEN** it is shown as "Needs approval" rather than "Open"

### Requirement: Archive

The system SHALL let a completed change be archived, and SHALL show archived
changes read-only.

#### Scenario: View archived changes

- **WHEN** changes exist under openspec/changes/archive/
- **THEN** they appear in a collapsed read-only Archived section

#### Scenario: Archive is agent-mediated

- **WHEN** a change is archived
- **THEN** the real `openspec archive` CLI is run via an agent tool, not from the web process
