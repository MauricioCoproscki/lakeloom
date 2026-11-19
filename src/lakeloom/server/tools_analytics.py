"""MCP tools for ontology graph analytics (M6b).

Two READ tools (scope ``lakeloom:read``, read_only_hint=True):

  - ``graph_stats``      — structural stats (depth, hubs, orphans, relations)
  - ``assess_coverage``  — realization coverage ratio per concept
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field
from rdflib import Graph

from lakeloom.core.annotations import effective_graph
from lakeloom.core.concepts import AssetRef
from lakeloom.core.graph_analytics import (
    ConceptStats,
    CoverageStats,
    HierarchyReport,
    analytics_markdown,
    concept_stats,
    hierarchy_report,
    realization_coverage,
)
from lakeloom.server.auth import Principal
from lakeloom.server.http_app import AppDeps
from lakeloom.server.tools_common import READ_SCOPE, guarded

__all__ = [
    "MAX_CONCEPTS_SCANNED",
    "RealizationIndex",
    "load_graph_and_realizations",
    "load_realization_index",
    "register_analytics_tools",
]

_log = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

_SCOPE_READ = "Requires scope `lakeloom:read`."

GRAPH_STATS_DESCRIPTION = f"""\
Compute structural statistics for a semantic model's SKOS graph:

- **Concept stats**: total concepts and relations, average and maximum depth, \
isolated concepts, hub concepts (high degree).
- **Hierarchy report**: root and leaf counts, polyhierarchy nodes, orphan \
concepts, top-concept declarations.

Returns a Markdown report and structured JSON in ``structured_content``.  \
Read-only, no side effects.  Unknown model → ``not_found``.

{_SCOPE_READ}"""

ASSESS_COVERAGE_DESCRIPTION = f"""\
Assess realization coverage for a semantic model: how many concepts have at \
least one physical asset (table or column) realizing them in OpenMetadata.

Returns coverage ratio, counts, and the top-realized concepts.  Read-only.

{_SCOPE_READ}"""


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class GraphStatsResult(BaseModel):
    model: str
    total_concepts: int
    total_relations: int
    avg_depth: float
    max_depth: int
    isolated_concepts: int
    hub_count: int
    root_count: int
    leaf_count: int
    polyhierarchy_count: int
    orphan_count: int
    total_top_concepts: int
    summary: str


class CoverageResult(BaseModel):
    model: str
    total_concepts: int
    concepts_with_realizations: int
    concepts_without_realizations: int
    coverage_ratio: float
    top_realized: list[str]
    summary: str


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


MAX_CONCEPTS_SCANNED = 2000
_LOAD_CONCURRENCY = 8


@dataclass
class RealizationIndex:
    """A model graph plus the assets realizing each concept (keyed by concept IRI).

    ``truncated`` is set when the model has more than :data:`MAX_CONCEPTS_SCANNED`
    concepts or a concept's asset list was capped: statistics then undercount.
    """

    graph: Graph
    assets: dict[str, list[AssetRef]] = field(default_factory=dict[str, list[AssetRef]])
    truncated: bool = False

    @property
    def realizations(self) -> dict[str, list[str]]:
        """``{iri: [table_fqn or table_fqn.column]}`` for concepts with any asset."""
        return {
            iri: [f"{a.fqn}.{a.column}" if a.column else a.fqn for a in items]
            for iri, items in self.assets.items()
            if items
        }

    def tables(self) -> dict[str, set[str]]:
        """``{iri: {table_fqn}}``: column realizations collapse onto their table."""
        return {iri: {a.fqn for a in items} for iri, items in self.assets.items() if items}


async def load_realization_index(deps: AppDeps, model: str) -> RealizationIndex:
    """Load ``model``'s graph and the realizations of up to MAX_CONCEPTS_SCANNED concepts."""
    from rdflib.namespace import RDF
    from rdflib.namespace import SKOS as _SKOS

    from lakeloom.core.concepts import find_assets_for_concept

    graph = await effective_graph(deps.models, getattr(deps, "proposal_repo", None), model)
    iris = sorted(str(s) for s in graph.subjects(RDF.type, _SKOS.Concept))
    out = RealizationIndex(graph, truncated=len(iris) > MAX_CONCEPTS_SCANNED)
    sem = asyncio.Semaphore(_LOAD_CONCURRENCY)

    async def one(iri: str) -> None:
        async with sem:
            page = await find_assets_for_concept(deps.models, deps.om, iri, limit=200)
        out.assets[iri] = list(page.items)
        if page.truncated or page.next_cursor:
            out.truncated = True

    await asyncio.gather(*(one(i) for i in iris[:MAX_CONCEPTS_SCANNED]))
    return out


