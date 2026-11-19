"""MCP tools for SKOS export with enriched annotations (M7).

Three READ tools (scope ``lakeloom:read``, read_only_hint=True):

  - ``export_skos``          — Turtle export with scope/history notes and deprecated markers
                               materialised from applied proposals.
  - ``skos_annotation_stats`` — Summary of how many concepts carry each annotation type.
  - ``skos_deprecated_list``  — List of deprecated concepts for a model.

The OM native ontology API does not store annotation predicates (scopeNote,
historyNote, owl:deprecated).  These tools recover them by scanning applied
proposals for the lakeloom-native ops and injecting them via
:func:`~lakeloom.rdf.skos_enrichment.enrich_graph_from_ops` before the SKOS
projection is built.
"""

from __future__ import annotations

import logging
from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from lakeloom.core.annotations import applied_operations
from lakeloom.rdf.skos import skos_projection
from lakeloom.rdf.skos_enrichment import enrich_graph_from_ops
from lakeloom.server.auth import Principal
from lakeloom.server.http_app import AppDeps
from lakeloom.server.tools_common import READ_SCOPE, guarded

__all__ = ["register_skos_tools"]

_log = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

_SCOPE_READ = "Requires scope `lakeloom:read`."

EXPORT_SKOS_DESCRIPTION = f"""\
Export a semantic model as a SKOS Turtle file enriched with annotation \
predicates that are stored in Lakeloom's proposal log but not in OpenMetadata:

- **skos:scopeNote** — scope constraints added via ``propose_add_scope_note``
- **skos:historyNote** — editorial history added via ``propose_change`` (``add_history_note``)
- **owl:deprecated true** — concepts flagged via ``propose_deprecate``

The export runs ``skos_projection`` on the enriched graph so only SKOS \
triples appear in the output.  Read-only, no side effects.  Unknown model → \
``not_found``.

{_SCOPE_READ}"""

SKOS_ANNOTATION_STATS_DESCRIPTION = f"""\
Return annotation coverage statistics for a semantic model's SKOS graph:
how many concepts carry scope notes, history notes, and ``owl:deprecated`` \
markers.  Read-only.

{_SCOPE_READ}"""

SKOS_DEPRECATED_LIST_DESCRIPTION = f"""\
List the IRIs and preferred labels of deprecated concepts in a semantic model.
Deprecation markers (``owl:deprecated true``) come from the proposal log.  \
Read-only.

{_SCOPE_READ}"""

_PAGE_LIMIT = 200  # max proposals to scan per model


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class ExportSkosResult(BaseModel):
    model: str
    concepts: int
    scope_notes: int
    history_notes: int
    deprecated: int
    turtle_bytes: int


class AnnotationStatsResult(BaseModel):
    model: str
    total_concepts: int
    with_scope_note: int
    with_history_note: int
    with_definition: int
    deprecated: int


class DeprecatedConceptEntry(BaseModel):
    iri: str
    pref_label: str
    reason: str | None = None


class DeprecatedListResult(BaseModel):
    model: str
    total_deprecated: int
    concepts: list[DeprecatedConceptEntry]


# ---------------------------------------------------------------------------
# Shared helper: load enriched SKOS graph for a model
# ---------------------------------------------------------------------------


