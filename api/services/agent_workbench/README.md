# Workbench authoring handoffs and recovery

Workbench keeps its candidate, simulation and evidence workflow. New-agent
authoring first resolves call direction from original sources, then produces a
`CallBrief` using the configured Workbench model. The user approves that exact
brief or submits corrections. Corrections are retained as human source records;
the next source snapshot retains its parent fingerprint and all original sources.
An approved summary supplements the originals rather than replacing their detail.
Existing-agent edits do not restart this initial requirements interview.

Organization tool creation and updates are proposals, not direct model actions.
The stable `read_node` dynamic tool exposes `organization_action` for existing
Codex conversations as well as new ones. Schemas and reference validation reuse
the normal tool-management service. The approval card displays the request and,
for updates, the previous configuration and shared impact. Decisions are scoped
to the authenticated organization, job, proposal ID and revision. Draft or memory
changes invalidate a pending decision. Updates additionally compare the tool's
revision under its database row lock. A stale change returns a diagnostic to the
authoring loop so it can propose a new version.

`Orchestration` provides reusable completed-step receipts and editing-state
checkpoints. Version 1 is stored privately in the existing job JSON; no new
database migration or model provider is required. Each authoring/repair input has
its own namespace, so repair rounds do not restore another round's working copy.
Candidate and suite creation save their operation receipt in the same transaction
as the artifact. Tool mutations reuse the shared tool-mutation receipt, and check
the active job lease and human approval in the mutation transaction.

Requests awaiting approval release their worker. Decisions requeue the same job
through the database outbox, preserving its original target, source evidence and
model snapshot. Transient model failures and worker lease expiry can recover a
checkpointed authoring task with the configured bounded retry policy. Pending
approvals are restored without executing them. Cancelled and failed tasks can be
continued explicitly; an expired worker cannot continue writing. Existing test
preparation, evaluation and build-review checkpoints remain in use.

The public API and compact session projections exclude private working copies.
The browser renders persisted approval state and uses generated API methods for
decisions and continuation. Applying a candidate to the draft remains a separate
user action; approving requirements or a tool does not publish an Agent.

Restart the API and Workbench worker after deploying these changes. Preserve the
existing Workbench/Codex data directory when restarting; durable conversations
still depend on that storage.
