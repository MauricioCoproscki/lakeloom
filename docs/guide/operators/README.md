# Operator guide

This guide is for the people who install and run Lakeloom. The project
[README](../../../README.md) remains the detailed reference for each subsystem; this page puts
the pieces in order and links to the right section.

## What you are running

Lakeloom is one container with:
- an MCP server (Streamable HTTP at `/mcp`, optionally stdio);
- a read-only HTTP API (`/v1`);
- a CLI (`lakeloom …`).

It depends on:

| Dependency | Required | Role |
|------------|----------|------|
| OpenMetadata 2.1+ (Ontology Studio) | Yes | Stores and governs the models |
| An OIDC identity provider (Keycloak, Okta, Entra ID…) | Yes, over HTTP | Signs in agents and humans; carries the scopes |
| State store: SQLite or PostgreSQL | Yes | Scans, proposals, audit log, job leases. PostgreSQL is needed for more than one replica |
| Lake sources: Delta, Unity Catalog, Snowflake, upstream MCP servers | Per use | What gets scanned |
| A Git remote: GitHub, GitLab, Gitea | Optional | Publication of applied changes; needed for releases and reconciliation |
| A triple store: Fuseki, GraphDB, RDF4J | Optional | SPARQL copy of each release |

## 1. Try it locally

The development stack runs everything on your machine: OpenMetadata, Keycloak, Gitea,
PostgreSQL, Lakeloom and a fixture lake.

```bash
docker compose -f deploy/compose/docker-compose.yml up -d --wait
curl localhost:8000/readyz
```

