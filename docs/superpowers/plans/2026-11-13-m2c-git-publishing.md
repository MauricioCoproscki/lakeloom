# M2c — Git Publishing on Apply Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every proposal a human approves and applies in OpenMetadata becomes one Git commit in the configured ontology repository. The commit holds the model's deterministic export, its manifest and an appended provenance record, with the FR-GIT-04 trailers. It is pushed directly to the target branch or as a pull request (per model). A failed push is never lost: it waits in the state store and is retried.

**Architecture:**
- A new `gitstore` package:
  - `git.py` runs the `git` CLI through `asyncio` subprocesses with an isolated environment and credentials passed only through the environment;
  - `repo.py` manages the working copy under `/data/git`;
  - `layout.py` renders the files and the commit message;
  - `forge.py` opens PRs on Gitea, GitHub or GitLab;
  - `publisher.py` is the queue worker.
- At the end of a successful approve, `ReviewService` takes a **snapshot**: the model exported from OM, plus the proposal's provenance. It stores the snapshot as a `publication` row (the outbox) and tries to publish it right away, within a time limit.
- A lifespan worker and `lakeloom git retry` drain whatever is left, oldest first and one repository at a time.

**Tech Stack:** existing stack; the `git` CLI (TC-8: subprocess, already in the image); httpx for forge APIs; Gitea 1.27.3 from the dev stack for contract tests. No new Python runtime dependencies (`pyyaml` is already one; the manifest is emitted by a small deterministic writer so key order and quoting never depend on the YAML library).

**Spec:** `REQUIREMENTS.md`:
- FR-GIT-01, 02, 03, 04, 05, 09;
- FR-PRO-03 (c), provenance in Git trailers;
- NFR-SEC-11 (Git credentials), NFR-DEP-04 (working copy on `/data`), NFR-DEP-07 (job lease: single replica here, see Decisions), NFR-OPS-02 (Git push failure metric), NFR-OPS-04 (queued pushes survive shutdown), NFR-TST-02 (Gitea contract tests: "Git commit content and trailers");
- the M2 acceptance criterion "Git contains the commit with all trailers".

This is part 3 of 3 of M2 (M2a proposals and M2b review are done). Background: ADR 0011, ADR 0012, `docs/backlog/m2b-deferred.md`.

## Decisions taken in this plan (controller; owner may overrule at plan review)

- **What a commit contains (FR-GIT-02, M2c subset):**
  - `ontologies/<slug>/ontology.ttl`, `ontology.jsonld`, `ontology.owl`: from `ModelService.export` (`turtle`, `jsonld`, `rdfxml`), already byte-deterministic (`rdf/canonical.py`, FR-GIT-03);
  - `ontologies/<slug>/manifest.yaml`;
  - one appended line in `provenance/<slug>.jsonl`;
  - a regenerated root `README.md` index.

  `shapes.ttl`, `context.jsonld` and `CHANGELOG.md` belong to M3 (FR-PUB-04/05, FR-GIT-06 releases) and are not written yet. The manifest's `release` field is `null` until M3.
- **Snapshot at apply, render at commit.** At apply time the snapshot holds:
  - the three export bodies;
  - the manifest inputs;
  - the provenance record;
  - the commit message.

  At commit time the publisher writes the snapshot over the current tip of the target branch. It merges `source_tables` into the previous manifest, appends the provenance line unless that publication id is already in the file, and regenerates `README.md`. A rejected push (non-fast-forward) **never runs `git rebase`**: the publisher resets to the new remote tip and renders the commit again. OM is the source of truth, so generated files are overwritten and textual conflicts cannot arise.
- **Ordering:** the snapshot and the enqueue happen under a per-model in-process lock, so queue order equals OM state order. The worker publishes the publications of one repository strictly in `seq` order. A `blocked` head stops the queue of that repository and is reported, rather than letting a later snapshot be overtaken by an earlier one.
- **If the export fails at apply** (OM unreachable), the snapshot is stored with `exports: null` (message and provenance are still built at apply) and the worker exports at publish time. The approval itself never fails because of Git: Git problems become a warning on the review record and in the tool output.
- **Commit message (FR-GIT-04):**
  - subject: `ontology(<slug>): <kind> x<n>, …`, with operation kinds in first-appearance order, capped at 100 characters (a cut subject ends in `…`);
  - body: at most 50 lines `- <op_key>`, then `… and <n> more`;
  - then a blank line and the trailers in this order:
    - `Lakeloom-Proposal: <id>`;
    - `Lakeloom-ChangeSet: <id>`, one per APPLIED change set, in part order with glossary first;
    - `Approved-By: <om_user or reviewer_username or reviewer_subject>`, from the approving `ReviewRecord`;
    - `Lake-Source-Version: <fqn>@<version|unknown>`, one per provenance table;
    - `Lakeloom-Scan: <scan_id>` when set;
    - `Lakeloom-Proposed-By: <principal_subject> (<principal_type>)`;
    - `Lakeloom-AI-Generated: true|false`;
    - `Lakeloom-Publication: <publication id>`.

  Trailer values have CR, LF and other control characters replaced by spaces and are capped at 200 characters. Committer and author are the Lakeloom service identity (`LAKELOOM_GIT_AUTHOR_NAME` / `_EMAIL`); the human is in `Approved-By`.
