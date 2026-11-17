# M4b deferred follow-ups

Findings from the M4b work on the Snowflake connector, the MCP mapping check and Databricks
SDK authentication, deliberately deferred. Triage when planning M4c. Decisions and known
limits: [ADR 0018](../decisions/0018-connectors.md).

## Live verification (needs accounts)

- The Snowflake connector and its profiling against a real account:
  - the column shapes of `SHOW … KEYS`;
  - `TAG_REFERENCES_ALL_COLUMNS` on Standard edition;
  - the errno values of auth failures;
  - the key-pair parameters on `snowflake-connector-python` 5.x;
  - that a view is rejected by `SHOW … KEYS IN TABLE` (views now skip it);
  - the errno of an expired session (390112/390114 assumed, plus `ReauthenticationRequest`);
  - `request_timeout` alone, with no client `query_timeout`, under a slow statement.
- Both MCP mapping examples against the real Databricks and Snowflake MCP servers, with
  `lakeloom sources check --call`. They keep their placeholders until then.
- `auth: databricks` against a workspace (OAuth M2M and a CLI profile).

## Snowflake

- `ACCOUNT_USAGE` as a cheaper bulk path for large accounts (one query per account instead of
  per table). It lags by up to hours and needs an extra grant.
- External, Iceberg and dynamic table metadata (stage location, refresh mode).
- `describe_table` runs five statements per table (table, columns, tags and two `SHOW … KEYS`).
  A scan of a large schema is sequential on one connection.
- Without a `databases` allow-list, `list_catalogs` includes the shared `SNOWFLAKE` database
  and every share the role can see.
- A view's `LAST_ALTERED` changes on DDL only, not when its base tables change, so drift by
  version misses data-level changes behind a view (by design for metadata, but worth stating).
- The connection is closed on `aclose()` only. A long-idle server keeps one session open per
  source until Snowflake's own idle timeout.

## Final review minors (M4b)

- `CheckReport.ok` is a property, so `--json` carries no `ok` field. Consumers use the exit code.
- The guide and ADR give exit code 1 for errors. A configuration error (a missing mapping file,
  for example) exits 2, and an unreachable MCP source exits 3.
- If a task is cancelled while `to_thread(connect)` runs, the connection the thread opens is
  never closed. A cancelled query also releases the lock while its thread still uses the
  connection. The driver's threadsafety is 2, so this is not a correctness bug.
- Profiling runs 1 + up to 2N statements one after another, holding the source lock for each.
  Other callers on that source wait. The capped `DISTINCT` subqueries could be combined.
- The TABLES row count is not read (`TableDescription` has no field for it).

## MCP mapping check

- `sources check --call` does not run `get_table_version` or `profile_table`.
- The schema check compares argument *names* only, not types or enum values.
- `--tools` prints every tool. A filter (`--tools PATTERN`) would help on servers with many
  tools.

## Databricks

- The SDK's `Config` is created on first use and kept for the life of the source. A changed
  `~/.databrickscfg` profile needs a restart.