async def _enriched_graph(deps: AppDeps, model: str):
    """Return an rdflib Graph enriched with annotation ops from applied proposals."""
    from rdflib import Graph

    # 1. base graph from OM
    graph: Graph = await deps.models.graph(model)

    # 2. every annotation op of the model's applied proposals, in application order
    ops = await applied_operations(deps.proposal_repo, model)

    # 3. enrich in-place (copy so we don't mutate the shared cached graph)
    import copy

    enriched = copy.deepcopy(graph)
    return enrich_graph_from_ops(enriched, ops)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_skos_tools(mcp: MCPServer, deps: AppDeps) -> None:
    """Register the SKOS export and annotation tools on ``mcp``."""

    # ------------------------------------------------------------------
    # READ: export_skos
    # ------------------------------------------------------------------

    async def export_skos(
        model: Annotated[str, Field(description="Semantic model slug.")],
        ctx: Context,
    ) -> Annotated[CallToolResult, ExportSkosResult]:
        async def body(_: Principal) -> CallToolResult:
            from rdflib.namespace import OWL, SKOS

            enriched = await _enriched_graph(deps, model)
            projection = skos_projection(enriched)

            turtle_bytes = projection.serialize(format="turtle").encode("utf-8")

            # Count annotation triples in the enriched graph (before projection filters).
            from rdflib.namespace import RDF

            concepts_set = set(enriched.subjects(RDF.type, SKOS.Concept))
            scope_notes = sum(1 for c in concepts_set if (c, SKOS.scopeNote, None) in enriched)
            history_notes = sum(1 for c in concepts_set if (c, SKOS.historyNote, None) in enriched)
            deprecated = sum(1 for c in concepts_set if (c, OWL.deprecated, None) in enriched)

            data = ExportSkosResult(
                model=model,
                concepts=len(concepts_set),
                scope_notes=scope_notes,
                history_notes=history_notes,
                deprecated=deprecated,
                turtle_bytes=len(turtle_bytes),
            )
            turtle_str = turtle_bytes.decode("utf-8")
            return CallToolResult(
                content=[TextContent(type="text", text=turtle_str)],
                structured_content=data.model_dump(mode="json"),
            )

        return await guarded("export_skos", ctx, READ_SCOPE, body)

    mcp.add_tool(
        export_skos,
        name="export_skos",
        title="Export SKOS",
        description=EXPORT_SKOS_DESCRIPTION,
        annotations=READ_ONLY,
    )

    # ------------------------------------------------------------------
    # READ: skos_annotation_stats
    # ------------------------------------------------------------------

    async def skos_annotation_stats(
        model: Annotated[str, Field(description="Semantic model slug.")],
        ctx: Context,
    ) -> Annotated[CallToolResult, AnnotationStatsResult]:
        async def body(_: Principal) -> CallToolResult:
            from rdflib.namespace import OWL, RDF, SKOS

            enriched = await _enriched_graph(deps, model)
            concepts = list(enriched.subjects(RDF.type, SKOS.Concept))

            with_scope = sum(1 for c in concepts if (c, SKOS.scopeNote, None) in enriched)
            with_history = sum(1 for c in concepts if (c, SKOS.historyNote, None) in enriched)
            with_def = sum(1 for c in concepts if (c, SKOS.definition, None) in enriched)
            depr = sum(1 for c in concepts if (c, OWL.deprecated, None) in enriched)

            lines = [
                f"## SKOS Annotation Stats — {model}",
                f"- Total concepts: {len(concepts)}",
                f"- With definition: {with_def}",
                f"- With scope note: {with_scope}",
                f"- With history note: {with_history}",
                f"- Deprecated: {depr}",
            ]
            md = "\n".join(lines)
            data = AnnotationStatsResult(
                model=model,
                total_concepts=len(concepts),
                with_scope_note=with_scope,
                with_history_note=with_history,
                with_definition=with_def,
                deprecated=depr,
            )
            return CallToolResult(
                content=[TextContent(type="text", text=md)],
                structured_content=data.model_dump(mode="json"),
            )

        return await guarded("skos_annotation_stats", ctx, READ_SCOPE, body)

    mcp.add_tool(
        skos_annotation_stats,
        name="skos_annotation_stats",
        title="SKOS annotation stats",
        description=SKOS_ANNOTATION_STATS_DESCRIPTION,
        annotations=READ_ONLY,
    )

    # ------------------------------------------------------------------
    # READ: skos_deprecated_list
    # ------------------------------------------------------------------

    async def skos_deprecated_list(
        model: Annotated[str, Field(description="Semantic model slug.")],
        ctx: Context,
    ) -> Annotated[CallToolResult, DeprecatedListResult]:
        async def body(_: Principal) -> CallToolResult:
            from rdflib.namespace import OWL, RDF, SKOS

            enriched = await _enriched_graph(deps, model)
            concepts = list(enriched.subjects(RDF.type, SKOS.Concept))

            entries: list[DeprecatedConceptEntry] = []
            for c in concepts:
                if (c, OWL.deprecated, None) not in enriched:
                    continue
                labels = list(enriched.objects(c, SKOS.prefLabel))
                label = str(labels[0]) if labels else str(c).rsplit("/", 1)[-1]
                # changeNote carries the deprecation reason.
                notes = list(enriched.objects(c, SKOS.changeNote))
                reason = str(notes[0]) if notes else None
                entries.append(DeprecatedConceptEntry(iri=str(c), pref_label=label, reason=reason))

            entries.sort(key=lambda e: e.pref_label)

            lines = [f"## Deprecated Concepts — {model}", f"Total: {len(entries)}", ""]
            for e in entries:
                reason_str = f" _(reason: {e.reason})_" if e.reason else ""
                lines.append(f"- **{e.pref_label}** `{e.iri}`{reason_str}")
            md = "\n".join(lines)

            data = DeprecatedListResult(
                model=model, total_deprecated=len(entries), concepts=entries
            )
            return CallToolResult(
                content=[TextContent(type="text", text=md)],
                structured_content=data.model_dump(mode="json"),
            )

        return await guarded("skos_deprecated_list", ctx, READ_SCOPE, body)

    mcp.add_tool(
        skos_deprecated_list,
        name="skos_deprecated_list",
        title="SKOS deprecated list",
        description=SKOS_DEPRECATED_LIST_DESCRIPTION,
        annotations=READ_ONLY,
    )
