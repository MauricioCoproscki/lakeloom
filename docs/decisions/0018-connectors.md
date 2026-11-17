# 0018. Snowflake connector, MCP mapping check and Databricks SDK auth

- Status: accepted (M4b, 2026-11-17)
- Date: 2026-11-17
- Requirements: FR-SRC-02, FR-SRC-04, FR-SRC-05, FR-SRC-08, FR-SRC-09, TC-7, NFR-SEC-14, FR-GIT-08

## Context

M4 ends in the first public release. Before that, the lake platforms most users ask about need
a path into Lakeloom: a native Snowflake source (FR-SRC-04), a way to make the Databricks and
Snowflake MCP mapping examples work against a real server (FR-SRC-05), and a real
`databricks` extra (TC-7). The design is
`docs/superpowers/specs/2026-11-16-m4b-connectors-design.md` and the plan is
`docs/superpowers/plans/2026-11-17-m4b-connectors.md`.

The owner has no Snowflake account or Databricks workspace for this work. Everything in M4b is
tested against local fakes, and the MCP examples keep their `<tool-from-tools/list>`
placeholders (owner decision).

## Decision

### Snowflake connector (`sources/snowflake.py`, `type: snowflake`)

- **Model.** A database is a catalog, and the `LakeSource` protocol is unchanged.
  `snowflake-connector-python>=5.1.0` is the `snowflake` extra, imported lazily. A missing
  extra is a configuration error with the hint `pip install 'lakeloom[snowflake]'`.
- **Auth.** Exactly one method:
  - `private_key_file` (key-pair, recommended; optional `private_key_passphrase_env`);
  - `password_env`;
  - `token_env` (OAuth access token, `authenticator=oauth`).

  Secrets are read from the environment when the connection opens. Every session sets
  `QUERY_TAG=lakeloom` and `STATEMENT_TIMEOUT_IN_SECONDS` (`query_timeout_seconds`, default 60).
  The same value bounds login and HTTP requests (`login_timeout`, `request_timeout`). No
  client-side `query_timeout` is set, so a slow statement is never cut short on the client and
  then retried. `client_session_keep_alive` is on. The 5.x parameter names are used:
  `private_key_password`, not the deprecated `private_key_file_pwd`.
- **Reads:**
  - `SHOW TERSE DATABASES`, filtered by the `databases` allow-list;
  - `<db>.INFORMATION_SCHEMA.SCHEMATA`, `TABLES` (`BASE TABLE` and `VIEW`) and `COLUMNS`;
  - `SHOW PRIMARY KEYS` / `SHOW IMPORTED KEYS IN TABLE`, grouped by constraint name and
    ordered by `key_sequence`. Snowflake has no `KEY_COLUMN_USAGE`. Views are not asked: they
    have no keys.
  - The table's version is `committed_at = LAST_ALTERED`, with no version number. The owner is
    `TABLE_OWNER`. The properties are `table_type` and `clustering_key`. `NUMBER` columns carry
    precision and scale (`NUMBER(38,0)`). Names keep the case Snowflake reports.
- **Tags.** One table function, `TAG_REFERENCES_ALL_COLUMNS(<table>, 'table')`, returns both
  levels. A row naming a column is a tag of that column, including tags inherited from the
  table, schema or database, which Snowflake reports per column. A row whose `LEVEL` is not
  `COLUMN` is also a table tag.
  - This replaces the two functions the design listed.
  - A tag renders as `NAME.VALUE` (`NAME` when the value is empty). OpenMetadata maps a
    Snowflake tag to `Classification.Tag`, so the default sensitive glob `PII.*` matches a `PII`
    tag whatever its value. The design said `NAME=VALUE`, which would have missed it.
  - Any `ProgrammingError` on the tag query (missing privilege, edition) leaves the tags
    *unknown*, with a DEBUG log carrying the errno. A describe then reports no tags and still
    succeeds. A **profile fails closed**: every column is treated as sensitive, so no values
    are shown.
  - For sensitivity matching, a tag with an empty value (`PII`) is also matched as `PII.`, so
    the default glob `PII.*` covers it.
  - `ACCOUNT_USAGE` is not used: it lags by up to hours and needs an extra grant.
- **Safety.** The driver's pyformat binding runs `query % params` on the client. So
  `quote_ident` doubles `"` and also `%`. Values are `%s` parameters, which the driver
  interpolates as quoted literals. A statement without parameters is unescaped once
  (`sql % ()`), as the driver would, so the database name `GROWTH_%` works in every statement.
  The test fake runs the driver's own `ClientSideBindingConverter.interpolate_query`. Comments
  are `UntrustedText(source="snowflake:<name>")`.
- **Errors.** No driver message is ever copied into an error; only the `errno` is. The mapping:

  | Driver error | Lakeloom error |
  |---|---|
  | errno 2003 or 2043 (object missing or not authorised) | `not_found` |
  | errno 390100, 390144 or 390318, or a `ProgrammingError` from `connect()` | `forbidden` (the hint names the env var or key file) |
  | `OperationalError`, `InterfaceError` | `upstream_unavailable` "cannot reach Snowflake" |
  | any other driver `Error` | `upstream_unavailable` "Snowflake error N" |

  errno 250001 ("could not connect to backend") is a network failure, not an auth failure.
