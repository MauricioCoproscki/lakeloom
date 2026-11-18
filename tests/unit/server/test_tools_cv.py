"""MCP tools for CV/Thesaurus/Taxonomy authoring (M5b).

Tests focus on:
- Tool registration, annotations and scope guards (FR-AGT-06)
- ``list_cv_candidates``: tag normalisation
- ``validate_thesaurus``: delegates to thesaurus_builder (integration with SKOS graph)
- ``propose_add_term``: creates a ``direct``-origin draft proposal
- Scope guard: read tools require lakeloom:read; write tools require lakeloom:propose
"""

from __future__ import annotations

from typing import Any

import pytest

from kit import AuthKit, DepsFactory, mcp_session
from test_tools_proposals import FakeProposals, proposal
from test_tools_semantic import fake_deps, structured, text

from lakeloom.core.audit import current_audit
from lakeloom.server.mcp_app import create_mcp, create_server_app

READ = "lakeloom:read"
PROPOSE = "lakeloom:propose"

READ_TOOLS = {
    "list_cv_candidates",
    "validate_thesaurus",
    "suggest_related_terms",
    "assess_taxonomy_coverage",
}
WRITE_TOOLS = {
    "propose_add_term",
    "propose_add_hierarchy",
    "propose_deprecate",
    "propose_add_scope_note",
}
ALL_CV_TOOLS = READ_TOOLS | WRITE_TOOLS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def deps_with_fake_proposals(make_deps: DepsFactory) -> Any:
    """Deps wired with a FakeProposals that also handles propose_operations."""

    deps = fake_deps(make_deps)
    fake = _ExtendedFakeProposals()
    deps.proposals = fake  # pyright: ignore[reportAttributeAccessIssue]
    return deps


class _ExtendedFakeProposals(FakeProposals):
    """FakeProposals extended with propose_operations (used by CV write tools)."""

    async def propose_operations(self, model: str, operations: Any, **kw: Any) -> Any:
        trail = current_audit()
        p = proposal()
        trail.model, trail.proposal_id, trail.change_set_id = p.model, p.id, "cs-n"
        return p


async def call_read(
    make_deps: DepsFactory,
    kit: AuthKit,
    tool: str,
    args: dict[str, Any] | None = None,
    **claims: Any,
) -> Any:
    """Call a READ tool (uses real model graph, no fake proposals needed)."""
    app = create_server_app(fake_deps(make_deps))
    async with mcp_session(app, kit.token(**claims)) as session:
        return await session.call_tool(tool, args or {})


async def call_write(
    make_deps: DepsFactory,
    kit: AuthKit,
    tool: str,
    args: dict[str, Any] | None = None,
    **claims: Any,
) -> Any:
    """Call a WRITE tool (uses fake proposals)."""
    app = create_server_app(deps_with_fake_proposals(make_deps))
    async with mcp_session(app, kit.token(**claims)) as session:
        return await session.call_tool(tool, args or {})


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


async def test_cv_tools_registered(make_deps: DepsFactory) -> None:
    tools = {t.name: t for t in await create_mcp(deps_with_fake_proposals(make_deps)).list_tools()}
    assert ALL_CV_TOOLS <= set(tools), f"Missing: {ALL_CV_TOOLS - set(tools)}"


async def test_read_tools_are_read_only(make_deps: DepsFactory) -> None:
    tools = {t.name: t for t in await create_mcp(deps_with_fake_proposals(make_deps)).list_tools()}
    for name in READ_TOOLS:
        ann = tools[name].annotations
        assert ann is not None
        assert ann.read_only_hint is True, f"{name} should be read_only"
        assert ann.destructive_hint is False


async def test_write_tools_are_not_read_only(make_deps: DepsFactory) -> None:
    tools = {t.name: t for t in await create_mcp(deps_with_fake_proposals(make_deps)).list_tools()}
    for name in WRITE_TOOLS:
        ann = tools[name].annotations
        assert ann is not None
        assert ann.read_only_hint is False, f"{name} should not be read_only"
        assert ann.destructive_hint is False


# ---------------------------------------------------------------------------
# list_cv_candidates
# ---------------------------------------------------------------------------


