"""MCP tools for SKOS export with enriched annotations (M7).

Tests focus on:
- Tool registration and read_only annotations
- ``get_annotation_stats``: returns annotation counts
- ``list_deprecated_concepts``: returns deprecated concepts
- Scope guards: all tools require lakeloom:read
- Unknown model → not_found error
"""

from __future__ import annotations

from typing import Any

from kit import AuthKit, DepsFactory, mcp_session
from test_tools_semantic import fake_deps, structured

from lakeloom.server.mcp_app import create_mcp, create_server_app

READ = "lakeloom:read"

SKOS_TOOLS = {"get_annotation_stats", "list_deprecated_concepts"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def call(
    make_deps: DepsFactory,
    kit: AuthKit,
    tool: str,
    args: dict[str, Any] | None = None,
    **claims: Any,
) -> Any:
    app = create_server_app(fake_deps(make_deps))
    async with mcp_session(app, kit.token(**claims)) as session:
        return await session.call_tool(tool, args or {})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_skos_tools_registered(make_deps: DepsFactory) -> None:
    tools = {t.name: t for t in await create_mcp(fake_deps(make_deps)).list_tools()}
    assert SKOS_TOOLS <= set(tools), f"Missing: {SKOS_TOOLS - set(tools)}"


async def test_skos_tools_read_only(make_deps: DepsFactory) -> None:
    tools = {t.name: t for t in await create_mcp(fake_deps(make_deps)).list_tools()}
    for name in SKOS_TOOLS:
        ann = tools[name].annotations
        assert ann is not None
        assert ann.read_only_hint is True, f"{name} should be read_only"
        assert ann.destructive_hint is False


# ---------------------------------------------------------------------------
# get_annotation_stats
# ---------------------------------------------------------------------------


async def test_skos_annotation_stats_returns_structured(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    result = await call(make_deps, kit, "get_annotation_stats", {"model": "retail"}, scope=READ)
    data = structured(result)
    assert "error" not in data, data.get("error")
    assert data["model"] == "retail"
    assert isinstance(data["total_concepts"], int)
    assert isinstance(data["with_scope_note"], int)
    assert isinstance(data["with_history_note"], int)
    assert isinstance(data["with_definition"], int)
    assert isinstance(data["deprecated"], int)
    # Counts are non-negative
    assert data["with_scope_note"] >= 0
    assert data["deprecated"] >= 0


async def test_skos_annotation_stats_unknown_model(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call(
        make_deps, kit, "get_annotation_stats", {"model": "no-such-model"}, scope=READ
    )
    data = structured(result)
    assert data["error"]["code"] == "not_found"


async def test_skos_annotation_stats_requires_read_scope(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    result = await call(make_deps, kit, "get_annotation_stats", {"model": "retail"}, scope=None)
    data = structured(result)
    assert data["error"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# list_deprecated_concepts
# ---------------------------------------------------------------------------


async def test_skos_deprecated_list_returns_structured(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    result = await call(make_deps, kit, "list_deprecated_concepts", {"model": "retail"}, scope=READ)
    data = structured(result)
    assert "error" not in data, data.get("error")
    assert data["model"] == "retail"
    assert isinstance(data["total_deprecated"], int)
    assert isinstance(data["concepts"], list)
    assert data["total_deprecated"] == len(data["concepts"])


async def test_skos_deprecated_list_unknown_model(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call(
        make_deps, kit, "list_deprecated_concepts", {"model": "no-such-model"}, scope=READ
    )
    data = structured(result)
    assert data["error"]["code"] == "not_found"


async def test_skos_deprecated_list_requires_read_scope(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    result = await call(make_deps, kit, "list_deprecated_concepts", {"model": "retail"}, scope=None)
    data = structured(result)
    assert data["error"]["code"] == "forbidden"


async def test_skos_deprecated_list_sorted_by_label(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call(make_deps, kit, "list_deprecated_concepts", {"model": "retail"}, scope=READ)
    data = structured(result)
    labels = [e["pref_label"] for e in data["concepts"]]
    assert labels == sorted(labels)
