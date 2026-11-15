# 0013. Git publishing on apply

- Status: accepted (M2c complete)
- Date: 2026-11-13
- Requirements: FR-GIT-01, FR-GIT-02, FR-GIT-03, FR-GIT-04, FR-GIT-05, FR-GIT-09, FR-PRO-03, NFR-SEC-11, NFR-DEP-04, NFR-DEP-07, NFR-OPS-02, NFR-OPS-04

## Context

M2c makes every applied proposal one Git commit in the ontology repository. The plan is
`docs/superpowers/plans/2026-11-13-m2c-git-publishing.md`; this ADR records its decisions
and, in § Research, what was observed live against the dev Gitea (1.27.3, git 2.54 on the
host) on 2026-11-13. Where research contradicts the plan, research wins and the deviation is
listed under "Deviations from the plan".

## Decision

- **Commit content (FR-GIT-02, M2c subset).** `ontologies/<slug>/ontology.{ttl,jsonld,owl}`
  from `ModelService.export` (byte-deterministic, FR-GIT-03), `ontologies/<slug>/manifest.yaml`
  (`release: null` until M3), one appended line in `provenance/<slug>.jsonl`, and a
  regenerated root `README.md`. `shapes.ttl`, `context.jsonld` and `CHANGELOG.md` are M3.
- **Snapshot at apply, render at commit.** Approve stores a snapshot (exports, manifest
  inputs, provenance record, commit message) as a `publication` row (the outbox). The
  publisher writes it over the current tip of the target branch, merges `source_tables` into
  the previous manifest, appends the provenance line unless the publication id is already
  present, and regenerates `README.md`. A rejected push never triggers `git rebase`: reset to
  the new remote tip and render again (OM is the source of truth).
- **Ordering.** Enqueue plus snapshot run under a per-model in-process lock. The row is
  inserted first, with a snapshot that needs no OpenMetadata read (`exports: null`); the
  exports, model version and OM version are then stored on it while no worker has attempted
  it. So an applied proposal is never left without a row unless the insert itself fails, and
  then the warning says `Git: not queued (internal error); an operator must re-publish it`. The worker
  publishes one repository's rows strictly in `seq` order; a `blocked` head stops that
  repository's queue and is reported.
- **Export failure at apply.** The snapshot is stored with `exports: null`; the worker
  exports at publish time. Approval never fails because of Git: problems become a warning on
  the review record and in the tool output.
- **Commit message (FR-GIT-04).** Subject `ontology(<slug>): <kind> x<n>, ...` (max 100
  chars, a cut subject ends in an ellipsis); body at most 50 `- <op_key>` lines then
  `... and <n> more`; trailers in order: `Lakeloom-Proposal`, `Lakeloom-ChangeSet` (one per
  APPLIED change set), `Approved-By`, `Lake-Source-Version: <fqn>@<version|unknown>` (one per
  provenance table), `Lakeloom-Scan` (when set), `Lakeloom-Proposed-By`,
  `Lakeloom-AI-Generated`, `Lakeloom-Publication`. Trailer values: control characters become
  spaces, capped at 200 characters. Author and committer are the Lakeloom service identity;
  the human is in `Approved-By`.
