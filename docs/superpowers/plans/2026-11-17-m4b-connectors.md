# M4b — Connectors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A native Snowflake source, a `lakeloom sources check` command that verifies MCP mappings against a live upstream, Databricks SDK authentication for the Unity Catalog source, and the R4 OpenMetadata probe.

**Architecture:** The Snowflake connector implements the unchanged `LakeSource` protocol over a synchronous DB-API connection run in `asyncio.to_thread`. Profiling privacy rules move to a pyarrow-free module that the Arrow and SQL profilers share. The MCP check reuses `McpUpstreamSource` for the connection and the calls, so it exercises the production code paths. `databricks-sdk` is only an auth-header provider for the existing httpx UC connector.

**Tech Stack:** Python 3.12, pydantic v2, Typer, `snowflake-connector-python`, `databricks-sdk`, the `mcp` SDK, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-11-16-m4b-connectors-design.md`

## Global Constraints

- The `LakeSource` protocol (`src/lakeloom/sources/base.py`) and `src/lakeloom/sources/models.py` do not change.
- Nothing may claim live verification: no doc, test name, docstring or CHANGELOG line says Snowflake or the MCP examples were verified against a real account or server.
- The MCP examples keep every `tool: "<tool-from-tools/list>"` placeholder.
- The base install never imports `snowflake.connector` or `databricks.sdk`. They are imported lazily. A missing extra is a `LakeloomError("validation_error", …, hint="pip install 'lakeloom[snowflake]'")` (or `[databricks]`), raised from `build_source`, which the registry turns into a `ConfigError` (the `delta` pattern).
- Extras: `snowflake = ["snowflake-connector-python>=5.1.0"]` and `databricks = ["databricks-sdk>=0.159.0"]`. Add both to the `dev` dependency group too, so unit tests run them.
- No secret (password, key passphrase, OAuth token, Databricks token) in a log, an error message or hint, an exception string or a test snapshot. Hints name the env var or file, never the value. Snowflake driver messages are never copied into errors; only the `errno` is.
- Every SQL identifier goes through `quote_ident`. Every value goes through driver binding (`%s`, pyformat).
- Snowflake comments are `UntrustedText(source="snowflake:<name>")`.
- Unit tests need no network and stay on SQLite.
- Commits: `git commit -s` as Elcio Abrahao <elcio@tuta.com>, conventional titles carrying requirement IDs, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- After each task: `uv run ruff check src tests`, `uv run ruff format --check src tests`, `uv run pyright src tests` and `uv run pytest tests/unit -q` are green.

## Review Focus

1. **Snowflake names that need quoting** (`Order Items`, `a"b`, mixed case `MyTable`, `x;DROP`): the generated SQL quotes them with `"` doubled, and names are returned exactly as Snowflake reports them. Pinned in Task 3 (`test_snowflake_identifiers_are_quoted`).
2. **A connection dropped between calls** (an `OperationalError` on `execute`): the source reconnects once and the call succeeds. A second failure is `upstream_unavailable`. Pinned in Task 3 (`test_snowflake_reconnects_once`).
3. **An empty table, or a VARIANT/OBJECT/ARRAY column, during profiling:** `row_count=0` gives `null_rate=None` and no division error. A semi-structured column gets no min/max, no distinct count and no top-k. Pinned in Task 4 (`test_snowflake_profile_empty_table_and_variant`).
4. **An MCP tool whose `inputSchema` has no `properties`** (`{}` or `{"type": "object"}`): no false `schema_mismatch`. Pinned in Task 6 (`test_check_schema_without_properties_is_ok`).
5. **The Databricks SDK failing while it authenticates** (bad profile, expired OAuth): `forbidden` with a hint naming the profile or the host, and no SDK message text. Pinned in Task 5 (`test_uc_databricks_auth_failure_is_forbidden`).

---

### Task 1: Shared profiling privacy rules

**Files:**
- Create `src/lakeloom/sources/privacy.py` (no pyarrow import).
- Modify `src/lakeloom/sources/profiling.py`: use the helpers; behaviour unchanged.
- Test `tests/unit/sources/test_privacy.py`.

**Interfaces:**
- Produces, in `lakeloom.sources.privacy`:
  - `ValueKind = Literal["number", "temporal", "string", "boolean", "other"]`
  - `is_sensitive(tags: list[str], patterns: list[str]) -> bool`: casefolded `fnmatchcase`, today's `_matches`.
  - `allow_minmax(kind: ValueKind, *, sensitive: bool, policy: ProfilePolicy) -> bool`: True for number and temporal, and for string only when `policy.samples`. Always False when sensitive or for boolean/other.
  - `allow_counts(kind: ValueKind) -> bool`: every kind except `other`.
  - `withheld_reason(*, sensitive: bool, policy: ProfilePolicy) -> Literal["sensitive", "samples_disabled"] | None`
  - `render(value: object) -> str` and `truncate(text: str, limit: int) -> str` (today's `_render` and `_truncate`, with the `…` suffix).
  - `rank_top_k(counts: Mapping[object, int], policy: ProfilePolicy) -> list[tuple[str, int]]`: rendered, sorted by `(-count, rendered)`, cut to `top_k`, values truncated.

- [ ] **Step 1:** Write `test_privacy.py`. It needs these tests:
  - `test_minmax_matrix`: parametrised over kind × sensitive × samples. Expect True only for (number|temporal, not sensitive) and (string, not sensitive, samples on).
  - `test_counts_excludes_other`
  - `test_withheld_reason`: sensitive wins over samples off.
  - `test_is_sensitive_glob_casefold`: `["PII.Email"]` matches `["pii.*"]`.
  - `test_rank_top_k_orders_and_truncates`: `{"bb": 2, "a": 2, "c": 5}`, `top_k=2`, `value_max_length=1` → `[("c", 5), ("a", 2)]`.
- [ ] **Step 2:** Run `uv run pytest tests/unit/sources/test_privacy.py -q` → FAIL (module missing).
- [ ] **Step 3:** Implement `privacy.py`. Refactor `profiling.py`: map Arrow types to `ValueKind` with one `_arrow_kind(dtype)`, and replace `_matches`, `_render`, `_truncate`, the min/max decision and the top-k ranking with the helpers.
- [ ] **Step 4:** Run `uv run pytest tests/unit/sources -q` → PASS, `test_profiling.py` included and unchanged.
- [ ] **Step 5:** Commit `refactor(sources): share profiling privacy rules outside the Arrow profiler (FR-SRC-08, FR-SRC-09)`.

---

### Task 2: Snowflake configuration, extras and registration

**Files:**
- Modify `src/lakeloom/sources/config.py`: `SnowflakeAuth`, `SnowflakeSourceConfig`, `"snowflake"` in `_BUILTIN_TYPES` and the union, `_resolve_relative` for `auth.private_key_file`.
- Modify `src/lakeloom/sources/registry.py`: `"snowflake": "lakeloom.sources.snowflake"`.
- Create `src/lakeloom/sources/snowflake.py`: `build_source` only in this task, raising the missing-extra error, otherwise `NotImplementedError` until Task 3.
- Modify `pyproject.toml` (both extras and the dev group) and `uv.lock` (`uv lock`).
- Test `tests/unit/sources/test_config.py` and `tests/unit/sources/test_registry.py`.

**Interfaces:**
- Produces:
  ```python
  class SnowflakeAuth(_Base):
      private_key_file: Path | None = None
      private_key_passphrase_env: str | None = None
      password_env: str | None = None
      token_env: str | None = None  # OAuth access token
      # validator: exactly one of private_key_file / password_env / token_env;
      # private_key_passphrase_env only with private_key_file


  class SnowflakeSourceConfig(_Base):
      type: Literal["snowflake"]
      name: str
      account: str
      user: str
      role: str | None = None
      warehouse: str | None = None
      databases: list[str] | None = None
      auth: SnowflakeAuth
      profiling: bool = False
      query_timeout_seconds: int = Field(default=60, ge=1, le=3600)
      om_service: str | None = None
      # validator: profiling: true needs warehouse
  ```
- `build_source(config: SnowflakeSourceConfig, settings: Settings) -> SnowflakeSource`

- [ ] **Step 1:** Write the tests:
  - `test_snowflake_config_parses_and_resolves_key_file`: the spec's YAML example. A relative `private_key_file` resolves against the YAML directory.
  - `test_snowflake_auth_needs_exactly_one_method`: parametrised over none, two, and a passphrase without a key → `ConfigError`.
  - `test_snowflake_profiling_needs_warehouse`
  - `test_snowflake_missing_extra_hint`: monkeypatch `importlib.util.find_spec` to return None for `snowflake.connector` → `build_sources` raises a `ConfigError` whose text contains `pip install 'lakeloom[snowflake]'`.
- [ ] **Step 2:** Run them → FAIL.
- [ ] **Step 3:** Implement. `build_source` checks `importlib.util.find_spec("snowflake.connector")` before anything else.
- [ ] **Step 4:** Run `uv run pytest tests/unit/sources -q` → PASS. `uv run python -c "import lakeloom.sources.snowflake, sys; assert 'snowflake.connector' not in sys.modules"` exits 0.
- [ ] **Step 5:** Commit `feat(sources): Snowflake source configuration and the snowflake and databricks extras (FR-SRC-04, TC-7)`.

---

### Task 3: Snowflake connector reads

**Files:**
- Modify `src/lakeloom/sources/snowflake.py`.
- Create `tests/unit/sources/fake_snowflake.py` and `tests/unit/sources/test_snowflake.py`.

**Interfaces:**
- Consumes: `SnowflakeSourceConfig` (Task 2).
- Produces:
  - `quote_ident(name: str) -> str`: `'"' + name.replace('"', '""') + '"'`.
  - `Connect = Callable[[dict[str, Any]], Any]`, a DB-API connection factory.
  - `class SnowflakeSource`, with `kind = "snowflake"` and `__init__(self, config: SnowflakeSourceConfig, *, connect: Connect | None = None)`. The default `connect` imports `snowflake.connector.connect` lazily.
  - The usual `LakeSource` methods and `aclose()`, plus `async def _query(self, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, Any]]`, which Task 4 uses.
  - The connector only uses the DB-API surface: `conn.cursor()`, `cur.execute(sql, params)`, `cur.description` (`item[0]` is the column name), `cur.fetchall()` (tuples), `cur.close()`, `conn.close()`.
- Fake (`fake_snowflake.py`):
  - `FakeConnection(responders: list[tuple[str, Callable[[tuple], list[dict] | Exception]]])`: a regex over whitespace-normalised SQL maps to rows or an exception.
  - `.statements: list[tuple[str, tuple]]` and `.closed: bool`.
  - `make_connect(*conns)` returns a `Connect` that yields the next connection on each call and records the params it got.

**Connection params** (the dict passed to `connect`):
- always: `account`, `user`, `role`, `warehouse` (when set), `login_timeout=query_timeout_seconds`, `network_timeout=query_timeout_seconds`, `session_parameters={"QUERY_TAG": "lakeloom", "STATEMENT_TIMEOUT_IN_SECONDS": query_timeout_seconds}`;
- key-pair: `private_key_file`, plus `private_key_file_pwd` from the env var;
- password: `password`;
- token: `authenticator="oauth"`, `token`.

An env var that is unset or empty is a `forbidden` error at the first query, with the hint "set environment variable <NAME>".

**Queries** (the spec's table, which binds the decisions):
- `list_catalogs`: `SHOW TERSE DATABASES`, column `name`, filtered by `databases`, sorted.
- `list_schemas`: `SELECT SCHEMA_NAME FROM <db>.INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME <> 'INFORMATION_SCHEMA' ORDER BY 1`.
- `list_tables`: `TABLES` where `TABLE_SCHEMA = %s AND TABLE_TYPE IN ('BASE TABLE', 'VIEW')`.
- `describe_table`:
  - `TABLES` row (`TABLE_TYPE, TABLE_OWNER, COMMENT, LAST_ALTERED, CLUSTERING_KEY`); no row → `not_found`;
  - `COLUMNS` ordered by `ORDINAL_POSITION`; `data_type` is `NUMBER(p,s)` when `DATA_TYPE='NUMBER'` and the precision is set, else `DATA_TYPE`;
  - `nullable = IS_NULLABLE == 'YES'`; `position = ORDINAL_POSITION`;
  - `owners = [TABLE_OWNER]` when set;
  - `properties` holds `table_type` and `clustering_key` when set;
  - `version = TableVersion(committed_at=LAST_ALTERED)`.
- Constraints:
  - `SHOW PRIMARY KEYS IN TABLE <fq>` (columns `column_name`, `key_sequence`, `constraint_name`);
  - `SHOW IMPORTED KEYS IN TABLE <fq>` (`fk_column_name`, `pk_database_name`, `pk_schema_name`, `pk_table_name`, `pk_column_name`, `key_sequence`, `fk_name`);
  - grouped by constraint name and ordered by `key_sequence`.
- Tags:
  - one query `SELECT TAG_NAME, TAG_VALUE, LEVEL, COLUMN_NAME FROM TABLE(<db>.INFORMATION_SCHEMA.TAG_REFERENCES_ALL_COLUMNS(%s, 'table'))`, with the bound param `<fq>` text;
  - `LEVEL = 'COLUMN'` gives column tags, any other level gives table tags;
  - a tag renders as `NAME=VALUE`, or `NAME` when the value is empty;
  - any `ProgrammingError` here → no tags, with a DEBUG log carrying the errno only.
  - This is one table function instead of the two the spec listed (it returns both levels); record that in ADR 0018.
- `get_table_version`: the `TABLES` row's `LAST_ALTERED` only.
- Missing catalog → `validation_error` with the hint "pass catalog (call list_catalogs to see them)".

**Errors** (from `snowflake.connector.errors`, imported lazily inside the mapper):
- `ProgrammingError` with errno 2003 or 2043 → `not_found`;
- errno 390100, 390144, 390318 or 250001, or an auth failure at connect → `forbidden`, with a hint naming the env var or key file;
- `OperationalError` or `InterfaceError` → `upstream_unavailable`;
- any other `Error` → `upstream_unavailable`, "Snowflake error <errno>".

**Runtime:**
- one connection, opened on first use under an `asyncio.Lock`, with every call in `asyncio.to_thread`;
- on `OperationalError` or `InterfaceError`, close, reconnect once and retry the statement once.

- [ ] **Step 1:** Write `test_snowflake.py`. It needs these tests:
  - `test_fr_src_04_lists_with_allow_list`: databases `SALES, HR, TMP` with `databases: [SALES, HR]` → `["HR", "SALES"]`.
  - `test_fr_src_04_describe_maps_columns_comments_owner_version`: a comment that holds `INJECTION` text comes back as `UntrustedText` with source `snowflake:sf`. `NUMBER(38,0)`. `nullable` False for `IS_NULLABLE='NO'`. `committed_at` equals `LAST_ALTERED`.
  - `test_fr_src_04_constraints_from_show_keys`: a two-column PK ordered by `key_sequence`, and an FK whose `ref_table` is `TableRef(catalog="SALES", schema="PUBLIC", name="CUSTOMERS")`.
  - `test_fr_src_04_tags_table_and_column`: `PII=EMAIL` on a column and `TIER` on the table. A tags query that raises `ProgrammingError(errno=3001)` → no tags, and the describe still succeeds.
  - `test_snowflake_identifiers_are_quoted` (Review Focus 1): table `a"b` in schema `My Schema` → the recorded statements contain `"My Schema"` and `"a""b"`. Values appear only in params, never in SQL text.
  - `test_snowflake_missing_table_is_not_found`
  - `test_snowflake_reconnects_once` (Review Focus 2)
  - `test_snowflake_errors_never_echo_driver_text_or_secrets`: the driver error message contains `s3cr3t`. The `LakeloomError` `message`/`hint`/`str()` and the caplog text do not, and the env var value is not in them either.
  - `test_snowflake_connect_params_per_auth`: parametrised over key-pair, password and token.
  - `test_snowflake_scan_end_to_end`: a `ScanService` over a `SnowflakeSource` on the fake (the repo fixture as in `tests/unit/core/test_scans.py`) → a `ScanRecord` with the table and no errors.
