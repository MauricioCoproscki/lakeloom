# 0015. Releases, versioned reads and the triple-store sink

- Status: accepted (M3b complete)
- Date: 2026-11-15
- Requirements: FR-GIT-02, FR-GIT-03, FR-GIT-06, FR-GIT-09, FR-PUB-01, FR-PUB-02, FR-PUB-07, FR-IO-01, FR-AGT-03, FR-TS-01, FR-TS-02, FR-TS-03, NFR-SEC-07, NFR-SEC-11

## Context

M3b adds named, immutable releases of a model, reads of any release, and an optional push of
releases to a triple store. The plan is `docs/superpowers/plans/` (M3b, releases). The pure core
(semantic diff and bump rules) lives in `lakeloom.core.release_diff`.

## Decision

- **Git only, never OpenMetadata releases.** OM 2.1-rc1 has `ontologyReleases`, which are sealed
  (a write). Lakeloom never seals and never calls those endpoints. A Lakeloom release is an
  annotated Git tag over OM's current state.
- **Version.** Strict `MAJOR.MINOR.PATCH`, regex `^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$`; no
  pre-release or build metadata. Tag `<slug>/v<version>`, annotated. The first release is `1.0.0`.
- **Bump (FR-GIT-06).** A later release diffs the previous release's `ontology.ttl` against the
  current Turtle export (`rdflib.compare.graph_diff` of `to_isomorphic` graphs). A *term* is a URIRef
  subject typed `owl:Class`, `skos:Concept`, `owl:DatatypeProperty` or `owl:ObjectProperty`.
  *Annotation predicates* are `skos:prefLabel`, `altLabel`, `hiddenLabel`, `definition`,
  `scopeNote`, `note`, `example`, `rdfs:label`, `rdfs:comment`, `dcterms:description`.
  - **major**: a term is removed (an IRI change is a removal plus an addition), or a triple whose
    predicate is not an annotation predicate is removed (a range, a `broader`, a restriction).
  - **minor**: otherwise, a term or a non-annotation triple is added.
  - **patch**: otherwise, only annotation triples differ.
  - **nothing**: isomorphic graphs give `validation_error` "nothing changed since `<prev>`",
    hint "approve a proposal first".
- **Override.** `bump` or an explicit `version`, never both. The result must be strictly greater
  than the highest existing release. A lower bump than computed is allowed; the preview warns
  "computed <x>, releasing as <y>".
- **Who may release.** Scope `lakeloom:publish` and `ModelAccess.publish_groups` (new, NFR-SEC-07;
  empty means no restriction). Agents may release; only review is human-only. `Released-By` is the
  principal's username, else the subject.
- **Two-step confirm (FR-AGT-03).** Without `confirm_token`, `create_release` is a dry run returning
  version, bump, reason, CHANGELOG section and a token. The token's state digest is
  `sha256(model_version + "\n" + (previous tag object sha or "none"))`, so a model change or a
  concurrent release invalidates it.
- **Preconditions** (all `validation_error` with a hint): Git enabled; no `pending` or `blocked`
  publication of the model in the repository; the tag does not exist on the remote (except for
  idempotency).
- **Release commit.** Always directly on `LAKELOOM_GIT_BRANCH`, in both Git modes: a release is an
  explicit `lakeloom:publish` act and a tag must point at a commit on the target branch. It holds
  the five exports from one graph state, `manifest.yaml` with `release` and `released_at`,
  `CHANGELOG.md` with the new section prepended, and the regenerated `README.md`. Nothing is
  appended to `provenance/<slug>.jsonl`. Subject `release(<slug>): v<version>`, body
  `Bump: <bump> (<reason>)`, trailers `Lakeloom-Release`, `Released-By`, `Lakeloom-Model-Version`,
  `Lakeloom-Release-Id`. Author and committer are the service identity.
- **Tag and push.** `git tag -a`, message `<slug> v<version>` plus the CHANGELOG section; one
  `git push --atomic origin refs/heads/<branch> refs/tags/<tag>`. A rejected push deletes the local
  tag, resets, re-renders, at most 3 times, then `upstream_unavailable`. Other failures are reported
  to the caller and nothing is queued.
- **Idempotency.** If the remote tag exists and its commit carries this `Lakeloom-Release-Id`, the
  release is complete. The id is `rel_` + the first 26 hex characters of `sha256(confirm token)`.
