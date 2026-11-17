# M4a deferred follow-ups

Findings from the M4a work on job leases, rate limiting, MCP sessions and reconciliation,
deliberately deferred. Triage when planning M4b and M4c. Decisions, live research and known
limits: [ADR 0017](../decisions/0017-jobs-limits-reconciliation.md).

## Out of scope for M4a

- A rate limit shared across replicas (in Redis or the state store). Today each replica
  counts on its own, so N replicas admit up to N times the configured rate.
- The per-model proposal lock across replicas. It is per process today.
- Releases and imports serialised across replicas. Today two replicas can race; the Git
  remote rejects the loser's push.
- Reconciliation reverting a direct edit in OpenMetadata. It only reports, or commits the
  edit to Git (FR-OM-02).
- Scheduled drift creating proposals. The `drift-schedule` job only reports.
- MCP session bindings shared across replicas. They live in one replica's memory, so several
  replicas need `LAKELOOM_MCP_STATELESS=true` or sticky sessions.

## Jobs and leases
- A crashed `lakeloom reconcile --commit` blocks other committing runs until its
  `reconcile-commit` lease expires (15 minutes).
- `JobRunner.stop()` can take the runner's grace on top of uvicorn's
  `timeout_graceful_shutdown` (both `LAKELOOM_SHUTDOWN_TIMEOUT_SECONDS`): documented, not
  bounded as one budget.

## Reconciliation
- ~~Reconciliation assumes OpenMetadata never exposes a half-applied change set in an export
  (ADR 0017 R4). Not verified; listed for the M4b probe.~~ Verified on OM 2.1.0-rc1 by the M4b
  probe: no partial export seen ([ADR 0018](../decisions/0018-connectors.md)).
- A reconcile commit's body lists the check-time diff, but its exports are re-read at commit
  time, so the body can lag an edit made between the check and the commit.
- A proposal stuck in `apply_failed` is reported as "publication in flight". A distinct reason
  ("apply failed, approve again or reject") would tell the operator what to do.
- `_in_flight` reads every pending proposal's change sets from OpenMetadata with no bound on
  the number of GETs.
- A CLI run is not visible in the server's `jobs.reconcile` (that block is the server's own
  last run). Its audit row is in the shared state store.
- A direct edit does not bump the glossary version (ADR 0017 R3), so version-keyed caches
  (HTTP `ETag`) stay stale until their TTL.

## Scheduled drift
- The `lakeloom_drift_tables{model}` gauge keeps a series for a model that is deleted or no
  longer has an applied scan (stale series).
- Every run writes fresh scan records (retention applies) and no audit row.

## Rate limiting and sessions
- A `rate_limited` call to an audited tool still writes an audit row.
- `_limiter()` fails open when `ctx.mcp_server` is not a `LakeloomMCP` (no limit applied).
- Abandoned MCP session bindings are pruned only while requests arrive.

## Tests
- The live rate-limit test runs an in-process app (the stack's OM and Keycloak, a private
  SQLite): the shared server is never restarted with a small burst.
- The live reconcile test runs with `LAKELOOM_RECONCILE_INTERVAL_SECONDS=0`, so the server's
  `jobs.reconcile` block is not checked live (unit-tested only); the audit row is checked
  instead.