- **Modes (FR-GIT-05).** `LAKELOOM_GIT_MODE` is the default; a model entry may override it
  with `git: {mode: direct|pull_request}`. `direct` commits on `LAKELOOM_GIT_BRANCH` and
  pushes. `pull_request` pushes `lakeloom/<slug>/<proposal-id>` from the remote tip
  (`--force`, the branch is Lakeloom's own) and opens a PR; an existing open PR for that head
  is reused. Every PR branch starts from the target tip and rewrites `README.md` and
  `ontologies/<slug>/manifest.yaml` and appends to `provenance/<slug>.jsonl`, so two open
  Lakeloom PRs conflict on those files (on `README.md` even across models): merge or close one
  before the next approval, or resolve the conflict keeping both provenance lines. Re-rendering
  open PR branches and stacked PRs are deferred. `pull_request` with `LAKELOOM_FORGE=none` is a
  startup `ConfigError`.
- **Empty remote.** If the target branch is missing, first push `chore: initialise ontology
  repository` (only `README.md`) straight to the branch, in both modes.
- **Idempotency.** Before committing, search the remote target branch (both modes) and the PR
  branch (pull_request) for `Lakeloom-Publication: <id>`; a hit marks the publication done (a
  PR merged and its branch deleted before the row was recorded is found on the target).
- **Credentials (FR-GIT-01, NFR-SEC-11).** HTTPS: `LAKELOOM_GIT_TOKEN` (+ `_FILE`) and
  `LAKELOOM_GIT_USERNAME` (default `x-access-token`) through a `GIT_ASKPASS` helper reading
  the child environment, never argv or the URL. SSH: `GIT_SSH_COMMAND` with `-i <key> -o
  IdentitiesOnly=yes -o BatchMode=yes`, plus `UserKnownHostsFile` and
  `StrictHostKeyChecking=yes` when `LAKELOOM_GIT_SSH_KNOWN_HOSTS_FILE` is set, else
  `accept-new` with a startup warning (not live-tested: dev Gitea has SSH off). Every git child
  runs with `GIT_TERMINAL_PROMPT=0`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`,
  `-c credential.helper=`. A password or token in the user info of `LAKELOOM_GIT_REMOTE` is a
  startup `ConfigError` (it would reach argv and `.git/config`); a user name alone is allowed.
  Remote-URL user info is redacted in logs, errors and tool output.
- **Forge auth.** Gitea: HTTP basic (username and token). GitHub: `Authorization: Bearer`.
  GitLab: `PRIVATE-TOKEN`. API base from `LAKELOOM_FORGE_API_URL`, else derived from the
  remote (`https://api.github.com`; `<scheme>://<host>/api/v4`; `<scheme>://<host>[:port]/api/v1`).
  Only Gitea is live-tested.
- **Retry (FR-GIT-09).** Transient (stay `pending`, `attempts += 1`, `next_attempt_at = now +
  min(60*2^(attempts-1), 3600)` s): timeout, transport error, forge 5xx, git `could not
  resolve host` or connection errors, a push rejected after 3 re-renders, and see "Deviations"
  for 404 right after a push. Permanent (`blocked` with `last_error`): authentication refused,
  repository not found, other forge 4xx except "already exists" and except a 404 on the pull
  request create right after a push (retried, then transient: Deviations 3), unrenderable
  snapshot.
  `lakeloom git retry` moves blocked rows back to `pending`; `last_error` is redacted and
  capped at 500 characters.
- **Job lease (NFR-DEP-07).** An in-process `asyncio.Lock` per repository, single replica;
  multi-replica leases stay deferred (m2a backlog).
- **Inline attempt.** After `applied` is stored, approve awaits the publish for at most
  `LAKELOOM_GIT_PUBLISH_TIMEOUT_SECONDS` (default 20) via `asyncio.wait_for(asyncio.shield(task))`;
  on timeout the task continues and the tool says "queued".
- **Settings added in Task 1.** `git_username`, `git_ssh_known_hosts_file`, `git_author_name`,
  `git_author_email`, `git_publish_timeout_seconds` (0..300), `git_retry_interval_seconds`
  (5..3600), `forge_api_url`; blank `git_remote`, `git_token`, `forge_api_url` count as unset.
- **Dev stack.** The Lakeloom service gets `LAKELOOM_GIT_USERNAME`/`LAKELOOM_GIT_TOKEN`
  (the fixed public Gitea admin credentials, DEV ONLY), `LAKELOOM_GIT_MODE=direct`,
  `LAKELOOM_FORGE_API_URL=http://gitea:3000/api/v1`, and waits for `gitea-init`. The image
  now ships `openssh-client` next to `git`.
- **Out of scope (backlog).** Releases/tags, CHANGELOG, SHACL, `context.jsonld`, import from
  Git, drift and `Lakeloom-Origin`, Git `Link` headers (M3); auto-approve (FR-REV-04);
  stacked PRs; live SSH.

## Research

Observed live on 2026-11-13: Gitea 1.27.3 at `http://localhost:3000`, git 2.54.0 on the host,
throwaway repositories created with `POST /api/v1/user/repos` and deleted afterwards.
Contract tests: `tests/contract/test_gitea_remote.py`.

### Gitea 1.27.3 (live)

- **Push with `GIT_ASKPASS`.** Works for `lakeloom-admin` with the password as secret, over
  HTTP basic. The helper answers `Username*` prompts with the user and everything else with the
  password, both from the child environment.
- **Empty repository.** `git ls-remote <url>` exits 0 with empty output.
  `git fetch <url> main` exits 128: `fatal: couldn't find remote ref main`. The same text
  appears for a missing branch of a non-empty repo (`... nonexist`). Pushing `main` to an empty
  repo succeeds (`* [new branch] main -> main`), so the initial commit is a normal push.
- **Wrong password** (exit 128), stderr:
  ```
  remote: Failed to authenticate user
  fatal: Authentication failed for 'http://localhost:3000/lakeloom-admin/<repo>.git/'
  ```
  The password is not echoed. The API answers 401 `{"message":"invalid username, password or
  token"}`.
- **Non-fast-forward push** (exit 1), stderr:
  ```
  To http://localhost:3000/lakeloom-admin/<repo>.git
   ! [rejected]        main -> main (fetch first)
  error: failed to push some refs to 'http://localhost:3000/lakeloom-admin/<repo>.git'
  hint: Updates were rejected because the remote contains work that you do not
  hint: have locally. ...
  ```
  Classify on `[rejected]` plus `(fetch first)` (also expect `(non-fast-forward)` from other
  servers). `git push --force` prints `+ <old>...<new> main -> main (forced update)`.
- **Missing repository** (exit 128): `remote: Repository not found` then `fatal: repository
  '<url>/' not found`. Classify: permanent.
- **Create PR** `POST /api/v1/repos/{owner}/{repo}/pulls` with `{title, body, head, base}`:
  201, body fields `number` (int), `html_url` (`http://localhost:3000/<owner>/<repo>/pulls/1`),
  `url` (same), `state: "open"`, `head.ref`, `base.ref`.
- **Duplicate PR for the same head:** 409, `{"message":"pull request already exists for these
  targets [id: 4, issue_id: 1, head_repo_id: 10, base_repo_id: 10, head_branch: lk/x,
  base_branch: main]", ...}`.
- **Other PR errors:** missing head branch 404 `ref "nobranch" does not exist`; head equal to
  base 422 `Invalid PullRequest: There are no changes between the head and the base`; unknown
  repo 404 `not found`; bad credentials 401.
- **List** `GET /repos/{owner}/{repo}/pulls?state=open`: 200, a JSON array. The `head` query
  parameter is **ignored** (tested with `br-a`, `owner:br-a` and a nonexistent value: all
  returned both open PRs). List and filter client-side on `head.ref` (and `head.label`).
- **Race after push (not in the plan).** Right after `git push` returns, the first
  `POST .../pulls` may answer 404 `{"message":"not found"}` for roughly 0.1 to 1 s; 6 trial
  repos gave 404x3 then 201, 201, 404 then 201, 404 then 201, 201, 201. The same request then
  succeeds with no change. A 404 on a repo that was just pushed to is therefore not proof that
  the repository is missing.

### GitHub (documented, not live-tested)

Docs: <https://docs.github.com/en/rest/pulls/pulls>.

- Create: `POST /repos/{owner}/{repo}/pulls`, body `head` and `base` required, `title`
  required unless `issue` is given, optional `body`. Success 201; response has `number` and
  `html_url`.
- List: `GET /repos/{owner}/{repo}/pulls`, query `state`, `head` (format `user:ref-name` or
  `organization:ref-name`), `base`; 200, each item has `number` and `html_url`.
- Duplicate: the docs list only "422 Validation failed, or the endpoint has been spammed";
  the message text is not documented. The plan's expectation (422 "A pull request already
  exists for <owner>:<branch>") is known GitHub behaviour but unverified here, so treat any 422
  as "look up the existing PR by listing with `head=<owner>:<branch>`" before failing.
- Auth: `Authorization: Bearer <token>` (plus `Accept: application/vnd.github+json`).

### GitLab (documented, not live-tested)

Docs: <https://docs.gitlab.com/api/merge_requests/>.

- Create: `POST /projects/:id/merge_requests` (`:id` numeric or URL-encoded `owner%2Frepo`),
  required `source_branch`, `target_branch`, `title`; optional `description`. The docs page
  shows an example response and no explicit status code (201 is the GitLab convention); the
  response has `iid` and `web_url`.
- List: `GET /projects/:id/merge_requests` with `source_branch`, `target_branch` and `state`
  (`opened`, `closed`, `locked`, `merged`, `all`; default `all`) filters; 200; items have `iid`
  and `web_url`. So GitLab, unlike Gitea, can filter by head server-side.
- Duplicate: the 409 expected by the plan ("Another open merge request already exists for this
  source branch") is **not stated** on the docs page; handle 409 by listing
  `?source_branch=<b>&state=opened` and reusing the hit.
- Auth: `PRIVATE-TOKEN: <token>`.

### Deviations from the plan

1. **Gitea duplicate PR is 409, not 422** (GitHub's duplicate is 422 per the plan). The forge
   client treats "already exists" per forge (Gitea 409 with message `pull request already
   exists`, GitHub 422, GitLab 409) by looking up the existing PR, never as a failure.
2. **Gitea ignores the `head` filter on list**: list open PRs and filter on `head.ref`
   client-side (Gitea only); GitHub and GitLab filter server-side.
3. **404 right after a push is transient on Gitea.** The plan classifies every forge 4xx
   other than "already exists" as permanent. Instead, after a successful push the PR create
   retries a 404 a few times (the test uses 20 x 0.25 s; 5 s with 0.5 s steps is enough)
   before giving up, and a 404 that persists after a successful push should leave the
   publication `pending` (transient), not `blocked`.
4. **Empty remote needs no special fetch.** `fetch main` on an empty repo fails with
   `couldn't find remote ref`, and `ls-remote` is empty with exit 0: detect "branch missing"
   with `ls-remote --heads <url> <branch>` (empty output) rather than parsing fetch errors.
5. GitHub's duplicate message and GitLab's 409 are not in their docs pages (see above); the
   code should not depend on the message text for those two.

## Consequences

What shipped in M2c:

- Every proposal a human approves and applies is one commit: the three deterministic exports,
  `manifest.yaml`, a `provenance/<slug>.jsonl` line and the root `README.md`, with the trailers
  above. Verified live (`tests/contract/test_m2c_acceptance.py`): alice's approval in the dev
  stack is committed to `lakeloom-admin/ontologies` with `Approved-By: alice`, one
  `Lakeloom-ChangeSet` per APPLIED change set and one `Lake-Source-Version` per scanned table;
  two later exports equal the committed `ontology.ttl` byte for byte; a model with
  `git: {mode: pull_request}` gets a pull request.
- The in-process publisher is exercised against throwaway Gitea repositories
  (`tests/contract/test_git_publisher_gitea.py`): the initial commit of an empty repository,
  direct commits, pull requests, reuse of an open pull request after a retry, and a wrong
  token that blocks the queue with a redacted error.
- The outbox survives restarts; `lakeloom git status|retry` and `get_server_info.git` show
  and unblock it. A Git failure never fails or undoes an approval.

Found by the live tests:

- **Model-scoped draft ids.** Draft ids of new terms and native members were derived from the
  operation alone, so a second model proposing the same concept collided with the first
  model's ids (OpenMetadata ids are global primary keys; the apply failed as "the name is
  already taken"). The ids now include the model slug.
- **Gitea links.** Gitea 1.27 builds API links (`html_url`) from the caller's `Host` header,
  so pull requests opened by the container named `http://gitea:3000/...`. The dev stack sets
  `GITEA__server__PUBLIC_URL_DETECTION=legacy` so links use `ROOT_URL`. A production forge
  behind a proxy needs the same care.
- Right after the first push into an empty Gitea repository the commits API may answer 409
  ("repository is empty") briefly; only the tests read it, and they retry.

Known limits:

- Single replica: the queue lock is in-process; `lakeloom git retry` must not run against the
  same working copy as a busy server.
- Open Lakeloom pull requests conflict with each other: each branch starts from the target
  tip and rewrites `README.md` and `manifest.yaml` and appends to `provenance/<slug>.jsonl`
  (`README.md` conflicts even across models). Merge or close one before the next approval, or
  resolve keeping both provenance lines; open PR branches are not re-rendered when the target
  moves, and there are no stacked PRs. GitHub and GitLab are not live-tested; SSH is not live-tested.
- Throwaway models of the dev contract tests leave their direct commits in the dev
  repository's history (dev only).
- Releases, tags, `CHANGELOG.md`, SHACL shapes, `context.jsonld`, import from Git, drift and
  Git `Link` headers remain M3/M4. The follow-ups are in `docs/backlog/m2c-deferred.md`.

*M3b note (ADR 0015):* a release (`create_release`) is always committed **directly** on
`LAKELOOM_GIT_BRANCH`, even for a model in `pull_request` mode. It is an explicit
`lakeloom:publish` act, and its tag must point at a commit on the target branch. The release
commit shares this queue's repository lock. Open Lakeloom pull requests of the model then
conflict on `manifest.yaml` (and `README.md`); merge or close them before releasing.
