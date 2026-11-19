"""Stage deliverables over MCP tools, MCP resources and the HTTP API."""

import csv
import io
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from kit import AuthKit, DepsFactory, mcp_session
from mcp.types import TextResourceContents
from starlette.testclient import TestClient
from test_tools_semantic import fake_deps, structured, text

from lakeloom.core.map_profile import ColumnEntry, MapProfile, TableMapEntry
from lakeloom.server import deliverables as service
from lakeloom.server.mcp_app import create_mcp, create_server_app

READ = "lakeloom:read"
TOOLS = {"export_glossary", "export_data_dictionary", "export_taxonomy", "export_synonyms"}


async def call(deps: Any, kit: AuthKit, tool: str, args: dict[str, Any], **claims: Any) -> Any:
    async with mcp_session(create_server_app(deps), kit.token(**claims)) as session:
        return await session.call_tool(tool, args)


async def test_tools_registered_read_only(make_deps: DepsFactory) -> None:
    tools = {t.name: t for t in await create_mcp(fake_deps(make_deps)).list_tools()}
    assert set(tools) >= TOOLS
    for name in TOOLS:
        ann = tools[name].annotations
        assert ann is not None and ann.read_only_hint is True


async def test_glossary_markdown_and_quality(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call(
        fake_deps(make_deps), kit, "export_glossary", {"model": "retail"}, scope=READ
    )
    data = structured(result)
    assert "error" not in data, data
    body = text(result)
    assert body.startswith("# Business glossary: retail") and "**Order**" in body
    assert data["media_type"] == "text/markdown" and data["filename"] == "glossary.md"
    q = data["quality"]
    assert q["terms"] == 6 and q["defined"] == 1  # the fixture defines only Order


async def test_glossary_csv(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call(
        fake_deps(make_deps),
        kit,
        "export_glossary",
        {"model": "retail", "format": "csv"},
        scope=READ,
    )
    rows = list(csv.DictReader(io.StringIO(text(result))))
    assert {r["fqn"] for r in rows} >= {"retail.Order", "retail.OrderDate"}


async def test_taxonomy_and_synonyms(make_deps: DepsFactory, kit: AuthKit) -> None:
    deps = fake_deps(make_deps)
    tree = await call(
        deps, kit, "export_taxonomy", {"model": "retail", "format": "mermaid"}, scope=READ
    )
    assert text(tree).startswith("flowchart TD")
    syn = await call(deps, kit, "export_synonyms", {"model": "retail"}, scope=READ)
    assert "Order, Purchase" in text(syn)  # the fixture's altLabel


async def test_requires_read_scope(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call(
        fake_deps(make_deps), kit, "export_glossary", {"model": "retail"}, scope="lakeloom:propose"
    )
    assert result.is_error and "lakeloom:read" in text(result)


async def test_unknown_model_and_bad_version(make_deps: DepsFactory, kit: AuthKit) -> None:
    deps = fake_deps(make_deps)
    missing = await call(deps, kit, "export_glossary", {"model": "nope"}, scope=READ)
    assert structured(missing)["error"]["code"] == "not_found"
    bad = await call(deps, kit, "export_glossary", {"model": "retail", "version": "v1"}, scope=READ)
    assert structured(bad)["error"]["code"] == "validation_error"


async def test_data_dictionary_without_scan_is_not_found(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    result = await call(
        fake_deps(make_deps), kit, "export_data_dictionary", {"model": "retail"}, scope=READ
    )
    err = structured(result)["error"]
    assert err["code"] == "not_found" and "scan_lake_source" in err["hint"]


async def test_data_dictionary_with_a_map(
    make_deps: DepsFactory, kit: AuthKit, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def profile(deps: Any, model: str, scan_id: str | None) -> MapProfile:
        return MapProfile(
            source_id="fx",
            scan_id=scan_id or "scan_9",
            model_slug=model,
            created_at=datetime(2026, 11, 19, tzinfo=UTC),
            table_entries=[
                TableMapEntry(
                    om_fqn="svc.retail.sales.orders",
                    local_name="orders",
                    source="fx",
                    columns=[
                        ColumnEntry("order_date", "DATE", "http://x/ontology/retail/OrderDate")
                    ],
                )
            ],
        )

    monkeypatch.setattr(service, "_profile", profile)
    result = await call(
        fake_deps(make_deps),
        kit,
        "export_data_dictionary",
        {"model": "retail", "format": "csv", "scan_id": "scan_1"},
        scope=READ,
    )
    (row,) = list(csv.DictReader(io.StringIO(text(result))))
    assert (row["column"], row["term"], row["status"]) == ("order_date", "OrderDate", "current")


# --- the same bytes over the MCP resource and HTTP ----------------------------------------


@pytest.fixture
def client(make_deps: DepsFactory) -> Iterator[TestClient]:
    with TestClient(create_server_app(fake_deps(make_deps)), base_url="https://lakeloom.test") as c:
        yield c


def test_http_deliverable(client: TestClient, kit: AuthKit) -> None:
    auth = {"Authorization": f"Bearer {kit.token()}"}
    r = client.get("/v1/models/retail/deliverables/glossary.csv", headers=auth)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv") and r.headers["etag"]
    assert r.text.startswith("fqn,term,definition")
    unknown = client.get("/v1/models/retail/deliverables/secrets.txt", headers=auth)
    assert unknown.status_code == 404
    old = client.get("/v1/models/retail/deliverables/glossary.md?version=1.0.0", headers=auth)
    assert old.status_code == 404  # Git publishing is off in the fixture


async def test_resource_matches_tool(make_deps: DepsFactory, kit: AuthKit) -> None:
    deps = fake_deps(make_deps)
    async with mcp_session(create_server_app(deps), kit.token(scope=READ)) as session:
        res = await session.read_resource("lakeloom://models/retail/deliverables/taxonomy.csv")
        tool = await session.call_tool("export_taxonomy", {"model": "retail", "format": "csv"})
        templates = (await session.list_resource_templates()).resource_templates
    content = res.contents[0]
    assert isinstance(content, TextResourceContents)
    assert content.mime_type == "text/csv" and content.text == text(tool)
    assert any("deliverables/{filename}" in t.uri_template for t in templates)


async def test_expand_query_tool(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call(
        fake_deps(make_deps),
        kit,
        "expand_query",
        {"model": "retail", "text": "late purchase by order date"},
        scope=READ,
    )
    data = structured(result)
    assert "error" not in data, data
    assert [c["label"] for c in data["concepts"]] == ["Order", "OrderDate"]
    assert data["concepts"][0]["matched"] == "purchase"
    assert "Purchase" in data["terms"] and "Search terms:" in text(result)