- **Runtime.** One connection per source, opened on first use, with statements serialised by
  an `asyncio.Lock` and run in `asyncio.to_thread`. The connection is dropped, reopened once and
  the statement retried once after any of:
  - a connection error (`OperationalError`, `InterfaceError`);
  - an expired session (`ReauthenticationRequest`, or errno 390112/390114), which the 5.x
    driver raises as a `ProgrammingError` subclass.
- **Profiling (opt-in, `profiling: true`, needs `warehouse`).** It runs warehouse queries, which
  cost credit:
  - one aggregate statement: row count, null counts, and the min/max the privacy rules allow;
  - one `COUNT(*)` over `SELECT DISTINCT … WHERE c IS NOT NULL LIMIT cap+1` per countable column,
    with `cap = low_cardinality + 1`;
  - a `GROUP BY` for top-k, only when samples are on, the column is not sensitive and its
    distinct count is within the cap.

  Semi-structured and binary types (`VARIANT`, `OBJECT`, `ARRAY`, `BINARY`, `GEOGRAPHY`, …) get
  null rates only.
- **Shared privacy rules** (`sources/privacy.py`, no pyarrow). Sensitive-tag matching, which
  value kinds may show min/max and counts, the withheld reason and top-k ranking moved out of
  `profiling.py`. The Arrow and SQL profilers decide alike, and the Arrow path's behaviour and
  tests are unchanged.

### `lakeloom sources check NAME [--call] [--tools] [--json]` (`sources/mcp_check.py`)

- `McpUpstreamSource.list_upstream_tools()` connects once, reads every page of `tools/list` and
  disconnects. It does not check the mapping.
- **Static check, per mapped operation:**
  - `placeholder`: the tool name is `<…>`;
  - `missing`: the tool is not in `tools/list`;
  - `schema_mismatch`: a mapped argument is not a declared property, or a `required` property is
    not mapped;
  - `ok`: none of the above.

  A schema with no declared properties (no `properties`, or an empty one without
  `additionalProperties: false`) is not checked: many servers publish `{"properties": {}}` for
  tools that take keyword arguments.
- **`--call`.** A second `McpUpstreamSource` is built from the `ok` operations only and started.
  It runs `list_catalogs` → `list_schemas` → `list_tables` → `describe_table`, each step on the
  first item of the one before, through the production adapter code.
  - A step whose input is missing, or whose operation is unmapped or not `ok`, is `skipped`.
  - A mapped but broken `list_catalogs` stops the chain.
  - `get_table_version` and `profile_table` are never called.
- **Other source types** report their availability, plus `list_catalogs` with `--call`.
- **Exit codes:** 0 when `ok`, 3 otherwise (the `reconcile` convention), 1 on an error.
- `lakeloom sources` is now a Typer group. Its bare form prints the same listing as before.
- The guide is `docs/guides/mcp-mappings.md`, and both example headers point to it.

### Databricks SDK authentication for Unity Catalog

- `databricks-sdk>=0.159.0` is the `databricks` extra. With `auth: databricks` (and an optional
  `profile` from `~/.databrickscfg`), the UC connector builds `databricks.sdk.core.Config`. Its
  `host` is the source `url`. Every request merges `Config.authenticate()` headers, run in a
  thread; the SDK caches and refreshes the token.
- That covers OAuth M2M, Azure, Google and CLI profiles, with no Lakeloom code per method. The
  connector itself stays plain httpx (ADR 0008).
- `token_env` and `auth: databricks` are mutually exclusive, and `profile` needs
  `auth: databricks`.
- An SDK failure is `forbidden` "Databricks authentication failed (<ExceptionType>)", with a
  hint naming the profile or the `DATABRICKS_*` environment and the host. The SDK's message is
  never copied, and a 401/403 from UC points at the SDK principal rather than `token_env`.

### R4 probe (ADR 0017): an export never shows a partial change set

`tests/contract/test_om_changeset_atomicity_probe.py` applies one change set of 40 terms while
a second task exports the glossary in a loop. Three runs on OM 2.1.0-rc1 (2026-11-17):

| Exports | Overlapping the apply | Apply time | Term counts seen |
|---|---|---|---|
| 238 | 33 | 0.414 s | {0, 40} |
| 346 | 36 | 0.322 s | {0, 40} |
| 418 | 48 | 0.314 s | {0, 40} |

No export held part of the change set, so the reconciliation assumption holds on this OM
version. The probe stays in the contract suite to catch a regression on an OM upgrade.

### Not verified live

The Snowflake connector, its profiling and both MCP mapping examples were exercised only
against fakes:
- a hand-written DB-API connection that mirrors `snowflake-connector-python`'s surface and
  error classes;
- in-process MCP servers.

The example test with filled-in placeholders proves an example is consistent with itself, not
that it matches a real Databricks or Snowflake MCP server. The Databricks SDK path was tested
with a fake `Config`.

## Consequences

- Snowflake tables scan, propose and drift like UC and Delta tables, with no core change. A scan
  through `ScanService` over the fake source is part of the unit suite.
- An operator with a real MCP server can turn a placeholder example into a working mapping with
  `sources check --tools`, then `check`, then `check --call`. A verified mapping can come back
  as a contribution.
- `pyproject.toml` silences ruff `S608` for `sources/snowflake.py` only: its SQL is built from
  quoted identifiers and bound values. A new f-string there is not flagged by the linter, so
  review has to catch it.
- Follow-ups are in `docs/backlog/m4b-deferred.md`.
