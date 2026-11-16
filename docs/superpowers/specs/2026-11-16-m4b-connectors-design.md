# M4b — connectors: design

Status: draft for owner review (2026-11-16). Requirements: FR-SRC-04, FR-SRC-05, TC-7.
Context: [ADR 0008](../../decisions/0008-lake-sources-design.md) (lake sources), the M4 split in
[the M4a plan](../plans/2026-11-16-m4a-jobs-limits-reconciliation.md), and
[the M4a backlog](../../backlog/m4a-deferred.md).

## Intent

M4 ends in the first public release (M4c). Before that, the two lake platforms most users
will ask about, Snowflake and Databricks, need a credible path into Lakeloom:

- **Snowflake** gets a native connector (FR-SRC-04), the same way Unity Catalog and Delta
  have one, so scans, proposals and drift work on Snowflake tables with no core change.
- **Databricks and Snowflake MCP servers** can be reused through the upstream MCP adapter
  (FR-SRC-05). The shipped mappings stay *examples to verify*; what M4b adds is the tooling
  that makes verifying them a five-minute job for an operator who has the server.
- **The `databricks` extra** (TC-7) gets a real reason to exist.

## Owner decisions

- **No live Snowflake or Databricks accounts.** Everything is tested against local fakes.
  Nothing in M4b may claim it was verified against a real Snowflake account or a real
  Databricks/Snowflake MCP server.
- **The MCP example mappings keep their `<tool-from-tools/list>` placeholders.** M4b does
  not research or guess the real tool names. Effort goes into the verification tooling and
  the guide.

## Goals

1. `type: snowflake` source: list databases, schemas and tables; describe a table with
   comments, column types and nullability, tags, owner, primary and foreign keys; report a
   table's last change time; optionally profile a table in the warehouse.
2. `lakeloom sources check <name>`: for an MCP source, connect, run `tools/list`, and
   report per mapped operation whether the tool exists and whether the mapping's arguments
   fit the tool's input schema; with `--call`, run the read operations once and check that
   the JMESPath results produce valid rows. `--tools` dumps `tools/list` (names, descriptions,
   input schemas) so the operator can fill the placeholders.
3. A guide, `docs/guides/mcp-mappings.md`: how to go from a placeholder example to a
   working mapping with `sources check`.
4. `lakeloom[databricks]` declares `databricks-sdk` and the Unity Catalog connector uses it,
   when asked, for Databricks authentication.
5. The R4 probe from the M4a backlog, against the local OpenMetadata.

## Non-goals

- Filling the MCP examples with real tool names, or testing them against real servers.
- A Snowflake emulator in the compose stack (no `fakesnow`, no LocalStack).
- Rewriting the Unity Catalog connector on `databricks-sdk`; it stays plain httpx
  (ADR 0008) and the SDK is only an authentication provider.
- Snowflake writes of any kind, Snowflake `ACCOUNT_USAGE` history (latency of up to hours
  and an extra grant), external or Iceberg-specific table metadata, dynamic tables.
- Anything listed for M4c (Helm, docs site, release, security review).

## Anti-goals (would make M4b a failure even if tests pass)

- Any doc, README line, CHANGELOG entry or test name that suggests Snowflake or the MCP
  examples were verified live.
- A Snowflake credential in a log, an error message, an exception string or a test
  snapshot (NFR-SEC rules as for the UC token).
- SQL built by pasting names into strings without quoting: a database, schema, table or
  column name containing `"` or `;` must not change the query.
- The base install (`pip install lakeloom`) importing `snowflake.connector` or
  `databricks.sdk`.
- A profile query that returns raw values the privacy policy forbids (FR-SRC-09).

## Constraints

- The `LakeSource` protocol (`src/lakeloom/sources/base.py`) and the models in
  `sources/models.py` do not change. Snowflake maps onto them: catalog = database.
- New built-in source types are registered in `sources/registry.py` (`_BUILTIN_MODULES`)
  and `sources/config.py` (`_BUILTIN_TYPES`, the discriminated union), like the others.
- Optional dependencies are imported lazily; a missing extra is a `ConfigError` with the hint
  `pip install 'lakeloom[snowflake]'` (the `delta` pattern).
- Comments read from Snowflake are `UntrustedText` with source `snowflake:<name>`.
- Unit tests stay on SQLite and need no network (DB default memory).
- Repo conventions: commits signed off by the personal identity, conventional commit titles
  carrying requirement IDs, an ADR for the milestone (`0018`), a `m4b-deferred.md` backlog.

## The Snowflake connector

### Configuration

