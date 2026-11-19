"""MCP tools that hand users a stage deliverable without finishing the pipeline first.

``export_glossary`` (stage 1), ``export_data_dictionary`` (stage 2), ``export_taxonomy``
(stage 3) and ``export_synonyms`` (stage 4) render the model's current effective state, or
read the copy a release carries. All are read-only and need ``lakeloom:read``.
"""

import hashlib
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from lakeloom.core.annotations import effective_graph
from lakeloom.core.deliverables import deliverable, quality_score, vocabulary_entries
from lakeloom.core.release_diff import parse_version
from lakeloom.server.auth import Principal
from lakeloom.server.deliverables import serve_deliverable
from lakeloom.server.http_app import AppDeps
from lakeloom.server.tools_common import READ_SCOPE, guarded

__all__ = ["register_deliverable_tools"]

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
MAX_BYTES = 8 * 1024 * 1024

_COMMON = """\
`version`: `latest` (default; the current model including approved notes and deprecations) \
or a release `MAJOR.MINOR.PATCH` (releases made before deliverables existed have none). \
The file is the text content; `structured` gives its name, media type, size and hash. \
Read-only, no side effects. Unknown model → `not_found`.

Requires scope `lakeloom:read`."""

GLOSSARY_DESCRIPTION = f"""\
Stage 1 deliverable: the model's **business glossary**, useful as soon as terms are agreed, \
long before a full ontology exists. One row per current term with its definition, synonyms \
and scope notes; deprecated terms are listed with their replacement. Terms without a \
definition are marked so the gaps are visible.

`format`: `markdown` (to read, share or publish in a wiki) or `csv` (spreadsheets, BI, \
catalog imports; columns fqn, term, definition, synonyms, scope_note, history_note, broader, \
related, status, replaced_by, iri). `structured.quality` scores the vocabulary (share of \
terms defined, with synonyms, with scope notes).

{_COMMON}"""

DATA_DICTIONARY_DESCRIPTION = f"""\
Stage 2 deliverable: a **data dictionary** of the scanned tables. For every column: its \
type, the business term it carries (from the MAP) and that term's definition; unmapped \
columns are marked. Useful for analysts, onboarding, audits and data-protection records.

`scan_id`: the scan to describe (default: the newest scan behind the model's proposals). \
`format`: `markdown` or `csv` (columns table, table_concept, column, data_type, term, \
term_fqn, definition, status).

{_COMMON}"""

TAXONOMY_DESCRIPTION = f"""\
Stage 3 deliverable: the model's **taxonomy tree** (broad to narrow), ready for navigation \
menus, portals, BI hierarchies or a diagram. Deprecated terms are left out; a term with \
several parents appears under each.

`format`: `markdown` (indented outline with definitions), `mermaid` (a flowchart that \
renders on GitHub) or `csv` (depth, fqn, term, parent_fqn).

{_COMMON}"""

SYNONYMS_DESCRIPTION = f"""\
Stage 4 deliverables for search and migration, built from the thesaurus:

- `solr` (default): a synonym file for Solr, Elasticsearch or OpenSearch. Each term and \
its synonyms are one equivalence rule; a deprecated term maps to its replacement \
(`old => new`), so searches for the old name find the new term.
- `csv`: one row per term and synonym (term, fqn, synonym).
- `deprecations`: deprecated term → replacement, with its history (to migrate reports \
and dashboards).

{_COMMON}"""


ModelArg = Annotated[str, Field(description="Model slug, as returned by list_models.")]
VersionArg = Annotated[str, Field(description="latest (default) or a release MAJOR.MINOR.PATCH.")]


class DeliverableResult(BaseModel):
    model: str
    version: str
    kind: str
    format: str
    filename: str
    media_type: str
    bytes: int
    sha256: str
    quality: dict[str, Any] | None = Field(
        default=None, description="Glossary only (latest): vocabulary quality score."
    )


