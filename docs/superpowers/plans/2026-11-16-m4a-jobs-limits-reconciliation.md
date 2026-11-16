# M4a — Job Leases, Rate Limiting, MCP Sessions and the Reconciliation Job Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** make Lakeloom safe to run as more than one replica and under load, and close FR-GIT-08:
- background jobs take a **lease in the state store**, so only one replica runs a given job (NFR-DEP-07);
- **rate limiting** per principal on MCP tools and the HTTP API (NFR-SEC-16);
- MCP sessions are **bound to the principal** that opened them, with an optional **stateless** mode for replicas without sticky sessions;
- the **reconciliation job** compares OM with the last Git commit of each model, reports direct edits made in OM's UI, and optionally commits them with `Lakeloom-Origin: om-direct-edit` (FR-GIT-08);
- **drift on a schedule**: a report-only drift check per model (FR-PRO-07, M3c backlog).

**Architecture:**
- **Leases:**
  - `state/leases.py` holds a `job_leases` table and an atomic acquire/renew/release that works on SQLite and Postgres;
  - `core/jobs.py` holds `JobRunner`, one loop per job that runs a tick only while it holds the job's lease;
  - the Git outbox worker, the triple-store sink worker, reconciliation and scheduled drift all run through `JobRunner`;
  - in-process locks (`repo_lock`, the per-model proposal lock) stay as they are: they guard one replica's working copy and its event loop.
- **Rate limiting:**
  - `server/ratelimit.py` is an in-process token bucket keyed by principal;
  - HTTP `/v1` checks it in a router dependency, MCP tools check it in `guarded()`;
  - the limit is per replica, and this is documented.
- **MCP sessions:** a thin ASGI wrapper around the MCP endpoint remembers which principal opened each `mcp-session-id` and refuses that session to anyone else. `LAKELOOM_MCP_STATELESS=true` serves MCP without sessions.
- **Reconciliation (`core/reconcile.py`):**
  - per model, export OM as the publisher does (`fill_exports`) and read `ontology.ttl` at the model's last `done` publication commit;
  - compare both through `semantic_view` + `diff_graphs` (M3c);
  - report the result; when `LAKELOOM_RECONCILE_COMMIT=true`, enqueue a publication whose snapshot carries the OM state and the `om-direct-edit` trailer.
  - A model is never committed by reconciliation while it has a pending or blocked publication.

**Tech Stack:** existing stack (SQLAlchemy + Alembic, FastAPI, MCP SDK 2.3, rdflib, prometheus-client, Typer). No new runtime dependency.

**Spec:** `REQUIREMENTS.md`:
- NFR-DEP-07 (stateless HTTP/MCP layer; background jobs use a lease in the state store; SQLite is single-replica only, documented);
- NFR-SEC-16 (rate limiting per principal, configurable, on MCP and HTTP), FR-AGT-05 (`rate_limited` error with a hint);
- FR-GIT-08 (reconciliation job, report OM-direct edits, optional commit with `Lakeloom-Origin: om-direct-edit`), FR-GIT-09 (Git operations serialised and retried; the queue is the only commit path);
- FR-PRO-07 (drift), NFR-OPS-02 (metrics), NFR-OPS-04 (graceful shutdown), NFR-SEC-12 (audit), NFR-SEC-11 (no secrets in logs);
- §13 M4: "rate limiting, reconciliation job".

Earlier decisions it builds on:
- ADR 0013 (Git outbox, publisher, retry classes, "Job lease: in-process, multi-replica deferred");
- ADR 0015 (releases, `ReleaseReader`, `release_diff`);
- ADR 0016 (`semantic_view`, `diff_graphs` with `bump is None` as "no semantic diff", `DriftService`).

New ADR: `docs/decisions/0017-jobs-limits-reconciliation.md`.

## How M4 is split (controller decision; owner may overrule at plan review)

§13 M4 holds three independent fronts, so M4 is split like M3:
- **M4a (this plan):** runtime hardening: leases, rate limiting, MCP sessions, reconciliation, scheduled drift, graceful shutdown.
- **M4b — connectors:**
  - Snowflake connector (FR-SRC-04, `snowflake-connector-python` as the `snowflake` extra), unit-tested with a fake connection;
  - the Databricks and Snowflake MCP mapping examples (FR-SRC-05), with a verification script and guide;
  - the `databricks` extra (TC-7).
  - Live verification needs owner accounts. Without them, the examples stay marked "examples to verify".
