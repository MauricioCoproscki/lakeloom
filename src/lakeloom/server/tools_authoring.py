"""Shared pieces of the direct-authoring MCP tools (vocabulary, taxonomy, thesaurus).

Every write goes through a proposal exactly like ``propose_from_scan``; nothing reaches
OpenMetadata until a reviewer approves it (``review_proposal``).
"""

from __future__ import annotations

from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel

from lakeloom.core.proposals import Proposal
from lakeloom.server.http_app import AppDeps
from lakeloom.server.tools_proposals import NEXT_STEP, ProposalResult

__all__ = [
    "DRAFT_NOTE",
    "MAX_TAGS",
    "MAX_TERMS_PER_CALL",
    "MAX_TOPICS",
    "READ_ONLY",
    "SCOPE_PROPOSE_TEXT",
    "SCOPE_READ_TEXT",
    "WRITE",
    "proposal_result",
    "resolve_in_model",
    "tool_result",
]

MAX_TERMS_PER_CALL = 500
MAX_TAGS = 1000
MAX_TOPICS = 50


async def resolve_in_model(deps: AppDeps, model: str, concept: str) -> str:
    """The FQN of ``concept`` (IRI, FQN or unambiguous name) within ``model`` only."""
    from lakeloom.core.concepts import resolve_concept

    ref = await resolve_concept(deps.models, deps.om, concept, within=[model])
    return ref.fqn


READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)

SCOPE_READ_TEXT = "Requires scope `lakeloom:read`."
SCOPE_PROPOSE_TEXT = "Requires scope `lakeloom:propose`."
DRAFT_NOTE = (
    "Nothing is applied: the change sets stay drafts until a human reviewer approves "
    "(review_proposal)."
)


def tool_result(text: str, data: BaseModel) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=data.model_dump(mode="json"),
    )


def proposal_result(p: Proposal, deps: AppDeps, *, next_step: bool = True) -> CallToolResult:
    summary = deps.proposals.summary_markdown(p)
    text = f"{summary}\n{NEXT_STEP}\n" if next_step and p.status == "draft" else summary
    data = ProposalResult(
        proposal_id=p.id,
        status=p.status,
        summary=summary,
        proposal=p,
        matches=p.matches,
        warnings=p.warnings,
    )
    return tool_result(text, data)