- [ ] **Step 2:** Run `uv run pytest tests/unit/sources/test_snowflake.py -q` → FAIL.
- [ ] **Step 3:** Implement the reads, the errors and the runtime as above.
- [ ] **Step 4:** Run `uv run pytest tests/unit -q` → PASS.
- [ ] **Step 5:** Commit `feat(sources): native Snowflake connector over INFORMATION_SCHEMA and SHOW KEYS (FR-SRC-04, NFR-SEC-14)`.

---

### Task 4: Snowflake profiling (opt-in)

**Files:**
- Modify `src/lakeloom/sources/snowflake.py`.
- Test `tests/unit/sources/test_snowflake.py`.

**Interfaces:**
- Consumes: `privacy.*` (Task 1), `SnowflakeSource._query` and `describe_table` (Task 3).
- Produces: `SnowflakeSource.profile_table(ref, policy) -> TableProfile`, and `snowflake_kind(data_type: str) -> ValueKind`. The mapping: `NUMBER`, `DECIMAL`, `INT…` and `FLOAT…` → number; `DATE`, `TIME` and `TIMESTAMP…` → temporal; `TEXT`/`VARCHAR` → string; `BOOLEAN` → boolean; anything else → other.

**Queries:**
- One aggregate query: `COUNT(*)`, and per column `COUNT_IF(c IS NULL)`, plus `MIN(c)`/`MAX(c)` only where `allow_minmax` holds.
- Per column where `allow_counts` holds: `SELECT COUNT(*) FROM (SELECT DISTINCT c FROM t WHERE c IS NOT NULL LIMIT cap+1)`, with `cap = low_cardinality + 1`. `distinct_count` is that count when `<= cap`, else None.
- Top-k only when `withheld_reason` is None and the distinct count is not None: `SELECT c, COUNT(*) FROM t WHERE c IS NOT NULL GROUP BY c`, then `rank_top_k`.
- min/max are rendered and, for strings, truncated.
- `profiling: false` → `validation_error`, hint "set profiling: true on the source (it runs warehouse queries)".

