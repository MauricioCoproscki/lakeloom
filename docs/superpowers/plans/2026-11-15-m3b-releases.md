# M3b — Releases, Versioned Reads and the Triple-Store Sink Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `create_release` turns the current state of a model into an immutable, tagged
release in Git, with:
- a computed semantic version;
- a generated `CHANGELOG.md` and the manifest `release` field.

Releases are then readable everywhere `latest` is:
- HTTP `/versions` and `?version=`;
- MCP resources and `export_model`;
- every artefact carries Git `Link` headers.

`list_releases` and `diff_releases` show the history. An optional sink pushes each release into
a SPARQL 1.1 Graph Store.

**Architecture:**
- **Pure core:**
  - `core/release_diff.py` compares two ontology graphs and derives the semver bump;
  - `gitstore/release_layout.py` renders the release files (manifest, CHANGELOG, README) and the commit/tag messages.
- **`core/releases.py` (`ReleaseService`)** plans a release (dry run with a confirm token) and runs it. It works under the **same per-repository lock** as the M2c publisher, on the same working copy: one release commit straight on the target branch, an annotated tag, and one atomic push.
- **`gitstore/release_reader.py`** reads tags and tagged files from the working copy (fetch under the lock, immutable-object reads cached). The artefact builders use it for every `version` other than `latest`.
- **`publish/triplestore.py`** is the optional Graph Store sink. A `releases` state row tracks its retries, and the existing background worker drives them.

**Tech Stack:** existing stack (rdflib incl. `rdflib.compare`, SQLAlchemy + Alembic, httpx, FastAPI, MCP SDK, Typer). No new runtime dependency.

**Spec:** `REQUIREMENTS.md`:
- FR-GIT-02 (`CHANGELOG.md`, manifest `release`), FR-GIT-06 (tag, manifest, CHANGELOG, bump rules), FR-GIT-09 (lock, retry);
- FR-PUB-01 (`?version=` on resources), FR-PUB-02 (`/versions`, `/versions/{version}`, released versions from Git), FR-PUB-07 (`Link` to the Git commit or tag);
- FR-IO-01 (export of "current or a release");
- FR-TS-01..03, §10 `LAKELOOM_TRIPLESTORE_ENABLED`, Q1 ("optional module, disabled by default, built last in M3");
- NFR-SEC-06/07 (`lakeloom:publish`, per-model publish groups), NFR-SEC-11, NFR-SEC-12 (audit);
- §7 tools `create_release`, `list_releases`, `diff_releases`, `export_model`;
- M3 acceptance criterion: "Release `v1.0.0` produces tag and artefacts".

Earlier decisions it builds on:
- ADR 0010 (validators, Link);
- ADR 0013 (Git publishing, lock, retry classes);
- ADR 0014 (shapes, context).

Deferred items from `docs/backlog/m3a-deferred.md` are triaged in Task 7; only "read a Git
snapshot's exports and the model version from one graph state" is fixed here (Task 3), because
release commits rely on it.

## Decisions taken in this plan (controller; owner may overrule at plan review)

- **Git only, never OpenMetadata releases.** OM 2.1-rc1 has `ontologyReleases` that are
  *sealed* (a write); Lakeloom never seals and never calls those endpoints. A Lakeloom release
  is a Git tag over OM's current state.
- **Version and bump (FR-GIT-06).**
  - **Version syntax:** strict `MAJOR.MINOR.PATCH` (no pre-release or build metadata in M3b), regex `^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$`.
  - **Tag:** `<slug>/v<version>`, annotated.
  - **First release:** the default version is `1.0.0`.
  - **Later releases** diff the previous release's `ontology.ttl` (read from its tag) against the current Turtle export (`rdflib.compare.graph_diff` of `to_isomorphic` graphs):
    - *Term*: a URIRef subject typed `owl:Class`, `skos:Concept`, `owl:DatatypeProperty` or `owl:ObjectProperty`.
    - *Annotation predicates*: `skos:prefLabel`, `altLabel`, `hiddenLabel`, `definition`, `scopeNote`, `note`, `example`, `rdfs:label`, `rdfs:comment`, `dcterms:description`.
    - **major**: a term is removed (an IRI change shows up as a removal plus an addition), or a triple whose predicate is not an annotation predicate is removed (for example a range, a `broader` or a restriction).
    - **minor**: otherwise, a term or a non-annotation triple is added.
    - **patch**: otherwise, only annotation triples differ.
    - **nothing**: the graphs are isomorphic. Then `validation_error` "nothing changed since `<prev>`", hint "approve a proposal first".
  - **Override:** `bump` (`major|minor|patch`) or an explicit `version`, never both. The result must be strictly greater than the highest existing release, else `validation_error`. A bump lower than the computed one is allowed, and the preview shows the warning "computed <x>, releasing as <y>".
- **Who may release:**
  - scope `lakeloom:publish`;
  - `ModelAccess.publish_groups` (new, NFR-SEC-07; empty means no restriction), so `AccessAction` gains `"publish"`;
  - agents may release (the spec makes only review human-only). `Released-By` is the principal's username, else the subject.
- **Two-step confirm (FR-AGT-03):**
  - Without `confirm_token`, `create_release` is a dry run. It returns the computed version, the bump and its reason, the CHANGELOG section and a token.
  - The token's state digest is `sha256(model_version + "\n" + (previous tag object sha or "none"))`, so a model change or a concurrent release invalidates it.
