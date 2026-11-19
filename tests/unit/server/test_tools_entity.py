"""MCP tools for entity linking (M6a).

Tests focus on:
- Tool registration, annotations, and scope guards
- ``suggest_external_mappings``: uses fake fetch; returns structured candidates
- ``propose_external_mappings``: creates direct-origin proposal with AddMapping ops
- Scope: suggest requires lakeloom:read; propose requires lakeloom:propose
"""

from __future__ import annotations

from typing import Any

from kit import AuthKit, DepsFactory, mcp_session
from test_tools_proposals import FakeProposals, proposal
from test_tools_semantic import fake_deps, structured

from lakeloom.core.audit import current_audit
from lakeloom.server.mcp_app import create_mcp, create_server_app

READ = "lakeloom:read"
PROPOSE = "lakeloom:propose"

READ_TOOLS = {"suggest_external_mappings"}
WRITE_TOOLS = {"propose_external_mappings"}
ALL_ENTITY_TOOLS = READ_TOOLS | WRITE_TOOLS


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------


class _ExtendedFakeProposals(FakeProposals):
    """FakeProposals with propose_operations for entity write tools."""

    async def propose_operations(self, model: str, operations: Any, **kw: Any) -> Any:
        trail = current_audit()
        p = proposal()
        trail.model, trail.proposal_id, trail.change_set_id = p.model, p.id, "cs-n"
        return p


def deps_with_fake_proposals(make_deps: DepsFactory) -> Any:
    deps = fake_deps(make_deps)
    deps.proposals = _ExtendedFakeProposals()  # pyright: ignore
    return deps


# Fake fetch that returns a minimal Wikidata API response.
async def _fake_wikidata_fetch(url: str, params: dict[str, str]) -> dict:
    term = params.get("search", "")
    return {
        "search": [
            {
                "id": "Q12345",
                "label": term,
                "description": f"A Wikidata entity for {term}",
            }
        ]
    }


# Fake fetch for schema.org JSON-LD.
_SCHEMA_ORG_JSONLD: dict = {
    "@graph": [
        {
            "@id": "schema:Customer",
            "@type": "rdfs:Class",
            "rdfs:label": "Customer",
            "rdfs:comment": "A customer entity.",
        },
        {
            "@id": "schema:Order",
            "@type": "rdfs:Class",
            "rdfs:label": "Order",
            "rdfs:comment": "A purchase order.",
        },
    ]
}


async def _fake_schema_fetch(url: str, params: dict[str, str]) -> dict:
    return _SCHEMA_ORG_JSONLD


async def call_suggest(
    make_deps: DepsFactory,
    kit: AuthKit,
    args: dict[str, Any] | None = None,
    **claims: Any,
) -> Any:
    # Patch the aiohttp fetch with the fake.
    import lakeloom.server.tools_entity as te

    original = te._aiohttp_fetch
    te._aiohttp_fetch = _fake_wikidata_fetch  # type: ignore[assignment]
    try:
        app = create_server_app(fake_deps(make_deps))
        async with mcp_session(app, kit.token(**claims)) as session:
            return await session.call_tool("suggest_external_mappings", args or {})
    finally:
        te._aiohttp_fetch = original  # type: ignore[assignment]


async def call_propose(
    make_deps: DepsFactory,
    kit: AuthKit,
    args: dict[str, Any] | None = None,
    **claims: Any,
) -> Any:
    app = create_server_app(deps_with_fake_proposals(make_deps))
    async with mcp_session(app, kit.token(**claims)) as session:
        return await session.call_tool("propose_external_mappings", args or {})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_entity_tools_registered(make_deps: DepsFactory) -> None:
    tools = {t.name: t for t in await create_mcp(deps_with_fake_proposals(make_deps)).list_tools()}
    assert ALL_ENTITY_TOOLS <= set(tools), f"Missing: {ALL_ENTITY_TOOLS - set(tools)}"


async def test_suggest_is_read_only(make_deps: DepsFactory) -> None:
    tools = {t.name: t for t in await create_mcp(fake_deps(make_deps)).list_tools()}
    ann = tools["suggest_external_mappings"].annotations
    assert ann is not None
    assert ann.read_only_hint is True
    assert ann.destructive_hint is False


async def test_propose_is_not_read_only(make_deps: DepsFactory) -> None:
    tools = {t.name: t for t in await create_mcp(deps_with_fake_proposals(make_deps)).list_tools()}
    ann = tools["propose_external_mappings"].annotations
    assert ann is not None
    assert ann.read_only_hint is False
    assert ann.destructive_hint is False


