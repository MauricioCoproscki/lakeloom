"""MCP tools for entity linking against external Knowledge Graphs (M6a).

Two tools:

READ tools (scope ``lakeloom:read``, read_only_hint=True):
  - ``suggest_external_mappings`` — search Wikidata / schema.org for concept matches

WRITE tools (scope ``lakeloom:write``, read_only_hint=False):
  - ``propose_external_mappings`` — turn approved candidates into AddMapping proposals
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from lakeloom.core.entity_linking import (
    ApprovedMapping,
    MappingCandidate,
    SchemaOrgMatch,
    WikidataMatch,
    candidates_to_ops,
    lookup_schema_org,
    lookup_wikidata,
)
from lakeloom.server.auth import Principal
from lakeloom.server.http_app import AppDeps
from lakeloom.server.tools_common import (
    PROPOSE_SCOPE,
    READ_SCOPE,
    client_name,
    guarded,
)
from lakeloom.server.tools_proposals import NEXT_STEP, ProposalResult

__all__ = ["register_entity_tools"]

_log = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
WRITE_ANN = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)

_SCOPE_READ = "Requires scope `lakeloom:read`."
_SCOPE_WRITE = "Requires scope `lakeloom:propose`."

SUGGEST_DESCRIPTION = f"""\
Search an external Knowledge Graph (Wikidata or schema.org) for entities that match the \
concepts of a semantic model.  Returns candidate URIs with similarity scores and suggested \
SKOS mapping kinds (exactMatch, closeMatch, relatedMatch) for human review.

``model``: slug of the semantic model whose concepts to search for.
``concepts``: list of concept FQNs to look up, or ``"all"`` to search every concept in the model.
``external_kg``: ``"wikidata"`` (default) or ``"schema.org"``.
``lang``: BCP 47 language code for Wikidata labels (default ``"en"``).
``limit_per_concept``: max candidates per concept (1–10, default 3).

Returns a list of candidates.  Read-only, no side effects.  Unknown model → ``not_found``.

{_SCOPE_READ}"""

PROPOSE_DESCRIPTION = f"""\
Create a proposal that adds SKOS external-mapping triples for a list of approved concept–URI \
pairs.  Each entry becomes an ``AddMapping`` operation stored in a new draft proposal.

``model``: model slug.
``mappings``: list of approved mappings, each with ``concept_fqn``, ``target_uri``, and \
``kind`` (exactMatch / closeMatch / broadMatch / narrowMatch / relatedMatch, default closeMatch).

Returns a ProposalResult.  Requires human approval before changes are applied to OpenMetadata.