async def load_graph_and_realizations(
    deps: AppDeps,
    model: str,
) -> tuple[Graph, dict[str, list[str]]]:
    """Return (rdflib Graph, realization index) for ``model``."""
    index = await load_realization_index(deps, model)
    return index.graph, index.realizations


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_analytics_tools(mcp: MCPServer, deps: AppDeps) -> None:
    """Register the two graph analytics tools on ``mcp``."""

    # ------------------------------------------------------------------
    # READ: graph_stats
    # ------------------------------------------------------------------

    async def graph_stats(
        model: Annotated[str, Field(description="Semantic model slug.")],
        ctx: Context,
    ) -> Annotated[CallToolResult, GraphStatsResult]:
        async def body(_: Principal) -> CallToolResult:
            graph, _real = await load_graph_and_realizations(deps, model)

            stats: ConceptStats = concept_stats(graph)
            hier: HierarchyReport = hierarchy_report(graph)
            # Pass empty realizations for graph_stats (coverage is a separate tool).
            cov: CoverageStats = realization_coverage(graph, {})

            md = analytics_markdown(stats, hier, cov)
            data = GraphStatsResult(
                model=model,
                total_concepts=stats.total_concepts,
                total_relations=stats.total_relations,
                avg_depth=stats.avg_depth,
                max_depth=stats.max_depth,
                isolated_concepts=stats.isolated_concepts,
                hub_count=len(stats.hub_concepts),
                root_count=len(hier.roots),
                leaf_count=len(hier.leaves),
                polyhierarchy_count=len(hier.polyhierarchy_nodes),
                orphan_count=len(hier.orphan_concepts),
                total_top_concepts=hier.total_top_concepts,
                summary=md,
            )
            return CallToolResult(
                content=[TextContent(type="text", text=md)],
                structured_content=data.model_dump(mode="json"),
            )

        return await guarded("graph_stats", ctx, READ_SCOPE, body)

    mcp.add_tool(
        graph_stats,
        name="graph_stats",
        title="Graph stats",
        description=GRAPH_STATS_DESCRIPTION,
        annotations=READ_ONLY,
    )

    # ------------------------------------------------------------------
    # READ: assess_coverage
    # ------------------------------------------------------------------

    async def assess_coverage(
        model: Annotated[str, Field(description="Semantic model slug.")],
        ctx: Context,
    ) -> Annotated[CallToolResult, CoverageResult]:
        async def body(_: Principal) -> CallToolResult:
            graph, realizations = await load_graph_and_realizations(deps, model)
            cov: CoverageStats = realization_coverage(graph, realizations)

            pct = round(cov.coverage_ratio * 100, 1)
            lines = [
                f"## Realization Coverage — {model}",
                f"**{cov.concepts_with_realizations}/{cov.total_concepts} concepts realized "
                f"({pct}%)**",
                f"- Without realizations: {cov.concepts_without_realizations}",
            ]
            if cov.top_realized:
                lines.append("\n**Top realized:**")
                for iri in cov.top_realized[:10]:
                    lines.append(f"  - `{iri}`")
            md = "\n".join(lines)

            data = CoverageResult(
                model=model,
                total_concepts=cov.total_concepts,
                concepts_with_realizations=cov.concepts_with_realizations,
                concepts_without_realizations=cov.concepts_without_realizations,
                coverage_ratio=cov.coverage_ratio,
                top_realized=cov.top_realized,
                summary=md,
            )
            return CallToolResult(
                content=[TextContent(type="text", text=md)],
                structured_content=data.model_dump(mode="json"),
            )

        return await guarded("assess_coverage", ctx, READ_SCOPE, body)

    mcp.add_tool(
        assess_coverage,
        name="assess_coverage",
        title="Assess coverage",
        description=ASSESS_COVERAGE_DESCRIPTION,
        annotations=READ_ONLY,
    )