```yaml
- type: snowflake
  name: sf_prod
  account: myorg-myaccount          # Snowflake account identifier
  user: LAKELOOM_SVC
  role: LAKELOOM_READER             # optional
  warehouse: LAKELOOM_WH            # needed only for profiling
  databases: [SALES, MARKETING]     # optional allow-list, like UC `catalogs`
  auth:                             # exactly one method
    private_key_file: /run/secrets/sf_key.p8   # key-pair (recommended)
    private_key_passphrase_env: SF_KEY_PASS    # optional
    # or: password_env: SF_PASSWORD
    # or: token_env: SF_OAUTH_TOKEN            # OAuth access token
  profiling: false                  # opt in: profiling runs warehouse queries (cost)
  query_timeout_seconds: 60
  om_service: snowflake_prod        # OpenMetadata service for FQN lookup (FR-SRC-07)
```

Secrets are referenced by environment-variable name or file path only, as for UC and MCP
sources. Relative `private_key_file` paths resolve against the sources YAML directory.

### Reads

| Operation | Query |
|---|---|
| `list_catalogs` | `SHOW TERSE DATABASES`, filtered by `databases` |
| `list_schemas(db)` | `<db>.INFORMATION_SCHEMA.SCHEMATA`, excluding `INFORMATION_SCHEMA` |
| `list_tables(db, schema)` | `<db>.INFORMATION_SCHEMA.TABLES` where `TABLE_SCHEMA = ?` and type is `BASE TABLE` or `VIEW` |
| `describe_table` | `TABLES` (comment, owner, `LAST_ALTERED`, row count, clustering key) + `COLUMNS` (name, type, nullable, comment, ordinal) |
| constraints | `SHOW PRIMARY KEYS IN TABLE …` and `SHOW IMPORTED KEYS IN TABLE …` (Snowflake has no `KEY_COLUMN_USAGE`) |
| tags | `TABLE(<db>.INFORMATION_SCHEMA.TAG_REFERENCES_ALL_COLUMNS(?, 'table'))` and `TAG_REFERENCES(?, 'table')`; a tag is rendered `TAG_NAME=TAG_VALUE` (or `TAG_NAME` when the value is empty). Missing privilege or edition → no tags, not a failure. |
| `get_table_version` | `TableVersion(version=None, committed_at=LAST_ALTERED)` |

- Catalog is required. Without it the call is a `validation_error` with the UC-style hint.
- Identifiers are always passed through one quoting helper (`"` + name with `"` doubled +
  `"`). Values go through driver parameter binding. The `SHOW … IN TABLE` statements take the
  quoted, fully qualified name.
- Names are returned as Snowflake reports them (usually upper case); no case folding.
- Partition columns: none (Snowflake has none). The clustering key goes into
  `properties["clustering_key"]`.
- Capabilities: `constraints=True`, `versions=True` (time only), `profiling=config.profiling`.

### Profiling (opt-in)

When `profiling: true`, `profile_table` runs aggregate SQL in the warehouse and applies the
same privacy rules as `profile_arrow_dataset` (ADR 0008):

- one query for row count, and per column `COUNT_IF(col IS NULL)` and
  `COUNT(DISTINCT col)` capped at `low_cardinality + 1` through an inner `LIMIT` subquery;
- min/max only where the Arrow path allows it (numeric, date and time types; strings only
  with `samples` on), and never for sensitive columns;
- top-k only with `samples` on, for non-sensitive columns at or below `low_cardinality`
  distinct values;
- rendered values truncated to `value_max_length`.

The privacy decisions (sensitive tag match, which aggregates are allowed per type) are moved
out of `profiling.py` into small shared helpers, so the Arrow path and the SQL path cannot
drift. The Arrow path's behaviour does not change.

### Runtime

- `snowflake-connector-python` is synchronous. Every query runs in `asyncio.to_thread`, on
  one connection per source, opened on first use, serialised by an `asyncio.Lock`, and
  reopened once after a connection-level error.
- Errors map as UC's do: auth failure → `forbidden` (hint names the env var or key file, never
  the value); object missing or not authorised → `not_found`; network or timeout →
  `upstream_unavailable`; anything else → `upstream_unavailable` with the Snowflake error code
  only (no SQL text, no message text that could echo a value).
- `aclose()` closes the connection.

### Tests

- A hand-written fake DB-API connection (`tests/unit/sources/fake_snowflake.py`) answers by
  matching the normalised SQL and parameters, returns rows the way the real driver does
  (`DictCursor` shape), and can raise the driver's error classes. It also records every
  statement so tests can assert quoting and binding.
- Unit tests cover each operation, the allow-list, constraints and tags, injection-shaped
  names, privacy rules for profiling, error mapping, secret redaction, and the missing-extra
  hint.
