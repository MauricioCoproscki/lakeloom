"""Snowflake connector against a fake DB-API connection (FR-SRC-04, NFR-SEC-14).

The fake mirrors the driver's surface and error classes; nothing here was run against a
live Snowflake account.
"""

import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from snowflake.connector.errors import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine

from lakeloom.config import Settings
from lakeloom.core.scans import ScanService
from lakeloom.errors import LakeloomError
from lakeloom.sources.config import SnowflakeAuth, SnowflakeSourceConfig
from lakeloom.sources.models import Constraint, TableRef
from lakeloom.sources.snowflake import SnowflakeSource, quote_ident
from lakeloom.state.db import create_engine, upgrade
from lakeloom.state.scans import ScanRepository
from lakeloom.untrusted import UntrustedText

from .fake_snowflake import INJECTION, Connector, FakeConnection, Responder, account, rows

ORDERS = TableRef(catalog="SALES", schema="PUBLIC", name="ORDERS")


def config(**overrides: Any) -> SnowflakeSourceConfig:
    values: dict[str, Any] = {
        "type": "snowflake",
        "name": "sf",
        "account": "myorg-acct",
        "user": "SVC",
        "role": "READER",
        "warehouse": "WH",
        "auth": SnowflakeAuth(password_env="SF_PASSWORD"),  # noqa: S106 (a variable name)
    }
    values.update(overrides)
    return SnowflakeSourceConfig(**values)


@pytest.fixture(autouse=True)
def _password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SF_PASSWORD", "pw-s3cr3t")


def source(
    responders: list[tuple[str, Responder]] | None = None, **overrides: Any
) -> tuple[SnowflakeSource, FakeConnection]:
    conn = FakeConnection(responders if responders is not None else account())
    return SnowflakeSource(config(**overrides), connect=Connector(conn)), conn


def test_quote_ident_doubles_quotes() -> None:
    assert quote_ident('a"b') == '"a""b"'
    assert quote_ident("My Schema") == '"My Schema"'


async def test_fr_src_04_lists_with_allow_list() -> None:
    src, _ = source(databases=["SALES", "HR"])
    assert await src.list_catalogs() == ["HR", "SALES"]
    assert await src.list_schemas("SALES") == ["PUBLIC"]
    assert [t.name for t in await src.list_tables("SALES", "PUBLIC")] == ["CUSTOMERS", "ORDERS"]
    all_dbs, _ = source()
    assert await all_dbs.list_catalogs() == ["HR", "SALES", "TMP"]


async def test_list_needs_catalog() -> None:
    src, _ = source()
    with pytest.raises(LakeloomError) as info:
        await src.list_schemas(None)
    assert info.value.code == "validation_error"
    assert "list_catalogs" in (info.value.hint or "")


async def test_fr_src_04_describe_maps_columns_comments_owner_version() -> None:
    src, _ = source()
    desc = await src.describe_table(ORDERS)
    assert desc.comment == UntrustedText(text=INJECTION, source="snowflake:sf")
    assert [c.name for c in desc.columns] == ["ORDER_ID", "CUSTOMER_ID", "EMAIL", "ORDER_DATE"]
    order_id, customer_id, email, order_date = desc.columns
    assert order_id.data_type == "NUMBER(38,0)" and order_id.nullable is False
    assert order_id.position == 1
    assert customer_id.comment == UntrustedText(text="Buyer", source="snowflake:sf")
    assert email.data_type == "TEXT" and email.nullable is True
    assert order_date.data_type == "DATE"
    assert desc.owners == ["SALES_ADMIN"]
    assert desc.properties == {"table_type": "BASE TABLE", "clustering_key": "LINEAR(ORDER_DATE)"}
    assert desc.partition_columns == []
    assert desc.version.version is None
    assert desc.version.committed_at == datetime(2026, 11, 1, 10, tzinfo=UTC)
    version = await src.get_table_version(ORDERS)
    assert version.committed_at == datetime(2026, 11, 1, 10, tzinfo=UTC)


