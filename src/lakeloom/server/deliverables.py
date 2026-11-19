"""Render stage deliverables from the live state: effective graph, latest scan, MAP.

Shared by the ``export_*`` MCP tools, the MCP resources and the HTTP routes, so all three
serve the same bytes for the same model state.
"""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from lakeloom.core.annotations import effective_graph
from lakeloom.core.deliverables import Deliverable, render, vocabulary_entries
from lakeloom.core.map_profile import MapProfile, build_map
from lakeloom.errors import LakeloomError
from lakeloom.publish.artifacts import Artifact, deliverable_artifact

if TYPE_CHECKING:
    from lakeloom.server.http_app import AppDeps

__all__ = ["deliverable_bytes", "latest_scan_id", "serve_deliverable"]

_PAGE = 200


async def latest_scan_id(
    deps: "AppDeps", model: str, realizations: dict[str, list[str]] | None = None
) -> str | None:
    """The newest scan that describes ``model``: one a proposal of the model came from, or
    the newest scan of a source whose tables already carry the model's terms (a scan made
    only to document the lake belongs to the model too). Scan ids are ULIDs: lexical order
    is time order. ``None`` when there is none."""
    best: str | None = None
    cursor: str | None = None
    statuses = ["draft", "submitted", "changes_requested", "applied", "rejected"]
    while True:
        page, cursor = await deps.proposal_repo.list_by_status(statuses, model, cursor, _PAGE)
        for p in page:
            if p.scan_id and (best is None or p.scan_id > best):
                best = p.scan_id
        if cursor is None:
            break
    if realizations:
        assets = {a for values in realizations.values() for a in values}
        for record in await deps.scans.latest_per_source():
            tables = {t.om_fqn for t in record.tables if t.om_fqn}
            touches = any(a == t or a.startswith(t + ".") for a in assets for t in tables)
            if touches and (best is None or record.id > best):
                best = record.id
    return best


async def _profile(deps: "AppDeps", model: str, scan_id: str | None) -> MapProfile:
    from lakeloom.server.tools_analytics import load_realization_index

    index = await load_realization_index(deps, model)
    sid = scan_id or await latest_scan_id(deps, model, index.realizations)
    if sid is None:
        raise LakeloomError(
            "not_found",
            f"model {model!r} has no scan to build a data dictionary from",
            "scan the lake with scan_lake_source and propose_from_scan, or pass scan_id",
        )
    record = await deps.scans.get(sid)
    return build_map(record, model_slug=model, realizations=index.realizations)


async def deliverable_bytes(
    deps: "AppDeps", model: str, d: Deliverable, *, scan_id: str | None = None
) -> bytes:
    """Deliverable ``d`` of ``model``'s current effective state."""
    graph = await effective_graph(deps.models, deps.proposal_repo, model, related=True)
    entries = vocabulary_entries(graph)
    profile = await _profile(deps, model, scan_id) if d.needs_scan else None
    return render(d, entries, model, profile).encode("utf-8")


def serve_deliverable(
    deps: "AppDeps", model: str, d: Deliverable, *, version: str | None, scan_id: str | None = None
) -> Callable[[], Awaitable[Artifact]]:
    """An artifact builder for the MCP resources and the HTTP API."""

    def latest() -> Awaitable[bytes]:
        return deliverable_bytes(deps, model, d, scan_id=scan_id)

    return lambda: deliverable_artifact(
        deps.models,
        model,
        d.filename,
        d.media_type,
        latest,
        version=version,
        releases=deps.reader,
        git=deps.git_links,
    )