- [ ] **Step 1:** Write the tests:
  - `test_fr_src_09_snowflake_profile_privacy`: a `PII.*` tagged column → no min/max/top-k, `values_withheld="sensitive"`. With samples off, a string column has no min/max. A number column has min/max. The recorded SQL has no `MIN(` for the sensitive column.
  - `test_snowflake_profile_high_cardinality`: a distinct count of `cap+1` → `distinct_count=None`, no top-k.
  - `test_snowflake_profile_empty_table_and_variant` (Review Focus 3)
  - `test_snowflake_profiling_disabled_is_validation_error`
- [ ] **Step 2:** Run them → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest tests/unit/sources -q` → PASS.
- [ ] **Step 5:** Commit `feat(sources): opt-in Snowflake profiling in the warehouse with the shared privacy rules (FR-SRC-08, FR-SRC-09)`.

---

### Task 5: Databricks SDK authentication for Unity Catalog

**Files:**
- Modify `src/lakeloom/sources/config.py` (`UnityCatalogSourceConfig`) and `src/lakeloom/sources/unity_catalog.py`.
- Test `tests/unit/sources/test_unity_catalog.py` and `tests/unit/sources/test_config.py`.

**Interfaces:**
- `UnityCatalogSourceConfig` gains:
  - `auth: Literal["token", "databricks"] = "token"` and `profile: str | None = None`;
  - a validator: `databricks` forbids `token_env`, and `profile` needs `databricks`.
- `UnityCatalogSource.__init__` gains `databricks_config: Callable[[str, str | None], Any] | None = None`. It is a factory `(host, profile) -> object with .authenticate() -> dict[str, str]`. The default lazily builds `databricks.sdk.core.Config(host=host, profile=profile)`.
- With `auth: databricks`:
  - `_get` merges `await asyncio.to_thread(cfg.authenticate)` into the headers on every request; the SDK caches and refreshes tokens;
  - any exception from `Config(...)` or `authenticate()` → `forbidden`, "Databricks authentication failed (<ExceptionType>)", hint "check the Databricks profile <profile> or the DATABRICKS_* environment for host <url>".
- The missing extra is checked in `build_source` when `auth == "databricks"`: `find_spec("databricks.sdk")` → hint `pip install 'lakeloom[databricks]'`.

- [ ] **Step 1:** Write the tests:
  - `test_uc_databricks_auth_sends_sdk_headers`: respx asserts `Authorization: Bearer from-sdk` from a fake config.
  - `test_uc_databricks_auth_failure_is_forbidden` (Review Focus 5): the fake raises `ValueError("token abc123 expired")` → `forbidden`, and `abc123` is not in any message.
  - `test_uc_auth_config_validation`
  - `test_uc_databricks_missing_extra_hint`
  - The existing UC tests stay green unchanged.
- [ ] **Step 2:** Run them → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest tests/unit/sources -q` → PASS.
- [ ] **Step 5:** Commit `feat(sources): Databricks SDK authentication for the Unity Catalog source (FR-SRC-02, TC-7)`.

