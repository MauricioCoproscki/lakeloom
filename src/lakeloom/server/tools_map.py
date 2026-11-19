"""MCP tools for the MAP (Metadata Application Profile) workflow (M5c).

The MAP documents the semantic contract between a physical lake source and an ontology:
which column/table in the lake is realized by which concept. Lakeloom builds the
*implementation layer* of the book's MAP (physical element → concept), computed on demand
from a scan; the canonical layer (elements and obligations), crosswalks and versioned MAP
documents are not modelled yet.

Three tools:

READ tools (scope ``lakeloom:read``, read_only_hint=True):
  - ``build_map``    — build a MAP profile from a completed scan + model
  - ``validate_map`` — validate coverage and consistency of the MAP
  - ``export_map``   — export the MAP as Markdown or JSON

All tools are read-only: they query but never modify the model or scan store.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from lakeloom.core.map_profile import (
    MapProfile,
    MapReport,
    build_map,
    export_map_json,
    export_map_markdown,
    validate_map,
)
from lakeloom.server.auth import Principal
from lakeloom.server.http_app import AppDeps
from lakeloom.server.tools_common import (
    READ_SCOPE,
    guarded,
)

__all__ = ["register_map_tools"]

_log = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

_SCOPE_READ = "Requires scope `lakeloom:read`."

BUILD_MAP_DESCRIPTION = f"""\
Build a MAP (Metadata Application Profile) from a completed lake scan and a semantic \
model.  The MAP shows, for each scanned table and column, which concept from the ontology \
realizes it — the semantic contract between the physical source and the model. This \
is the implementation layer of a MAP (physical element → concept), computed from the scan \
on each call; it is not stored or versioned.

Steps performed:
1. Retrieve the scan record (table + column schema).
2. Enumerate all concepts in the model's SKOS graph.
3. For each concept, fetch its realizing assets from OpenMetadata.
4. Cross-reference physical columns with concept realizations to produce the MAP.

Returns a Markdown summary (canonical layer + implementation layer) and structured \
JSON in ``structured_content``.  Read-only, no side effects.  Unknown scan or model → \
``not_found``.

{_SCOPE_READ}"""

VALIDATE_MAP_DESCRIPTION = f"""\
Validate the MAP between a scan and a model for coverage and consistency:

- **MAP-01** (warning): tables with no concept mapping at all.
- **MAP-02** (warning): tables where some columns have no concept mapping.
- **MAP-03** (warning): scan record contains no tables.

Returns a validation report with counts and issue list.  Read-only.

{_SCOPE_READ}"""

EXPORT_MAP_DESCRIPTION = f"""\
Export the MAP between a scan and a model as a full document.

``format``:
- ``"markdown"`` (default): human-readable document with canonical and implementation \
  layers.
- ``"json"``: machine-readable JSON with ``canonical_layer`` and ``implementation_layer`` \
  dictionaries.

Read-only, no side effects.

