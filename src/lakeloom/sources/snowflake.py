"""Snowflake connector over ``INFORMATION_SCHEMA`` and ``SHOW … KEYS`` (FR-SRC-04).

``snowflake-connector-python`` is the optional ``snowflake`` extra and is imported lazily,
so the base install never loads it. A database is a catalog. Reads (ADR 0018):

* ``SHOW TERSE DATABASES``, filtered by the ``databases`` allow-list;
* ``<db>.INFORMATION_SCHEMA.SCHEMATA``, ``TABLES`` and ``COLUMNS``;
* ``SHOW PRIMARY KEYS`` / ``SHOW IMPORTED KEYS IN TABLE`` (Snowflake has no
  ``KEY_COLUMN_USAGE``);
* tags from ``TAG_REFERENCES_ALL_COLUMNS(<table>, 'table')``, which returns table and column
  levels (inherited tags arrive as rows for each column they apply to); without the privilege
  or the edition for it a table has no tags in a describe, and a profile treats every column
  as sensitive (it fails closed).

Every identifier goes through :func:`quote_ident` (``"`` doubled, and ``%`` doubled because
the driver's pyformat binding runs ``query % params`` on the client) and every value is a
bound parameter, so a name cannot change a statement. Statements without parameters are
unescaped once (``sql % ()``), as the driver would. The driver is synchronous: statements run
in a thread on one connection per source, serialised by a lock, reconnecting once after a
lost connection or an expired session (``ReauthenticationRequest``).
Driver messages are never copied into errors (they can echo values); only the ``errno`` is.
Comments are untrusted text (NFR-SEC-14). Credentials are read from the environment when the
connection opens and never logged.
"""

import asyncio
import importlib.util
import logging
import os
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

from lakeloom.config import Settings
from lakeloom.errors import LakeloomError
from lakeloom.sources.base import ProfilePolicy, SourceCapabilities
from lakeloom.sources.config import SnowflakeSourceConfig
from lakeloom.sources.models import (
    ColumnInfo,
    ColumnProfile,
    Constraint,
    TableDescription,
    TableProfile,
    TableRef,
    TableVersion,
)
from lakeloom.sources.privacy import (
    ValueKind,
    allow_counts,
    allow_minmax,
    is_sensitive,
    rank_top_k,
    render,
    truncate,
    withheld_reason,
)
from lakeloom.untrusted import UntrustedText

__all__ = ["Connect", "SnowflakeSource", "build_source", "quote_ident", "snowflake_kind"]

_log = logging.getLogger(__name__)

Connect = Callable[[dict[str, Any]], Any]

_NOT_FOUND = frozenset({2003, 2043})  # object does not exist or not authorised
_AUTH = frozenset({390100, 390144, 390318})  # bad credentials, invalid JWT, expired token
_SESSION_EXPIRED = frozenset({390112, 390114})  # session or master token expired: reconnect
_HINT = "check that the Snowflake account is reachable and the source settings are right"


def quote_ident(name: str) -> str:
    """A Snowflake quoted identifier: case kept, ``"`` doubled, ``%`` doubled for binding."""
    return '"' + name.replace('"', '""').replace("%", "%%") + '"'


_NUMBER = ("NUMBER", "DECIMAL", "NUMERIC", "INT", "BIGINT", "SMALLINT", "TINYINT", "BYTEINT")
_FLOAT = ("FLOAT", "DOUBLE", "REAL")


def snowflake_kind(data_type: str) -> ValueKind:
    """The profiling kind of a Snowflake type; semi-structured and binary types are ``other``."""
    base = data_type.upper().split("(", 1)[0].strip()
    if base in _NUMBER or base.startswith(_FLOAT):
        return "number"
    if base in ("DATE", "TIME") or base.startswith("TIMESTAMP") or base == "DATETIME":
        return "temporal"
    if base in ("TEXT", "VARCHAR", "CHAR", "CHARACTER", "STRING", "NCHAR", "NVARCHAR"):
        return "string"
    if base == "BOOLEAN":
        return "boolean"
    return "other"


def _require_connector() -> None:
    if importlib.util.find_spec("snowflake") is None or (
        importlib.util.find_spec("snowflake.connector") is None
    ):
        raise LakeloomError(
            "validation_error",
            "the Snowflake connector needs the 'snowflake-connector-python' package",
            hint="pip install 'lakeloom[snowflake]'",
        )


def _errors() -> ModuleType:
    from snowflake.connector import errors

    return errors