---

### Task 6: `lakeloom sources check` and the mapping guide

**Files:**
- Create `src/lakeloom/sources/mcp_check.py`, `docs/guides/mcp-mappings.md` and `tests/unit/sources/test_mcp_check.py`.
- Modify:
  - `src/lakeloom/sources/mcp_adapter.py` (`list_upstream_tools`);
  - `src/lakeloom/cli.py` (the `sources` group);
  - `tests/unit/sources/fake_upstream.py` (configurable tools);
  - `tests/unit/test_cli.py`;
  - the headers of both `examples/mcp-mappings/*.example.yaml` (one line: `# See docs/guides/mcp-mappings.md and run: lakeloom sources check <name> --tools`).

**Interfaces:**
- `McpUpstreamSource.list_upstream_tools(self) -> dict[str, Tool]`:
  - reads the token (`_token`), opens its own session through `self._factory` and lists every page of `tools/list` under `connect_timeout`, then closes;
  - it does not check the mapping;
  - errors → `upstream_unavailable` as in `start()`.
- In `lakeloom.sources.mcp_check`:
  ```python
  CheckStatus = Literal["ok", "placeholder", "missing", "schema_mismatch"]
  class OperationCheck(BaseModel): operation: OperationName; tool: str; status: CheckStatus; problems: list[str] = []
  class CallCheck(BaseModel): operation: OperationName; status: Literal["ok", "error", "skipped"]; detail: str | None = None
  class ToolInfo(BaseModel): name: str; description: str | None; input_schema: dict[str, Any]
  class CheckReport(BaseModel):
      source: str; kind: str; connected: bool; error: str | None = None
      operations: list[OperationCheck] = []; calls: list[CallCheck] = []; tools: list[ToolInfo] = []
      @property
      def ok(self) -> bool  # connected, every operation ok, no call "error"
  def static_checks(mapping: McpMapping, tools: Mapping[str, Tool]) -> list[OperationCheck]
  async def check_mcp_source(config: McpSourceConfig, mapping: McpMapping, *, call: bool = False,
                             include_tools: bool = False,
                             session_factory: SessionFactory = default_session_factory) -> CheckReport
  ```

