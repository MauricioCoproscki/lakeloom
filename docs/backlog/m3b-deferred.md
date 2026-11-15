# M3b deferred follow-ups

Findings from the M3b work on releases, versioned reads and the triple-store sink,
deliberately deferred. Triage when planning M3c. Decisions, live research and known limits:
[ADR 0015](../decisions/0015-releases.md).

## Out of scope for M3b
- `import_model`, `import_from_git`, drift detection and the SPARQL proxy (M3c).
- Pre-release and build-metadata versions (`1.0.0-rc.1`, `1.0.0+build`). Only strict
  `MAJOR.MINOR.PATCH` is accepted today.
- Signed tags (and signed release commits).
- Releases through pull requests: a release always commits directly on the target branch,
  even for a model in `pull_request` mode. Open Lakeloom PRs of the model then conflict on
  `manifest.yaml` and `README.md`.
- Asset context and `lakeloom://models` pinned to a release (both are `latest` only).
- Multi-replica leases for the release path and the sink worker: the repository lock and the
  sink queue are in-process (single replica, as in M2c). With several instances, a re-render
  after a rejected push reuses the snapshot exported before the first attempt.
- Retention of the release reader cache:
  - file bodies are kept LRU by count (128 entries) and not by size, so 128 large ontology
    files can use a lot of memory;
  - the per-slug fetch stamps (`_fetched`) are never dropped.

  Bound the cache by bytes, add a TTL, and make both configurable.

## Triple-store sink
- Run the Graph Store sink against a real Fuseki (`rdf` compose profile). The live contract
  test `tests/contract/test_triplestore_fuseki.py` is skipped because the profile is not
  started in the dev stack, so PUTs against a real server are unverified.
- Register `base64(user:password)` and the URL-encoded password for redaction as well, and
  add a test for an `Authorization` header echoed back in an error body.
- Re-check `sink_next_attempt_at` inside `_attempt_locked`: a stale due-list can use up an
  attempt early.
- The `latest` graph stays stale when the highest release's sink row ends `failed`. Push
  `latest` from the highest *successful* release, or say so in `get_server_info`.
- `create_release` reports `triplestore: queued` even when the inline attempt already
  finished (`done` or `failed`).

## Releases and CHANGELOG
- Leave vocabulary terms (SKOS, OWL, the OpenMetadata ontology namespace) out of the
  release diff. The first release's term list and later diffs now include `skos:Concept`,
  `om:Glossary` and similar terms the export declares, which makes the CHANGELOG noisy.
- A first release reuses the `CHANGELOG.md` at the branch tip. When a model's tags were
  deleted by hand (or a slug was deleted and re-created), the old sections stay below the
  new one. Decide whether a first release starts a fresh file.
- The audit row of `create_release` stores the tag in `change_set_id`. Give it its own
  column, or a generic `subject` column.
- The `already_done` path does not compare the arguments bound in the token with those of the
  retry (the token check covers them only on the first push).
- `plan()` runs `ls-remote` after taking the lock, and `create()` calls `_ensure_branch`
  before verifying the token. Reorder both so a bad token never touches the remote.
- `core/releases.py` is over 700 lines. Split planning, the Git path and the idempotent path.
- The manifest's `last_proposal` is `""` instead of `null` when there is none. Changing it
  alters FR-GIT-03 bytes, so do it once with a migration note.
- Manifests gained `released_at: null`: the first publication after the upgrade changes every
  touched `manifest.yaml`.
- `release_layout`:
  - drop the unused `slug` parameter of `_old_sections`;
  - match duplicate sections on the whole `## [x]` heading instead of `split("]")`;
  - render an empty label as a placeholder instead of an empty code span;
  - strengthen the determinism test.

## Reading releases and links
- Strip the query string and fragment from `LAKELOOM_GIT_WEB_URL`: today they are kept in
  `Link`, and a token put there by mistake would leak.
- The reader's `not_found` echoes the untruncated slug, and its hint is a literal
  `{slug}` placeholder.
- `diff_releases` accepts `from_version=latest` (it compares latest with itself, or with a
  later release). Reject it, or define it.
- Quote the plan warnings in the dry-run Markdown.
- Release artefacts are immutable: give them `Cache-Control: immutable` (and a long
  `max-age`).
- `ReleaseReader`:
  - `refresh` may stamp `_fetched` after a concurrent `invalidate()`;
  - a non-`GitError` failure in `refresh` is not throttled;
  - `show()` decodes bytes as UTF-8 and encodes them again, which would alter a non-UTF-8
    blob;
  - `_converted` lists twice;
  - the `_lock` alias in the publisher is still there.
- Gitea's single-tag API (`GET /repos/{owner}/{repo}/tags/{tag}`) can show the old commit of
  a tag that was deleted and re-created (live, Gitea 1.27.3). Lakeloom reads tags through Git
  only. Keep it that way if a forge-API path is ever added.

## Tests
- Hold the publication queue in a live test, so that the refusal of a release while a
  publication is `pending` is covered against the stack (unit-tested only today).
- `test_release_diff.py` imports `_graphs` through `sys.path.insert`. Move it to a shared
  helper.
- Cover the 50-line cap in `diff_markdown`, and `old=None` with `reason` and
  `annotation_only`. Drop the stray isomorphism call in
  `test_first_release_lists_all_terms_added`.
- Scope test: add the read + propose (no publish) case for `create_release`.