- **CHANGELOG (FR-GIT-02).** `ontologies/<slug>/CHANGELOG.md`, regenerated at each release, newest
  section first, old sections kept by parsing `## [` headings. Added, Changed and Removed come from
  the diff (capped at 50 lines each); Proposals are the commits since the previous tag that carry
  `Lakeloom-Proposal`. Text from OM or agents is sanitised and quoted in code spans.
- **Release record.** Table `releases` (migration `0004_releases`), unique on (`repo`, `tag`); it is
  the audit trail and the sink queue. Git is the source of truth for listing, so tags made outside
  Lakeloom are still listed and served.
- **Reading releases.** `ReleaseReader` lists `<slug>/v*` tags (fetch under the repository lock, at
  most once per 30 s), keeps those matching the version regex that hold `ontology.ttl`, orders by
  semver descending, and reads files with `git show <tag>:<path>` (cached by tag object sha and
  file). `ntriples` and `skos` are converted from the stored Turtle. Hostile versions are
  `validation_error` (tools) or `not_found` (reads), never passed to git unvalidated.
- **Validators and links (FR-PUB-07).** A release artefact has the body hash as ETag and the tagger
  date as `Last-Modified`, with `via` links to the tag and commit web URLs; `latest` keeps the OM
  `via` link and adds a `related` link to the last done publication. The web base is
  `LAKELOOM_GIT_WEB_URL` or derived from `LAKELOOM_GIT_REMOTE` (no user info, no `.git`); none with
  `LAKELOOM_FORGE=none`. Paths differ per forge (GitHub, Gitea, GitLab).
- **Surfaces.** HTTP `/versions`, `/versions/{version}` and `?version=`; MCP `?version=` on
  `ontology.*`, `shapes.ttl`, `context.jsonld`; tools `create_release`, `list_releases`,
  `diff_releases`, `export_model(version=)`; CLI `lakeloom release create|list|sink-retry`.