**Rules:**
- A tool name matching `^<.*>$` → `placeholder`. A tool absent from the tools → `missing`.
- `schema_mismatch` only when the `inputSchema` has a `properties` object:
  - a mapping argument key not in it gives the problem "argument '<k>' is not in the tool's input schema";
  - a `required` entry not among the argument keys gives "required argument '<k>' is not mapped".
- With `call=True`:
  - build a second `McpUpstreamSource` whose mapping holds only the `ok` operations, then `start()` it;
  - run `list_catalogs` → `list_schemas(first catalog or None)` → `list_tables(first schema)` → `describe_table(first table)`;
  - an operation that is unmapped or not `ok`, or that has no input from the previous step, is `skipped` with a reason;
  - a `LakeloomError` → `error`, with detail `"<code>: <message> (hint: <hint>)"`;
  - `get_table_version` and `profile_table` are never called.
- A connection failure → `connected=False`, `error` set, and no operations.
- `ToolInfo.description` is made single-line in plain output only.

**CLI:**
- `sources_app = typer.Typer(invoke_without_command=True)`. Its callback prints today's listing when `ctx.invoked_subcommand is None`. Register it with `app.add_typer(sources_app, name="sources")`.
- `lakeloom sources check NAME [--call] [--tools] [--json]`:
  - the source comes from the same file `load_lake_sources` reads;
  - an unknown name → `not_found`, exit 1;
  - an MCP source → `check_mcp_source` with `load_mapping(config.mapping)`;
  - another type → availability through `_open_sources`, plus `list_catalogs` as a `CallCheck` with `--call`;
  - plain output is one line per operation, `operation<TAB>tool<TAB>status<TAB>problems`, then the calls and the tools;
  - exit 0 when `report.ok`, else 3.