def register_deliverable_tools(mcp: MCPServer, deps: AppDeps) -> None:
    async def run(
        ctx: Context,
        tool: str,
        model: str,
        kind: str,
        fmt: str,
        version: str,
        scan_id: str | None = None,
    ) -> CallToolResult:
        async def body(_: Principal) -> CallToolResult:
            if version != "latest":
                parse_version(version)  # malformed → validation_error
            d = deliverable(kind, fmt)
            artifact = await serve_deliverable(
                deps,
                model,
                d,
                version=None if version == "latest" else version,
                scan_id=scan_id,
            )()
            data = artifact.body
            if len(data) > MAX_BYTES:
                from lakeloom.errors import LakeloomError

                raise LakeloomError(
                    "validation_error",
                    f"{d.filename} is {len(data)} bytes, over the {MAX_BYTES} byte limit",
                    "read it over the HTTP API or from the release in Git",
                )
            quality = None
            if kind == "glossary" and version == "latest":
                graph = await effective_graph(deps.models, deps.proposal_repo, model)
                q = quality_score(vocabulary_entries(graph))
                quality = {
                    "terms": q.total,
                    "defined": q.defined,
                    "defined_ratio": round(q.defined_ratio, 3),
                    "with_synonyms": q.with_synonyms,
                    "with_scope_note": q.with_scope_note,
                    "deprecated": q.deprecated,
                    "undefined_sample": list(q.undefined),
                }
            result = DeliverableResult(
                model=model,
                version=version,
                kind=kind,
                format=fmt,
                filename=d.filename,
                media_type=d.media_type,
                bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                quality=quality,
            )
            return CallToolResult(
                content=[TextContent(type="text", text=data.decode("utf-8"))],
                structured_content=result.model_dump(mode="json"),
            )

        return await guarded(tool, ctx, READ_SCOPE, body)

    async def export_glossary(
        model: ModelArg,
        ctx: Context,
        format: Annotated[Literal["markdown", "csv"], Field(description="File format.")] = (
            "markdown"
        ),
        version: VersionArg = "latest",
    ) -> Annotated[CallToolResult, DeliverableResult]:
        return await run(ctx, "export_glossary", model, "glossary", format, version)

    async def export_data_dictionary(
        model: ModelArg,
        ctx: Context,
        format: Annotated[Literal["markdown", "csv"], Field(description="File format.")] = (
            "markdown"
        ),
        scan_id: Annotated[
            str | None, Field(description="Scan to describe (default: the newest).")
        ] = None,
        version: VersionArg = "latest",
    ) -> Annotated[CallToolResult, DeliverableResult]:
        return await run(
            ctx, "export_data_dictionary", model, "data_dictionary", format, version, scan_id
        )

    async def export_taxonomy(
        model: ModelArg,
        ctx: Context,
        format: Annotated[
            Literal["markdown", "mermaid", "csv"], Field(description="File format.")
        ] = "markdown",
        version: VersionArg = "latest",
    ) -> Annotated[CallToolResult, DeliverableResult]:
        return await run(ctx, "export_taxonomy", model, "taxonomy", format, version)

    async def export_synonyms(
        model: ModelArg,
        ctx: Context,
        format: Annotated[
            Literal["solr", "csv", "deprecations"], Field(description="File format.")
        ] = "solr",
        version: VersionArg = "latest",
    ) -> Annotated[CallToolResult, DeliverableResult]:
        return await run(ctx, "export_synonyms", model, "synonyms", format, version)

    for fn, name, title, description in (
        (export_glossary, "export_glossary", "Export glossary", GLOSSARY_DESCRIPTION),
        (
            export_data_dictionary,
            "export_data_dictionary",
            "Export data dictionary",
            DATA_DICTIONARY_DESCRIPTION,
        ),
        (export_taxonomy, "export_taxonomy", "Export taxonomy", TAXONOMY_DESCRIPTION),
        (export_synonyms, "export_synonyms", "Export synonyms", SYNONYMS_DESCRIPTION),
    ):
        mcp.add_tool(fn, name=name, title=title, description=description, annotations=READ_ONLY)