- **M4c — first public release:**
  - Helm chart (NFR-DEP-06), MkDocs Material site with generated tool and HTTP references (NFR-OSS-05);
  - release workflow to GHCR and PyPI with cosign signing and SBOM (NFR-OSS-03, NFR-DEP-01), actions pinned by SHA (m0 backlog);
  - security review (NFR-TST-05), the MUST audit with an ADR for each deferral;
  - quick start timed on a clean machine (< 15 min), and `v0.1.0`.
  - The `lakeloom` PyPI name looks reserved already (`pypi_files/lakeloom-0.0.1`): to confirm with the owner then.

## Decisions taken in this plan (controller; owner may overrule at plan review)

- **Lease table** `job_leases` (migration `0005_job_leases`):
  - columns `name` (String(128), PK), `holder` (String(128)), `acquired_at`, `expires_at` (DateTime tz);
  - `holder` is the instance id `"<hostname>-<pid>-<8 hex>"`, made once per process (`instance_id()`).
- **Acquire is one atomic statement, never read-then-write:**
  - `UPDATE job_leases SET holder=:me, expires_at=:exp, acquired_at=CASE WHEN holder=:me THEN acquired_at ELSE :now END WHERE name=:n AND (holder=:me OR expires_at < :now)`;
  - when no row was updated, `INSERT`, and an `IntegrityError` means "held by someone else";
  - the database clock is not used: every replica's clock is assumed within the TTL (documented).
  - TTL = `max(3 × interval, 60 s)`. A tick renews before it runs, and `release` deletes only its own row.
- **What runs under a lease:**
  - `git-publisher:<repo_key>` (the outbox, interval `LAKELOOM_GIT_RETRY_INTERVAL_SECONDS`);
  - `triplestore-sink` (the same tick as today, now its own job);
  - `reconcile` (interval `LAKELOOM_RECONCILE_INTERVAL_SECONDS`);
  - `drift-schedule` (interval `LAKELOOM_DRIFT_INTERVAL_SECONDS`).
  - The inline publish after approve and the inline sink attempt stay inline. They take no lease: the publisher's row lock and the `Lakeloom-Publication` trailer search already make a double attempt safe (ADR 0013).
- **Rate limit:**
  - a token bucket per `(principal.type, principal.subject)`: `LAKELOOM_RATE_LIMIT_PER_MINUTE` (default `300`, `0` = off) refills, `LAKELOOM_RATE_LIMIT_BURST` (default `60`, ≥ 1) is the bucket size;
  - checked on every MCP `tools/call` (in `guarded()`, before the body) and every `/v1` request (router dependency). Health, metrics, PRM, MCP `initialize` and `*/list` are not limited;
  - over the limit:
    - MCP returns the tool error `rate_limited`, message "rate limit exceeded for this principal", hint "retry after <n> s";
    - HTTP returns `429` with the typed body and `Retry-After: <n>` (whole seconds, ≥ 1);
  - metric `lakeloom_rate_limited_total{surface}` (`mcp` / `http`). Buckets idle for 10 minutes are dropped (memory bound). The limit is per replica.
- **MCP sessions:**
  - `server/mcp_sessions.py` wraps the MCP ASGI app. On a response carrying `mcp-session-id`, it records `session id → (type, subject)`. A request whose `mcp-session-id` is bound to another principal gets `404` with the typed `not_found` body "unknown MCP session", the same answer as an unknown id, so session ids cannot be probed. Entries go when the SDK's session ends (`DELETE`) or after `session_idle_timeout` (1800 s, the SDK default);
  - `LAKELOOM_MCP_STATELESS` (default `false`) passes `stateless_http=True` (and `json_response=True`) to `streamable_http_app`. Then no session ids exist and the wrapper is a pass-through. ADR 0017 and the Helm chart (M4c) say that more than one replica needs either stateless mode or sticky sessions.