- [ ] **Step 1:** Extend `fake_upstream.py` with `ToolsFake(tools: dict[str, Callable])`, which serves arbitrary tools with the `MCPServer` decorators. Its input schemas follow the function signatures.
- [ ] **Step 2:** Write `test_mcp_check.py`. It needs these tests:
  - `test_check_reports_ok_missing_and_mismatch`: one operation each for `ok`, `missing` and `schema_mismatch` (an extra argument, and a required argument left out).
  - `test_check_schema_without_properties_is_ok` (Review Focus 4)
  - `test_examples_report_placeholders`: parametrised over both example files, loaded as shipped → every operation is `placeholder`, and `report.ok` is False.
  - `test_examples_are_internally_consistent_with_call`: parametrised over both examples. The placeholders are replaced by `fake_<operation>`. A `ToolsFake` serves tools whose arguments and results match that example's argument names and JMESPath → every operation is `ok`, every call is `ok`, and `describe_table` returns columns. The docstring states that this checks consistency, not a real server.
  - `test_check_call_skips_after_empty_list`: no catalogs → the rest are skipped.
  - `test_check_connection_failure`
  - `test_check_never_prints_token`: run through the CLI with `token_env` set to `tok-xyz` → `tok-xyz` is in neither stdout nor stderr.
- [ ] **Step 3:** In `tests/unit/test_cli.py`, write:
  - `test_cli_sources_bare_still_lists`: the output of the existing `test_cli_sources_and_tables` is unchanged.
  - `test_cli_sources_check_exit_codes`: 0 when ok, 3 for a placeholder mapping, 1 for an unknown name. The `--json` output parses as `CheckReport`.
