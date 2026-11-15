# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Repository skeleton, tooling (uv, ruff, pyright, pytest) and community files.
- `lakeloom version` CLI command.
- Settings from environment variables and `*_FILE` secrets.
- OpenMetadata client (async, OpenAPI-checked).
- OIDC bearer-token authentication with RFC 9728 protected-resource metadata.
- Health, readiness and Prometheus metrics endpoints, and JSON logs.
- MCP server over Streamable HTTP with the `get_server_info` tool.
- Container image (non-root, read-only-filesystem compatible).
- Docker Compose dev stack (OpenMetadata 2.1.0-rc1, Keycloak, Gitea, PostgreSQL, optional RDF profile).
- Contract tests against the dev stack.
- CI: lint, typecheck, unit (Python 3.12 and 3.13), contract and image jobs with dependency audit,
  SBOM, provenance and Trivy scan; DCO check; Dependabot.
- M1a lake sources: `LakeSource` protocol, operator YAML config (`LAKELOOM_CONFIG_FILE`) and
  `lakeloom.sources` plugin entry points; Delta storage (delta-rs, extra `delta`), Unity Catalog
  (REST API) and upstream MCP (mapping YAML; OpenMetadata mapping shipped, Databricks and
  Snowflake as examples to verify) connectors.
- Streaming column profiles computed in place, with sample values off by default, sensitive-tag
  exclusion and value truncation.
- Scan service and scan record store (Alembic migrations) with per-table errors and
  OpenMetadata lookup by FQN.
- MCP tools `list_sources`, `list_tables`, `describe_table` and `scan_lake_source`;
  `get_server_info` now lists the configured sources with their availability.
- CLI commands `lakeloom sources`, `lakeloom tables SOURCE` and `lakeloom scan SOURCE`, plus
  `lakeloom db upgrade`.
- Dev stack: Unity Catalog OSS, fixture seeding for OpenMetadata and Unity Catalog, and three
  configured sources (`fixture_delta`, `fixture_uc`, `openmetadata_mcp`).
- A warning at startup when an upstream MCP tool used by a mapping is not annotated read-only.
- M1b semantic read path: model service over OpenMetadata glossaries with deterministic RDF
  export (Turtle, RDF/XML, N-Triples, JSON-LD, SKOS), concept search and explanation, and asset
  context in Markdown, JSON and JSON-LD with token estimates and `max_tokens` truncation.
- MCP tools `list_models`, `get_model`, `find_concepts`, `explain_concept`,
  `find_assets_for_concept`, `get_asset_context` and `export_model`.
- CLI commands `lakeloom models`, `lakeloom concept`, `lakeloom context` and `lakeloom export`.
- Setting `LAKELOOM_MODEL_CACHE_TTL_SECONDS` (default 60).
- M1c publication read surfaces: read-only HTTP API under `/v1` (models, versions, concepts and
  asset context) with a committed OpenAPI spec, and MCP resources serving the same bytes.
- Content negotiation (`Accept` with q-values, `406` listing supported types) and strong `ETag`,
  `Last-Modified`, `Link` and `If-None-Match` handling on every artefact.
- stdio transport with a configured local principal (`LAKELOOM_STDIO_PRINCIPAL`,
  `LAKELOOM_STDIO_SCOPES`), refused in containers unless explicitly enabled.
- OpenTelemetry traces and metrics over OTLP, off unless `LAKELOOM_OTEL_EXPORTER_OTLP_ENDPOINT`
  is set.
- Concept name resolution ranks exact name, then synonym, then local name.
- M1 acceptance contract test across MCP tools, MCP resources and HTTP.
- M2a proposals:
  - MCP tools `create_model` (scope `lakeloom:admin`; glossary plus paired native ontology),
    `propose_from_scan`, `propose_change`, `propose_realization`, `get_proposal`,
    `diff_proposal`, `submit_proposal` and `discard_proposal`. Proposals are OpenMetadata
    Draft change sets, and nothing is applied.
  - Confirm tokens for `create_model` and `discard_proposal`, bound to the calling principal.
  - Only a proposal's creator or a `lakeloom:admin` caller may change, submit or discard it.
  - Incremental scans: operations on a concept still pending in another open proposal are
    dropped with a warning instead of failing; `propose_change` skips operations pending
    elsewhere and concepts or attributes the model already has.
  - Realizations are limited to concepts of the model and the models it imports.
  - Attributes, axioms and relations on existing concepts of a model paired with
    `pair_existing=true` declare the concept's native class first.
  - Rule ids of agent edits are always `R-AGENT`; operation provenance is size-limited.
  - One audit record per write (partial results are audited as errors); MCP prompts need
    `lakeloom:read`.
  - MCP prompts `bootstrap_data_product` and `explain_table`.
  - CLI commands `lakeloom propose`, `lakeloom proposal` and `lakeloom submit`.
  - Dev realm client `lakeloom-admin-agent`.
  - M2a acceptance contract test.