- **Reconciliation:**
  - **Scope:** the models whose Git directory has at least one `done` publication. Git disabled means the job is off and `lakeloom reconcile` fails with `validation_error` "Git publishing is not configured".
  - **Skips:**
    - a pending or blocked publication: `skipped`, reason "publication in flight";
    - no done publication: `skipped`, reason "never published";
    - the baseline commit missing even after fetching the target branch and the publication's branch: `skipped`, reason "baseline commit not found";
    - OM unreachable: `error`, with no commit.
  - **Baseline:** `ontology.ttl` at the commit of the model's latest `done` publication (`PublicationRepository.last_done`), read with `WorkingCopy.show(sha, path)` under `repo_lock`. A release commit never changes `ontology.ttl`, so the last publication is the last state Git holds.
  - **Compare:** `semantic_view` of both graphs with the model's base IRI, then `diff_graphs`:
    - `bump is None` means `in_sync`;
    - anything else is `drifted`, with the added, changed and removed term lists of `ReleaseDiff`.
  - **Report:** `ReconcileResult(model, status: Literal["in_sync","drifted","skipped","error"], reason: str | None, added/changed/removed: list[str], publication_id: str | None)`. A run writes:
    - one audit row per drifted model (tool `reconcile`, principal `system:reconcile`, type `system`);
    - the gauge `lakeloom_reconcile_drifted_models`;
    - the counter `lakeloom_reconcile_runs_total{outcome}`;
    - a JSON log line per drifted model (model and counts, never term text);
    - a `jobs.reconcile` block in `get_server_info` (last run time, the counts per status, the drifted slugs).
  - **Commit** (only with `LAKELOOM_RECONCILE_COMMIT=true` or `lakeloom reconcile --commit`): enqueue a `Publication`:
    - `proposal_id` is `rec_<ulid>`: the column is unique, and a reconcile id never collides with `prp_` ids;
    - the snapshot is built from the OM export exactly as `ReviewGit._enqueue` builds an applied one;
    - message subject `ontology(<slug>): direct edits in OpenMetadata`; the body lists up to 50 term keys, like `build_message`;
    - trailers `Lakeloom-Origin: om-direct-edit` and `Lakeloom-Reconcile: <rec id>`. The publisher adds `Lakeloom-Publication` as it does for every row;
    - provenance record `{"origin": "om-direct-edit", "reconcile_id": …, "at": …}`;
    - publication mode: the model's mode (a PR in `pull_request` mode).
- **Scheduled drift:**
  - `DriftService.check(principal, client, model) -> DriftReport` runs `_plan` and returns the report with `proposal=None`. It never writes a proposal, but it writes the fresh scan records, as `detect` does;
  - the `drift-schedule` job runs `check` for every model with at least one applied scan (`applied_scan_ids`), as `system:drift`;
  - it reports through the gauge `lakeloom_drift_tables{model}` (changed + added + removed tables), a JSON log line, and `jobs.drift` in `get_server_info`;
  - `LAKELOOM_DRIFT_INTERVAL_SECONDS` defaults to `0` (off): every run scans the lake.
- **Principals of jobs:** `Actor(subject="system:reconcile" | "system:drift", type="system")`. The type `system` is new in audit rows only; no token can carry it. Jobs bypass per-model groups, as `Actor` already does (NFR-SEC-07).
- **Graceful shutdown (NFR-OPS-04):**
  - `LAKELOOM_SHUTDOWN_TIMEOUT_SECONDS` (default `30`, 1–600) is passed to uvicorn as `timeout_graceful_shutdown`;
  - on shutdown, `JobRunner.stop()` cancels each loop between ticks and releases its leases. Queued publications stay in the store, as today.
- **New settings (REQUIREMENTS §10):**
  - `LAKELOOM_RATE_LIMIT_PER_MINUTE` `300`, `LAKELOOM_RATE_LIMIT_BURST` `60`;
  - `LAKELOOM_MCP_STATELESS` `false`;
  - `LAKELOOM_RECONCILE_INTERVAL_SECONDS` `3600` (`0` or 60–86400), `LAKELOOM_RECONCILE_COMMIT` `false`;
  - `LAKELOOM_DRIFT_INTERVAL_SECONDS` `0` (`0` or 300–604800);
  - `LAKELOOM_SHUTDOWN_TIMEOUT_SECONDS` `30`.