- [ ] **Step 4:** Run `uv run pytest tests/unit/sources/test_mcp_check.py tests/unit/test_cli.py -q` → FAIL.
- [ ] **Step 5:** Implement `list_upstream_tools`, `mcp_check.py` and the CLI group.
- [ ] **Step 6:** Run `uv run pytest tests/unit -q` → PASS.
- [ ] **Step 7:** Write `docs/guides/mcp-mappings.md`:
  - the mapping format, with a link to ADR 0008;
  - the loop `sources check NAME --tools` → edit the mapping → `sources check NAME` → `sources check NAME --call`;
  - a worked example with the status lines;
  - what each status means;
  - that the shipped examples are not verified against real servers;
  - how to contribute a verified mapping.
- [ ] **Step 8:** Commit `feat(cli): lakeloom sources check verifies MCP mappings against tools/list (FR-SRC-05)`.

---

### Task 7: R4 probe — is a change set ever half-visible in an export?

**Files:**
- Create `tests/contract/test_om_changeset_atomicity_probe.py`, modelled on `tests/contract/test_om_direct_edit_probe.py` (same fixtures style; a throwaway glossary `llm4b_probe_<hex>`, hard-deleted in teardown even on failure, never sealed).

**Steps:**
- [ ] **Step 1:** Write `test_r4_export_never_shows_a_partial_change_set`:
  - in the probe glossary, build a change set of 20 `AddConcept` terms (`to_om_all`);
  - start a task that calls `client.export_ontology(glossary_id)` in a loop and records, per export, how many of the 20 terms it contains;
  - `apply_change_set`, then let the loop run one more second and stop it;
  - assert every recorded count is 0 or 20, and that at least one export ran while the apply was in flight (record the timestamps).