async def test_fr_src_04_constraints_from_show_keys() -> None:
    responders = account()
    composite = rows(
        {"column_name": "LINE", "key_sequence": 2, "constraint_name": "PK_ORDERS"},
        {"column_name": "ORDER_ID", "key_sequence": 1, "constraint_name": "PK_ORDERS"},
    )
    responders.insert(0, (r"^SHOW PRIMARY KEYS IN TABLE", composite))
    src, _ = source(responders)
    desc = await src.describe_table(ORDERS)
    assert desc.constraints == [
        Constraint(kind="primary_key", columns=["ORDER_ID", "LINE"]),
        Constraint(
            kind="foreign_key",
            columns=["CUSTOMER_ID"],
            ref_table=TableRef(catalog="SALES", schema="PUBLIC", name="CUSTOMERS"),
            ref_columns=["CUSTOMER_ID"],
        ),
    ]


async def test_fr_src_04_tags_table_and_column() -> None:
    src, _ = source()
    desc = await src.describe_table(ORDERS)
    assert desc.tags == ["TIER"]
    email = next(c for c in desc.columns if c.name == "EMAIL")
    assert email.tags == ["PII=EMAIL"]

    responders = account()
    denied = ProgrammingError(msg="Insufficient privileges", errno=3001)
    responders.insert(0, (r"TAG_REFERENCES_ALL_COLUMNS", lambda _s, _p: denied))
    src, _ = source(responders)
    desc = await src.describe_table(ORDERS)
    assert desc.tags == [] and all(c.tags == [] for c in desc.columns)


async def test_snowflake_identifiers_are_quoted() -> None:
    src, conn = source()
    ref = TableRef(catalog="SALES", schema="My Schema", name='a"b')
    with pytest.raises(LakeloomError):  # the fake account has no such table
        await src.describe_table(ref)
    await src.list_tables("x;DROP", "PUBLIC")
    sql = [text for text, _ in conn.statements]
    assert any('"x;DROP".INFORMATION_SCHEMA.TABLES' in s for s in sql)
    params = [p for _, p in conn.statements]
    assert ("My Schema", 'a"b') in params
    # Values are bound, never pasted into the SQL text.
    assert not any("My Schema" in s or 'a"b' in s for s in sql)


async def test_snowflake_names_keep_their_case() -> None:
    responders = account()
    responders.insert(0, (r"INFORMATION_SCHEMA\.SCHEMATA", rows({"SCHEMA_NAME": "MixedCase"})))
    src, _ = source(responders)
    assert await src.list_schemas("SALES") == ["MixedCase"]


async def test_snowflake_missing_table_is_not_found() -> None:
    src, _ = source()
    with pytest.raises(LakeloomError) as info:
        await src.describe_table(TableRef(catalog="SALES", schema="PUBLIC", name="NOPE"))
    assert info.value.code == "not_found"

    responders = account()
    gone = ProgrammingError(msg="Database 'X' does not exist or not authorized.", errno=2003)
    responders.insert(0, (r"INFORMATION_SCHEMA\.SCHEMATA", lambda _s, _p: gone))
    src, _ = source(responders)
    with pytest.raises(LakeloomError) as info:
        await src.list_schemas("X")
    assert info.value.code == "not_found"


async def test_snowflake_reconnects_once() -> None:
    first = FakeConnection(account())
    first.closed = True  # the session died between calls
    second = FakeConnection(account())
    connector = Connector(first, second)
    src = SnowflakeSource(config(), connect=connector)
    assert await src.list_catalogs() == ["HR", "SALES", "TMP"]
    assert len(connector.params) == 2

    dead = FakeConnection(account())
    dead.closed = True
    also_dead = FakeConnection(account())
    also_dead.closed = True
    src = SnowflakeSource(config(), connect=Connector(dead, also_dead))
    with pytest.raises(LakeloomError) as info:
        await src.list_catalogs()
    assert info.value.code == "upstream_unavailable"