## Global Constraints

- **Earlier constraints still apply:**
  - Python ≥ 3.12; uv / ruff / pyright strict on `src/` (CI also runs pyright on `tests/`, so a test-only import must resolve).
  - Conventional Commits with requirement IDs; `git commit -s`; identity `Elcio Abrahao <elcio@tuta.com>`; trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  - Typed `LakeloomError`s with hints; untrusted text quoted in tool output.
  - Tools run through `guarded()`; every OM endpoint is in `OMClient.PATHS` and in the committed spec.
  - Code default and unit tests on SQLite; the compose stack and CI on Postgres. M4a adds one migration (`0005_job_leases`), tested on both.
- **FR-OM-02:** reconciliation only reads OM. It never writes OM, and it never reverts a direct edit.
- **FR-GIT-03:** a reconcile commit renders files through the same `render`, so they are byte-identical to what an applied proposal with the same OM state would produce.
- **FR-GIT-09:** every Git write still goes through the publication outbox; reconciliation never commits or pushes on its own.
- **NFR-SEC-11:** lease holders, rate-limit keys and session bindings never hold tokens; logs name the principal subject at most.
- **Dev-stack rules (owner):**
  - Throwaway models only (`retail_m4a`, hard-deleted in teardown); never modify `retail`; never seal OM releases.
  - Approve only as alice; no tokens printed or committed.
  - No `down -v`, no prune, never touch `supabase_*`.

## Review Focus

