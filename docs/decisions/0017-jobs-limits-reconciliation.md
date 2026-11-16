# 0017. Job leases, rate limiting, MCP sessions and reconciliation

- Status: accepted (M4a, 2026-11-16; live research in § Research, acceptance in § Consequences)
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
  sleep. While a tick runs, a heartbeat renews the lease every TTL/3; a refused renewal (or two
  failed ones in a row) cancels the tick (`last_error` `LeaseLost`, `held` false), so a tick
  longer than the TTL never runs on two replicas at once. A tick that raises is logged with its exception class only and the loop goes on;
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
- Checked on every MCP `tools/call` (in `guarded()`), every `resources/read` (the same bucket)
  and every `/v1` request; health, metrics, PRM, MCP `initialize` and `*/list` are not
  limited.
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
  ("never published"), the model deleted in OM ("model not found in OpenMetadata"), a baseline
  commit that cannot be found; OM unreachable is `error`.
- Pull request mode: the baseline is the publication's commit, merged or not, so an unmerged
  or closed pull request (`prp_` or `rec_`) still counts as the baseline until the model's next
  publication. When that commit is missing after fetching the target and the publication's
  branch (a squash merge deleted it), the commit of `origin/<target>` with the publication's
  `Lakeloom-Publication` trailer is the baseline (squash merges keep trailers; the publisher
  finds merged pull requests the same way); only then is it `skipped` "baseline commit not
  found".
- Leases: `reconcile` only schedules the job. A committing run (the job with
  `LAKELOOM_RECONCILE_COMMIT=true`, or `lakeloom reconcile --commit`) also holds
  `reconcile-commit` (TTL 15 minutes, renewed while it runs, released after), so two committing
  runs never queue the same drift twice. The CLI does not take `reconcile`; `--commit` is refused
  (`conflict`) only while another process holds `reconcile-commit`, and the job skips a
  committing tick while the CLI holds it.
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
  direct edits. Reconciliation also assumes OM never exposes a half-applied change set in an
  export; that is not verified and is listed for the M4b probe.
- Reconciliation reads OM only through endpoints Lakeloom already uses (the glossary and native
  exports, `get_change_set`); it adds no OM endpoint.

## Consequences

- Two or more replicas can share one PostgreSQL state store: each job runs on one of them, and
  another replica picks up a dead replica's jobs after at most one TTL plus one job interval
  (after a graceful release, up to one interval). A running tick keeps its lease by renewal and
  is cancelled if it loses it.
- **Replicas never share `LAKELOOM_DATA_DIR`.** The Git working copy at `<data_dir>/git/repo` is
  guarded by an in-process lock only, so each replica needs its own data directory. The lease table is tested on SQLite and
  PostgreSQL.
- Clock skew between replicas larger than the TTL could let two replicas hold one job; the
  outbox stays safe even then (row lock and trailer search, ADR 0013).
- **Shutdown** can take up to `timeout_graceful_shutdown` (in-flight requests) plus the
  runner's grace for a running tick: both are `LAKELOOM_SHUTDOWN_TIMEOUT_SECONDS`, so up to twice
  that value. Orchestrator grace periods (Kubernetes `terminationGracePeriodSeconds`) should
  allow for it.
- **The rate limit is per replica**, and MCP and HTTP share one bucket per principal: N
  replicas behind a load balancer admit up to N times the configured rate.
- **More than one replica** needs `LAKELOOM_MCP_STATELESS=true` or sticky sessions: MCP session
  bindings (and the SDK's sessions) live in one replica's memory. SQLite is single-replica
  only; several replicas need the shared PostgreSQL state store.
- **Manual reconciliation.** Reconciliation runs once at every server start (the job's first
  tick) and then every interval; it only reports unless `LAKELOOM_RECONCILE_COMMIT=true`. A
  manual `lakeloom reconcile --commit` works while the server's job runs: it takes only the
  `reconcile-commit` lease and is refused (`conflict` "a committing reconciliation is already
  running; try again later") only while a committing run elsewhere holds it. A run without
  `--commit` takes no lease and always works; when it skips every model, the CLI warns on
  stderr (exit code unchanged). A CLI run is not shown in the server's `jobs.reconcile` (that block
  is the server's own last run); its audit row is in the shared state store.
- **Scheduled drift** writes fresh scan records every interval (subject to their retention)
  and no audit row; it never writes a proposal.
- **A direct OM edit does not bump the glossary version** (R3), so version-keyed caches (HTTP
  ETags) can serve the old state until their TTL.
- **Approvals not yet in Git skip their model** (R4), including one stuck in `apply_failed`
  (reported as "publication in flight"); the residual windows are those listed in R4.
- **Live acceptance** (`tests/contract/test_m4a_acceptance.py`, dev stack, Git mode `direct`,
  PostgreSQL state store, `LAKELOOM_RECONCILE_INTERVAL_SECONDS=0` in compose). The compose
  stack also sets a generous rate limit (6000 per minute, burst 1000): with the defaults the
  contract suite, which calls as one agent principal, was throttled (`rate_limited`) in the
  M1b, M2a and M2b acceptance tests.
  - `retail_m4a` is built from `fixture_delta`, approved by alice and published. `lakeloom
    reconcile --model retail_m4a --json` (run in the Lakeloom container) is `in_sync`, exit 0.
  - The bot `PATCH`es the description of `Customer` outside any change set (R2). The next run
    is `drifted` with exactly `Customer` in `changed`, exit 3, nothing queued, and one audit row
    (`system:reconcile`, type `system`).
  - `--commit` queues a `rec_…` publication, which the server's `git-publisher` job publishes
    (`done`); the commit's subject is `ontology(retail_m4a): direct edits in OpenMetadata`, its
    trailers `Lakeloom-Origin: om-direct-edit`, `Lakeloom-Reconcile: rec_…` and
    `Lakeloom-Publication: <id>`, and its `ontology.ttl` holds the new `skos:definition`. The
    next run is `in_sync` again.
  - Leases on the stack's PostgreSQL: two holders racing for one lease give exactly one winner,
    and the loser can neither take nor release it; the running server's `git-publisher:<key>`
    row names the server's instance id (its container hostname), unexpired, and
    `get_server_info` shows the job `held`.
  - Rate limit: an in-process app on the live OM and Keycloak with a burst of 5 answers the
    sixth `GET /v1/models` of the agent with `429`, `Retry-After` and the typed
    `rate_limited` body, while alice's call still succeeds.
  - An MCP session opened by the agent and replayed with alice's token is `404` "unknown MCP
    session", the same body as a never-issued id; the agent's own session keeps working.
- Deferred follow-ups: [M4a backlog](../backlog/m4a-deferred.md).