- M2b review:
  - MCP tools `review_proposal` (approve, reject or request changes; human principals only,
    scope `lakeloom:review`, confirm token for approve and reject), `rebase_proposal` and
    `list_pending_reviews` (grouped by model and data product, paginated).
  - Per-model `access` groups, on-behalf-of OpenMetadata tokens for reviewers and a review
    service that applies proposals as the reviewer.
  - MCP prompt `review_with_specialist`.
  - CLI commands `lakeloom pending` and `lakeloom rebase`.
  - Dev stack: the Lakeloom container exchanges reviewers' tokens for OpenMetadata tokens
    (client `lakeloom`, DEV ONLY secret, compose-network token endpoint); forward mode is not
    usable in the dev stack.
  - M2b acceptance contract test: agents and callers without `lakeloom:review` or
    OpenMetadata's reviewer permission are refused, and OpenMetadata records the human
    reviewer as the one who applied (FR-REV-01, FR-REV-02).
  - Reviewers cannot review their own proposals; `request_changes` also works on
    `apply_failed` proposals.
  - A later part referencing (through a relation, mapping or synonym) a term an earlier part
    of the same approve changed is re-drafted once instead of failing as stale; a stale-base
    conflict names the affected terms instead of OpenMetadata operation ids (FR-OM-03).
  - OpenMetadata rejecting a reviewer's token (HTTP 401) is `forbidden` with a sign-in hint
    and drops the cached exchanged token; reviewer clients sharing the OpenMetadata
    connection pool keep no cookies.
  - `LAKELOOM_OM_EXCHANGE_SCOPE` is unset by default (the dev stack sets
    `openmetadata-audience`).
- M2c Git publishing:
  - Every applied proposal becomes one commit in the ontology repository: the three
    deterministic exports, `manifest.yaml`, a `provenance/<slug>.jsonl` line and a regenerated
    root `README.md`, with the trailers `Lakeloom-Proposal`, `Lakeloom-ChangeSet`,
    `Approved-By`, `Lake-Source-Version`, `Lakeloom-Scan`, `Lakeloom-Proposed-By`,
    `Lakeloom-AI-Generated` and `Lakeloom-Publication` (FR-GIT-04, FR-PRO-03).
  - Modes `direct` and `pull_request` (`LAKELOOM_GIT_MODE`, per model `git.mode` in
    `LAKELOOM_CONFIG_FILE`); pull requests on Gitea, GitHub and GitLab, an open one reused
    (FR-GIT-05). Only Gitea is live-tested.
  - A publication outbox in the state store (Alembic migration) with per-repository ordering,
    retry with exponential backoff, `blocked` publications on permanent errors and
    trailer-based idempotency (FR-GIT-09).
  - An isolated `git` runner: credentials through `GIT_ASKPASS` or an SSH key, no system or
    global config, process-group kill on timeout, redacted errors (FR-GIT-01, NFR-SEC-11).
  - `review_proposal` ends with a `Git:` line and returns `publication`; `get_proposal` shows
    it; `get_server_info` reports `git`; metrics `lakeloom_git_publications_total` and
    `lakeloom_git_push_failures_total`.
  - CLI commands `lakeloom git status` and `lakeloom git retry`.
  - Settings `LAKELOOM_GIT_USERNAME`, `LAKELOOM_GIT_SSH_KNOWN_HOSTS_FILE`,
    `LAKELOOM_GIT_AUTHOR_NAME`, `LAKELOOM_GIT_AUTHOR_EMAIL`,
    `LAKELOOM_GIT_PUBLISH_TIMEOUT_SECONDS`, `LAKELOOM_GIT_RETRY_INTERVAL_SECONDS` and
    `LAKELOOM_FORGE_API_URL`.
  - Dev stack: Lakeloom publishes to the Gitea repository `lakeloom-admin/ontologies` in
    `direct` mode (DEV ONLY admin credentials); the model `retail_m2c_pr` uses
    `pull_request`; Gitea builds API links from `ROOT_URL`; the image ships `openssh-client`.
  - Contract tests: the publisher against throwaway Gitea repositories, and the M2c acceptance
    test (an approval by alice is committed with all trailers, the export equals the committed
    Turtle byte for byte, pull-request mode opens a PR).
