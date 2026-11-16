# 0017. Job leases, rate limiting, MCP sessions and reconciliation

- Status: proposed (M4a, 2026-11-16; § Research is filled in by M4a Task 4)
- Date: 2026-11-16
- Requirements: NFR-DEP-07, NFR-OPS-04, NFR-SEC-07, NFR-SEC-11, NFR-SEC-16, FR-GIT-03, FR-GIT-08, FR-GIT-09, FR-OM-02, FR-PRO-07

## Context

Until M3c Lakeloom ran as one process: the Git outbox and the triple-store sink were drained by
one loop in the app lifespan, nothing bounded how fast a principal could call tools, MCP session
ids were trusted as the SDK hands them out, and an edit made directly in OpenMetadata was only
noticed by the next proposal. M4a makes Lakeloom safe to run as more than one replica and adds:

- **job leases**, so each background job runs on one replica at a time (NFR-DEP-07), with a
  graceful shutdown (NFR-OPS-04);
- a **rate limit** per principal (NFR-SEC-16);
- **MCP sessions bound to their principal**;
- **reconciliation**: detect direct edits in OM against the last published Git state and,
  optionally, commit them through the outbox (FR-GIT-08);
- a **scheduled drift check** on top of the M3c drift service (FR-PRO-07).

The plan is `docs/superpowers/plans/2026-11-16-m4a-jobs-limits-reconciliation.md`; its Decisions
section is the long form of the Decision below.

## Decision

### Job leases (`state/leases.py`, migration `0005_job_leases`)

- Table `job_leases`: `name` (String(128), primary key), `holder` (String(128)), `acquired_at`,
  `expires_at` (timezone-aware). `holder` is the instance id `"<hostname>-<pid>-<8 hex>"`, made
  once per process (`instance_id()`); never a token or a principal (NFR-SEC-11).
- **Acquire is one atomic statement, never read-then-write:** a conditional `UPDATE … WHERE
  name = :n AND (holder = :me OR expires_at < :now)` takes or renews the lease (a renewal keeps
  `acquired_at`, a takeover resets it). When no row was updated, an `INSERT` creates it, and an
  `IntegrityError` means another replica holds it. This is correct on PostgreSQL under
  read-committed (a concurrent `UPDATE` re-checks its `WHERE` on the new row version) and on
  SQLite (one writer at a time).
- Expiry uses the caller's clock, not the database's: every replica's clock is assumed to agree
  to well within the TTL.
- TTL = `max(3 × interval, 60 s)`. A tick renews before it runs; a replica whose renew fails
  skips the tick. `release` deletes only its own row.

### Job runner (`core/jobs.py`)

- A `Job` is a name, an interval and a tick. `JobRunner` loops per job: renew, tick if held,
  sleep. A tick that raises is logged with its exception class only and the loop goes on;
  `status()` reports `held`, `last_run_at` and `last_error` (a class name) per job.
- Jobs under a lease:
  - `git-publisher:<repo_key>`: the outbox, every `LAKELOOM_GIT_RETRY_INTERVAL_SECONDS`;
  - `triplestore-sink`: the sink queue, now its own job;
  - `reconcile` (`LAKELOOM_RECONCILE_INTERVAL_SECONDS`) and `drift-schedule`
    (`LAKELOOM_DRIFT_INTERVAL_SECONDS`), below.
- The inline publish after approve and the inline sink attempt stay inline and take no lease:
  the publisher's row lock and the `Lakeloom-Publication` trailer search already make a double
  attempt safe (ADR 0013).

### Graceful shutdown (NFR-OPS-04)

- `LAKELOOM_SHUTDOWN_TIMEOUT_SECONDS` (default `30`, 1–600) is uvicorn's
  `timeout_graceful_shutdown`.
- On shutdown `JobRunner.stop()` ends each loop between ticks (a sleeping loop is cancelled, a
  running tick may finish within the same timeout) and releases its leases, so another replica
  takes over without waiting for the TTL. Queued publications and pending sinks stay in the
  store.

### Rate limit (NFR-SEC-16)

- A token bucket per `(principal.type, principal.subject)`: `LAKELOOM_RATE_LIMIT_PER_MINUTE`
  (default `300`, `0` = off) refills, `LAKELOOM_RATE_LIMIT_BURST` (default `60`) is its size.
- Checked on every MCP `tools/call` (in `guarded()`) and every `/v1` request; health, metrics,
  PRM, MCP `initialize` and `*/list` are not limited.
- Over the limit: MCP tool error `rate_limited` with a "retry after <n> s" hint; HTTP `429` with
  `Retry-After` (whole seconds, ≥ 1). Metric `lakeloom_rate_limited_total{surface}`. Idle buckets
  are dropped after 10 minutes. The limit is per replica.

### MCP sessions

- A wrapper around the MCP ASGI app binds each `mcp-session-id` to the principal that created
  it. A foreign or unknown session id gets the same `404` "unknown MCP session", so ids cannot
  be probed. Entries end with the session (`DELETE`) or after 1800 s idle.
- `LAKELOOM_MCP_STATELESS` (default `false`) runs the SDK stateless (no session ids). More than
  one replica needs either stateless mode or sticky sessions.

### Reconciliation (FR-GIT-08, FR-OM-02, FR-GIT-03, FR-GIT-09)

- Scope: models whose Git directory has a `done` publication; Git publishing off means the job
  is off and `lakeloom reconcile` fails with `validation_error`.
- Baseline: `ontology.ttl` at the model's latest `done` publication. The OM export and the
  baseline are compared with `semantic_view` + `diff_graphs`: no bump is `in_sync`, anything
  else `drifted` with the added, changed and removed terms.