- **Preconditions, all `validation_error` with a hint:**
  - Git enabled;
  - no `pending` or `blocked` publication of this model in this repository ("approved changes are not in Git yet; wait or run `lakeloom git retry`");
  - the tag does not exist on the remote yet (exception: idempotency, below).
- **Release commit.** It is always committed **directly on `LAKELOOM_GIT_BRANCH`**, in both Git modes: a release is an explicit `lakeloom:publish` act, and a tag must point at a commit on the target branch. Open Lakeloom PRs of the model then conflict on the manifest (documented in the ADR). The commit is rendered over the current tip:
  - the five exports, freshly exported from one graph state;
  - `manifest.yaml` with `release: "<version>"` plus a new `released_at: "<ISO-8601 UTC, seconds>"`, other fields as in M2c, `last_proposal` and the source tables kept from the previous manifest;
  - `CHANGELOG.md` with the new section prepended;
  - the regenerated `README.md`, whose Release column now shows the version.
  - Nothing is appended to `provenance/<slug>.jsonl`.
  - **Subject:** `release(<slug>): v<version>`. **Body:** one line `Bump: <bump> (<reason>)`.
  - **Trailers, in order:** `Lakeloom-Release: <slug>/v<version>`, `Released-By: <user>`, `Lakeloom-Model-Version: <model_version>`, `Lakeloom-Release-Id: <rel_…>`.
  - **Author and committer:** the service identity, as in M2c.
- **Tag and push:**
  - `git tag -a <tag> -F -`, whose message is `<slug> v<version>`, a blank line, then the CHANGELOG section.
  - One `git push --atomic origin refs/heads/<branch> refs/tags/<tag>`.
  - **A rejected push:** delete the local tag, reset to the new tip and render again, at most 3 times (as in M2c), then `upstream_unavailable` "the branch kept moving; retry".
  - **Other failures:** auth → `forbidden`, not found → `not_found`, network → `upstream_unavailable`. They are reported to the caller; nothing is queued, the caller retries. A retry is safe because of idempotency.
- **Idempotency:**
  - **Before rendering:** the remote tag already exists → if its commit carries `Lakeloom-Release-Id: <this id>`, the release is complete (the earlier push succeeded) and the row is marked done; otherwise it is the `validation_error` "release `<tag>` already exists".
  - **The release id:** it is derived from the confirm token, `rel_` + the first 26 hex characters of `sha256(token)`, so retrying the same confirmed call is idempotent.
- **CHANGELOG (FR-GIT-02, "generated from proposal summaries").** The file is `ontologies/<slug>/CHANGELOG.md`. It is regenerated from scratch at every release as `# Changelog — <slug>`, a line about Keep a Changelog and semver, then the sections newest first. The old sections are kept by parsing the previous file at `## [` headings; an unparsable file is replaced with a single new section and a warning in the result. Section layout:
  ```
  ## [1.1.0] - 2026-11-15

  Released by alice. Bump: minor (2 terms added).

  ### Added
  - `Customer.email` — <iri>
  ### Changed
  - `Customer` — <iri>
  ### Removed
  - …

  ### Proposals
  - ontology(retail): add_attribute x2 — prop_01J…, approved by alice (`1a2b3c4`)
  ```
  - **Added, Changed and Removed** list terms (label, else local name) from the diff. *Changed* means any triple of a kept term differs. Each group is capped at 50 lines plus `… and <n> more`, and empty groups are omitted. On the first release the only group is *Added*, headed `Initial release (<n> terms)`.
  - **Proposals** are the commits since the previous tag (or since the root) that touch `ontologies/<slug>/` and carry `Lakeloom-Proposal`, oldest first. Each line gives the subject, the `Lakeloom-Proposal` and `Approved-By` trailers and the short sha.
  - **Text from OM or agents** is sanitised (control characters become spaces, backticks are removed, at most 200 characters) and quoted in code spans.
- **Release record (state store, migration `0004_releases`).** Table `releases`, columns:
  - `id` (pk `rel_…`), `repo`, `model`, `version`, `tag`, `commit_sha`, `released_by`, `created_at`;
  - `status` (`done`), and the sink state: `sink_status` (`none|pending|done|failed`), `sink_attempts`, `sink_next_attempt_at`, `sink_last_error` (500 characters).
  - Unique on (`repo`, `tag`).
  - **Git is the source of truth for listing.** The table is the audit trail and the sink queue: a tag made outside Lakeloom is still listed and served.
- **Reading releases (FR-PUB-02, FR-PUB-01, FR-IO-01):**
  - **Listing.** `ReleaseReader.list(slug)` runs `git fetch origin --force "+refs/tags/<slug>/v*:refs/tags/<slug>/v*"` under the repository lock, at most once every 30 s per process. Then `for-each-ref refs/tags/<slug>/` with the tag object sha, commit sha, tagger date and subject.
  - **Which tags count.** Only tags that match the version regex and point at a commit that holds `ontologies/<slug>/ontology.ttl`.
  - **Order.** Semver descending.
  - **File reads.** `git show <tag>:ontologies/<slug>/<file>` (tags are immutable), in an LRU of 128 entries keyed by (tag object sha, file).
  - **Formats.** `turtle`, `jsonld` and `rdfxml` are the stored bytes. `ntriples` and `skos` are `rdf/canonical.serialize(parse(ontology.ttl), fmt)`. Shapes in JSON-LD or N-Triples are converted from the stored `shapes.ttl` the same way.
  - **Tags created before M3a** may lack `shapes.ttl` or `context.jsonld` → `not_found` "release <v> has no shapes".
  - **Unknown version** → `not_found` "no release <v> of <slug>", hint `GET /v1/models/{slug}/versions`. Git disabled → the same `not_found`, hint "Git publishing is off".