# ---------------------------------------------------------------------------
# suggest_external_mappings
# ---------------------------------------------------------------------------


async def test_suggest_returns_candidates(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_suggest(
        make_deps,
        kit,
        {"model": "retail", "concepts": "all", "external_kg": "wikidata"},
        scope=READ,
    )
    data = structured(result)
    assert "error" not in data, data.get("error")
    assert data["model"] == "retail"
    assert data["external_kg"] == "wikidata"
    assert isinstance(data["candidates"], list)
    assert isinstance(data["total_concepts_searched"], int)


async def test_suggest_requires_read_scope(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_suggest(
        make_deps,
        kit,
        {"model": "retail", "concepts": "all"},
        scope=None,
    )
    data = structured(result)
    assert data["error"]["code"] == "forbidden"


async def test_suggest_unknown_model_returns_not_found(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    result = await call_suggest(
        make_deps,
        kit,
        {"model": "no-such-model", "concepts": "all"},
        scope=READ,
    )
    data = structured(result)
    assert data["error"]["code"] == "not_found"


async def test_suggest_specific_concepts(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_suggest(
        make_deps,
        kit,
        {"model": "retail", "concepts": ["retail.Order"], "external_kg": "wikidata"},
        scope=READ,
    )
    data = structured(result)
    assert "error" not in data, data.get("error")
    # the FQN filter matches (it used to compare FQNs against IRIs and find nothing) and the
    # search uses the prefLabel; the candidate carries the real FQN
    assert data["total_concepts_searched"] == 1
    assert [c["concept_fqn"] for c in data["candidates"]] == ["retail.Order"]
    assert data["candidates"][0]["label"] == "Order"


async def test_suggest_unknown_concept_is_not_found(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_suggest(
        make_deps, kit, {"model": "retail", "concepts": ["retail.Nope"]}, scope=READ
    )
    assert structured(result)["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# propose_external_mappings
# ---------------------------------------------------------------------------


async def test_propose_creates_draft(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_propose(
        make_deps,
        kit,
        {
            "model": "retail",
            "mappings": [
                {
                    "concept_fqn": "retail.Customer",
                    "target_uri": "http://www.wikidata.org/entity/Q12345",
                    "kind": "exactMatch",
                }
            ],
        },
        scope=PROPOSE,
    )
    data = structured(result)
    assert "error" not in data, data.get("error")
    assert "proposal_id" in data
    assert data["status"] == "draft"


async def test_propose_multiple_mappings(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_propose(
        make_deps,
        kit,
        {
            "model": "retail",
            "mappings": [
                {
                    "concept_fqn": "retail.Customer",
                    "target_uri": "http://www.wikidata.org/entity/Q12345",
                    "kind": "closeMatch",
                },
                {
                    "concept_fqn": "retail.Order",
                    "target_uri": "https://schema.org/Order",
                    "kind": "exactMatch",
                },
            ],
        },
        scope=PROPOSE,
    )
    data = structured(result)
    assert "error" not in data, data.get("error")
    assert "proposal_id" in data


async def test_propose_requires_propose_scope(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_propose(
        make_deps,
        kit,
        {
            "model": "retail",
            "mappings": [
                {
                    "concept_fqn": "retail.Customer",
                    "target_uri": "http://www.wikidata.org/entity/Q12345",
                    "kind": "exactMatch",
                }
            ],
        },
        scope=READ,  # read scope is not enough
    )
    data = structured(result)
    assert data["error"]["code"] == "forbidden"


async def test_propose_external_mappings_is_audited_and_rechecked(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    seen: dict[str, Any] = {}

    class Rec(_ExtendedFakeProposals):
        async def propose_operations(self, model: str, operations: Any, **kw: Any) -> Any:
            seen.update(kw)
            return await super().propose_operations(model, operations, **kw)

    deps = fake_deps(make_deps)
    deps.proposals = Rec()  # pyright: ignore[reportAttributeAccessIssue]
    args = {
        "model": "retail",
        "mappings": [{"concept_fqn": "retail.Order", "target_uri": "http://x.org/Q1"}],
    }
    async with mcp_session(create_server_app(deps), kit.token(scope=PROPOSE)) as session:
        result = await session.call_tool("propose_external_mappings", args)
    assert "error" not in structured(result)
    assert seen["recheck_pending"] is True
