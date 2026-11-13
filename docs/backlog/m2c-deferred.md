# M2c deferred follow-ups

Findings from the M2c work on Git publishing, deliberately deferred. Triage when planning M3.
Decisions and live research: [ADR 0013](../decisions/0013-git-publishing.md).

## Out of scope for M2c
- Releases, tags, `CHANGELOG.md`, SHACL shapes, `context.jsonld`, import from Git,
  reconciliation and drift (M3, M4).
- Git `Link` headers on published artefacts (M3).
- Auto-approve (FR-REV-04) and its `auto_approved` Git flag.
- Multi-replica job leases: the queue lock is in-process only (as for proposals, see
  m2a-deferred). `lakeloom git retry` can collide with a running server on the same working
  copy: document it better or add a file lock.
- Stacked pull requests, live SSH tests, signed commits; GitHub and GitLab are not live-tested.

## Git runner and working copy
- `init_branch` and `checkout_tip` can act on a parent repository when called before
  `ensure` (set `GIT_CEILING_DIRECTORIES` or assert a valid working copy).
- `ensure` removes any non-repository path, including the runner's own work directory (the
  cached `GIT_ASKPASS` helper is then never rewritten).
- SSH isolation is partial: no `-F /dev/null`, and `BatchMode` only when a key is configured.
- The `.git` path refusal is case-sensitive (`.GIT` on case-insensitive file systems).
- git's HTTP `returned error: 404` is classified as a network error (transient) instead of
  not found.
- The timeout test's "grandchild is dead" check may flake where zombies are not reaped (CI
  containers).
- The redaction list of known secrets is process-global and never cleared, so tests may mask
  a redaction regression.

## Repository layout and commit message
- The root `README.md` drops a model whose manifest fails to parse (it should keep a row).
- A snapshot whose exports miss a file name raises `KeyError` instead of `ValueError` (it is
  retried instead of blocked).
- Provenance dedupe checks the publication id argument but appends the snapshot's own record:
  if the two disagree, lines repeat.
- The 200-character trailer cap can cut the `@version` of a long `Lake-Source-Version`; the
  sanitiser can leave a trailing space.
- Format characters (bidi overrides, zero-width) are not stripped from trailers; no tests for
  U+2028, U+2029 and U+0085, JSON serialisability of the provenance record, or the operation
  key cap.
- `release` is always written `null`: M3 must carry the released version over.
- `validate_slug` uses `re.match` with `$`, which accepts a trailing newline (pre-existing).

## Forges
- `forge_api_url` is normalised with a trailing slash for a bare host; URL joins must stay
  careful.
- A listed pull request with an unexpected body can raise `KeyError` from the matching code.
- The 404-after-push retry budget counts sleep time only, not request time.
- A GitHub 422 that is not a duplicate is reported as "duplicate but no open PR".
- A bad port in the remote URL is a `ValueError`, not a `ConfigError`.
- `GitHubForge` subclasses `GiteaForge`; function-level imports; test gaps (a bad create body,
  the 404 retry on other forges, a zero budget).
- Listing stops at a short page, assuming the server honours the page size (a lowered Gitea
  `MAX_RESPONSE_ITEMS` would miss a PR silently); the 20-page bound and GitHub/GitLab paging
  are untested.

## Queue and retry
- Pull-request idempotency searches only the PR branch: if the PR is merged and its branch
  deleted before the publication is recorded, the retry blocks on "nothing to commit" or opens
  a second PR (also search the target branch, or treat the publication id in the provenance as
  done).
- A corrupt payload makes the queue head fail on every tick without ever being blocked, and
  `status()` propagates the error.
- A naive `now=` datetime raises `TypeError` out of `process()`.
- `requeue_blocked` keeps `last_error` and `updated_at`.
- No pull-request-mode crash-after-push test; a changed remote URL orphans existing rows.

## Review integration
- When the enqueue itself failed, the internal-error warning still promises a retry (it should
  say the proposal is unpublished).
- Background publish tasks started inline are not drained at shutdown before the Git HTTP
  client and the engine are closed.
- `fill_exports` loads the model once per format (plus the version), so a snapshot can mix two
  OpenMetadata states.
- The inline wait holds the proposal lock for up to the publish timeout; `git status` is not
  filtered by repository.
- A caller's cancel can be swallowed without `uncancel` when the publish task finishes in the
  same loop iteration; on MCP (anyio) a cancelled approve is still audited as "cancelled" at the
  publication lookup, contrary to the docstring.

## Dev stack and tests
- The Gitea throwaway-repository helper of `test_gitea_remote.py` ignores the `DELETE` status
  (a leak would be silent).
- The acceptance test's throwaway models have fixed names (`retail_m2c`, `retail_m2c_pr`:
  the PR model needs a config entry); their direct commits stay in the dev repository history.
- "Recreate the stack from scratch" was done by rebuilding the Lakeloom container, not with
  fresh volumes (the owner's dev OpenMetadata data must survive); CI runs from scratch.