- **Triple-store sink (FR-TS, optional, off by default).** On a done release with the sink enabled
  for the model, the release row is queued and an inline attempt runs in the background, with
  Graph Store HTTP PUTs of the release graph, then `latest` and shapes (both only for the
  highest release, so an older release's retry never overwrites them). The graph IRIs are
  `<base>release/<version>`, `<base>latest` and `<base>shapes`; when the base IRI ends in
  neither `/` nor `#`, a `/` is inserted before the suffix (a deliberate deviation from literal
  concatenation, so `http://x/onto` gives `http://x/onto/latest`). The password is redacted in
  its plain, URL-encoded and HTTP-basic-token forms.
  2xx succeeds; 408, 429, 5xx and transport errors retry with backoff; other 4xx and 10 attempts are
  `failed`. The sink never blocks or undoes the Git release (FR-TS-03) and its password never
  appears in logs or errors (NFR-SEC-11).
- **Locking.** Releases, tag fetches and the publisher share one per-repository lock (FR-GIT-09).

## Research

### Gitea 1.27.3 (live, dev stack)

- **Tag API.** `GET` and `DELETE /api/v1/repos/{owner}/{repo}/tags/{tag}` accept a tag with a
  `/` when it is percent-encoded as one segment (`retail_m3b%2Fv1.0.0`); `DELETE` answers 204.
- **The single-tag endpoint can be stale.** After a tag was deleted and a tag of the same name
  was pushed again, `GET .../tags/{tag}` kept showing the *old* commit for well over 30 s while
  `git ls-remote` showed the new one. Git is the ground truth: Lakeloom reads tags with
  `ls-remote` and `fetch` only, never through the forge API, and the acceptance test checks
  the commit with `ls-remote` and uses the API only for existence and deletion.
- **Releasing as an agent.** The dev client `lakeloom-agent` has `lakeloom:publish` as an
  optional scope, so a client-credentials request with `scope=lakeloom:publish` gives a
  release-capable agent token without changing the realm. `Released-By` is then the service
  account's subject (a UUID): it has no username claim.
- **Web links.** With `LAKELOOM_FORGE=gitea` and the remote `http://gitea:3000/...`, the derived
  web base is the container's host name; the `Link` of `/versions/1.0.0` holds
  `/src/tag/retail_m3b/v1.0.0` and `/commit/<sha>` as specified. A browser outside the compose
  network needs `LAKELOOM_GIT_WEB_URL`.
- **CLI, Git on.** `docker compose exec lakeloom lakeloom release list retail` prints
  `No releases of retail.`; `lakeloom release create retail` (no `--yes`) prints the plan
  (`retail/v1.0.0`, "Initial release (25 terms)", the term list) and
  `Dry run: nothing was changed. Re-run with --yes to release.`, with no tag created.

### Graph Store sink

Not run live: the `rdf` compose profile (Fuseki) is not started in the dev stack, and building
it is out of scope for M3b. `tests/contract/test_triplestore_fuseki.py` skips without it; the
sink is covered by unit tests against a fake Graph Store (`tests/unit/publish/test_triplestore.py`).
A PUT against a real Fuseki stays unverified (see `docs/backlog/m3b-deferred.md`).

## Consequences

What shipped in M3b:

- `create_release` (MCP), `lakeloom release create|list|sink-retry` (CLI), `list_releases`
  and `diff_releases`. Verified live (`tests/contract/test_m3b_acceptance.py`): an agent with
  `lakeloom:publish` releases a throwaway model as `1.0.0` (dry run with "Initial release",
  then confirm); the annotated tag `retail_m3b/v1.0.0` points at a commit with the five
  exports, `manifest.yaml` `release: "1.0.0"` and `CHANGELOG.md` `## [1.0.0]`; `/versions`
  lists `latest` and `1.0.0`; `/versions/1.0.0`, `shapes?version=1.0.0` and
  `lakeloom://models/retail_m3b/ontology.ttl?version=1.0.0` equal the tagged files, with Git
  `Link`s. After alice approves an added attribute, the next release is computed as minor
  (`1.1.0`), with the attribute under *Added* and the proposal under *Proposals*, and
  `diff_releases(1.0.0, 1.1.0)` lists it.
- A release refused while a publication is `pending` or `blocked` is covered by unit tests
  only: the dev stack's queue cannot be held without breaking Gitea for other suites.
- The triple-store sink is off by default; when on, a sink failure never fails or undoes a
  release.

Found by the live tests:

- **Re-created tags returned a stale release row.** If a tag was deleted on the remote and its
  version was released again, `ReleaseRepository.add` returned the old row for (`repo`,
  `tag`). The tool then reported the old commit and id, and the sink hook did not run. A
  row is now returned only when it is the same release (id, tag and commit). Any other row
  with the same id or tag is stale and replaced (`fix(state)`, pinned by
  `test_add_replaces_stale_row_of_a_recreated_tag` and
  `test_release_again_after_tag_deleted_records_new_commit`).

Known limits:

- **Audit.** The audit row of `create_release` stores the tag in `change_set_id` (no
  dedicated column).
- **Pull-request mode.** Releases commit directly on the target branch even when the model
  publishes through pull requests. Open Lakeloom PRs of the model then conflict on
  `manifest.yaml` (and `README.md`, as in ADR 0013); merge or close them before releasing.
- **Re-render after a rejected push.** The re-render reuses the snapshot exported before the
  first attempt. With a single replica the model cannot change in between, because approvals
  publish under the same lock. With several instances a release can tag a state that is
  slightly older than the tip (multi-instance only).
- **Stale `latest` graph.** The sink writes `<base>latest` and `<base>shapes` only for the
  highest release. If that release's sink row ends `failed`, both graphs keep the previous
  release until
  `lakeloom release sink-retry` succeeds.
- **Manifests gain `released_at: null`.** Every publication manifest now carries `release` and
  `released_at` (null until a release). The first publication after the upgrade therefore
  changes `manifest.yaml` of every model it touches.
- **CLI working copy.** The repository lock is per process, so the CLI release commands use
  their own clone (`<data_dir>/git/cli-repo`) instead of the server's `<data_dir>/git/repo`.
  A CLI release next to a running server is serialised by the remote only: a rejected push is
  re-rendered on the new tip (at most 3 times). The CLI `git` queue commands still open the
  server's working copy.
- **Reader refresh.** The tag list served is the one captured under the repository lock at
  the last refresh (at most every 30 s, pruned against the remote), so a tag that exists only
  locally is never listed. When the fetch fails, the local tags are served (throttled), and
  other Git failures of the reader are `upstream_unavailable`; `/versions` then lists
  `latest` only.
- **A CHANGELOG left by deleted tags.** A first release reuses the `CHANGELOG.md` at the
  branch tip. If the model's earlier tags were deleted by hand, their sections stay below the
  new one.
- **Single replica.** The repository lock and the sink worker are in-process, as in M2c.
- **Graph Store against a real Fuseki** is not live-tested (see Research).
- Pre-release versions, signed tags, releases through pull requests, asset context and
  `lakeloom://models` pinned to a release, and multi-replica leases are deferred:
  `docs/backlog/m3b-deferred.md`.