def _default_connect(params: dict[str, Any]) -> Any:
    import snowflake.connector

    return snowflake.connector.connect(**params)


def _run(conn: Any, sql: str, params: tuple[object, ...]) -> list[dict[str, Any]]:
    cursor = conn.cursor()
    try:
        if params:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql % ())  # no binding: unescape the ``%%`` of quote_ident
        description: list[tuple[Any, ...]] = list(cursor.description or [])
        names = [str(d[0]) for d in description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    finally:
        cursor.close()


class _Skipped(Exception):
    """An optional statement failed with a ``ProgrammingError`` (privilege, edition)."""


def _utc(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _data_type(row: dict[str, Any]) -> str:
    kind = str(row.get("DATA_TYPE") or "unknown")
    precision = row.get("NUMERIC_PRECISION")
    if kind == "NUMBER" and precision is not None:
        return f"NUMBER({precision},{row.get('NUMERIC_SCALE') or 0})"
    return kind


def _match_forms(tags: list[str]) -> list[str]:
    """Tags for sensitivity matching: a bare ``PII`` (empty value) also as ``PII.``, so the
    default glob ``PII.*`` covers it."""
    return tags + [f"{t}." for t in tags if "." not in t]


def _tag(row: dict[str, Any]) -> str:
    value = row.get("TAG_VALUE")
    # NAME.VALUE, as OpenMetadata maps a Snowflake tag to Classification.Tag; so the default
    # sensitive_tags glob ``PII.*`` matches a ``PII`` tag whatever its value.
    return f"{row['TAG_NAME']}.{value}" if value else str(row["TAG_NAME"])


class SnowflakeSource:
    """A read-only view of one Snowflake account."""

    kind = "snowflake"

    def __init__(self, config: SnowflakeSourceConfig, *, connect: Connect | None = None) -> None:
        self.name = config.name
        self.om_service = config.om_service
        self.capabilities = SourceCapabilities(
            profiling=config.profiling, versions=True, constraints=True
        )
        self._config = config
        self._connect = connect or _default_connect
        self._conn: Any = None
        self._lock = asyncio.Lock()
        self._source = f"snowflake:{config.name}"

    # --- connection ---------------------------------------------------------------------------

    def _credential_ref(self) -> str:
        auth = self._config.auth
        if auth.private_key_file is not None:
            return f"key file {auth.private_key_file}"
        return f"environment variable {auth.password_env or auth.token_env}"

    def _secret(self, env: str) -> str:
        value = os.environ.get(env, "").strip()
        if not value:
            raise LakeloomError(
                "forbidden",
                f"the credential for Snowflake source '{self.name}' is not set",
                hint=f"set environment variable {env}",
            )
        return value

    def _params(self) -> dict[str, Any]:
        cfg, auth = self._config, self._config.auth
        timeout = cfg.query_timeout_seconds
        params: dict[str, Any] = {
            "account": cfg.account,
            "user": cfg.user,
            "login_timeout": timeout,
            # HTTP requests only; statements are bounded by STATEMENT_TIMEOUT_IN_SECONDS on the
            # server, so a slow statement is not cut short client-side and then retried.
            "request_timeout": timeout,
            "client_session_keep_alive": True,
            "session_parameters": {
                "QUERY_TAG": "lakeloom",
                "STATEMENT_TIMEOUT_IN_SECONDS": timeout,
            },
        }
        if cfg.role:
            params["role"] = cfg.role
        if cfg.warehouse:
            params["warehouse"] = cfg.warehouse
        if auth.private_key_file is not None:
            params["private_key_file"] = str(auth.private_key_file)
            if auth.private_key_passphrase_env:
                params["private_key_password"] = self._secret(auth.private_key_passphrase_env)
        elif auth.password_env:
            params["password"] = self._secret(auth.password_env)
        elif auth.token_env:
            params["authenticator"] = "oauth"
            params["token"] = self._secret(auth.token_env)
        return params

    async def _connection(self) -> Any:
        if self._conn is None:
            params = self._params()
            try:
                self._conn = await asyncio.to_thread(self._connect, params)
            except Exception as exc:
                raise self._mapped(exc, what="connect", connecting=True) from None
        return self._conn

    async def _drop(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await asyncio.to_thread(conn.close)
            except Exception:  # the connection is already broken
                _log.debug("snowflake source %s: close after error failed", self.name)

    def _mapped(self, exc: Exception, *, what: str, connecting: bool = False) -> Exception:
        """A typed error for a driver error (no driver text); anything else unchanged."""
        errors = _errors()
        if not isinstance(exc, errors.Error):
            return exc
        errno = getattr(exc, "errno", None)
        if errno in _AUTH or (connecting and isinstance(exc, errors.ProgrammingError)):
            return LakeloomError(
                "forbidden",
                f"Snowflake refused the credentials for source '{self.name}' (errno {errno})",
                hint=f"check the credentials in {self._credential_ref()} and the user's grants",
            )
        if errno in _NOT_FOUND:
            return LakeloomError(
                "not_found",
                f"Snowflake has no such object, or the role cannot see it: {what}",
                hint="call list_catalogs / list_schemas / list_tables to see what exists",
            )
        if isinstance(exc, errors.OperationalError | errors.InterfaceError):
            return LakeloomError(
                "upstream_unavailable", f"cannot reach Snowflake (errno {errno})", hint=_HINT
            )
        return LakeloomError("upstream_unavailable", f"Snowflake error {errno}", hint=_HINT)

    @staticmethod
    def _lost(exc: Exception) -> bool:
        """A dead connection or an expired session: reconnecting may help."""
        errors = _errors()
        reauth = getattr(errors, "ReauthenticationRequest", None)
        if isinstance(exc, errors.OperationalError | errors.InterfaceError):
            return True
        if reauth is not None and isinstance(exc, reauth):
            return True
        return isinstance(exc, errors.Error) and getattr(exc, "errno", None) in _SESSION_EXPIRED

    async def _query(
        self,
        sql: str,
        params: tuple[object, ...] = (),
        *,
        what: str = "query",
        optional: bool = False,
    ) -> list[dict[str, Any]]:
        """Run one statement; ``optional`` turns a ``ProgrammingError`` into ``_Skipped``."""
        async with self._lock:
            for attempt in (0, 1):
                conn = await self._connection()
                try:
                    return await asyncio.to_thread(_run, conn, sql, params)
                except Exception as exc:
                    errors = _errors()
                    lost = self._lost(exc)
                    if lost and attempt == 0:
                        await self._drop()
                        continue
                    if optional and isinstance(exc, errors.ProgrammingError):
                        _log.debug(
                            "snowflake source %s: optional %s skipped (errno %s)",
                            self.name,
                            what,
                            getattr(exc, "errno", None),
                        )
                        raise _Skipped from None
                    if lost:
                        await self._drop()
                    raise self._mapped(exc, what=what) from None
            raise AssertionError("unreachable")

    # --- LakeSource ---------------------------------------------------------------------------

    @staticmethod
    def _need_catalog(catalog: str | None) -> str:
        if not catalog:
            raise LakeloomError(
                "validation_error",
                "Snowflake tables live in a database (the catalog)",
                hint="pass catalog (call list_catalogs to see them)",
            )
        return catalog

    def _fq(self, ref: TableRef) -> str:
        catalog = self._need_catalog(ref.catalog)
        return ".".join(quote_ident(p) for p in (catalog, ref.schema_, ref.name))

    async def list_catalogs(self) -> list[str]:
        found = await self._query("SHOW TERSE DATABASES", what="databases")
        allowed = self._config.databases
        names = (str(r["name"]) for r in found if r.get("name"))
        return sorted(n for n in names if allowed is None or n in allowed)

    async def list_schemas(self, catalog: str | None) -> list[str]:
        db = self._need_catalog(catalog)
        found = await self._query(
            f"SELECT SCHEMA_NAME FROM {quote_ident(db)}.INFORMATION_SCHEMA.SCHEMATA "
            "WHERE SCHEMA_NAME <> 'INFORMATION_SCHEMA' ORDER BY 1",
            what=f"database {db}",
        )
        return sorted(str(r["SCHEMA_NAME"]) for r in found)

    async def list_tables(self, catalog: str | None, schema: str) -> list[TableRef]:
        db = self._need_catalog(catalog)
        found = await self._query(
            f"SELECT TABLE_NAME FROM {quote_ident(db)}.INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE IN ('BASE TABLE', 'VIEW') ORDER BY 1",
            (schema,),
            what=f"schema {db}.{schema}",
        )
        return sorted(
            (TableRef(catalog=db, schema=schema, name=str(r["TABLE_NAME"])) for r in found),
            key=lambda r: r.name,
        )

    async def _table_row(self, ref: TableRef) -> dict[str, Any]:
        db = self._need_catalog(ref.catalog)
        found = await self._query(
            "SELECT TABLE_NAME, TABLE_TYPE, TABLE_OWNER, COMMENT, LAST_ALTERED, CLUSTERING_KEY "
            f"FROM {quote_ident(db)}.INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
            (ref.schema_, ref.name),
            what=f"table {ref.full_name}",
        )
        if not found:
            raise LakeloomError(
                "not_found",
                f"Snowflake has no such table, or the role cannot see it: {ref.full_name}",
                hint="call list_tables to see the tables of this schema",
            )
        return found[0]

    async def describe_table(self, ref: TableRef) -> TableDescription:
        return (await self._describe(ref))[0]

    async def _tags(self, ref: TableRef) -> tuple[dict[str, list[str]], list[str]] | None:
        """Column and table tags; ``None`` when they could not be read (unknown, not none)."""
        db, fq = self._need_catalog(ref.catalog), self._fq(ref)
        try:
            rows = await self._query(
                "SELECT TAG_NAME, TAG_VALUE, LEVEL, COLUMN_NAME FROM "
                f"TABLE({quote_ident(db)}.INFORMATION_SCHEMA.TAG_REFERENCES_ALL_COLUMNS"
                "(%s, 'table'))",
                (fq,),
                what="tags",
                optional=True,
            )
        except _Skipped:
            return None
        column_tags: dict[str, list[str]] = defaultdict(list)
        table_tags: list[str] = []
        for row in rows:
            tag = _tag(row)
            column = row.get("COLUMN_NAME")
            # A tag set on the table, schema or database is reported for each column it
            # applies to (inherited); it is a tag of those columns and of the table.
            if column and tag not in column_tags[str(column)]:
                column_tags[str(column)].append(tag)
            if row.get("LEVEL") != "COLUMN" and tag not in table_tags:
                table_tags.append(tag)
        return dict(column_tags), table_tags

    async def _describe(self, ref: TableRef) -> tuple[TableDescription, bool]:
        """The description, and whether its tags are known (a profile fails closed if not)."""
        table = await self._table_row(ref)
        db = self._need_catalog(ref.catalog)
        raw_columns = await self._query(
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COMMENT, ORDINAL_POSITION, "
            f"NUMERIC_PRECISION, NUMERIC_SCALE FROM {quote_ident(db)}.INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
            (ref.schema_, ref.name),
            what=f"table {ref.full_name}",
        )
        tags = await self._tags(ref)
        column_tags, table_tags = tags if tags is not None else ({}, [])
        columns = [
            ColumnInfo(
                name=str(c["COLUMN_NAME"]),
                data_type=_data_type(c),
                nullable=c.get("IS_NULLABLE") == "YES",
                comment=self._untrusted(c.get("COMMENT")),
                tags=column_tags.get(str(c["COLUMN_NAME"]), []),
                position=int(c.get("ORDINAL_POSITION") or i + 1),
            )
            for i, c in enumerate(raw_columns)
        ]
        properties = {
            key: str(table[column])
            for key, column in (("table_type", "TABLE_TYPE"), ("clustering_key", "CLUSTERING_KEY"))
            if table.get(column)
        }
        owner = table.get("TABLE_OWNER")
        # A view has no keys; SHOW … KEYS IN TABLE is only asked of tables.
        is_view = table.get("TABLE_TYPE") == "VIEW"
        description = TableDescription(
            ref=ref,
            comment=self._untrusted(table.get("COMMENT")),
            columns=columns,
            constraints=[] if is_view else await self._constraints(ref),
            tags=table_tags,
            owners=[str(owner)] if owner else [],
            properties=properties,
            version=TableVersion(committed_at=_utc(table.get("LAST_ALTERED"))),
        )
        return description, tags is not None

    def _untrusted(self, value: object) -> UntrustedText | None:
        return UntrustedText(text=str(value), source=self._source) if value else None

    async def _constraints(self, ref: TableRef) -> list[Constraint]:
        fq, what = self._fq(ref), f"table {ref.full_name}"
        primary = await self._query(f"SHOW PRIMARY KEYS IN TABLE {fq}", what=what)
        imported = await self._query(f"SHOW IMPORTED KEYS IN TABLE {fq}", what=what)
        out: list[Constraint] = []
        pks: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in primary:
            pks[str(row.get("constraint_name"))].append(row)
        for name in sorted(pks):
            ordered = sorted(pks[name], key=lambda r: int(r.get("key_sequence") or 0))
            out.append(
                Constraint(kind="primary_key", columns=[str(r["column_name"]) for r in ordered])
            )
        fks: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in imported:
            fks[str(row.get("fk_name"))].append(row)
        for name in sorted(fks):
            ordered = sorted(fks[name], key=lambda r: int(r.get("key_sequence") or 0))
            first = ordered[0]
            out.append(
                Constraint(
                    kind="foreign_key",
                    columns=[str(r["fk_column_name"]) for r in ordered],
                    ref_table=TableRef(
                        catalog=str(first["pk_database_name"]),
                        schema=str(first["pk_schema_name"]),
                        name=str(first["pk_table_name"]),
                    ),
                    ref_columns=[str(r["pk_column_name"]) for r in ordered],
                )
            )
        return out

    async def get_table_version(self, ref: TableRef) -> TableVersion:
        table = await self._table_row(ref)
        return TableVersion(committed_at=_utc(table.get("LAST_ALTERED")))

    async def profile_table(self, ref: TableRef, policy: ProfilePolicy) -> TableProfile:
        """Aggregates computed in the warehouse, under the shared privacy rules (FR-SRC-09).

        One aggregate statement gives the row count, null counts and the allowed min/max; a
        bounded ``DISTINCT`` subquery per countable column caps the distinct count at
        ``low_cardinality + 1``; values are grouped only for top-k, which needs samples on.
        """
        if not self._config.profiling:
            raise LakeloomError(
                "validation_error",
                f"profiling is disabled for Snowflake source {self.name!r}",
                hint="set profiling: true on the source (it runs warehouse queries)",
            )
        description, tags_known = await self._describe(ref)
        fq, what = self._fq(ref), f"table {ref.full_name}"
        cap = policy.low_cardinality + 1
        plans: list[tuple[ColumnInfo, ValueKind, bool, bool]] = []
        select = ['COUNT(*) AS "N"']
        for i, column in enumerate(description.columns):
            kind = snowflake_kind(column.data_type)
            # Unknown tags fail closed: no column may show values.
            sensitive = not tags_known or is_sensitive(
                _match_forms(column.tags), policy.sensitive_tags
            )
            minmax = allow_minmax(kind, sensitive=sensitive, policy=policy)
            name = quote_ident(column.name)
            select.append(f'COUNT_IF({name} IS NULL) AS "N{i}"')
            if minmax:
                select += [f'MIN({name}) AS "LO{i}"', f'MAX({name}) AS "HI{i}"']
            plans.append((column, kind, sensitive, minmax))
        (totals,) = await self._query(f"SELECT {', '.join(select)} FROM {fq}", what=what)
        rows_total = int(totals["N"] or 0)
        out: list[ColumnProfile] = []
        for i, (column, kind, sensitive, minmax) in enumerate(plans):
            name = quote_ident(column.name)
            distinct: int | None = None
            if allow_counts(kind):
                (found,) = await self._query(
                    f'SELECT COUNT(*) AS "D" FROM (SELECT DISTINCT {name} FROM {fq} '
                    f"WHERE {name} IS NOT NULL LIMIT %s)",
                    (cap + 1,),
                    what=what,
                )
                count = int(found["D"] or 0)
                distinct = count if count <= cap else None
            withheld = withheld_reason(sensitive=sensitive, policy=policy)
            top_k: list[tuple[str, int]] | None = None
            if withheld is None and distinct is not None:
                groups = await self._query(
                    f'SELECT {name} AS "V", COUNT(*) AS "N" FROM {fq} '
                    f"WHERE {name} IS NOT NULL GROUP BY 1",
                    what=what,
                )
                top_k = rank_top_k({g["V"]: int(g["N"]) for g in groups}, policy)
            lo = hi = None
            if minmax:
                lo, hi = totals.get(f"LO{i}"), totals.get(f"HI{i}")
                lo = render(lo) if lo is not None else None
                hi = render(hi) if hi is not None else None
                if kind == "string":
                    lo = truncate(lo, policy.value_max_length) if lo is not None else None
                    hi = truncate(hi, policy.value_max_length) if hi is not None else None
            nulls = int(totals.get(f"N{i}") or 0)
            out.append(
                ColumnProfile(
                    name=column.name,
                    null_rate=nulls / rows_total if rows_total else None,
                    distinct_count=distinct,
                    min=lo,
                    max=hi,
                    top_k=top_k,
                    values_withheld=withheld,
                )
            )
        return TableProfile(ref=ref, row_count=rows_total, columns=out)

    async def aclose(self) -> None:
        async with self._lock:
            await self._drop()


def build_source(config: SnowflakeSourceConfig, settings: Settings) -> SnowflakeSource:
    """Registry factory (FR-SRC-06)."""
    _require_connector()
    return SnowflakeSource(config)