- Skips: a pending or blocked publication ("publication in flight"), no done publication
  ("never published"), a baseline commit that cannot be fetched; OM unreachable is `error`.
- A run writes one audit row per drifted model (principal `system:reconcile`, type `system`), the
  gauge `lakeloom_reconcile_drifted_models`, the counter `lakeloom_reconcile_runs_total{outcome}`,
  a JSON log line per drifted model (counts, never term text) and `jobs.reconcile` in
  `get_server_info`.
- Commit (only with `LAKELOOM_RECONCILE_COMMIT=true` or `lakeloom reconcile --commit`): a
  `Publication` with `proposal_id` `rec_<ulid>`, rendered exactly as an applied proposal would be,
  subject `ontology(<slug>): direct edits in OpenMetadata` and trailers
  `Lakeloom-Origin: om-direct-edit`, `Lakeloom-Reconcile: <id>`, `Lakeloom-Publication: <id>`;
  in the model's publication mode. Reconciliation only reads OM and never reverts an edit, and
  every Git write still goes through the outbox.

### Scheduled drift (FR-PRO-07)

- `DriftService.check` plans drift without writing a proposal (it writes the fresh scan
  records, as `detect` does). The `drift-schedule` job runs it as `system:drift` for every model
  with an applied scan, and reports the gauge `lakeloom_drift_tables{model}`, a JSON log line and
  `jobs.drift` in `get_server_info`. Off by default (`LAKELOOM_DRIFT_INTERVAL_SECONDS=0`): every
  run scans the lake.

### Principals of jobs and settings

- Jobs act as `Actor(subject="system:reconcile" | "system:drift", type="system")`. The type
  `system` exists in audit rows only; no token can carry it. Jobs bypass per-model groups, as
  `Actor` already does (NFR-SEC-07).
- New settings: `LAKELOOM_RATE_LIMIT_PER_MINUTE` `300`, `LAKELOOM_RATE_LIMIT_BURST` `60`,
  `LAKELOOM_MCP_STATELESS` `false`, `LAKELOOM_RECONCILE_INTERVAL_SECONDS` `3600` (`0` or
  60–86400), `LAKELOOM_RECONCILE_COMMIT` `false`, `LAKELOOM_DRIFT_INTERVAL_SECONDS` `0` (`0` or
  300–604800), `LAKELOOM_SHUTDOWN_TIMEOUT_SECONDS` `30`.

## Research

Checked live on the dev stack (OM 2.1.0-rc1) by `tests/contract/test_om_direct_edit_probe.py`
(throwaway glossary `llm4a_probe_<hex>`, two terms created by a change set the bot applies,
hard-deleted in teardown, never sealed). An OM upgrade that changes any of this fails there.

- **R1. The export is stable.** Two glossary exports of an unchanged glossary have isomorphic
  semantic views (`semantic_view` with the model's base IRI drops `om:fullyQualifiedName`,
  `skos:inScheme`, `skos:topConceptOf` and every subject outside the base IRI, so the glossary
  id, prefixes and triple order never matter); `diff_graphs` of the two reports `bump: None`.
  An untouched model is therefore `in_sync`.
- **R2. A direct edit is possible and visible.** A bot `PATCH /api/v1/glossaryTerms/{id}`
  (`application/json-patch+json`, `replace /description`) outside any change set returns `200`.
  The next export carries the new text as the term's `skos:definition` (the predicate ADR 0016
  recorded for descriptions) and nothing else of substance: `diff_graphs` names exactly that term
  in `changed`, with no added or removed term (`bump: patch`, annotation only). No change-set
  fallback was needed.
- **R3. A term `PATCH` does not move the glossary's `updatedAt`.** The model version
  (`g<glossary updatedAt>-n<native revision>`) therefore does not change on a direct term edit:
  only a fresh export shows it. Reconciliation drops the model's cached graph
  (`ModelService.invalidate(slug)`) before it reads, so a cached export never hides an edit.
  Version-keyed caches elsewhere (HTTP ETags) can stay stale until the cache TTL expires; that is
  unchanged behaviour, noted here for M4b.
- **R4. Approvals in flight.** `GitHook` inserts an approval's publication row only after OM has
  applied it, so "a pending or blocked publication" alone does not cover an apply. After its
  OM read, and again right before it enqueues a commit, reconciliation also skips ("publication
  in flight") when a proposal of the model is pending (`submitted`/`apply_failed`) with a change
  set that is `APPLIED` (stored, or in OM now: read with `get_change_set`), or `applied` since
  the baseline publication was created without a publication row. The checks follow an
  approval's own order, so one that moves on between two checks is caught by a later one. A
  commit is enqueued only with exports read after the check (a failed export enqueues
  nothing), so it never takes in an approval made after the check. The per-model
  `GitHook` lock is not used: it is per process and held only around the enqueue, not during
  the apply. Residual windows: an `applied` proposal whose row could never be inserted
  (`Git: not queued`, an operator must re-publish it) from before the baseline publication is
  not seen; a proposal stopped part-way (`apply_failed`) keeps its model skipped until it is
  approved again or rejected; changes of a rejected, partly applied proposal are reported as
  direct edits.
- Reconciliation reads OM only through endpoints Lakeloom already uses (the glossary and native
  exports, `get_change_set`); it adds no OM endpoint.

## Consequences

- Two or more replicas can share one PostgreSQL state store: each job runs on one of them, and a
  replica that dies loses its jobs after at most one TTL. The lease table is tested on SQLite and
  PostgreSQL.
- Clock skew between replicas larger than the TTL could let two replicas hold one job; the
  outbox stays safe even then (row lock and trailer search, ADR 0013).
- The rate limit and MCP session bindings are per replica: a load balancer without sticky
  sessions needs `LAKELOOM_MCP_STATELESS=true`.