async def test_list_cv_candidates_normalises_tags(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_read(
        make_deps, kit, "list_cv_candidates",
        {"tags": ["PII.Email", "Domain.Order_Management", "!!!"]},
        scope=READ,
    )
    data = structured(result)
    assert data["total_input"] == 3
    assert data["total_valid"] == 2
    names = [c["name"] for c in data["candidates"]]
    assert "Email" in names
    assert "Order Management" in names


async def test_list_cv_candidates_frequency_map_sorts(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    result = await call_read(
        make_deps, kit, "list_cv_candidates",
        {
            "tags": ["PII.Email", "Domain.Order"],
            "frequency_map": {"PII.Email": 5, "Domain.Order": 20},
        },
        scope=READ,
    )
    data = structured(result)
    assert data["candidates"][0]["name"] == "Order"


async def test_list_cv_candidates_requires_read_scope(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    # Token with no scope at all → forbidden
    result = await call_read(
        make_deps, kit, "list_cv_candidates",
        {"tags": ["PII.Email"]},
        scope=None,  # drops the scope claim
    )
    data = structured(result)
    assert data["error"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# validate_thesaurus (uses fake model's SKOS graph)
# ---------------------------------------------------------------------------


async def test_validate_thesaurus_ok(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_read(
        make_deps, kit, "validate_thesaurus",
        {"model": "retail"},
        scope=READ,
    )
    data = structured(result)
    assert data["model"] == "retail"
    assert isinstance(data["total_concepts"], int)
    assert isinstance(data["error_count"], int)


async def test_validate_thesaurus_missing_model(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_read(
        make_deps, kit, "validate_thesaurus",
        {"model": "no-such-model"},
        scope=READ,
    )
    data = structured(result)
    assert data["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# assess_taxonomy_coverage
# ---------------------------------------------------------------------------


async def test_assess_taxonomy_coverage(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_read(
        make_deps, kit, "assess_taxonomy_coverage",
        {"model": "retail", "topics": ["Order", "Customer"]},
        scope=READ,
    )
    data = structured(result)
    assert data["model"] == "retail"
    assert 0.0 <= data["coverage_ratio"] <= 1.0


# ---------------------------------------------------------------------------
# propose_add_term
# ---------------------------------------------------------------------------


async def test_propose_add_term_creates_draft(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_write(
        make_deps, kit, "propose_add_term",
        {
            "model": "retail",
            "terms": [{"name": "Invoice", "definition": "A billing document."}],
        },
        scope=PROPOSE,
    )
    data = structured(result)
    assert "error" not in data, f"Tool returned error: {data.get('error')}"
    assert data["status"] == "draft"
    assert data["proposal_id"]


async def test_propose_add_term_requires_propose_scope(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    # Read scope is not enough for a write tool.
    result = await call_write(
        make_deps, kit, "propose_add_term",
        {"model": "retail", "terms": [{"name": "Invoice"}]},
        scope=READ,
    )
    data = structured(result)
    assert data["error"]["code"] == "forbidden"


async def test_propose_add_term_multiple_terms(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_write(
        make_deps, kit, "propose_add_term",
        {
            "model": "retail",
            "terms": [
                {"name": "Shipment"},
                {"name": "Return", "broader": "retail.Order"},
            ],
        },
        scope=PROPOSE,
    )
    data = structured(result)
    assert data["status"] == "draft"


# ---------------------------------------------------------------------------
# propose_add_hierarchy
# ---------------------------------------------------------------------------


async def test_propose_add_hierarchy(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call_write(
        make_deps, kit, "propose_add_hierarchy",
        {
            "model": "retail",
            "parent_name": "PaymentMethod",
            "children": ["CreditCard", "BankTransfer"],
            "parent_definition": "A means of settling an invoice.",
        },
        scope=PROPOSE,
    )
    data = structured(result)
    assert data["status"] == "draft"


# ---------------------------------------------------------------------------
# propose_add_scope_note
# ---------------------------------------------------------------------------


async def test_propose_add_scope_note_missing_concept(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    result = await call_write(
        make_deps, kit, "propose_add_scope_note",
        {
            "model": "retail",
            "concept": "NonExistentConcept9999",
            "note": "Should fail.",
        },
        scope=PROPOSE,
    )
    data = structured(result)
    assert data["error"]["code"] == "not_found"