1. **Two replicas racing for one job** (two `LeaseRepository` instances with different holders on one database, including an expired lease taken over while the old holder is mid-tick). Expected: exactly one holder at any time; the old holder's next renew fails and it stops before running another tick; its `release` never deletes the new holder's row. → Task 1 `test_only_one_holder_under_contention`, `test_takeover_after_expiry`, `test_release_keeps_other_holder`, `test_renew_lost_skips_tick`.
2. **A burst from one agent while another works** (agent A sends 100 calls at once, agent B one). Expected: A gets `rate_limited` with a `Retry-After`/hint of at least 1 s after its burst; B is not affected; a human and an agent with the same `sub` text but different types do not share a bucket. → Task 2 `test_burst_then_limited`, `test_principals_do_not_share_buckets`, `test_retry_after_is_whole_seconds_at_least_one`.
3. **A stolen MCP session id** (principal B replays A's `mcp-session-id`). Expected: `404` "unknown MCP session", the same as an unknown id; A's session keeps working. → Task 3 `test_session_refused_to_other_principal`, `test_unknown_and_foreign_sessions_look_alike`.
4. **Reconciliation racing an approval** (a proposal is applied, so OM moved, but its publication is still pending or blocked). Expected: the model is `skipped` with "publication in flight", never `drifted`, and nothing is committed. A model never published is `skipped` too. → Task 4 `test_pending_publication_skips`, `test_blocked_publication_skips`, `test_never_published_skips`.
5. **An OM export that differs only in bookkeeping** (glossary id, change-set ids, triple order, prefixes). Expected: `in_sync`. A real direct edit (a description changed in OM) is `drifted` and names the term. → Task 4 `test_bookkeeping_only_is_in_sync`, `test_description_edit_is_drifted`.

## File structure

```
src/lakeloom/state/leases.py                       # LeaseRepository, instance_id
src/lakeloom/state/migrations/versions/0005_job_leases.py
src/lakeloom/core/jobs.py                          # Job, JobRunner, JobStatus
src/lakeloom/server/ratelimit.py                   # RateLimiter
src/lakeloom/server/mcp_sessions.py                # SessionBindingMiddleware
src/lakeloom/core/reconcile.py                     # ReconcileService, ReconcileResult, ReconcileRun
src/lakeloom/core/drift_service.py                 # + check (modify)
src/lakeloom/gitstore/layout.py                    # reconcile message + trailers (modify)
src/lakeloom/state/publications.py                 # + has_open(repo, model), last_done reuse (modify)
src/lakeloom/server/http_app.py, mcp_app.py, tools_common.py, http_api.py, server_info.py   (modify)
src/lakeloom/metrics.py, config.py, cli.py          (modify)
docs/decisions/0017-jobs-limits-reconciliation.md
tests/unit/state/test_leases.py, tests/unit/core/test_jobs.py, tests/unit/server/test_ratelimit.py,
tests/unit/server/test_mcp_sessions.py, tests/unit/core/test_reconcile.py,
tests/contract/test_om_direct_edit_probe.py, tests/contract/test_m4a_acceptance.py
```

---

### Task 1: Job leases, the job runner and ADR 0017 (proposed)

**Files:**
- Create `src/lakeloom/state/leases.py`, `src/lakeloom/state/migrations/versions/0005_job_leases.py`, `src/lakeloom/core/jobs.py`, `tests/unit/state/test_leases.py`, `tests/unit/core/test_jobs.py`, `docs/decisions/0017-jobs-limits-reconciliation.md` (status proposed; the Decisions above).
- Modify:
  - `src/lakeloom/state/models.py` (`JobLeaseRow`);
  - `src/lakeloom/server/http_app.py`: `git_worker` becomes two `Job`s run by a `JobRunner` in the lifespan; `AppDeps.jobs`;
  - `src/lakeloom/config.py` (`shutdown_timeout_seconds`), `src/lakeloom/cli.py` (`timeout_graceful_shutdown`);
  - `tests/unit/test_config.py`, `tests/unit/state/test_migrations.py`.

**Interfaces:**
- **Produces:**
  - `def instance_id() -> str` (cached per process);
  - `class LeaseRepository(engine)` with:
    - `async acquire(name: str, holder: str, ttl: timedelta, *, now: datetime | None = None) -> bool` (acquire or renew);
    - `async release(name: str, holder: str) -> None`;
    - `async holder_of(name: str) -> str | None`;
  - `@dataclass Job(name: str, interval: Callable[[], float], tick: Callable[[], Awaitable[None]])`;
  - `class JobRunner(leases: LeaseRepository, holder: str, *, clock=utcnow, sleep=asyncio.sleep)` with:
    - `add(job: Job) -> None`, `async start() -> None`, `async stop() -> None`;
    - `status() -> dict[str, JobStatus]`, where `JobStatus(held: bool, last_run_at: datetime | None, last_error: str | None)`;
  - a tick that raises is logged with the class name only and does not stop the loop.
- [ ] **Step 1: Tests:**
  - `test_acquire_free_lease`;
  - `test_only_one_holder_under_contention` (20 concurrent acquires by distinct holders → exactly one `True`);
  - `test_renew_by_holder`;
  - `test_takeover_after_expiry`;
  - `test_release_keeps_other_holder`;
  - `test_renew_lost_skips_tick` (runner: a fake lease that returns `False` on the second acquire → the tick ran once);
  - `test_tick_error_does_not_stop_loop`;
  - `test_stop_releases_leases`;
  - `test_migration_0005_up_and_down` in `tests/unit/state/test_migrations.py` (SQLite, plus Postgres when `LAKELOOM_TEST_DB_URL` is set, as `test_upgrade_postgres` does);
  - `test_leases_postgres` (the contention test on Postgres when `LAKELOOM_TEST_DB_URL` is set);
  - `test_git_worker_runs_under_lease` (app lifespan with a fake publisher: `process` is called only while the lease is held).
- [ ] **Step 2:** FAIL → implement → pass; ruff and pyright.
- [ ] **Step 3: Commit** `feat(state): job leases in the state store and a lease-guarded job runner (NFR-DEP-07, NFR-OPS-04)`.

---

### Task 2: Rate limiting per principal

**Files:**
- Create `src/lakeloom/server/ratelimit.py`, `tests/unit/server/test_ratelimit.py`.
- Modify:
  - `src/lakeloom/server/tools_common.py` (`guarded()` checks the limiter before the body; the limiter comes from `deps`);
  - `src/lakeloom/server/http_api.py` (a router dependency next to `_require_read`; `lakeloom_error_handler` sets `Retry-After` from `RateLimited.retry_after`);
  - `src/lakeloom/errors.py` (`class RateLimited(LakeloomError)` with `retry_after: int`, and `details()` returning `{"retry_after": n}`);
  - `src/lakeloom/metrics.py` (`RATE_LIMITED`), `src/lakeloom/config.py`;
  - `src/lakeloom/server/auth.py`: the m0 backlog item "rate-limit the 503 traceback log" logs the IdP traceback at most once per 60 s.

**Interfaces:**
- **Produces:**
  - `class RateLimiter(per_minute: int, burst: int, *, clock: Callable[[], float] = time.monotonic)` with `check(key: tuple[str, str]) -> int | None` (`None` means allowed; else the retry-after seconds, ≥ 1), and `enabled: bool`;
  - `def principal_key(p: Principal | Caller) -> tuple[str, str]`.
- [ ] **Step 1: Tests:**
  - `test_burst_then_limited` (burst 5 → 5 allowed, the 6th returns ≥ 1);
  - `test_refill_over_time` (fake clock);
  - `test_principals_do_not_share_buckets` (same subject, agent vs human);
  - `test_zero_disables`;
  - `test_idle_buckets_dropped`;
  - `test_retry_after_is_whole_seconds_at_least_one`;
  - `test_mcp_tool_rate_limited` (in-process MCP client: the third call with burst 2 returns `rate_limited` with the hint "retry after");
  - `test_http_v1_rate_limited` (`429`, `Retry-After`, typed body);
  - `test_health_and_metrics_not_limited`;
  - `test_tools_list_not_limited`.
- [ ] **Step 2:** FAIL → implement → pass.
- [ ] **Step 3: Commit** `feat(server): rate limiting per principal on MCP tools and the HTTP API (NFR-SEC-16, FR-AGT-05)`.

---

### Task 3: MCP sessions bound to their principal, and stateless mode

**Files:**
- Create `src/lakeloom/server/mcp_sessions.py`, `tests/unit/server/test_mcp_sessions.py`.
- Modify `src/lakeloom/server/mcp_app.py` (wrap `mcp_http`; pass `stateless_http`/`json_response` from settings), `src/lakeloom/config.py` (`mcp_stateless`), `src/lakeloom/server/server_info.py` (`features.mcp_stateless`).

**Interfaces:**
- **Produces:** `class SessionBindingMiddleware(app: ASGIApp, *, idle_timeout: float = 1800, clock=time.monotonic)`. It reads `scope["state"]["principal"]`, which `BearerAuthMiddleware` sets before it runs.
- [ ] **Step 1: Tests:**
  - `test_session_bound_on_initialize` (the response header id is recorded);
  - `test_session_refused_to_other_principal` (`404`, typed `not_found`);
  - `test_unknown_and_foreign_sessions_look_alike` (status and body equal);
  - `test_owner_keeps_using_session`;
  - `test_delete_unbinds`;
  - `test_idle_binding_expires`;
  - `test_stateless_mode_has_no_session_header` (a full app with `mcp_stateless=True`: `initialize` then `tools/call` with no session header both succeed).
- [ ] **Step 2:** FAIL → implement → pass.
- [ ] **Step 3: Commit** `feat(server): bind MCP sessions to their principal; optional stateless MCP (NFR-DEP-07, NFR-SEC-02)`.

---

### Task 4: The reconciliation service

**Files:**
- Create `src/lakeloom/core/reconcile.py`, `tests/unit/core/test_reconcile.py`, `tests/contract/test_om_direct_edit_probe.py`.
- Modify:
  - `src/lakeloom/gitstore/layout.py` (`build_reconcile_message(slug, diff: ReleaseDiff, reconcile_id: str) -> str`);
  - `src/lakeloom/state/publications.py` (`async has_open(repo: str, model: str) -> bool`: a pending or blocked row exists);
  - `src/lakeloom/core/review_git.py`: extract the snapshot building of `_enqueue` into `async build_snapshot(models, om, slug, *, proposal_id, message, provenance, source_tables) -> Snapshot`, used by both paths;
  - `src/lakeloom/metrics.py`.

**Interfaces:**
- **Consumes:** `fill_exports`, `semantic_view`, `diff_graphs`, `parse_rdf`, `WorkingCopy.show`/`fetch_branch`, `repo_lock`, `PublicationRepository.last_done`, `GitPublisher.enqueue`, `AuditRepository`.
- **Produces:**
  - `class ReconcileResult(BaseModel)` (fields in Decisions);
  - `class ReconcileRun(BaseModel)`: `at: datetime`, `results: list[ReconcileResult]`;
  - `class ReconcileService(...)` with:
    - `async check(slug: str) -> ReconcileResult`;
    - `async run(*, commit: bool) -> ReconcileRun`;
    - `last_run: ReconcileRun | None`.
- [ ] **Step 1: Live probe first** (`test_om_direct_edit_probe.py`, throwaway `llm4a_probe_<hex>`, hard-deleted, never sealed):
  - a bot `PATCH` of a glossary term description outside any change set succeeds on rc1;
  - the glossary export then shows the new `skos:definition` (or whichever predicate ADR 0016 recorded).
  - Record both in ADR 0017 § Research. If the direct PATCH is refused, use a change set applied as the bot instead (ADR 0011 test setup) for the acceptance test, and say so in the ADR.
- [ ] **Step 2: Tests** (fake OM export, fake working copy):
  - `test_bookkeeping_only_is_in_sync`;
  - `test_description_edit_is_drifted` (names the term in `changed`);
  - `test_added_concept_is_drifted`;
  - `test_pending_publication_skips`, `test_blocked_publication_skips`, `test_never_published_skips`;
  - `test_baseline_missing_after_fetch_skips`;
  - `test_om_unreachable_is_error_without_commit`;
  - `test_commit_enqueues_publication_with_trailers` (`rec_` id, subject, `Lakeloom-Origin: om-direct-edit`, `Lakeloom-Reconcile`, the model's mode);
  - `test_in_sync_commits_nothing`;
  - `test_reconcile_render_equals_apply_render` (same OM state → byte-identical files, FR-GIT-03);
  - `test_audit_row_per_drifted_model` (principal `system:reconcile`, type `system`).
- [ ] **Step 3:** FAIL → implement → pass.
- [ ] **Step 4: Commit** `feat(core): reconciliation of OpenMetadata against the last Git commit (FR-GIT-08)`.

---

### Task 5: Scheduled jobs, CLI and server info

**Files:**
- Modify:
  - `src/lakeloom/core/drift_service.py` (`check`), `src/lakeloom/server/http_app.py` (`reconcile` and `drift-schedule` jobs added to the runner when their interval is > 0 and, for reconcile, Git is enabled);
  - `src/lakeloom/server/server_info.py` (`jobs`: per job `held`, `last_run_at`, `last_error`, plus `reconcile` and `drift` summaries);
  - `src/lakeloom/cli.py` (`lakeloom reconcile [--model SLUG] [--commit] [--json]`; exit code `0` in sync, `3` drift found, `1` error);
  - `src/lakeloom/config.py`, `src/lakeloom/metrics.py`;
  - `tests/unit/core/test_drift_service.py`, `tests/unit/server/test_server_info.py`, `tests/unit/test_cli.py`.

**Interfaces:**
- **Consumes:** `JobRunner`/`Job` (Task 1), `ReconcileService` (Task 4), `DriftService._plan`.
- **Produces:** `async DriftService.check(principal: Caller, client: str | None, model: str) -> DriftReport` (with `proposal` always `None`).
- [ ] **Step 1: Tests:**
  - `test_check_never_writes_a_proposal`;
  - `test_drift_job_runs_check_per_model_with_applied_scan`;
  - `test_drift_job_off_by_default`;
  - `test_reconcile_job_off_without_git`;
  - `test_reconcile_job_commit_flag`;
  - `test_server_info_jobs_block` (no secret, no term text);
  - `test_cli_reconcile_exit_codes`;
  - `test_cli_reconcile_without_git_is_validation_error`;
  - `test_settings_ranges` (`0` or 60–86400 for reconcile; `0` or 300–604800 for drift).
- [ ] **Step 2:** FAIL → implement → pass.
- [ ] **Step 3: Commit** `feat: scheduled reconciliation and drift checks, lakeloom reconcile (FR-GIT-08, FR-PRO-07)`.

---

### Task 6: M4a acceptance (live) and wrap-up

**Files:**
- Create `tests/contract/test_m4a_acceptance.py`, `docs/backlog/m4a-deferred.md`.
- Modify:
  - ADR 0017 (accepted; Consequences), ADR 0013 (the "Job lease" line points to 0017);
  - README (multi-replica notes, rate limits, reconciliation), CHANGELOG;
  - `REQUIREMENTS.md` §10 (the seven new settings);
  - `docs/backlog/m3c-deferred.md` (strike the reconciliation and scheduled-drift lines), `docs/backlog/m0-deferred.md` (strike "stateful MCP sessions are not bound to a principal" and the 503 log item).
  - In `deploy/compose/docker-compose.yml`, the lakeloom service gets `LAKELOOM_RECONCILE_INTERVAL_SECONDS: "0"` (the test drives reconciliation through the CLI, so no background run interferes).

- [ ] **Step 1: `test_m4a_acceptance.py`** runs against the running stack (Git mode `direct`, Postgres state). Teardown hard-deletes `retail_m4a` in OM; it never seals.
  - `test_m4a_reconcile_reports_and_commits_direct_edit`:
    - setup on `retail_m4a`: agent scan of `fixture_delta`, `create_model`, `propose_from_scan`, submit, alice approves, publication `done`;
    - `docker compose exec lakeloom lakeloom reconcile --model retail_m4a --json` → `in_sync`, exit 0;
    - a direct edit in OM (the Task 4 probe's method) changes the description of the concept `Customer`;
    - reconcile again → `drifted`, with `Customer` in `changed`, exit 3; `get_server_info` → `jobs.reconcile` lists `retail_m4a`;
    - `lakeloom reconcile --model retail_m4a --commit` → a publication is queued, then becomes `done`; the Gitea commit carries `Lakeloom-Origin: om-direct-edit` and `Lakeloom-Reconcile: rec_…`, and its `ontology.ttl` holds the new description;
    - reconcile once more → `in_sync`.
  - `test_m4a_leases_on_postgres`: two `LeaseRepository` holders on the stack's Postgres (the URL from `stack.py`, never printed) → one holder; the lease row of the running server's `git-publisher:*` job exists with the server's holder id.
  - `test_m4a_rate_limit_live`: an in-process app built from the stack's settings, with `rate_limit_burst=5` and the live OM and Keycloak (the shared service is never restarted for this). Six `GET /v1/models` calls with the agent token → the sixth is `429` with `Retry-After` and the typed body; one call with alice's token still succeeds.
  - `test_m4a_mcp_session_foreign_principal_refused`: open a session as the agent, replay its id with alice's token → `404`.
- [ ] **Step 2:** rebuild Lakeloom (`docker compose -f deploy/compose/docker-compose.yml up -d --build --wait lakeloom`), then run the full unit and contract suites, ruff and pyright (on `src` and `tests`) green. Never `down -v`.
- [ ] **Step 3: Docs.** ADR 0017 accepted; README; CHANGELOG; REQUIREMENTS §10. Write `m4a-deferred.md`, at least with:
  - a shared rate limit across replicas (Redis or the state store);
  - the per-model proposal lock across replicas;
  - releases and imports serialised across replicas (today the remote rejects the loser's push);
  - reconciliation reverting a direct edit in OM;
  - scheduled drift creating proposals;
  - MCP session bindings shared across replicas.
- [ ] **Step 4: Commit** `test(contract): M4a acceptance — reconciliation of a direct OM edit, leases on Postgres (FR-GIT-08, NFR-DEP-07, NFR-SEC-16)`.

---

## Out of scope for M4a

- The Snowflake connector, the MCP mapping examples and the `databricks` extra (M4b).
- Helm chart, docs site, release workflow, signing, the security review, the MUST audit and `v0.1.0` (M4c).
- A rate limit shared across replicas; per-model proposal locks across replicas.
- Reverting OM direct edits; scheduled drift that writes proposals.
