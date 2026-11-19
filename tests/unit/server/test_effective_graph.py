"""Governance annotations of applied proposals are visible on every read path (audit B10/B11).

The OM graph never carries scope notes, history notes or deprecations; they live in the
proposal log. explain_concept, find_concepts and export_model must show them.
"""

from typing import Any

from kit import AuthKit, DepsFactory, mcp_session
from test_tools_proposals import proposal
from test_tools_semantic import fake_deps, structured, text

from lakeloom.core.operations import AddScopeNote, DeprecateConcept
from lakeloom.server.mcp_app import create_server_app

READ = "lakeloom:read"


def annotated(make_deps: DepsFactory) -> Any:
    deps = fake_deps(make_deps)
    deps.seed = [
        DeprecateConcept(
            op="deprecate_concept",
            fqn="retail.OrderDate",
            base_version="1",
            reason="dates move to the event model",
            successor="retail.MonetaryAmount",
        ),
        AddScopeNote(op="add_scope_note", concept_fqn="retail.Order", note="Only paid orders."),
    ]
    return deps


async def call(deps: Any, kit: AuthKit, tool: str, args: dict[str, Any]) -> Any:
    async with mcp_session(create_server_app(deps), kit.token(scope=READ)) as session:
        if getattr(deps, "seed", None):  # tables exist once the app has started
            p = proposal("applied", deps.seed).model_copy(update={"model": "retail"})
            await deps.proposal_repo.add(p)
        return await session.call_tool(tool, args)


async def test_explain_shows_deprecation_and_successor(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    result = await call(annotated(make_deps), kit, "explain_concept", {"concept": "OrderDate"})
    data = structured(result)
    gov = data["governance"]
    assert gov["deprecated"] is True and gov["replaced_by"]["fqn"] == "retail.MonetaryAmount"
    assert "**Deprecated.** Use `MonetaryAmount`" in text(result)
    assert "dates move to the event model" in text(result)


async def test_explain_shows_scope_notes(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call(annotated(make_deps), kit, "explain_concept", {"concept": "Order"})
    assert structured(result)["governance"]["scope_notes"][0]["text"] == "Only paid orders."
    assert "## Scope notes" in text(result)


async def test_explain_without_annotations_has_no_governance(
    make_deps: DepsFactory, kit: AuthKit
) -> None:
    result = await call(fake_deps(make_deps), kit, "explain_concept", {"concept": "Order"})
    assert structured(result)["governance"] is None


async def test_find_concepts_flags_deprecated(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call(
        annotated(make_deps), kit, "find_concepts", {"query": "OrderDate", "model": "retail"}
    )
    assert "**(deprecated)**" in text(result)


async def test_export_latest_includes_annotations(make_deps: DepsFactory, kit: AuthKit) -> None:
    result = await call(
        annotated(make_deps), kit, "export_model", {"model": "retail", "format": "turtle"}
    )
    body = text(result)
    assert "owl:deprecated" in body and "Only paid orders." in body
    assert "isReplacedBy" in body