Ports, the dev users (`alice`, `bob`, `carol`) and how to get an agent token are in the
README, under [Quick start](../../../README.md#quick-start). Every credential in the dev stack is
a public default: never reuse one outside your machine.

## 2. Connect an assistant

- **Over HTTP:** point any Streamable HTTP MCP client at `https://<host>/mcp` with a bearer
  token from your IdP. See
  [Call a tool from an agent](../../../README.md#call-a-tool-from-an-agent).
- **Locally over stdio:** run `lakeloom serve --transport stdio`. The client is the
  configured local principal, with the scopes in `LAKELOOM_STDIO_SCOPES`. stdio is disabled
  in the container image. See [stdio transport](../../../README.md#stdio-transport-local-use).

Ask the assistant to call `get_server_info` first. It reports:
- the versions, and whether OpenMetadata is reachable;
- the enabled features;
- the configured sources and models;
- the caller's identity and scopes.

## 3. Configure

Settings are environment variables prefixed `LAKELOOM_`. Secrets also accept a `_FILE`
variant. Sources and per-model policies go in a YAML file named by `LAKELOOM_CONFIG_FILE`.

### Core

| Variable | Default | Purpose |
|----------|---------|---------|
| `LAKELOOM_ENV` | `development` | `production` enforces the production rules (secrets required, stdio off) |
| `LAKELOOM_PUBLIC_URL` | — | Public base URL. Models' base IRIs are `{public_url}/ontology/{slug}/` |
| `LAKELOOM_OM_URL`, `LAKELOOM_OM_BOT_TOKEN` | — | OpenMetadata and its bot token, used for reads and drafts |
| `LAKELOOM_DB_URL` | `sqlite:////data/lakeloom.db` | State store (use `postgresql+…` in production) |
| `LAKELOOM_DATA_DIR` | `/data` | Git working copies and local data. Never share it between replicas |
| `LAKELOOM_CONFIRM_SECRET` | random in dev | HMAC key of confirm tokens. Required in production |
| `LAKELOOM_LOG_LEVEL`, `LAKELOOM_HOST`, `LAKELOOM_PORT` | `INFO`, `0.0.0.0`, `8000` | Process settings |

### Identity and access

| Variable | Default | Purpose |
|----------|---------|---------|
| `LAKELOOM_OIDC_ISSUER`, `LAKELOOM_OIDC_AUDIENCE` | —, `lakeloom` | Token validation |
| `LAKELOOM_OIDC_JWKS_URL` | discovered | Override the JWKS location |
| `LAKELOOM_OIDC_AGENT_CLAIM` | `client_id` | The claim that marks a token as an agent (agents never review) |
| `LAKELOOM_SCOPE_GROUP_MAP` | `{}` | Map IdP groups to Lakeloom scopes |
| `LAKELOOM_OIDC_TOKEN_EXCHANGE`, `LAKELOOM_OIDC_CLIENT_ID`, `LAKELOOM_OIDC_CLIENT_SECRET`, `LAKELOOM_OIDC_TOKEN_ENDPOINT` | `true` | Exchange a reviewer's token for an OpenMetadata one, so approvals run **as the reviewer** |
| `LAKELOOM_OM_FORWARD_USER_TOKEN` | `false` | Alternative to exchange: forward the reviewer's token |

Scopes are `lakeloom:read`, `propose`, `review`, `publish` and `admin`. Per-model groups live
in `LAKELOOM_CONFIG_FILE`:

```yaml
models:
  - name: retail
    access:
      propose_groups: [retail-stewards]
      review_groups: [retail-reviewers]
      publish_groups: [retail-owners]
    git: {mode: direct}            # optional override
    triplestore: {enabled: false}  # optional per-model opt-out
```

Reviewers also need the OpenMetadata role `LakeloomOntologyReviewer`. Background:
[Review](../../../README.md#review) and
[ADR 0012](../../decisions/0012-review-and-human-identity.md).

### Lake sources

Declared under `sources:` in `LAKELOOM_CONFIG_FILE`. Agents can only name a configured
source. See [Lake sources and scans](../../../README.md#lake-sources-and-scans) for Delta,
Unity Catalog, Snowflake and upstream MCP sources. To check an upstream MCP mapping, run
`lakeloom sources check NAME`.

Privacy defaults:
- profiles stay where the data lives;
- sample values are off (`LAKELOOM_PROFILE_SAMPLES=false`);
- columns matching `LAKELOOM_SENSITIVE_TAGS` (default `PII.*`) never expose values.

### Git publishing and releases

| Variable | Default | Purpose |
|----------|---------|---------|
| `LAKELOOM_GIT_REMOTE` | — | Turns on publishing (never put a token in the URL) |
| `LAKELOOM_GIT_MODE` | `pull_request` | Or `direct` |
| `LAKELOOM_GIT_BRANCH` | `main` | Target branch |
| `LAKELOOM_GIT_TOKEN` or `LAKELOOM_GIT_SSH_KEY_FILE` (+ `_KNOWN_HOSTS_FILE`) | — | Credentials |
| `LAKELOOM_FORGE`, `LAKELOOM_FORGE_API_URL`, `LAKELOOM_GIT_WEB_URL` | `github` | Pull requests and links |

Details: [Git publishing](../../../README.md#git-publishing) and
[Releases](../../../README.md#releases).

### Triple store (optional)

`LAKELOOM_TRIPLESTORE_ENABLED=true` with `LAKELOOM_TRIPLESTORE_URL` (a Graph Store
endpoint). Each release is pushed to these graphs:
- `<base>release/<version>`, with its SKOS view at `<base>release/<version>/skos`
  (`LAKELOOM_TRIPLESTORE_SKOS`, default `true`);
- for the highest release only, also `<base>latest`, `<base>skos` and the shapes at
  `<base>shapes` (`LAKELOOM_TRIPLESTORE_SHAPES`, default `true`). Retry failed pushes with
`lakeloom release sink-retry`.

### Background jobs

| Variable | Default | Job |
|----------|---------|-----|
| `LAKELOOM_RECONCILE_INTERVAL_SECONDS` | `3600` (`0` = off) | Finds edits made directly in OpenMetadata. It only reports, unless `LAKELOOM_RECONCILE_COMMIT=true` |
| `LAKELOOM_DRIFT_INTERVAL_SECONDS` | `0` (off) | Re-scans the tables of each model and reports drift. `get_next_steps` shows the result |
| `LAKELOOM_GIT_RETRY_INTERVAL_SECONDS` | `60` | Retries queued Git publications |

See [Reconciliation and scheduled drift](../../../README.md#reconciliation-and-scheduled-drift).

### Limits

Rate limit: `LAKELOOM_RATE_LIMIT_PER_MINUTE` (300) and `_BURST` (60), per principal and per
replica.

Imports:
- `LAKELOOM_IMPORT_MAX_BYTES`: 20 MB;
- `_MAX_TRIPLES`: 200000;
- `_TIMEOUT_SECONDS`: 60.

SPARQL:
- `LAKELOOM_SPARQL_TIMEOUT_SECONDS`: 30;
- `_MAX_RESULT_BYTES`: 5 MiB;
- `_MAX_QUERY_CHARS`: 20000.

Proposals hold at most `LAKELOOM_MAX_PROPOSAL_OPS` (50) operations per part. Models are
cached for `LAKELOOM_MODEL_CACHE_TTL_SECONDS` (60).

## 4. Run in production

- Use PostgreSQL. Give each replica its own `LAKELOOM_DATA_DIR`. Set
  `LAKELOOM_MCP_STATELESS=true` or use sticky sessions. See
  [Running more than one replica](../../../README.md#running-more-than-one-replica).
- Give the OpenMetadata bot, and any upstream MCP source token, only the rights it needs.
  Reviewers act with their own identity.
- Keep clocks in sync (NTP), because job leases depend on them.

## 5. Observe

- `GET /healthz` reports that the process is up. `GET /readyz` reports that OpenMetadata,
  the database and Git are reachable.
- `GET /metrics` serves Prometheus metrics: tool calls, OpenMetadata latency, scans, Git
  publications, rate limits, reconcile and drift.
- OpenTelemetry starts when `LAKELOOM_OTEL_EXPORTER_OTLP_ENDPOINT` is set.
- The audit log has one record per write and per dry run, both on the `lakeloom.audit` log
  and in the `audit_log` table. Arguments are never recorded.

See [Observability](../../../README.md#observability).

## 6. The CLI

Operators have a CLI for the same operations:

| Area | Commands |
|------|----------|
| Explore | `lakeloom sources`, `lakeloom tables`, `lakeloom scan` |
| Read and export | `lakeloom models`, `lakeloom concept`, `lakeloom context`, `lakeloom export`, `lakeloom validate` |
| Proposals | `lakeloom propose`, `lakeloom proposal`, `lakeloom submit`, `lakeloom rebase`, `lakeloom pending` |
| Releases | `lakeloom release create`, `lakeloom release list`, `lakeloom release sink-retry` |
| Import and drift | `lakeloom import`, `lakeloom import-git`, `lakeloom drift`, `lakeloom sparql` |
| Git and jobs | `lakeloom git status`, `lakeloom git retry`, `lakeloom reconcile` |

There is no CLI review command: review decisions are made by humans through MCP, with their
own identity.
