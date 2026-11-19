"""MCP tools for MAP workflow (M5c).

Tests focus on:
- Tool registration and read_only annotations
- ``build_map_from_scan``: builds a MAP and returns structured content
- ``validate_map``: validates coverage and returns a report
- ``export_map``: exports as markdown or JSON
- Scope guard: all tools require lakeloom:read
- Not_found when scan_id is unknown
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from kit import AuthKit, DepsFactory, mcp_session
from test_tools_semantic import fake_deps, structured

from lakeloom.server.mcp_app import create_mcp, create_server_app
from lakeloom.sources.models import ColumnInfo, TableDescription, TableVersion
from lakeloom.sources.models import TableRef as SourceTableRef
from lakeloom.state.scans import ScannedTable, ScanRecord

READ = "lakeloom:read"

MAP_TOOLS = {"build_map_from_scan", "validate_map", "export_map"}

NOW = datetime(2026, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------


def _col(name: str, data_type: str = "string", position: int = 0) -> ColumnInfo:
    return ColumnInfo(name=name, data_type=data_type, nullable=True, position=position)


def _scan_record() -> ScanRecord:
    ref = SourceTableRef(schema="sales", name="orders")
    table = ScannedTable(
        ref=ref,
        version=TableVersion(version=1),
        om_fqn="svc.sales.orders",
        description=TableDescription(
            ref=ref,
            columns=[_col("order_id", "bigint"), _col("status")],
            version=TableVersion(version=1),
        ),
    )
    return ScanRecord(
        id="scan-1",
        source="lake",
        created_at=NOW,
        principal_subject="agent-1",
        principal_type="agent",
        status="completed",
        tables=[table],
    )


class _FakeScans:
    """Minimal ScanService stub for MAP tool tests."""

    def __init__(self, records: dict[str, ScanRecord] | None = None) -> None:
        self._records: dict[str, ScanRecord] = records or {}

    async def get(self, scan_id: str) -> ScanRecord:
        from lakeloom.errors import LakeloomError

        if scan_id not in self._records:
            raise LakeloomError(
                "not_found",
                f"scan {scan_id!r} does not exist",
                "Use a scan_id returned by scan_lake_source.",
            )
        return self._records[scan_id]


def deps_with_scan(make_deps: DepsFactory, scan_record: ScanRecord | None = None) -> Any:
    deps = fake_deps(make_deps)
    deps.scans = _FakeScans({"scan-1": scan_record or _scan_record()})  # pyright: ignore
    return deps


async def call(
    make_deps: DepsFactory,
    kit: AuthKit,
    tool: str,
    args: dict[str, Any] | None = None,
    **claims: Any,
) -> Any:
    app = create_server_app(deps_with_scan(make_deps))
    async with mcp_session(app, kit.token(**claims)) as session:
        return await session.call_tool(tool, args or {})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_map_tools_registered(make_deps: DepsFactory) -> None:
    tools = {t.name: t for t in await create_mcp(deps_with_scan(make_deps)).list_tools()}
    assert MAP_TOOLS <= set(tools), f"Missing: {MAP_TOOLS - set(tools)}"


async def test_map_tools_read_only(make_deps: DepsFactory) -> None:
    tools = {t.name: t for t in await create_mcp(deps_with_scan(make_deps)).list_tools()}
    for name in MAP_TOOLS:
        ann = tools[name].annotations
        assert ann is not None
        assert ann.read_only_hint is True, f"{name} should be read_only"
        assert ann.destructive_hint is False


# ---------------------------------------------------------------------------
# build_map_from_scan
# ---------------------------------------------------------------------------


async def test_build_map_from_scan_returns_profile(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call(
        make_deps,
        kit,
        "build_map_from_scan",
        {"scan_id": "scan-1", "model": "retail"},
        scope=READ,
    )
    data = structured(result)
    assert "error" not in data, data.get("error")
    assert data["scan_id"] == "scan-1"
    assert data["model_slug"] == "retail"
    assert isinstance(data["total_tables"], int)
    assert "canonical_layer" in data
    assert "implementation_layer" in data


async def test_build_map_from_scan_unknown_scan(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call(
        make_deps,
        kit,
        "build_map_from_scan",
        {"scan_id": "no-such-scan", "model": "retail"},
        scope=READ,
    )
    data = structured(result)
    assert data["error"]["code"] == "not_found"


async def test_build_map_from_scan_requires_read_scope(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    result = await call(
        make_deps,
        kit,
        "build_map_from_scan",
        {"scan_id": "scan-1", "model": "retail"},
        scope=None,
    )
    data = structured(result)
    assert data["error"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# validate_map
# ---------------------------------------------------------------------------


async def test_validate_map_returns_report(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call(
        make_deps,
        kit,
        "validate_map",
        {"scan_id": "scan-1", "model": "retail"},
        scope=READ,
    )
    data = structured(result)
    assert "error" not in data, data.get("error")
    assert data["scan_id"] == "scan-1"
    assert isinstance(data["coverage_ratio"], float)
    assert isinstance(data["warning_count"], int)


async def test_validate_map_unknown_model(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call(
        make_deps,
        kit,
        "validate_map",
        {"scan_id": "scan-1", "model": "no-such-model"},
        scope=READ,
    )
    data = structured(result)
    assert data["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# export_map
# ---------------------------------------------------------------------------


async def test_export_map_markdown(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call(
        make_deps,
        kit,
        "export_map",
        {"scan_id": "scan-1", "model": "retail", "format": "markdown"},
        scope=READ,
    )
    data = structured(result)
    assert "error" not in data, data.get("error")
    assert data["format"] == "markdown"
    assert "MAP" in data["content"]


async def test_export_map_json(make_deps: DepsFactory, kit: AuthKit) -> None:
    import json

    result = await call(
        make_deps,
        kit,
        "export_map",
        {"scan_id": "scan-1", "model": "retail", "format": "json"},
        scope=READ,
    )
    data = structured(result)
    assert data["format"] == "json"
    parsed = json.loads(data["content"])
    assert "canonical_layer" in parsed
    assert "implementation_layer" in parsed
