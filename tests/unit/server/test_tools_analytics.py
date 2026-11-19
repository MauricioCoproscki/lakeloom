"""MCP tools for graph analytics (M6b).

Tests focus on:
- Tool registration and read_only annotations
- ``get_model_stats``: returns structured stats for a model
- ``assess_realization_coverage``: returns coverage ratio
- Scope guard: both tools require lakeloom:read
"""

from __future__ import annotations

from typing import Any

from kit import AuthKit, DepsFactory, mcp_session
from test_tools_semantic import fake_deps, structured

from lakeloom.server.mcp_app import create_mcp, create_server_app

READ = "lakeloom:read"

ANALYTICS_TOOLS = {"get_model_stats", "assess_realization_coverage"}


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


async def test_analytics_tools_registered(make_deps: DepsFactory) -> None:
    tools = {t.name: t for t in await create_mcp(fake_deps(make_deps)).list_tools()}
    assert ANALYTICS_TOOLS <= set(tools), f"Missing: {ANALYTICS_TOOLS - set(tools)}"


async def test_analytics_tools_read_only(make_deps: DepsFactory) -> None:
    tools = {t.name: t for t in await create_mcp(fake_deps(make_deps)).list_tools()}
    for name in ANALYTICS_TOOLS:
        ann = tools[name].annotations
        assert ann is not None
        assert ann.read_only_hint is True, f"{name} should be read_only"
        assert ann.destructive_hint is False


# ---------------------------------------------------------------------------
# get_model_stats
# ---------------------------------------------------------------------------


async def test_graph_stats_returns_structured(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call(make_deps, kit, "get_model_stats", {"model": "retail"}, scope=READ)
    data = structured(result)
    assert "error" not in data, data.get("error")
    assert data["model"] == "retail"
    assert isinstance(data["total_concepts"], int)
    assert isinstance(data["max_depth"], int)
    assert isinstance(data["root_count"], int)
    assert isinstance(data["leaf_count"], int)
    assert "summary" in data


async def test_graph_stats_unknown_model(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call(make_deps, kit, "get_model_stats", {"model": "no-such-model"}, scope=READ)
    data = structured(result)
    assert data["error"]["code"] == "not_found"


async def test_graph_stats_requires_read_scope(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call(make_deps, kit, "get_model_stats", {"model": "retail"}, scope=None)
    data = structured(result)
    assert data["error"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# assess_realization_coverage
# ---------------------------------------------------------------------------


async def test_assess_coverage_returns_structured(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call(
        make_deps, kit, "assess_realization_coverage", {"model": "retail"}, scope=READ
    )
    data = structured(result)
    assert "error" not in data, data.get("error")
    assert data["model"] == "retail"
    assert isinstance(data["total_concepts"], int)
    assert isinstance(data["coverage_ratio"], float)
    assert 0.0 <= data["coverage_ratio"] <= 1.0
    assert isinstance(data["top_realized"], list)


async def test_assess_coverage_unknown_model(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call(
        make_deps, kit, "assess_realization_coverage", {"model": "no-such-model"}, scope=READ
    )
    data = structured(result)
    assert data["error"]["code"] == "not_found"


async def test_assess_coverage_requires_read_scope(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call(
        make_deps, kit, "assess_realization_coverage", {"model": "retail"}, scope=None
    )
    data = structured(result)
    assert data["error"]["code"] == "forbidden"