- **Validators and Links (FR-PUB-07, replaces ADR 0010 § Link for Git):**
  - **A release artefact:** the ETag is the body hash, and `Last-Modified` is the tagger date. `Link: <tag web URL>; rel="via"` and `<commit web URL>; rel="via"`, both when a web base is known.
  - **A `latest` artefact** keeps the OM `via` link and adds `<commit web URL of the model's last done publication>; rel="related"; title="last Git publication"` when one exists.
  - **Web base:** `LAKELOOM_GIT_WEB_URL` when set. Otherwise it is derived from `LAKELOOM_GIT_REMOTE`:
    - `http(s)` URLs lose their user info and a trailing `.git`;
    - `ssh://[user@]host[:port]/path` and scp-like `user@host:path` become `https://host/path` minus `.git`;
    - with `LAKELOOM_FORGE=none` and no override there is no web base, so there are no Git links.
  - **Paths:**
    - GitHub: `/tree/<tag>`, `/commit/<sha>`;
    - Gitea: `/src/tag/<tag>`, `/commit/<sha>`;
    - GitLab: `/-/tree/<tag>`, `/-/commit/<sha>`.

    The tag is percent-encoded with `/` kept.
  - `/versions` (JSON) lists `latest` first, then the releases: `{version, tag, commit, released_at, media_types, links}`.
- **Surfaces:**
  - **HTTP:** `GET /v1/models/{slug}/versions`, `/versions/{version}`, and `?version=` (default `latest`) on `/models/{slug}`, `/shapes` and `/context.jsonld`.
  - **MCP resources:** `?version=` on `ontology.*`, `shapes.ttl` and `context.jsonld`. `lakeloom://models` and asset context stay `latest`-only (asset context against a release is deferred).
  - **Tools:** `export_model(model, format, version="latest")`. New tools:
    - `create_release(model, version?, bump?, confirm_token?)` (`lakeloom:publish`, destructive annotation, audited);
    - `list_releases(model)` (`lakeloom:read`);
    - `diff_releases(model, from_version, to_version="latest")` (`lakeloom:read`). It returns the structured `ReleaseDiff`, a Markdown summary and the bump it implies.
  - **CLI:** `lakeloom release create <model> [--version|--bump] [--yes]` (the dry run is printed, `--yes` confirms), `lakeloom release list <model>` and `lakeloom release sink-retry`.
  - **`get_server_info`:** gains `features.releases` and a `triplestore` block (enabled, pending, failed).
- **Triple-store sink (FR-TS, optional, disabled by default):**
  - **Settings:**
    - `LAKELOOM_TRIPLESTORE_ENABLED` (`false`);
    - `LAKELOOM_TRIPLESTORE_URL`: the Graph Store endpoint, e.g. `http://fuseki:3030/lakeloom/data`. Required when enabled, else `ConfigError`. Operator-only (NFR-SEC-13);
    - `LAKELOOM_TRIPLESTORE_USERNAME`;
    - `LAKELOOM_TRIPLESTORE_PASSWORD` / `_FILE` (secret, never logged);
    - `LAKELOOM_TRIPLESTORE_SHAPES` (`true`).
  - **Per model:** `triplestore: {enabled: bool = true, shapes: bool | null = null}` in the `models:` policy entry.
  - **On a done release with the sink enabled for the model:** `sink_status = pending`, then one inline attempt in a background task. The release tool returns without waiting and reports "triple store: queued". It runs these PUTs (Graph Store HTTP Protocol: `PUT {url}?graph=<iri>`, `Content-Type: text/turtle`, HTTP basic when a username is set):
    1. the release's `ontology.ttl` to `<{base-iri}release/{version}>`;
    2. the same body to `<{base-iri}latest>`, only when this version is the highest release;
    3. `shapes.ttl` to `<{base-iri}shapes>`, when shapes are enabled and the file exists.

    The graph IRIs follow the spec literally (string concatenation with the base IRI). A model without a base IRI is `failed` "model has no base IRI".
  - **Outcomes:**
    - **Any 2xx** is success.
    - **Transient** (stays `pending`, backoff as `backoff_seconds`): 408, 429, 5xx and transport errors.
    - **Permanent** (`failed`): 401, 403, 404, other 4xx, and 10 attempts.
  - The sink never blocks or undoes the Git release.
  - The Git worker loop of `http_app` also calls `SinkWorker.process()`.
  - `lakeloom release sink-retry` moves `failed` rows back to `pending`.

## Global Constraints

- **Earlier constraints still apply:**
  - Python ≥ 3.12; uv / ruff / pyright strict on `src/`.
  - Conventional Commits with requirement IDs; `git commit -s`; identity `Elcio Abrahao <elcio@tuta.com>`; trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  - Typed `LakeloomError`s with hints; untrusted text quoted in tool output.
  - Tools run through `guarded()` and take no URLs; every OM endpoint is in `OMClient.PATHS`.
  - Code default and unit tests on SQLite; the compose stack and CI on Postgres. The migration must run on both.