- A scan of the fake source through the existing `ScanService` runs end to end, proving no
  core change was needed.

## MCP mapping verification

### `lakeloom sources check <name> [--call] [--tools] [--json]`

- Works for `type: mcp` sources. For other types it reports availability (as `lakeloom
  sources` does) and runs `list_catalogs` with `--call`.
- Static checks, per operation in the mapping:
  - the tool name is still a placeholder (`<…>`) → `placeholder`;
  - the tool is not in `tools/list` → `missing`;
  - a mapping argument is not a property of the tool's `inputSchema`, or a required
    property has no mapping argument → `schema_mismatch`, naming the arguments;
  - otherwise `ok`.
- With `--call`: runs `list_catalogs`, then `list_schemas` on the first catalog, `list_tables`
  on the first schema and `describe_table` on the first table, through the normal adapter,
  so the JMESPath and validation code is the code that runs in production. Each step reports
  `ok`, or the adapter's error (code, message, hint). Write-shaped or unmapped operations
  are never called.
- `--tools` prints every tool's name, description and input schema (untrusted text, made
  single-line in plain output).
- Exit code 0 when every mapped operation is `ok`, 3 otherwise (the `reconcile` convention).
- Nothing it prints contains the token.

### Tests

- The existing fake upstream (`tests/unit/sources/fake_upstream.py`) is extended to serve
  configurable tools with input schemas.
- Each example mapping is loaded with its placeholders and `sources check` reports
  `placeholder` for every operation (this keeps the examples parseable and honest).
- A copy of each example with the placeholders replaced by fake tool names, served by the fake
  upstream with result shapes that match the example's JMESPath, passes `check --call`. This
  proves the examples are internally consistent, not that they match a real server; the test
  docstrings and the guide say so.

### Guide

`docs/guides/mcp-mappings.md`: what the mapping format is (link to ADR 0008), the
`--tools` → edit → `check` → `check --call` loop, a worked example on the fake server, and a
"report your verified mapping" note for contributors. The two example headers gain a line
pointing to the guide.

## The `databricks` extra

- `databricks = ["databricks-sdk>=<current>"]` in `pyproject.toml`.
- `UnityCatalogSourceConfig` gains `auth: databricks` (default stays the current bearer
  `token_env`). With `auth: databricks`, the connector builds a `databricks.sdk.core.Config`
  from the source's `url` as host, plus an optional `profile` (from `~/.databrickscfg`), and
  takes the request headers from its credentials provider on each request. That gives
  service-principal OAuth (M2M), Azure and Google auth and CLI profiles, with no Lakeloom code
  per method.
- `token_env` and `auth: databricks` are mutually exclusive.
- Without the extra, `auth: databricks` is a `ConfigError` with the `pip install
  'lakeloom[databricks]'` hint.
- Tests: the SDK `Config` is replaced by a fake that returns headers; no network.

## R4 probe

A contract test against the local OpenMetadata, like `test_om_direct_edit_probe.py`: apply a
multi-term change set while exporting the glossary in a loop, and record whether any export
shows part of the change set. The result goes in ADR 0018; if a half-applied export is
seen, reconciliation's assumption in ADR 0017 is reopened as a backlog item (no fix in M4b).

## Docs

- ADR 0018 `connectors`: the Snowflake design choices, the check command, the SDK auth
  choice, the R4 result.
- README: the Snowflake source, `sources check`, the extras table, the not-verified-live
  statement. CHANGELOG. `REQUIREMENTS.md` stays as is unless a setting is added (none
  planned: everything is per-source YAML).
- `m4b-deferred.md`.

## Calls made by Claude (owner may overrule)

- Snowflake auth by key-pair, password or OAuth token; key-pair documented as recommended.
- Snowflake profiling exists but is opt-in, because it spends warehouse credit.
- Tags via the `INFORMATION_SCHEMA` table functions, not `ACCOUNT_USAGE` (real time, fewer
  grants); missing privilege or edition degrades to no tags.
- A fake DB-API connection rather than `fakesnow` or another emulator.
- `databricks-sdk` is used only as an authentication provider for the existing UC connector.
- The R4 probe is in scope.
- `sources check` exit code 3 for "not ok", matching `lakeloom reconcile`.
- `lakeloom sources` becomes a Typer group whose bare form (`invoke_without_command`) keeps
  today's listing, so `lakeloom sources check` sits beside it without breaking scripts.

## Left to the builder

- Task split and order; the exact SQL text; fake connection internals.
- How the privacy helpers are factored out of `profiling.py`, as long as the Arrow path's
  tests pass unchanged.
- The minimum versions of `snowflake-connector-python` and `databricks-sdk` (current
  releases at least two weeks old).
