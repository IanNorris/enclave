# Add OpenSpec change lifecycle + archive

## Why

The Specs tab showed finished changes as "Open" — `add-openspec-ui` is 15/15
tasks and shipped, but predates the review UI so it was never approved in-UI and
had no clear terminal state. Changes also had no way to be archived (moved out of
the active list once complete), even though OpenSpec supports archiving.

## What changes

- **Auto-close (derived, no mutation)**: a change derives a terminal
  `done` state when all tasks are complete AND the latest review is approved.
  A `needs_approval` state covers the tasks-complete-but-unapproved case (like
  the legacy `add-openspec-ui`). Purely derived from tasks.md + the review log;
  no persisted "closed" event.
- **Grouping**: the Specs list shows active/needs-approval changes at top, a
  collapsed **Done** group, and a collapsed read-only **Archived** section.
- **Archived view (read-only)**: archived changes under
  `openspec/changes/archive/<date>-<name>/` are listed and viewable; the date
  prefix is stripped for display.
- **Archive action (agent-mediated)**: archiving runs the real `openspec archive`
  CLI via a new `openspec_archive` agent tool (mirroring `openspec_revision_log`),
  NOT from the web process. This keeps the web process CLI-free and
  container-portable (a web subprocess call would break in containers anyway,
  since the CLI is host-only). The archive action lands with the container
  rebuild that provisions the OpenSpec CLI into the agent image.

## Decision: archive stays agent-mediated

The web process deliberately never invokes the `openspec` CLI. Archiving is a
mutation that both moves files and rewrites canonical specs, so it uses the real
CLI — exposed through an agent tool, not a web subprocess. Rationale: the CLI is
absent from container images, so a web subprocess would break Phase B; pushing
the dependency to the agent image (a clean Dockerfile step) preserves the
portability the no-CLI rule exists to protect.

## Impact

- Backend: derived `lifecycle` field on change summaries; archived changes
  surfaced in the list; `get_change` resolves archived paths. (Shipped.)
- Frontend: lifecycle badges + Done/Archived groups. (Shipped.)
- Agent: new `openspec_archive` tool. (Rides the container rebuild.)
- Containers: OpenSpec CLI added to the agent image. (Rides the rebuild.)

## Non-goals

- Auto-archiving on finish (a surprising mutation; archiving stays explicit).
- A persisted "closed" event (state is derived).
- Reimplementing OpenSpec's spec-merge logic in Python.