{_SCOPE_READ}"""


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class MapProfileResult(BaseModel):
    source_id: str
    scan_id: str
    model_slug: str
    total_tables: int
    mapped_tables: int
    total_columns: int
    mapped_columns: int
    canonical_layer: dict[str, list[str]]
    implementation_layer: dict[str, str]
    summary: str


class MapReportResult(BaseModel):
    source_id: str
    scan_id: str
    model_slug: str
    total_tables: int
    mapped_tables: int
    total_columns: int
    mapped_columns: int
    coverage_ratio: float
    error_count: int
    warning_count: int
    summary: str


class MapExportResult(BaseModel):
    source_id: str
    scan_id: str
    model_slug: str
    format: str
    content: str


# ---------------------------------------------------------------------------
# Shared helper: build MAP from deps
# ---------------------------------------------------------------------------


async def _build_profile(
    deps: AppDeps,
    scan_id: str,
    model: str,
) -> MapProfile:
    """Collect live data and produce a :class:`MapProfile`."""
    from lakeloom.server.tools_analytics import load_realization_index

    scan_record = await deps.scans.get(scan_id)
    index = await load_realization_index(deps, model)
    return build_map(scan_record, model_slug=model, realizations=index.realizations)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_map_tools(mcp: MCPServer, deps: AppDeps) -> None:
    """Register the three MAP tools on ``mcp``."""

    # ------------------------------------------------------------------
    # READ: build_map_tool
    # ------------------------------------------------------------------

    async def build_map_tool(
        scan_id: Annotated[
            str,
            Field(
                description=(
                    "ID of the completed scan (returned by ``scan_lake_source`` or "
                    "the proposals made from it)."
                )
            ),
        ],
        model: Annotated[str, Field(description="Target model slug.")],
        ctx: Context,
    ) -> Annotated[CallToolResult, MapProfileResult]:
        async def body(_: Principal) -> CallToolResult:
            profile = await _build_profile(deps, scan_id, model)
            md = export_map_markdown(profile)
            data = MapProfileResult(
                source_id=profile.source_id,
                scan_id=profile.scan_id,
                model_slug=profile.model_slug,
                total_tables=profile.total_tables,
                mapped_tables=profile.mapped_tables,
                total_columns=profile.total_columns,
                mapped_columns=profile.mapped_columns_count,
                canonical_layer=profile.canonical_layer(),
                implementation_layer=profile.implementation_layer(),
                summary=md,
            )
            return CallToolResult(
                content=[TextContent(type="text", text=md)],
                structured_content=data.model_dump(mode="json"),
            )

        return await guarded("build_map", ctx, READ_SCOPE, body)

    mcp.add_tool(
        build_map_tool,
        name="build_map",
        title="Build MAP from scan",
        description=BUILD_MAP_DESCRIPTION,
        annotations=READ_ONLY,
    )

    # ------------------------------------------------------------------
    # READ: validate_map
    # ------------------------------------------------------------------

    async def validate_map_tool(
        scan_id: Annotated[str, Field(description="Scan ID to validate the MAP for.")],
        model: Annotated[str, Field(description="Model slug.")],
        ctx: Context,
    ) -> Annotated[CallToolResult, MapReportResult]:
        async def body(_: Principal) -> CallToolResult:
            profile = await _build_profile(deps, scan_id, model)
            report: MapReport = validate_map(profile)
            md = report.to_markdown()
            data = MapReportResult(
                source_id=profile.source_id,
                scan_id=profile.scan_id,
                model_slug=profile.model_slug,
                total_tables=report.total_tables,
                mapped_tables=report.mapped_tables,
                total_columns=report.total_columns,
                mapped_columns=report.mapped_columns,
                coverage_ratio=report.coverage_ratio,
                error_count=len(report.errors),
                warning_count=len(report.warnings),
                summary=md,
            )
            return CallToolResult(
                content=[TextContent(type="text", text=md)],
                structured_content=data.model_dump(mode="json"),
            )

        return await guarded("validate_map", ctx, READ_SCOPE, body)

    mcp.add_tool(
        validate_map_tool,
        name="validate_map",
        title="Validate MAP",
        description=VALIDATE_MAP_DESCRIPTION,
        annotations=READ_ONLY,
    )

    # ------------------------------------------------------------------
    # READ: export_map
    # ------------------------------------------------------------------

    async def export_map_tool(
        scan_id: Annotated[str, Field(description="Scan ID.")],
        model: Annotated[str, Field(description="Model slug.")],
        ctx: Context,
        format: Annotated[
            Literal["markdown", "json"],
            Field(description="Output format: `markdown` (default) or `json`."),
        ] = "markdown",
    ) -> Annotated[CallToolResult, MapExportResult]:
        async def body(_: Principal) -> CallToolResult:
            profile = await _build_profile(deps, scan_id, model)
            if format == "json":
                content = export_map_json(profile)
            else:
                content = export_map_markdown(profile)
            data = MapExportResult(
                source_id=profile.source_id,
                scan_id=profile.scan_id,
                model_slug=profile.model_slug,
                format=format,
                content=content,
            )
            return CallToolResult(
                content=[TextContent(type="text", text=content)],
                structured_content=data.model_dump(mode="json"),
            )

        return await guarded("export_map", ctx, READ_SCOPE, body)

    mcp.add_tool(
        export_map_tool,
        name="export_map",
        title="Export MAP",
        description=EXPORT_MAP_DESCRIPTION,
        annotations=READ_ONLY,
    )