- [ ] **Step 2:** Run `uv run pytest -m contract tests/contract/test_om_changeset_atomicity_probe.py -v` against the running stack.
  - If it passes, the R4 assumption holds on OM 2.1.0-rc1.
  - If it fails, change the assertion to pin the observed behaviour (the test keeps passing), and record the finding for Task 8.
- [ ] **Step 3:** Commit `test(contract): probe whether an OM export can show a partial change set (FR-GIT-08)`.

---

### Task 8: M4b wrap-up docs and full verification

**Files:**
- Create `docs/decisions/0018-connectors.md` (from `0000-template.md`, status accepted) and `docs/backlog/m4b-deferred.md`.
- Modify `README.md`, `CHANGELOG.md` (`[Unreleased]` → Added), `docs/backlog/m4a-deferred.md` (strike the R4 item, pointing to ADR 0018) and `docs/decisions/0017-jobs-limits-reconciliation.md` (the R4 note points to the 0018 result).

**Steps:**
- [ ] **Step 1: ADR 0018.**
  - Each call from the spec's "Calls made by Claude" section.
  - The single tag table function.
  - The error-code mapping.
  - The reconnect-once rule.
  - That the check reuses the adapter.
  - The SDK as auth only.
  - The R4 result, with the counts and timing observed.
  - A **"Not verified live"** section: Snowflake and both MCP examples were exercised against fakes only.
- [ ] **Step 2: README.**
  - The Snowflake source YAML, with key-pair recommended.
  - The minimal Snowflake grants: `USAGE` on warehouse, database and schema; `SELECT` on tables, only for profiling; `REFERENCES` or ownership for `SHOW … KEYS` on tables not owned; `APPLY TAG` not needed.
  - `auth: databricks`.
  - The extras table.
  - `lakeloom sources check`, with a link to the guide.
  - The not-verified-live statement.
- [ ] **Step 3: CHANGELOG lines** for each feature.
- [ ] **Step 4: `m4b-deferred.md`.** At least:
  - live verification of the Snowflake connector and of both MCP examples, once accounts exist;
  - `ACCOUNT_USAGE` as a cheaper bulk path for large accounts;
  - Snowflake external and Iceberg table metadata;
  - `sources check --call` for `get_table_version` and `profile_table`;
  - plus anything found during the tasks.
- [ ] **Step 5: Full verification.**
  - `docker compose -f deploy/compose/docker-compose.yml up -d --build --wait lakeloom` (never `down -v`);
  - `uv run pytest tests/unit -q`;
  - `uv run pytest -m contract -q`;
  - `uv run ruff check src tests`, `uv run ruff format --check src tests` and `uv run pyright src tests`, all green.
  - Confirm the base install does not pull the extras: `uv run --no-dev --isolated --with . python -c "import lakeloom.cli"` succeeds without `snowflake` or `databricks` installed.
- [ ] **Step 6:** Commit `docs: M4b wrap-up — ADR 0018, Snowflake and sources check docs, R4 result (FR-SRC-04, FR-SRC-05, TC-7)`.

---

## Out of scope for M4b

- Real tool names in the MCP examples; live Snowflake or Databricks verification.
- A Snowflake emulator in compose.
- Rewriting the UC connector on `databricks-sdk`.
- Everything listed for M4c.