- M3a shapes, JSON-LD context and validation (ADR 0014):
  - Generated SHACL shapes per model: a node shape per class, property shapes with
    `sh:datatype`, `sh:class`, `sh:in` and counts from identifiers and cardinality
    restrictions, a class-specific shape when a restriction applies (FR-PUB-04). Served at
    `GET /v1/models/{slug}/shapes` (Turtle, JSON-LD, N-Triples) and as the MCP resource
    `lakeloom://models/{slug}/shapes.ttl`.
  - The full JSON-LD `@context` of a model (a typed term per class and per property, unique
    short aliases) at `GET /v1/models/{slug}/context.jsonld` and
    `lakeloom://models/{slug}/context.jsonld` (FR-PUB-05). Asset JSON-LD now declares
    `@version: 1.1` and its `@context` is the model's context pruned to the terms and prefixes
    the payload uses (the minimal context plus keywords for today's payload), so `max_tokens`
    is not spent on unused terms (FR-CTX-02, FR-CTX-06).
  - MCP tool `validate_model` and CLI command `lakeloom validate` (exit code 1 when the model
    does not conform): duplicate labels, hierarchy cycles, dangling domains, external ranges,
    isolated or undefined concepts, untyped attributes, shapes counts.
  - Git publications also commit `ontologies/<slug>/shapes.ttl` and `context.jsonld`, the same
    bytes as the HTTP API (FR-GIT-02, FR-GIT-03).
  - `pyshacl` as a dev dependency; the M3a acceptance test validates the fixture lake's data
    against a throwaway model's published shapes and reads a JSON record through its context.
  - Serialised shapes (per media type) and the context document are cached per model
    version; the Git files are the same cached bytes.
- M3b releases, versioned reads and the triple-store sink (ADR 0015):
  - MCP tool `create_release` (scope `lakeloom:publish`, `publish_groups` per model, two-step
    confirm) and CLI `lakeloom release create`. A release is one commit directly on the
    target branch with the five exports, `manifest.yaml` (`release`, `released_at`), the
    model's `CHANGELOG.md` and the root `README.md`, plus the annotated tag
    `<slug>/v<version>`, pushed atomically. The version is computed from a semantic diff with
    the previous release: major, minor or patch, or an explicit `bump` / `version`. Retrying
    the same confirmed call is idempotent (FR-GIT-02, FR-GIT-06, FR-GIT-09, FR-AGT-03).
  - `list_releases`, `diff_releases`, `export_model(version=)` and `lakeloom release list`.
    `GET /v1/models/{slug}/versions` and `/versions/{version}`, and `?version=` on the model,
    shapes and context over HTTP and as MCP resources, serve a release from its Git tag
    (FR-PUB-01, FR-PUB-02, FR-IO-01).
  - Git `Link` headers: a release links its tag and commit web pages, and `latest` links the
    last Git publication. `LAKELOOM_GIT_WEB_URL` sets the web base, otherwise it is derived
    from the remote (FR-PUB-07).
  - Optional Graph Store sink: each release is pushed to `<base-iri>release/<version>`,
    `<base-iri>latest` and `<base-iri>shapes`, with retries and backoff, and never blocks the
    release. Settings `LAKELOOM_TRIPLESTORE_*`, per-model `triplestore:` policy, CLI
    `lakeloom release sink-retry`, and `get_server_info` `triplestore` (FR-TS-01, FR-TS-02,
    FR-TS-03).
  - State migration `0004_releases`. Publication manifests now carry `release` and
    `released_at` (null until a release). A Git snapshot reads its exports and the model
    version from one graph state.
  - The M3b acceptance test releases a throwaway model twice against the dev stack's Gitea.

### Fixed

- An attribute whose xsd datatype is not in the OWL 2 datatype map (such as `xsd:date` from a
  Spark `date` column) gets the native range `rdfs:Literal`, so OpenMetadata applies it; the
  proposal keeps the precise datatype.
- Draft ids of new terms, native classes, properties, relations and axioms include the model,
  so two models proposing the same concept no longer collide on OpenMetadata's global ids
  (the second model's approve failed with "the name is already taken").