- **Modes (FR-GIT-05):** `LAKELOOM_GIT_MODE` is the default. A model entry in the config file overrides it with `git: {mode: direct|pull_request}`.
  - `direct`: commit on `LAKELOOM_GIT_BRANCH` and push.
  - `pull_request`: branch `lakeloom/<slug>/<proposal-id>` from the remote tip of the target branch, one commit, push (`--force`: the branch is Lakeloom's own), then open a PR. An existing open PR for that head is reused.

  Each PR carries a full snapshot, so merging a later PR supersedes an earlier one; stacking PRs on each other is deferred (backlog). `pull_request` with `LAKELOOM_FORGE=none` is a startup `ConfigError`.
- **Empty remote:** when the target branch does not exist on the remote, the publisher first pushes an initial commit `chore: initialise ontology repository` with only `README.md`, straight to the branch, in both modes.
- **Idempotency:** before committing, the publisher searches the remote target branch (direct) or PR branch (pull_request) for `Lakeloom-Publication: <id>`. A hit marks the publication done with that commit; a retry never commits twice.
- **Credentials (FR-GIT-01, NFR-SEC-11):**
  - HTTPS: `LAKELOOM_GIT_TOKEN` (+ `_FILE`) and `LAKELOOM_GIT_USERNAME` (default `x-access-token`). They are passed through a `GIT_ASKPASS` helper script that reads them from the child process environment, never from argv or the remote URL.
  - SSH: `GIT_SSH_COMMAND` with `-i <LAKELOOM_GIT_SSH_KEY_FILE> -o IdentitiesOnly=yes -o BatchMode=yes`, plus `-o UserKnownHostsFile=<LAKELOOM_GIT_SSH_KNOWN_HOSTS_FILE> -o StrictHostKeyChecking=yes` when that file is set, else `StrictHostKeyChecking=accept-new` with a startup warning. SSH is not live-tested (dev Gitea has SSH disabled; backlog).
  - Every git child runs with `GIT_TERMINAL_PROMPT=0`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null` and `-c credential.helper=`.
  - User info in a remote URL is redacted in every log, error and tool output.
- **Forge auth:**
  - Gitea: HTTP basic with username and token, which also accepts the dev password;
  - GitHub: `Authorization: Bearer <token>`;
  - GitLab: `PRIVATE-TOKEN`.

  The API base comes from `LAKELOOM_FORGE_API_URL`, or is derived from the remote: `https://api.github.com` for github.com, `<scheme>://<host>/api/v4` for GitLab, `<scheme>://<host>[:port]/api/v1` for Gitea. Owner and repository come from the remote path. Only Gitea is live-tested; GitHub and GitLab are unit-tested against their documented request and response shapes (Task 1 research records the doc URLs).
- **Retry (FR-GIT-09):**
  - **Transient failures** stay `pending` with `attempts += 1` and `next_attempt_at = now + min(60·2^(attempts−1), 3600)` seconds. They are: a timeout, a transport error, a forge 5xx, a git `could not resolve host` / `connection` error, or a rejected push after 3 re-renders.
  - **Permanent failures** become `blocked` with `last_error`. They are: authentication refused (git `Authentication failed`, or HTTP 401/403), a repository that is not found, a forge 4xx other than "already exists", and a snapshot that cannot be rendered.

  The worker skips blocked rows. `lakeloom git retry` moves them back to `pending`. `last_error` is redacted and capped at 500 characters.
- **Job lease (NFR-DEP-07):** an in-process `asyncio.Lock` per repository, as for proposals; multi-replica leases stay deferred (already in the m2a backlog). This is documented in the ADR.
- **Inline attempt:** after `applied` is stored, approve awaits the publish for at most `LAKELOOM_GIT_PUBLISH_TIMEOUT_SECONDS` (default 20) via `asyncio.wait_for(asyncio.shield(task))`. On timeout the task keeps running and the tool says "queued".
- **Out of scope for M2c (backlog):**
  - releases and tags, CHANGELOG, SHACL, `context.jsonld`, import from Git, drift and `Lakeloom-Origin` (M3);
  - Git `Link` headers on `latest` artefacts (M3 serves released versions from Git);
  - auto-approve (FR-REV-04);
  - stacked PRs;
  - live SSH.

## Global Constraints

- **Earlier constraints still apply:**
  - Python ≥ 3.12; uv / ruff / pyright strict on `src/`.
  - Conventional Commits with requirement IDs; `git commit -s`; identity `Elcio Abrahao <elcio@tuta.com>`; trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  - Typed `LakeloomError`s with hints; untrusted text quoted in tool output.
  - Tools run through `guarded()` and take no URLs; every OM endpoint goes in `OMClient.PATHS`.
- **FR-GIT-03:** "the same ontology state MUST produce byte-identical files (stable prefix order, sorted subjects/predicates/objects, canonical blank-node labelling, LF line endings, UTF-8). Covered by property-based tests." This applies to the manifest, the provenance line and the README too.
- **FR-GIT-04:** "commit with message `ontology(<model>): <summary>` and trailers `Lakeloom-ChangeSet: <id>`, `Lakeloom-Proposal: <id>`, `Approved-By: <user>`, `Lake-Source-Version: <fqn>@<version>` (one per table)."
- **FR-GIT-09:** "Git operations are serialised per repository (lock) and retried on transient failures; a failed push MUST NOT lose the commit (it stays queued in the state store)."
- **NFR-SEC-11:** Git credentials come from env or `_FILE`, are never logged, never in argv, never in errors, audit rows or tool output.
- **Git is never allowed to fail or undo an approval:** OM has already applied it.
- **Dev-stack rules (owner):**
  - Never seal ontology releases. Write tests use throwaway glossaries, ontologies and Gitea repositories (deleted in teardown) and never modify `retail`.
  - Never touch other projects' containers (`supabase_*`); no prune commands; OrbStack, not Docker Desktop.
  - Every credential added to compose is a fixed public **DEV ONLY** default and is commented as such.
- **Git in tests:**
  - Unit tests use real `git` against local bare repositories (`file://` remotes under `tmp_path`), never the network.
  - Tests never read the developer's global git config: the runner's isolated environment guarantees it, and a test asserts it.

## Review Focus

1. **The push fails after the commit was made.** Gitea is down, or credentials are revoked, between the commit and the push. The approval still reports success. The publication stays `pending` (transient) or `blocked` (auth), and a later retry produces exactly one commit on the remote. → Task 5 tests `test_push_failure_keeps_publication_and_retry_commits_once` and `test_auth_failure_blocks_and_git_retry_requeues`.
2. **Someone else pushed to the branch.** Another writer moved `main` after our fetch. The push is rejected, so the publisher re-renders on the new tip: the other writer's unrelated files survive, our generated files win, and the provenance has one line per publication. → Task 5 `test_non_fast_forward_rerenders_on_new_tip`.
3. **Secrets leak.** A token appears in argv (`ps`), in the remote URL, in `last_error`, in logs, or in the tool output. → Task 2 `test_token_never_in_argv_or_errors`; Task 5 `test_last_error_redacted`.
4. **Two approvals of the same model at once, or a crash mid-publish.** Snapshots are queued in OM order and published in that order. A process killed after the push but before the state update finds its commit by `Lakeloom-Publication` and does not commit again. → Task 5 `test_crash_after_push_is_detected_by_trailer`; Task 6 `test_snapshots_enqueued_in_apply_order`.
5. **Pull-request mode edge cases:** an empty remote, a PR that already exists for the head, and `forge=none`. → Task 4 `test_existing_pr_is_reused`; Task 5 `test_empty_remote_gets_initial_commit_then_pr`; Task 6 `test_pull_request_with_forge_none_is_config_error`.

## File structure

```
src/lakeloom/gitstore/__init__.py
src/lakeloom/gitstore/git.py          # GitRunner: isolated subprocess env, credentials, timeouts, redaction
src/lakeloom/gitstore/repo.py         # WorkingCopy: clone/fetch/reset, write, commit, push, find-by-trailer
src/lakeloom/gitstore/layout.py       # Snapshot model; files, manifest, provenance line, README, message
src/lakeloom/gitstore/forge.py        # Forge protocol + Gitea/GitHub/GitLab PR clients
src/lakeloom/gitstore/publisher.py    # GitPublisher: enqueue, process queue, retry/backoff, modes
src/lakeloom/gitstore/snapshot.py     # take_snapshot / fill_exports from ModelService (Task 6)
src/lakeloom/state/publications.py    # PublicationRepository (outbox)
src/lakeloom/state/migrations/versions/0003_publications.py
src/lakeloom/config.py, core/model_policy.py, core/review.py   # settings, per-model git mode, hook (modify)
src/lakeloom/server/http_app.py, tools_review.py, tools_proposals.py, server_info.py, cli.py (modify)
deploy/compose/docker-compose.yml, deploy/docker/Dockerfile (openssh-client) (modify)
docs/decisions/0013-git-publishing.md
```

---

### Task 1: Research, settings and dev-stack Git credentials

**Files:**
- Create `docs/decisions/0013-git-publishing.md` (status proposed: the Decisions above plus § Research) and `tests/contract/test_gitea_remote.py`.
- Modify `src/lakeloom/config.py`, `tests/unit/test_config.py`, `deploy/compose/docker-compose.yml`, `deploy/docker/Dockerfile`, `.env.example`, the `REQUIREMENTS.md` §10 config table.

**Interfaces:**
- **Produces, Settings (all optional):**
  - `git_username: str = "x-access-token"`;
  - `git_ssh_known_hosts_file: Path | None = None`;
  - `git_author_name: str = "Lakeloom"`, `git_author_email: str = "lakeloom@localhost.invalid"`;
  - `git_publish_timeout_seconds: float = Field(20, ge=0, le=300)`;
  - `git_retry_interval_seconds: int = Field(60, ge=5, le=3600)`;
  - `forge_api_url: AnyHttpUrl | None = None`.

  `git_token` is already in `SECRET_FILE_FIELDS`. Blank values of `git_remote`, `git_token` and `forge_api_url` count as unset (extend `_blank_is_unset`).
- **Produces, compose (Lakeloom service, DEV ONLY comments):**
  - `LAKELOOM_GIT_USERNAME: lakeloom-admin`, `LAKELOOM_GIT_TOKEN: lakeloom-admin` (the Gitea admin password, which Gitea accepts for basic auth);
  - `LAKELOOM_GIT_MODE: direct`;
  - `LAKELOOM_FORGE_API_URL: http://gitea:3000/api/v1`;
  - `depends_on: gitea-init: condition: service_completed_successfully`.

  Dockerfile: install `openssh-client` next to `git`.
- **Steps:**
- [ ] **Step 1: Live research (Gitea 1.27.3 in the dev stack; record in ADR 0013 § Research).** Using a throwaway repo created via `POST /api/v1/user/repos` and deleted after:
  - Does `git push` over HTTP with basic auth through `GIT_ASKPASS` work for `lakeloom-admin`?
  - What does a push to a missing branch of an empty repo do?
  - What does a rejected non-fast-forward push print (stderr text to classify)?
  - What does a wrong password print?
  - `POST /repos/{owner}/{repo}/pulls` `{title, body, head, base}`: status code and the fields `number` and `html_url`. What does a duplicate PR for the same head return (status and body)?
  - `GET /repos/{owner}/{repo}/pulls?state=open`: can it be filtered by head, or must it be listed and filtered?

  For GitHub and GitLab, record the documented create and list endpoints, the status codes for duplicates (GitHub 422 "A pull request already exists"; GitLab 409), and the URL fields (`html_url` / `web_url`) with doc links. Where they differ from this plan, the research wins; note it in the ADR.
- [ ] **Step 2: Config unit tests:**
  - `test_git_settings_defaults`;
  - `test_git_token_file`;
  - `test_blank_git_remote_is_unset`;
  - `test_git_remote_not_in_repr` (exists? extend).
- [ ] **Step 3: Contract** `test_gitea_remote.py`: `test_push_with_askpass_credentials` and `test_open_pull_request` (raw `git` plus httpx, throwaway repo, deleted in a `finally`). The token is passed only through the environment.
- [ ] **Step 4:** FAIL → implement → pass. Recreate the stack, `/readyz` green.
- [ ] **Step 5: Commit** `feat(config): Git publishing settings and dev-stack Gitea credentials (FR-GIT-01, NFR-SEC-11)`.

---

### Task 2: Git runner and working copy

**Files:**
- Create `src/lakeloom/gitstore/__init__.py`, `gitstore/git.py`, `gitstore/repo.py`.
- Test `tests/unit/gitstore/test_git.py`, `tests/unit/gitstore/test_repo.py` (fixture: a bare repo under `tmp_path` as `file://` remote, plus a helper that pushes a foreign commit through a second clone).

**Interfaces:**
- **Consumes:** Settings (Task 1).
- **Produces (`git.py`):**
  - `class GitError(Exception)` with `kind: Literal["auth", "not_found", "rejected", "conflict", "network", "timeout", "failed"]` and `message: str` (redacted stderr, at most 500 characters). Classification uses the Task 1 stderr research; anything unknown is `failed`.
  - `redact(text: str) -> str` removes URL user info and the token value.
  - `class GitRunner(settings: Settings, workdir: Path, *, timeout: float = 60.0)`:
    - `async run(*args: str, cwd: Path | None = None, input: bytes | None = None) -> str` runs `git -c credential.helper= -c core.autocrlf=false -c user.name=… -c user.email=… <args>` with the isolated env from Decisions.
    - It writes the askpass script `<workdir>/askpass.sh` (mode 0700) once. The script prints `$LAKELOOM_GIT_ASKPASS_USER` for prompts containing "Username" and `$LAKELOOM_GIT_ASKPASS_PASS` otherwise. These two variables are set only in the child env, and only when a token is configured.
    - On timeout it kills the process (`proc.kill()` then `wait()`) and raises `GitError("timeout")`. A non-zero exit raises a classified `GitError`.
- **Produces (`repo.py`):**
  - `class WorkingCopy(runner: GitRunner, path: Path, remote: str)`:
    - `async ensure() -> None`: clone when missing, or set `origin` to `remote` when it changed; a missing or corrupt `.git` → delete and clone again.
    - `async remote_head(branch: str) -> str | None`: `ls-remote --heads`; `None` for a missing or empty branch.
    - `async checkout_tip(branch: str) -> None`: `fetch origin <branch>`, `checkout -B <local> origin/<branch>`, `reset --hard`, `clean -fdx`.
    - `async start_branch(name: str, from_branch: str) -> None`.
    - `async init_branch(branch: str, files: Mapping[str, bytes], message: str) -> str`: orphan initial commit, returns its sha.
    - `write(files: Mapping[str, bytes]) -> None` (paths relative, `..` refused); `read(path: str) -> bytes | None`.
    - `async commit(message: str) -> str` (`add -A`; nothing staged → `GitError("failed", "nothing to commit")`).
    - `async push(local: str, remote_branch: str, *, force: bool = False) -> None`.
    - `async find_trailer(ref: str, key: str, value: str) -> str | None`: `log <ref> --format=%H%x00%(trailers:key=<key>,valueonly)` and an exact match of the value.
- [ ] **Step 1: Tests:**
  - `test_env_is_isolated` (`GIT_CONFIG_GLOBAL=/dev/null`, no `HOME` config read: a fake `~/.gitconfig` with `user.name=WRONG` is ignored);
  - `test_token_never_in_argv_or_errors` (fake `git` on `PATH` records argv and env; a failing push's `GitError` text has no token);
  - `test_askpass_answers_username_and_password`;
  - `test_ssh_command_with_known_hosts` and `test_ssh_command_accept_new_without_known_hosts`;
  - `test_timeout_kills_process`;
  - `test_classify_stderr` (parametrized over the recorded texts);
  - `test_clone_commit_push_roundtrip`;
  - `test_remote_head_none_for_empty_repo`;
  - `test_init_branch_on_empty_remote`;
  - `test_push_rejected_when_remote_moved` (kind `rejected`);
  - `test_find_trailer_exact_match` (`p1` does not match `p10`);
  - `test_write_refuses_parent_paths`;
  - `test_ensure_recovers_corrupt_copy`;
  - `test_lf_and_bytes_preserved` (`core.autocrlf=false`).
- [ ] **Step 2:** FAIL → implement → pass; pyright strict.
- [ ] **Step 3: Commit** `feat(gitstore): isolated git runner and working copy (FR-GIT-01, FR-GIT-09, NFR-SEC-11)`.

---

### Task 3: Repository layout, manifest, provenance and commit message

**Files:**
- Create `src/lakeloom/gitstore/layout.py`.
- Test `tests/unit/gitstore/test_layout.py` (hypothesis).

**Interfaces:**
- **Consumes:** `Proposal`, `ReviewRecord`, `provenance_block` and `op_key` (core); `ModelInfo`.
- **Produces:**
  - `class Snapshot(BaseModel)`:
    - `model: str`, `glossary_id: str`, `glossary_fqn: str`, `base_iri: str | None`, `prefixes: dict[str, str]`;
    - `om_version: str | None`, `model_version: str`;
    - `exports: dict[Literal["ontology.ttl", "ontology.jsonld", "ontology.owl"], str] | None` (UTF-8 text; `None` when the export at apply failed, together with `om_version` and `model_version`);
    - `last_change_set: str | None`, `proposal_id: str`;
    - `source_tables: list[TableProvenance]`;
    - `provenance: dict[str, Any]`;
    - `message: str`.
  - `build_message(p: Proposal, approval: ReviewRecord, publication_id: str) -> str` (exact format in Decisions).
  - `provenance_record(p: Proposal, approval: ReviewRecord, publication_id: str) -> dict[str, Any]`, with keys:
    - `publication_id`, `proposal_id`, `model`;
    - `approved_by`, `om_user`, `reviewer_subject`, `applied_at` (`approval.at` ISO);
    - `change_sets` (APPLIED ids, part order);
    - `parts` (`[provenance_block(p, part) …]`).
  - `render(snapshot: Snapshot, publication_id: str, *, previous_manifest: bytes | None, previous_provenance: bytes | None, manifests: Mapping[str, bytes]) -> dict[str, bytes]` returns the full file map for the commit. `manifests` holds the other models' manifests, for the README.
    - **`manifest.yaml`**: keys in this fixed order: `model, glossary_id, glossary_fqn, base_iri, prefixes (sorted), om_version, model_version, last_change_set, last_proposal, release: null, source_tables (merged by fqn with the previous manifest, sorted by fqn)`. Scalar style, LF, UTF-8, trailing newline.
    - **`provenance/<slug>.jsonl`**: previous content plus one `json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))` line. The line is not appended when a line with that `publication_id` already exists.
    - **`README.md`**: `# Ontologies`, a generated-by note, then a table `| Model | Base IRI | Last change set | Release |` with one row per manifest, sorted by slug.
  - `sanitize_trailer(value: str) -> str`.
- [ ] **Step 1: Tests:**
  - `test_message_has_all_trailers_in_order`, checked with real `git interpret-trailers --parse`: every trailer from Decisions, with one `Lakeloom-ChangeSet` per applied set and one `Lake-Source-Version` per table;
  - `test_unknown_table_version_is_unknown`;
  - `test_subject_capped_with_ellipsis`;
  - `test_body_caps_op_lines`;
  - `test_trailer_values_strip_newlines` (an agent client name `"x\nApproved-By: mallory"` cannot inject a trailer);
  - `test_approved_by_prefers_om_user`;
  - `test_manifest_deterministic` (hypothesis: random prefixes, tables and order → identical bytes for equal sets);
  - `test_manifest_merges_source_tables`;
  - `test_provenance_line_appended_once`;
  - `test_readme_lists_models_sorted`;
  - `test_render_is_lf_utf8`.
- [ ] **Step 2:** FAIL → implement → pass.
- [ ] **Step 3: Commit** `feat(gitstore): deterministic repository layout and commit trailers (FR-GIT-02, FR-GIT-03, FR-GIT-04, FR-PRO-03)`.

---

### Task 4: Forge pull-request clients

**Files:**
- Create `src/lakeloom/gitstore/forge.py`.
- Test `tests/unit/gitstore/test_forge.py` (respx; payloads from Task 1 research).

**Interfaces:**
- **Consumes:** Settings; `GitError` kinds (reused for classification); `redact`.
- **Produces:**
  - `class PullRequest(BaseModel)`: `number: int`, `url: str`.
  - `class Forge(Protocol)`: `async open_pull_request(*, head: str, base: str, title: str, body: str) -> PullRequest`. It reuses an open PR with the same head on a duplicate answer, or lists open PRs first when Task 1 showed that duplicates are accepted silently.
  - `forge_for(settings: Settings, http: httpx.AsyncClient) -> Forge | None` returns `None` for `forge=none` and raises `ConfigError` when owner and repository cannot be parsed from `git_remote`.
  - `GiteaForge`, `GitHubForge`, `GitLabForge`, with the auth and API bases from Decisions. Errors: 401/403 → `GitError("auth")`; 404 → `not_found`; other 4xx → `failed`; 5xx or transport → `network`.
- [ ] **Step 1: Tests:**
  - `test_parse_owner_repo` (https, `.git` suffix, ssh `git@host:owner/repo.git`, GitLab subgroups);
  - `test_api_base_derivation`;
  - `test_gitea_create_pr_basic_auth`;
  - `test_github_bearer_and_html_url`;
  - `test_gitlab_private_token_and_web_url` (project path URL-encoded);
  - `test_existing_pr_is_reused` (each forge's duplicate answer);
  - `test_error_classification`;
  - `test_token_not_in_errors`.
- [ ] **Step 2:** FAIL → implement → pass.
- [ ] **Step 3: Commit** `feat(gitstore): pull requests on Gitea, GitHub and GitLab (FR-GIT-05)`.

---

### Task 5: Publication outbox and publisher

**Files:**
- Create `src/lakeloom/state/publications.py`, `state/migrations/versions/0003_publications.py`, `src/lakeloom/gitstore/publisher.py`.
- Modify `src/lakeloom/metrics.py`.
- Test `tests/unit/state/test_publications.py`, `tests/unit/gitstore/test_publisher.py` (local bare remote; a fake forge recording calls).

**Interfaces:**
- **Consumes:** Tasks 2, 3 and 4; `new_ulid_id`.
- **Produces:**
  - Table `publications`:
    - `seq` (integer primary key, autoincrement), `id` (`pub_` + ULID, unique);
    - `repo` (`sha256(remote)` hex, 64 characters), `model`, `proposal_id` (unique);
    - `mode`, `status` (`pending | done | blocked`), `attempts`, `next_attempt_at`, `last_error`;
    - `commit_sha`, `branch`, `pr_url`;
    - `created_at`, `updated_at`;
    - `payload` (JSON: the `Snapshot`).

    Index `(repo, status, seq)`. A migration test runs `0002 → 0003 → downgrade`.
  - `class Publication(BaseModel)` mirrors the row.
  - `PublicationRepository`:
    - `add(pub) -> Publication` (a duplicate `proposal_id` → returns the existing one);
    - `head(repo) -> Publication | None` (lowest seq not done);
    - `update(pub)`;
    - `for_proposal(id)`;
    - `list(status=None, limit=50)`;
    - `requeue_blocked(repo) -> int`;
    - `counts(repo) -> dict[str, int]`.
  - `class GitPublisher(settings, repo: PublicationRepository, working_copy: WorkingCopy, forge: Forge | None, *, clock)`:
    - `enabled: bool` (`git_remote` is set);
    - `async enqueue(pub: Publication) -> Publication`;
    - `async process(*, now=None) -> list[Publication]` publishes the head while it is due and not blocked, under the repository lock. Per publication:
      1. `ensure`;
      2. an empty remote → `init_branch`;
      3. direct: `checkout_tip`; pull_request: `start_branch`, after checking whether the PR branch already exists on the remote (it is reused when it has the trailer);
      4. `find_trailer` → done;
      5. `exports is None` → `exporter(snapshot)`, an injected `Callable[[Snapshot], Awaitable[Snapshot]]` that fills `exports`, `om_version` and `model_version` (Task 6 passes one built on `ModelService`); a failure is transient;
      6. `render` with the previous files read from the working copy;
      7. `write`, `commit`, `push`; on `rejected`, re-render up to 3 times;
      8. pull_request: `open_pull_request(title=subject, body=body + trailers)`;
      9. `done` with `commit_sha`, `branch` and `pr_url`.

      Failures follow Decisions § Retry; nothing propagates out of `process`.
    - `async retry_blocked() -> int`.
    - `status() -> dict[str, Any]` (`enabled, mode, pending, blocked, last_error`) for `server_info`.
    - Metrics `lakeloom_git_publications_total{result="done|retry|blocked"}` and `lakeloom_git_push_failures_total`.
- [ ] **Step 1: Tests:**
  - `test_direct_publish_commits_files_and_trailers` (clone the bare remote and assert files and `git interpret-trailers`);
  - `test_pull_request_mode_pushes_branch_and_opens_pr`;
  - `test_empty_remote_gets_initial_commit_then_pr`;
  - `test_non_fast_forward_rerenders_on_new_tip` (a foreign commit adding `other.txt` stays; one provenance line);
  - `test_push_failure_keeps_publication_and_retry_commits_once` (remote made unreachable, then restored);
  - `test_auth_failure_blocks_and_git_retry_requeues`;
  - `test_crash_after_push_is_detected_by_trailer` (push succeeds, `update` raises once; the next `process` marks it done with no second commit);
  - `test_fifo_blocked_head_stops_queue`;
  - `test_backoff_schedule`;
  - `test_missing_exports_filled_at_publish`;
  - `test_last_error_redacted`;
  - `test_duplicate_proposal_enqueue_returns_existing`;
  - `test_disabled_when_no_remote`.
- [ ] **Step 2:** FAIL → implement → pass; migration upgrade on SQLite in the unit suite and on Postgres in contract (existing `db-upgrade` path).
- [ ] **Step 3: Commit** `feat(gitstore): queued Git publications with retry and per-repository ordering (FR-GIT-04, FR-GIT-05, FR-GIT-09)`.

---

### Task 6: Publish on apply — review hook, wiring, tools and CLI

**Files:**
- Modify:
  - `src/lakeloom/core/review.py`, `core/model_policy.py` (`git: {mode}`);
  - `server/http_app.py` (AppDeps `publisher`, lifespan worker, startup validation), `server/tools_review.py`, `server/tools_proposals.py` (`get_proposal` shows the publication), `server/server_info.py`;
  - `cli.py` (`lakeloom git status`, `lakeloom git retry`).
- Create `src/lakeloom/gitstore/snapshot.py`: `take_snapshot(models: ModelService, om: OMClient, p: Proposal, approval: ReviewRecord, publication_id: str) -> Snapshot` and `fill_exports(models, om, snapshot) -> Snapshot` (the publisher's exporter).
- Test `tests/unit/core/test_review_git.py`, `tests/unit/server/test_tools_review.py` (extend), `tests/unit/test_cli.py` (extend), `tests/unit/server/test_http_app.py` (extend).

**Interfaces:**
- **Consumes:** `GitPublisher` and `PublicationRepository` (Task 5), `Snapshot` and `build_message` (Task 3), `ModelService.export` and `version`, `OMClient.get_capabilities` (bot client, reads only).
- **Produces:**
  - `ModelPolicyEntry.git: GitPolicy | None` with `mode: Literal["direct", "pull_request"]`; `git_mode_for(policies, model, default) -> str`.
  - `ReviewService(..., publisher: GitPublisher | None = None)`. After `applied` is saved:
    1. under the per-model publish lock, `take_snapshot` (an export failure leaves `exports=None` plus a warning), then `enqueue`;
    2. the inline `process` with the timeout from Decisions;
    3. the outcome is appended to the approving record's warnings only when it is not `done`: `Git: queued (<reason>); it is retried automatically` or `Git: blocked (<redacted error>); an operator must run lakeloom git retry`.

    Never raises. Then a best-effort store of the updated record (shielded).
  - **Review tool and `get_proposal` text**:
    - a line `Git: committed <sha7> to <branch>`, or `Git: pull request <url>`, or `Git: queued` / `blocked`;
    - structured content gains `publication: {id, status, commit, branch, pr_url}` (null when Git is off).
  - **`server_info.features.git`** = `publisher.status()` without the remote URL.
  - **Lifespan**: when `publisher.enabled`, a task loops `process()` every `git_retry_interval_seconds` and exits on cancel (rows stay queued, NFR-OPS-04). `build_deps` raises `ConfigError` for `pull_request` (default or any model) with `forge=none`, and for a remote that cannot be parsed when a forge is needed.
  - **CLI**: `lakeloom git status` lists pending and blocked publications (id, model, proposal, attempts, last error); `lakeloom git retry` requeues blocked rows and runs `process()` once.
- [ ] **Step 1: Tests:**
  - `test_approve_enqueues_snapshot_and_publishes_inline`;
  - `test_git_failure_never_fails_approval` (publisher raises → proposal `applied`, warning);
  - `test_export_failure_enqueues_snapshot_without_exports`;
  - `test_inline_timeout_leaves_task_running`;
  - `test_snapshots_enqueued_in_apply_order` (two concurrent approves of one model);
  - `test_reject_and_request_changes_do_not_publish`;
  - `test_apply_failed_does_not_publish`;
  - `test_per_model_git_mode_override`;
  - `test_pull_request_with_forge_none_is_config_error`;
  - `test_review_tool_shows_git_line`;
  - `test_get_proposal_shows_publication`;
  - `test_server_info_git_status_has_no_remote_url`;
  - `test_cli_git_status_and_retry`;
  - `test_lifespan_worker_cancelled_cleanly`.
- [ ] **Step 2:** FAIL → implement → pass; full unit suite, ruff, pyright.
- [ ] **Step 3: Commit** `feat(review): publish applied proposals to Git (FR-GIT-04, FR-GIT-05, FR-PRO-03)`.

---

### Task 7: M2c acceptance (live) and wrap-up

**Files:**
- Create `tests/contract/test_m2c_acceptance.py` and `tests/contract/test_git_publisher_gitea.py`.
- Modify `deploy/compose/docker-compose.yml`, `deploy/compose/lakeloom-sources.yaml` (model `retail_m2c_pr` with `git: {mode: pull_request}`).
- Modify the docs: ADR 0013 (accepted; consequences), `docs/backlog/m2b-deferred.md` (close the Git item), create `docs/backlog/m2c-deferred.md`; README (Git publishing section, `lakeloom git` commands), CHANGELOG.

- [ ] **Step 1: `test_git_publisher_gitea.py`** runs in-process: `GitPublisher` against a throwaway Gitea repo (created and deleted via the API) with synthetic snapshots:
  - `test_direct_mode_on_gitea` (an empty repo gets the initial commit, then a commit with the trailers);
  - `test_pull_request_mode_on_gitea` (the PR exists with the head branch and `html_url`);
  - `test_reused_pr_on_gitea`;
  - `test_wrong_token_blocks_on_gitea`.
- [ ] **Step 2: `test_m2c_acceptance.py`** runs through MCP over HTTP against the running stack (`direct` on `lakeloom-admin/ontologies`). It uses a throwaway model `retail_m2c` (glossary and ontology hard-deleted in teardown, never sealed). Its `ontologies/retail_m2c/` folder stays in the dev repo's history (dev only; noted in the README).
  - `test_m2c_alice_approval_is_committed_with_all_trailers`. The flow is agent scan → `create_model` → `propose_from_scan` → submit → alice approves. The review output shows `Git: committed`. A clone of the Gitea repo has a commit whose `Lakeloom-Proposal` is that id, with:
    - `Approved-By: alice`;
    - one `Lakeloom-ChangeSet` per applied set, equal to OM's APPLIED change sets;
    - one `Lake-Source-Version: <fqn>@<version>` per scanned table, equal to the scan versions.

    The commit holds `ontologies/retail_m2c/ontology.ttl`, which parses with rdflib and contains the new concepts, plus `manifest.yaml` and a `provenance/retail_m2c.jsonl` line.
  - `test_m2c_export_is_deterministic`: two exports of the unchanged model equal the committed `ontology.ttl` byte for byte.
  - `test_m2c_pull_request_mode`: model `retail_m2c_pr`, approve, then the PR exists in Gitea. Teardown closes the PR and deletes its branch through the API.
- [ ] **Step 3:** Recreate the stack from scratch → full unit and contract suite green locally. Push the branch only when the owner asks.
- [ ] **Step 4:** Update the ADR, the backlogs, the README and the CHANGELOG; the REQUIREMENTS config table already lists the new settings (Task 1).
- [ ] **Step 5: Commit** `test(contract): M2c acceptance — applied proposals committed to Git with trailers (FR-GIT-04, FR-GIT-05)`.

---

## Out of scope for M2c

- Releases, tags, CHANGELOG, SHACL shapes, `context.jsonld`, `import_from_git`, reconciliation and drift (M3, M4).
- Git `Link` headers on published artefacts (M3).
- Auto-approve (FR-REV-04) and its `auto_approved` Git flag.
- Multi-replica job leases (in-process lock only, as for proposals).
- Stacked pull requests, live SSH tests, signed commits.