Requires scope `lakeloom:propose`."""


# ---------------------------------------------------------------------------
# aiohttp fetch helper
# ---------------------------------------------------------------------------


async def _aiohttp_fetch(url: str, params: dict[str, str]) -> Any:
    """Fetch JSON from ``url`` with optional query ``params`` via httpx."""
    async with httpx.AsyncClient(
        headers={"User-Agent": "lakeloom-entity-linker/1.0"},
        timeout=10.0,
        follow_redirects=True,
    ) as client:
        resp = await client.get(url, params=params or None)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class MappingCandidateResult(BaseModel):
    concept_fqn: str
    candidate_uri: str
    label: str
    description: str
    match_score: float
    mapping_kind: str
    source: str  # "wikidata" or "schema.org"


class SuggestResult(BaseModel):
    model: str
    external_kg: str
    total_concepts_searched: int
    candidates: list[MappingCandidateResult]


class ProposeMappingsResult(BaseModel):
    model: str
    mappings_count: int


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_entity_tools(mcp: MCPServer, deps: AppDeps) -> None:
    """Register the entity-linking tools on ``mcp``."""

    # ------------------------------------------------------------------
    # READ: suggest_external_mappings
    # ------------------------------------------------------------------

    async def suggest_external_mappings(
        model: Annotated[str, Field(description="Semantic model slug.")],
        ctx: Context,
        concepts: Annotated[
            list[str] | Literal["all"],
            Field(
                description=(
                    "List of concept FQNs (e.g. ['retail.Customer']) or 'all' "
                    "to search every concept in the model."
                )
            ),
        ] = "all",
        external_kg: Annotated[
            Literal["wikidata", "schema.org"],
            Field(description="External KG to search: 'wikidata' (default) or 'schema.org'."),
        ] = "wikidata",
        lang: Annotated[
            str,
            Field(description="BCP 47 language code for Wikidata labels (default 'en')."),
        ] = "en",
        limit_per_concept: Annotated[
            int,
            Field(description="Max candidates per concept (1–10, default 3).", ge=1, le=10),
        ] = 3,
    ) -> Annotated[CallToolResult, SuggestResult]:
        async def body(_: Principal) -> CallToolResult:
            from rdflib.namespace import RDF
            from rdflib.namespace import SKOS as _SKOS

            # Enumerate concept names from SKOS graph.
            graph = await deps.models.graph(model)
            concept_iris = [str(s) for s in graph.subjects(RDF.type, _SKOS.Concept)]

            # Determine which concepts to search.
            if concepts == "all":
                search_iris = concept_iris[:100]  # safety cap
            else:
                # Accept FQNs; find matching IRIs by suffix.
                fqn_set = set(concepts)
                search_iris = [
                    iri for iri in concept_iris if any(iri.endswith(fqn) for fqn in fqn_set)
                ]

            candidates: list[MappingCandidateResult] = []
            for iri in search_iris:
                # Derive a human-readable name from the IRI (last path segment).
                name = iri.rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
                concept_fqn = f"{model}.{name}"

                if external_kg == "wikidata":
                    matches: list[MappingCandidate] = await lookup_wikidata(
                        name, fetch=_aiohttp_fetch, lang=lang, limit=limit_per_concept
                    )
                else:
                    matches = await lookup_schema_org(
                        name, fetch=_aiohttp_fetch, limit=limit_per_concept
                    )

                for m in matches:
                    if isinstance(m, WikidataMatch):
                        candidates.append(
                            MappingCandidateResult(
                                concept_fqn=concept_fqn,
                                candidate_uri=m.entity_uri,
                                label=m.label,
                                description=m.description,
                                match_score=round(m.match_score, 3),
                                mapping_kind=m.kind,
                                source="wikidata",
                            )
                        )
                    elif isinstance(m, SchemaOrgMatch):
                        candidates.append(
                            MappingCandidateResult(
                                concept_fqn=concept_fqn,
                                candidate_uri=m.type_uri,
                                label=m.label,
                                description=m.description,
                                match_score=round(m.match_score, 3),
                                mapping_kind=m.kind,
                                source="schema.org",
                            )
                        )

            data = SuggestResult(
                model=model,
                external_kg=external_kg,
                total_concepts_searched=len(search_iris),
                candidates=candidates,
            )
            lines = [f"## External Mapping Candidates — {model} → {external_kg}"]
            lines.append(
                f"Searched {len(search_iris)} concepts, found {len(candidates)} candidates.\n"
            )
            for c in candidates:
                lines.append(
                    f"- **{c.concept_fqn}** → [{c.label}]({c.candidate_uri}) "
                    f"({c.mapping_kind}, score={c.match_score})"
                )
                if c.description:
                    lines.append(f"  _{c.description}_")
            md = "\n".join(lines)

            return CallToolResult(
                content=[TextContent(type="text", text=md)],
                structured_content=data.model_dump(mode="json"),
            )

        return await guarded("suggest_external_mappings", ctx, READ_SCOPE, body)

    mcp.add_tool(
        suggest_external_mappings,
        name="suggest_external_mappings",
        title="Suggest external mappings",
        description=SUGGEST_DESCRIPTION,
        annotations=READ_ONLY,
    )

    # ------------------------------------------------------------------
    # WRITE: propose_external_mappings
    # ------------------------------------------------------------------

    async def propose_external_mappings(
        model: Annotated[str, Field(description="Semantic model slug.")],
        mappings: Annotated[
            list[ApprovedMapping],
            Field(
                description=(
                    "List of approved mappings. Each has: concept_fqn (str), "
                    "target_uri (str, from suggest_external_mappings), "
                    "kind (exactMatch|closeMatch|broadMatch|narrowMatch|relatedMatch)."
                )
            ),
        ],
        ctx: Context,
    ) -> Annotated[CallToolResult, ProposalResult]:
        async def body(principal: Principal) -> CallToolResult:
            ops = candidates_to_ops(mappings)
            if not ops:
                from lakeloom.server.tools_common import tool_error
                return tool_error("validation_error", "No mappings provided.", "")

            p = await deps.proposals.propose_operations(
                model,
                ops,
                origin="direct",
                principal=principal,
                client=client_name(ctx),
                tool="propose_external_mappings",
            )
            summary = deps.proposals.summary_markdown(p)
            text = f"{summary}\n{NEXT_STEP}\n" if p.status == "draft" else summary
            data = ProposalResult(
                proposal_id=p.id,
                status=p.status,
                summary=summary,
                proposal=p,
                matches=p.matches,
                warnings=p.warnings,
            )
            return CallToolResult(
                content=[TextContent(type="text", text=text)],
                structured_content=data.model_dump(mode="json"),
            )

        return await guarded("propose_external_mappings", ctx, PROPOSE_SCOPE, body)

    mcp.add_tool(
        propose_external_mappings,
        name="propose_external_mappings",
        title="Propose external mappings",
        description=PROPOSE_DESCRIPTION,
        annotations=WRITE_ANN,
    )
