# Matrix optionality

## ADDED Requirements

### Requirement: Matrix can be disabled without breaking agents

Enclave SHALL run fully web-UI-only when Matrix is disabled: agents send and
receive messages, and history is available, entirely through the control socket
and per-session storage.

#### Scenario: Orchestrator starts without Matrix credentials

- **WHEN** the orchestrator starts with Matrix disabled (or no credentials and
  no explicit `enabled`)
- **THEN** it starts web-UI-only with a warning
- **AND** it does NOT exit

#### Scenario: Existing Matrix deployment is unchanged

- **WHEN** `enabled` is omitted and Matrix credentials are present
- **THEN** Matrix is enabled, exactly as before this change

#### Scenario: Explicit enable with missing credentials still fails

- **WHEN** `enabled: true` and required Matrix credentials are missing
- **THEN** the orchestrator hard-fails with a clear misconfiguration error

### Requirement: No Matrix background work when disabled

When Matrix is disabled, the orchestrator SHALL perform no Matrix network
activity and SHALL leave no perpetually-pending Matrix task.

#### Scenario: No sync task is created

- **WHEN** Matrix is disabled
- **THEN** no `sync_forever` task is created
- **AND** shutdown completes cleanly without cancelling a Matrix sync task

### Requirement: Approval requests resolve without Matrix

When Matrix is disabled, an agent permission request SHALL resolve immediately
via an operator-configured policy rather than waiting on a Matrix poll.

#### Scenario: Host-mode restricted op with Matrix off

- **WHEN** a host-mode agent requests permission for a restricted operation and
  Matrix is disabled
- **THEN** the request resolves immediately per `off_approval_policy`
- **AND** the resolution is logged
- **AND** the agent does not block for the poll timeout

### Requirement: Synthetic room ids never reach Matrix

When Matrix is disabled, a session's room id SHALL be an unmistakably
non-Matrix value, and no code path SHALL treat it as a real Matrix room.

#### Scenario: Web UI does not silently blackhole a message

- **WHEN** the web UI's Matrix fallback send path is reached with Matrix disabled
- **THEN** it fails visibly rather than silently no-oping the message
