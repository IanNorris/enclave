# Design

## Derived lifecycle (pure)

`_lifecycle(task_progress, review, archived)`:
- `archived` if under changes/archive/
- `done` if tasks total>0 and done==total and review.state=="approved"
- `needs_approval` if tasks complete but not approved
- `active` otherwise

No persisted state; computed on each read from tasks.md + the review log. The
legacy add-openspec-ui (15/15, unapproved) surfaces as `needs_approval` and is
resolved by a one-time in-UI Approve — never retroactively auto-closed.

## Archive integration (agent-mediated, no CLI in web)

`openspec archive <name>` moves the change to changes/archive/<date>-<name>/ and
rewrites canonical specs. It runs via a new `openspec_archive` agent tool (like
openspec_revision_log), not a web subprocess: the CLI is host-only and absent
from containers, so a web subprocess would break Phase B anyway. The dependency
belongs in the agent image. The tool passes --json -y and surfaces validation
failures; the agent commits the resulting openspec/specs/ changes.

## Read path

The list includes archived changes (archived: true); get_change falls back to
changes/archive/<name> so an archived change is viewable read-only.