- **FR-GIT-03:** "the same ontology state MUST produce byte-identical files". This covers the release manifest, `CHANGELOG.md` (given the same inputs and clock) and README; it is tested property-based where inputs vary.
- **FR-GIT-09:** "Git operations are serialised per repository (lock)". Releases, tag fetches and the publisher share `gitstore.publisher._lock(repo_key)` (made public as `repo_lock`).
- **FR-TS-03:** "failures retried and reported, never blocking the Git release."
- **NFR-SEC-11:** the triple-store password and the Git token never appear in logs, errors, tool output or `Link` headers (web URLs carry no user info).
- **Dev-stack rules (owner):**
  - Throwaway models only (`retail_m3b`, hard-deleted in teardown); never modify `retail`; never seal OM releases.
  - Approve only as alice; no tokens printed or committed.
  - No `down -v`, no prune, never touch `supabase_*`.
  - Tags made by tests are deleted through the Gitea API in teardown.

## Review Focus

1. **Two releases racing** (two confirmed calls for the same model, or a release racing an approval's publication). Expected: the repository lock serialises them. The second either finds its tag already pushed (same id → done) or fails with "already exists" or "nothing changed", and never produces two tags on one commit or a tag on an unpushed commit. → Task 4 `test_concurrent_releases_serialised`, `test_release_waits_for_publication_lock`.
2. **The first release of a model whose Git history has no proposals** (for example a model created and released at once), and **a release after a model's tags were made by hand.** Expected: a CHANGELOG with "Initial release" and an empty Proposals list; hand-made tags count for ordering, and non-semver tags are ignored. → Task 2 `test_changelog_first_release_without_proposals`; Task 3 `test_list_ignores_non_semver_and_foreign_tags`.
3. **A version string that is hostile or near-valid** (`1.0`, `01.0.0`, `1.0.0-rc1`, `../x`, very long, `v1.0.0`) in a tool argument, an HTTP path or `?version=`. Expected: `validation_error` (tool) or `not_found` (read), never a git argument injection, and never a path outside `ontologies/<slug>/`. → Task 1 `test_parse_version_rejects`; Task 3 `test_reader_rejects_unsafe_version`.
4. **A push that fails after the tag was created locally** (network, then a retry). Expected: the local tag is deleted, the release row is not marked done, and a retry with the same token creates exactly one tag. → Task 4 `test_push_failure_leaves_no_local_tag_and_retry_is_idempotent`.
5. **A triple store that is down or wrong for a long time.** Expected: the Git release returns success, the row is retried with backoff and is `failed` after 10 attempts, `server_info` reports it, and the password never appears in `sink_last_error`. → Task 6 `test_sink_down_never_blocks_release`, `test_sink_error_redacts_password`.

## File structure

```
src/lakeloom/core/release_diff.py        # parse_version, ReleaseDiff, diff_graphs, bump_for, next_version, diff_markdown
src/lakeloom/gitstore/release_layout.py  # changelog_section, render_changelog, render_release, release_message, tag_message
src/lakeloom/gitstore/repo.py            # + tags, show, tag_annotated, delete_tag, push_atomic, log_since (modify)
src/lakeloom/gitstore/release_reader.py  # ReleaseReader: list, file, artefact bytes per media
src/lakeloom/gitstore/links.py           # web_base, tag_url, commit_url
src/lakeloom/gitstore/layout.py, snapshot.py, publisher.py   # manifest release fields, one graph state, repo_lock (modify)
src/lakeloom/state/releases.py           # ReleaseRow, Release, ReleaseRepository
src/lakeloom/state/migrations/versions/0004_releases.py
src/lakeloom/core/releases.py            # ReleaseService: plan, create
src/lakeloom/core/model_policy.py        # publish_groups, triplestore policy (modify)
src/lakeloom/publish/artifacts.py        # version resolution + Git links (modify)
src/lakeloom/publish/triplestore.py      # TripleStoreSink, GraphStoreSink, SinkWorker
src/lakeloom/server/tools_release.py     # create_release, list_releases, diff_releases
src/lakeloom/server/http_api.py, resources.py, tools_semantic.py, server_info.py, http_app.py, cli.py, config.py  (modify)
docs/decisions/0015-releases.md
```

---

### Task 1: Semantic diff and version rules

**Files:**
- Create `src/lakeloom/core/release_diff.py`, `tests/unit/core/test_release_diff.py`.
- Create `docs/decisions/0015-releases.md` (status proposed; Decisions above).

**Interfaces:**
- **Consumes:** `rdflib.compare.to_isomorphic` and `graph_diff`; the graph helpers in `tests/unit/publish/_graphs.py`.
- **Produces:**
  - `Version = tuple[int, int, int]`;
  - `parse_version(text: str) -> Version` (raises `LakeloomError("validation_error")`);
  - `format_version(v: Version) -> str`;
  - `Bump = Literal["major", "minor", "patch"]`;
  - `class TermChange(BaseModel): iri: str; label: str; change: Literal["added","changed","removed"]`;
  - `class ReleaseDiff(BaseModel): from_version: str | None; to_version: str; added: list[TermChange]; changed: list[TermChange]; removed: list[TermChange]; removed_triples: int; added_triples: int; annotation_only: bool; bump: Bump | None; reason: str`;
  - `diff_graphs(old: Graph | None, new: Graph, *, from_version: str | None, to_version: str) -> ReleaseDiff` (`old=None` means a first release: everything is added, `bump=None`);
  - `next_version(previous: Version | None, bump: Bump | None) -> Version` (`None` → `(1, 0, 0)`);
  - `diff_markdown(d: ReleaseDiff) -> str`.
- [ ] **Step 1: Tests:**
  - `test_parse_version_accepts` (`0.0.1`, `1.10.0`);
  - `test_parse_version_rejects` (`1.0`, `01.0.0`, `1.0.0-rc1`, `v1.0.0`, `../x`, `""`, 300 digits);
  - `test_versions_order_numerically` (`1.10.0 > 1.9.0`);
  - `test_removed_term_is_major`;
  - `test_iri_change_is_major`;
  - `test_removed_range_is_major`;
  - `test_added_term_is_minor`;
  - `test_added_broader_is_minor`;
  - `test_label_or_definition_only_is_patch`;
  - `test_isomorphic_graphs_have_no_bump` (blank-node restrictions relabelled);
  - `test_first_release_lists_all_terms_added`;
  - `test_changed_term_listed_once`;
  - `test_next_version_rules` (`1.4.2` + major → `2.0.0`, minor → `1.5.0`, patch → `1.4.3`; `None` → `1.0.0`);
  - `test_diff_markdown_quotes_labels` (a label with a backtick and a newline).
- [ ] **Step 2:** FAIL → implement → pass; ruff and pyright.
- [ ] **Step 3: Commit** `feat(core): semantic release diff and semver bump rules (FR-GIT-06)`.

---

### Task 2: Release files and messages

**Files:**
- Create `src/lakeloom/gitstore/release_layout.py`, `tests/unit/gitstore/test_release_layout.py`.
- Modify `src/lakeloom/gitstore/layout.py`: `_manifest` writes `release` and `released_at` from the previous manifest, so an ordinary publication keeps the last release instead of resetting it to `null`; `_readme` is unchanged. Extend `tests/unit/gitstore/test_layout.py`.

**Interfaces:**
- **Consumes:** `Snapshot`, `render`, `sanitize_trailer`, `_scalar` (made public as `yaml_scalar`) from `layout.py`; `ReleaseDiff` and `TermChange` (Task 1).
- **Produces:**
  - `class ProposalEntry(BaseModel): subject: str; proposal_id: str; approved_by: str; short_sha: str`;
  - `changelog_section(version: str, day: date, released_by: str, diff: ReleaseDiff, proposals: list[ProposalEntry]) -> str`;
  - `render_changelog(slug: str, previous: bytes | None, section: str) -> tuple[bytes, list[str]]` (the bytes and the warnings);
  - `render_release(snapshot: Snapshot, *, version: str, released_at: datetime, changelog: bytes, previous_manifest: bytes | None, manifests: Mapping[str, bytes]) -> dict[str, bytes]`. It is `render` without the provenance append, with the release fields set and `CHANGELOG.md` added;
  - `release_message(slug: str, version: str, diff: ReleaseDiff, released_by: str, model_version: str, release_id: str) -> str`;
  - `tag_name(slug: str, version: str) -> str`;
  - `tag_message(slug: str, version: str, section: str) -> str`.
- [ ] **Step 1: Tests:**
  - `test_section_layout_exact` (the example in Decisions, byte for byte);
  - `test_section_caps_groups_at_50`;
  - `test_section_sanitises_untrusted_text`;
  - `test_changelog_first_release_without_proposals`;
  - `test_changelog_prepends_and_keeps_old_sections`;
  - `test_unparsable_changelog_replaced_with_warning`;
  - `test_render_release_manifest_fields` (`release: "1.1.0"`, `released_at`, `last_proposal` kept);
  - `test_render_release_has_no_provenance_append`;
  - `test_publication_keeps_last_release` (the `layout.render` after a release keeps `release`);
  - `test_release_message_trailers_in_order`;
  - `test_release_files_deterministic` (hypothesis over diffs and proposal lists).
- [ ] **Step 2:** FAIL → implement → pass.
- [ ] **Step 3: Commit** `feat(gitstore): CHANGELOG, release manifest and release messages (FR-GIT-02, FR-GIT-06)`.

---

### Task 3: Git primitives, reading releases and web links

**Files:**
- Modify `src/lakeloom/gitstore/repo.py`, `publisher.py` (export `repo_lock(repo_key) -> asyncio.Lock`; `_lock` stays as an alias), `snapshot.py` (`fill_exports` reads all five exports and the version from one `ModelService.graph` state: take `version` first and fail transiently if it changed after the exports, closing the m3a backlog item).
- Create `src/lakeloom/gitstore/release_reader.py`, `src/lakeloom/gitstore/links.py`.
- Modify `src/lakeloom/config.py` (`git_web_url: AnyHttpUrl | None`, blank = unset).
- Tests: `tests/unit/gitstore/test_repo.py` (local bare remotes, as today), `test_release_reader.py`, `test_links.py`, `test_snapshot.py` (new or extend).

**Interfaces:**
- **Produces on `WorkingCopy`:**
  - `async fetch_tags(pattern: str) -> None`;
  - `async tags(prefix: str) -> list[TagInfo]`, where `TagInfo(BaseModel): name: str; object_sha: str; commit_sha: str; tagged_at: datetime; subject: str`;
  - `async remote_tag(name: str) -> str | None` (the commit sha via `ls-remote --tags`, peeled `^{}`);
  - `async show(ref: str, path: str) -> bytes | None`;
  - `async tag_annotated(name: str, message: str) -> str`;
  - `async delete_tag(name: str) -> None`;
  - `async push_atomic(branch: str, tag: str) -> None`;
  - `async log_since(since: str | None, path: str, key: str) -> list[LogEntry]`, where `LogEntry(BaseModel): sha: str; subject: str; trailers: dict[str, list[str]]`, oldest first;
  - `async trailer_of(ref: str, key: str) -> list[str]`.

  Every ref or path argument is validated (no leading `-`; tag names match `^[a-z0-9][a-z0-9_-]*/v\d+\.\d+\.\d+$` with the slug pattern; paths through `_safe_rel`).
- **Produces:**
  - `ReleaseReader(wc: WorkingCopy, repo_key: str, *, refresh_seconds: float = 30, clock=…)`, with:
    - `async list(slug: str) -> list[ReleaseInfo]`, where `ReleaseInfo(BaseModel): version: str; tag: str; object_sha: str; commit_sha: str; released_at: datetime`;
    - `async get(slug: str, version: str) -> ReleaseInfo` (`not_found`);
    - `async file(slug: str, version: str, name: Literal["ontology.ttl","ontology.jsonld","ontology.owl","shapes.ttl","context.jsonld","manifest.yaml","CHANGELOG.md"]) -> bytes` (`not_found` when absent);
    - `async ontology(slug, version, fmt: ExportFormat) -> bytes`;
    - `async shapes(slug, version, media: ShapesMedia) -> bytes`;
    - `invalidate(slug: str) -> None` (called after a release).
  - In `links.py`: `web_base(settings: Settings) -> str | None`, `tag_url(base: str, forge: str, tag: str) -> str`, `commit_url(base: str, forge: str, sha: str) -> str`.
- [ ] **Step 1: Tests:**
  - `test_tag_push_atomic_and_remote_tag`;
  - `test_push_atomic_rejected_leaves_remote_unchanged`;
  - `test_show_reads_tagged_file`;
  - `test_log_since_lists_publication_commits_with_trailers`;
  - `test_git_args_reject_dash_and_bad_tags`;
  - `test_list_sorted_semver_desc`;
  - `test_list_ignores_non_semver_and_foreign_tags` (`retail/vbeta`, `retail_x/v1.0.0` not listed for `retail`; a tag whose commit lacks the ontology is ignored);
  - `test_list_refresh_is_throttled`;
  - `test_file_cache_hits_by_object_sha`;
  - `test_ntriples_and_skos_converted_from_turtle`;
  - `test_release_without_shapes_is_not_found`;
  - `test_reader_rejects_unsafe_version`;
  - `test_web_base_derivation` (https with user info, `.git`, `ssh://`, scp-like, override, forge none);
  - `test_tag_and_commit_urls_per_forge`;
  - `test_fill_exports_one_graph_state` (a version change during export → raises, the publisher treats it as transient).
- [ ] **Step 2:** FAIL → implement → pass.
- [ ] **Step 3: Commit** `feat(gitstore): tags, tagged reads and forge web links (FR-PUB-02, FR-PUB-07)`.

---

### Task 4: Release service and release records

**Files:**
- Create `src/lakeloom/state/releases.py`, `src/lakeloom/state/migrations/versions/0004_releases.py`, `src/lakeloom/core/releases.py`.
- Modify `src/lakeloom/core/model_policy.py` (`publish_groups`, `AccessAction` + `"publish"`, `TriplestorePolicy`, `triplestore_for(entries, model) -> TriplestorePolicy`), `src/lakeloom/state/publications.py` (`async open_for_model(repo: str, model: str) -> int`, the count of pending and blocked rows; `async last_done(repo: str, model: str) -> Publication | None`).
- Tests: `tests/unit/state/test_releases.py`, `tests/unit/core/test_releases.py` (a bare local remote, a fake `ModelService` exporting fixed graphs, the real `ConfirmTokens`), `tests/unit/core/test_model_policy.py`.

**Interfaces:**
- **Consumes:** Tasks 1–3; `fill_exports`; `ConfirmTokens.issue` / `verify`; `require_group`; `AuditRepository`; `repo_lock`.
- **Produces:**
  - `Release(BaseModel)`, mirroring the row (Decisions);
  - `ReleaseRepository(engine)`, with: `add`, `get(id)`, `update`, `for_tag(repo, tag)`, `sink_due(repo, now) -> list[Release]`, `sink_counts(repo) -> dict[str, int]`, `requeue_failed_sinks(repo) -> int`;
  - `class ReleasePlan(BaseModel): model: str; version: str; tag: str; previous: str | None; diff: ReleaseDiff; requested_bump: Bump | None; warnings: list[str]; changelog_section: str; confirm_token: str; expires_at: int`;
  - `class ReleaseResult(BaseModel): release: Release; plan: ReleasePlan; already_done: bool; triplestore: Literal["off","queued"]`;
  - `ReleaseService(settings, models, om, wc, reader, releases, publications, confirm, audit, policies, *, clock=…, on_released: Callable[[Release], Awaitable[None]] | None = None)`, with:
    - `async plan(principal, model, *, version: str | None, bump: Bump | None) -> ReleasePlan`;
    - `async create(principal, model, *, version, bump, confirm_token: str) -> ReleaseResult`.

    `create` re-plans under the lock, verifies the token against the fresh state digest, then commits, tags and pushes, records the row, calls `reader.invalidate` and `on_released`, and audits (NFR-SEC-12: principal, model, tag, result).
- [ ] **Step 1: Tests:**
  - `test_first_release_is_1_0_0_with_tag_and_files` (the tag on the remote holds the five exports, `manifest.yaml` `release: "1.0.0"` and `CHANGELOG.md`);
  - `test_second_release_bumps_minor_after_addition`;
  - `test_label_change_bumps_patch`;
  - `test_nothing_changed_is_validation_error`;
  - `test_explicit_version_must_be_greater`;
  - `test_bump_and_version_are_exclusive`;
  - `test_lower_bump_warns`;
  - `test_pending_publication_blocks_release`;
  - `test_release_needs_publish_group` (an agent without the group → `forbidden`);
  - `test_stale_token_after_model_change`;
  - `test_release_commits_direct_in_pull_request_mode`;
  - `test_changelog_lists_proposals_since_previous_tag`;
  - `test_rejected_push_rerenders_on_new_tip`;
  - `test_push_failure_leaves_no_local_tag_and_retry_is_idempotent`;
  - `test_existing_foreign_tag_is_validation_error`;
  - `test_concurrent_releases_serialised` (two `create` calls with two valid tokens → one release, one "nothing changed" or "already exists");
  - `test_release_waits_for_publication_lock`;
  - `test_release_is_audited`;
  - `test_release_never_calls_om_writes` (the fake OM client records calls: only GETs);
  - `test_migration_0004_upgrade_downgrade` (SQLite).
- [ ] **Step 2:** FAIL → implement → pass.
- [ ] **Step 3: Commit** `feat(core): create_release with semver, annotated tag and CHANGELOG (FR-GIT-06, FR-GIT-09)`.

---

### Task 5: Versioned reads, Git links and the release tools

**Files:**
- Modify `src/lakeloom/publish/artifacts.py`:
  - replace `check_version` with `resolve_version(reader: ReleaseReader | None, slug: str, version: str | None) -> ReleaseInfo | None` (`None` means latest);
  - `model_artifact`, `shapes_artifact` and `model_context_artifact` take `releases: ReleaseReader | None` and `git: GitLinks | None`, where `GitLinks(base: str, forge: str, last_commit: Callable[[str], Awaitable[str | None]])`.
- Modify `server/http_api.py` (`/versions`, `/versions/{version}`, `?version=` on model, shapes and context), `server/resources.py`, `server/tools_semantic.py` (`export_model` `version`), `server/server_info.py`, `server/http_app.py` (AppDeps: `releases_repo`, `reader`, `release_service`, `git_links`), `cli.py` (`release create|list`), `docs/api/openapi.json` (regenerated).
- Create `src/lakeloom/server/tools_release.py`.
- Tests: `tests/unit/publish/test_artifacts.py`, `tests/unit/server/test_http_api.py`, `test_resources.py`, `test_tools_release.py` (new), `test_tools_semantic.py`, `test_server_info.py`, `tests/unit/test_cli.py`, the MCP conformance test (schemas, descriptions, annotations).

**Interfaces:**
- **Consumes:** `ReleaseReader`, `ReleaseService`, `ReleasePlan` / `ReleaseResult`, `links.*`, `PublicationRepository.last_done`.
- **Produces:**
  - Tool results: `CreateReleaseResult(preview: ReleasePlan | None, result: ReleaseResult | None)`, `ReleaseList(model: str, releases: list[ReleaseInfo])`, and `ReleaseDiff` for `diff_releases`.
  - The text of `create_release` follows the `_confirm_line` pattern of `tools_proposals.py`.
  - `diff_releases` with `to_version="latest"` diffs against the current export.
- [ ] **Step 1: Tests:**
  - `test_release_artifact_validators` (ETag = body hash, Last-Modified = tagger date, two `via` Links to the tag and the commit);
  - `test_latest_adds_related_commit_link`;
  - `test_links_never_carry_userinfo`;
  - `test_versions_lists_latest_then_releases`;
  - `test_get_version_serves_tagged_bytes` (equal to `git show`);
  - `test_shapes_and_context_by_version`;
  - `test_unknown_version_404_with_hint`;
  - `test_git_off_version_404`;
  - `test_conditional_get_on_release_304`;
  - `test_resource_version_pinning` (`ontology.ttl?version=1.0.0`, `shapes.ttl?version=…`);
  - `test_export_model_release_ntriples`;
  - `test_create_release_dry_run_then_confirm`;
  - `test_create_release_requires_publish_scope`;
  - `test_list_and_diff_releases`;
  - `test_server_info_reports_releases_feature`;
  - `test_cli_release_create_and_list`;
  - `test_openapi_up_to_date`.
- [ ] **Step 2:** FAIL → implement → pass.
- [ ] **Step 3: Commit** `feat(server): released versions over HTTP, MCP and tools with Git links (FR-PUB-01, FR-PUB-02, FR-PUB-07, FR-IO-01)`.

---

### Task 6: Triple-store sink (optional, last)

**Files:**
- Create `src/lakeloom/publish/triplestore.py`, `tests/unit/publish/test_triplestore.py` (httpx `MockTransport`), `tests/contract/test_triplestore_fuseki.py`.
- Modify `config.py` (settings in Decisions; `ConfigError` when enabled without a URL; the password is a secret with `_FILE`), `server/http_app.py` (the worker loop also calls `sink.process()`; `on_released` wires the inline attempt), `server/server_info.py`, `cli.py` (`release sink-retry`), `deploy/compose/docker-compose.yml` (comment only: how to point `LAKELOOM_TRIPLESTORE_URL` at the `rdf` profile Fuseki; the default stays off).

**Interfaces:**
- **Consumes:** `ReleaseRepository.sink_due` / `update` / `requeue_failed_sinks`; `ReleaseReader.file` / `list`; `triplestore_for`; `backoff_seconds`; `ModelService.get_model` (base IRI).
- **Produces:**
  - `class TripleStoreSink(Protocol): async def replace_graph(self, graph_iri: str, turtle: bytes) -> None`;
  - `GraphStoreSink(http: httpx.AsyncClient, url: str, username: str | None, password: SecretStr | None)` (raises `SinkError(transient: bool, message: str)`);
  - `SinkWorker(settings, releases_repo, reader, models, sink, policies, *, clock=…)`, with `async process(now: datetime | None = None) -> list[Release]` (never raises) and `async status() -> dict[str, Any]`;
  - `MAX_SINK_ATTEMPTS = 10`.
- [ ] **Step 1: Unit tests:**
  - `test_put_release_latest_and_shapes_graph_iris` (exact `?graph=` values, `Content-Type`, basic auth);
  - `test_latest_only_for_highest_release` (a sink retried for `1.0.0` after `1.1.0` exists does not replace latest);
  - `test_shapes_off_by_policy`;
  - `test_model_without_base_iri_fails`;
  - `test_transient_and_permanent_classification` (408, 429, 500, transport error; 401, 404, 400);
  - `test_failed_after_10_attempts`;
  - `test_sink_down_never_blocks_release` (a `ReleaseService.create` with a sink that raises returns done, `triplestore: "queued"`);
  - `test_sink_error_redacts_password`;
  - `test_enabled_without_url_is_config_error`;
  - `test_sink_retry_cli_requeues_failed`;
  - `test_server_info_triplestore_block`.
- [ ] **Step 2: Contract test** (`test_triplestore_fuseki.py`, skipped unless the `rdf` profile Fuseki answers `/$/ping`):
  - create the in-memory dataset `lakeloom_m3b` via `POST /$/datasets` (admin credentials from `FUSEKI_ADMIN_PASSWORD`, default `admin`, dev only);
  - run `GraphStoreSink` against it, then a SPARQL `ASK` on `GRAPH <…release/1.0.0>` and on `<…latest>`;
  - delete the dataset in teardown.
- [ ] **Step 3:** FAIL → implement → pass.
- [ ] **Step 4: Commit** `feat(publish): optional Graph Store sink for releases (FR-TS-01, FR-TS-02, FR-TS-03)`.

---

### Task 7: M3b acceptance (live) and wrap-up

**Files:**
- Create `tests/contract/test_m3b_acceptance.py`.
- Modify ADR 0015 (accepted; Research and Consequences), ADR 0010 (§ Link and § Versions superseded for Git), ADR 0013 (releases commit directly), README (releases, versions, sink), CHANGELOG (repository), `docs/backlog/m3a-deferred.md` (mark the items fixed in Task 3).
- Create `docs/backlog/m3b-deferred.md`.

- [ ] **Step 1: `test_m3b_acceptance.py`** runs through MCP and HTTP against the running stack (Git mode `direct`), on a throwaway model `retail_m3b`. The setup is: agent scan of `fixture_delta`, `create_model`, `propose_from_scan`, submit, alice approves, then wait until the publication is `done`. Teardown deletes the test tags through the Gitea API (`DELETE /api/v1/repos/lakeloom-admin/ontologies/tags/{tag}`) and hard-deletes the glossary and the ontology; it never seals.
  - `test_m3b_release_v1_produces_tag_and_artefacts`:
    - `create_release` as an agent with `lakeloom:publish`: the dry run shows `1.0.0` and "Initial release"; confirm;
    - the Gitea tag `retail_m3b/v1.0.0` exists and its commit holds the five exports, `manifest.yaml` `release: "1.0.0"` and `CHANGELOG.md` with `## [1.0.0]`;
    - `GET /v1/models/retail_m3b/versions` lists `latest` and `1.0.0`;
    - `GET /versions/1.0.0` (turtle) equals the tagged `ontology.ttl`, and its `Link` contains `/src/tag/retail_m3b/v1.0.0`;
    - `shapes?version=1.0.0` equals the tagged `shapes.ttl`;
    - `lakeloom://models/retail_m3b/ontology.ttl?version=1.0.0` equals the HTTP body.
  - `test_m3b_second_release_is_minor`:
    - propose an added attribute, alice approves, the publication is done;
    - the dry run shows `1.1.0` with the attribute under *Added* and the proposal under *Proposals*;
    - confirm; `diff_releases(1.0.0, 1.1.0)` lists the attribute.
  - `test_m3b_release_refused_while_publication_pending`: only when the queue can be held (otherwise the case stays covered by unit tests; documented).
- [ ] **Step 2:** Rebuild Lakeloom (`docker compose -f deploy/compose/docker-compose.yml up -d --build --wait lakeloom`), then run the full unit and contract suites green. Never `down -v`.
- [ ] **Step 3: Docs:** ADR 0015 accepted, ADR 0010/0013 notes, README, CHANGELOG. Triage `m3a-deferred.md` and write `m3b-deferred.md` (at least: pre-release versions, asset context by version, release through pull requests, multi-replica leases for the sink, signed tags, retention of the reader cache).
- [ ] **Step 4: Commit** `test(contract): M3b acceptance — release v1.0.0 produces tag and artefacts (FR-GIT-06, FR-PUB-02)`.

---

## Out of scope for M3b

- `import_model`, `import_from_git`, drift detection and the SPARQL proxy (M3c).
- Pre-release and build-metadata versions; signed tags; releases through pull requests.
- Asset context and `lakeloom://models` pinned to a release.
- Multi-replica leases for the release and sink workers (single replica, as in M2c).