async def test_snowflake_errors_never_echo_driver_text_or_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    leaky = OperationalError(msg="password pw-s3cr3t rejected for s3cr3t", errno=250003)
    responders: list[tuple[str, Responder]] = [(r".", lambda _s, _p: leaky)]
    src = SnowflakeSource(
        config(), connect=Connector(FakeConnection(responders), FakeConnection(responders))
    )
    with pytest.raises(LakeloomError) as info:
        await src.list_catalogs()
    err = info.value
    assert err.code == "upstream_unavailable"
    text = " ".join([err.message, err.hint or "", str(err), caplog.text])
    assert "s3cr3t" not in text

    def refuse(_params: dict[str, Any]) -> FakeConnection:
        raise ProgrammingError(msg="Incorrect username or password pw-s3cr3t", errno=390100)

    src = SnowflakeSource(config(), connect=refuse)
    with pytest.raises(LakeloomError) as info:
        await src.list_catalogs()
    assert info.value.code == "forbidden"
    assert "SF_PASSWORD" in (info.value.hint or "")
    assert "s3cr3t" not in " ".join([info.value.message, info.value.hint or "", caplog.text])


async def test_snowflake_unset_secret_is_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SF_PASSWORD")
    src, _ = source()
    with pytest.raises(LakeloomError) as info:
        await src.list_catalogs()
    assert info.value.code == "forbidden"
    assert "set environment variable SF_PASSWORD" in (info.value.hint or "")


@pytest.mark.parametrize(
    ("auth", "expected"),
    [
        (
            SnowflakeAuth(
                private_key_file=Path("/k.p8"),
                private_key_passphrase_env="SF_KEY_PASS",  # noqa: S106 (a variable name)
            ),
            {"private_key_file": "/k.p8", "private_key_file_pwd": "key-pass"},
        ),
        (
            SnowflakeAuth(password_env="SF_PASSWORD"),  # noqa: S106 (a variable name)
            {"password": "pw-s3cr3t"},
        ),
        (
            SnowflakeAuth(token_env="SF_TOKEN"),  # noqa: S106 (a variable name)
            {"authenticator": "oauth", "token": "oauth-tok"},
        ),
    ],
)
async def test_snowflake_connect_params_per_auth(
    monkeypatch: pytest.MonkeyPatch, auth: SnowflakeAuth, expected: dict[str, Any]
) -> None:
    monkeypatch.setenv("SF_KEY_PASS", "key-pass")
    monkeypatch.setenv("SF_TOKEN", "oauth-tok")
    connector = Connector(FakeConnection(account()))
    src = SnowflakeSource(config(auth=auth, query_timeout_seconds=45), connect=connector)
    await src.list_catalogs()
    (params,) = connector.params
    for key, value in expected.items():
        assert params[key] == value
    assert params["account"] == "myorg-acct" and params["user"] == "SVC"
    assert params["role"] == "READER" and params["warehouse"] == "WH"
    assert params["login_timeout"] == 45 and params["network_timeout"] == 45
    assert params["session_parameters"] == {
        "QUERY_TAG": "lakeloom",
        "STATEMENT_TIMEOUT_IN_SECONDS": 45,
    }
    other = {"password", "token", "authenticator", "private_key_file", "private_key_file_pwd"}
    assert not (other - set(expected)) & set(params)


async def test_aclose_closes_the_connection() -> None:
    src, conn = source()
    await src.list_catalogs()
    await src.aclose()
    assert conn.closed


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncGenerator[ScanRepository]:
    eng: AsyncEngine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    await upgrade(eng)
    yield ScanRepository(eng)
    await eng.dispose()


async def test_snowflake_scan_end_to_end(repo: ScanRepository) -> None:
    src, _ = source(databases=["SALES"])
    service = ScanService({"sf": src}, repo, None, Settings())
    record = await service.scan(
        "sf", principal_subject="agent-7", principal_type="agent", client="pytest"
    )
    assert record.status == "completed", record.warnings
    assert sorted(t.ref.name for t in record.tables) == ["CUSTOMERS", "ORDERS"]
