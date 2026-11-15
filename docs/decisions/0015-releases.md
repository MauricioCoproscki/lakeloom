# 0015. Releases, versioned reads and the triple-store sink

- Status: proposed
- Date: 2026-11-15
- Requirements: FR-GIT-02, FR-GIT-03, FR-GIT-06, FR-GIT-09, FR-PUB-01, FR-PUB-02, FR-PUB-07, FR-IO-01, FR-AGT-03, FR-TS-01, FR-TS-03, NFR-SEC-07, NFR-SEC-11

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
  Graph Store HTTP PUTs of the release graph, `latest` (only for the highest release) and shapes.
  2xx succeeds; 408, 429, 5xx and transport errors retry with backoff; other 4xx and 10 attempts are
  `failed`. The sink never blocks or undoes the Git release (FR-TS-03) and its password never
  appears in logs or errors (NFR-SEC-11).
- **Locking.** Releases, tag fetches and the publisher share one per-repository lock (FR-GIT-09).

## Consequences

To be filled at acceptance. Known up front: open Lakeloom PRs of a model conflict on the manifest
once a release is committed directly on the target branch.
