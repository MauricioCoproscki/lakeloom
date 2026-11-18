"""The snapshot an applied proposal is published from (ADR 0013 § Snapshot at apply).

:func:`base_snapshot` (no OpenMetadata read: commit message, provenance record, source tables)
is what the publication row is stored with right after ``applied``. :func:`take_snapshot` then
adds the five exports (ontology x3, shapes.ttl, context.jsonld), the model version and the
OM version from OpenMetadata (bot client, reads only); they are left empty when that fails, so
the publisher exports at publish time through :func:`fill_exports` (its ``exporter``).

M8: when ``ops`` is provided to :func:`fill_exports`, an enriched SKOS graph (with
``skos:scopeNote``, ``skos:historyNote``, ``owl:deprecated``) is also written as
``skos.ttl``.  Snapshots taken at apply-time omit ``skos.ttl``; ``ReleaseReader``
falls back to projecting ``ontology.ttl`` for older releases.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from lakeloom.core.models import ModelInfo, ModelService
from lakeloom.core.proposals import Proposal, ReviewRecord
from lakeloom.errors import LakeloomError

if TYPE_CHECKING:
    from lakeloom.core.operations import ProposalOperation
from lakeloom.gitstore.layout import (
    Snapshot,
    applied_change_sets,
    build_message,
    provenance_record,
)
from lakeloom.om.client import OMClient

__all__ = ["base_snapshot", "exports_or_base", "fill_exports", "take_snapshot"]

_log = logging.getLogger(__name__)


async def fill_exports(
    models: ModelService,
    om: OMClient,
    snapshot: Snapshot,
    *,
    ops: "list[ProposalOperation] | None" = None,
) -> Snapshot:
    """``snapshot`` with the model's current exports, model version and OM version.

    Raises whatever the export raises (the publisher treats it as transient). The five
    exports must come from one graph state: the version is read first and again after the
    exports, and a change in between raises a plain (transient) error.

    When ``ops`` is supplied (applied :class:`~lakeloom.core.operations.ProposalOperation`
    instances for the model), an enriched SKOS Turtle is also written as ``skos.ttl``
    alongside the standard exports.  ``skos.ttl`` is omitted when ``ops`` is ``None``
    so that apply-time snapshots and older callers are unaffected.
    """
    slug = snapshot.model
    info = await models.get_model(slug)
    version = await models.version(slug)
    ttl = await models.export(slug, "turtle")
    jsonld = await models.export(slug, "jsonld")
    owl = await models.export(slug, "rdfxml")
    shapes = await models.shapes_bytes(slug, "text/turtle")  # cached: same bytes as HTTP
    context = await models.jsonld_context_bytes(slug)
    if await models.version(slug) != version:
        raise RuntimeError(f"model {slug} changed while it was being exported")
    caps = await om.get_capabilities()

    skos_text: str | None = None
    if ops is not None:
        import asyncio
        import copy

        from lakeloom.rdf.canonical import parse_rdf, serialize
        from lakeloom.rdf.skos_enrichment import enrich_graph_from_ops

        graph = await asyncio.to_thread(parse_rdf, ttl, "turtle")
        graph = await asyncio.to_thread(enrich_graph_from_ops, copy.deepcopy(graph), ops)
        skos_bytes = await asyncio.to_thread(serialize, graph, "skos", prefixes=dict(info.prefixes))
        skos_text = skos_bytes.decode("utf-8")

    exports: dict = {
        "ontology.ttl": ttl.decode("utf-8"),
        "ontology.jsonld": jsonld.decode("utf-8"),
        "ontology.owl": owl.decode("utf-8"),
        "shapes.ttl": shapes.decode("utf-8"),
        "context.jsonld": context.decode("utf-8"),
    }
    if skos_text is not None:
        exports["skos.ttl"] = skos_text

    return snapshot.model_copy(
        update={
            "glossary_id": info.glossary_id,
            "glossary_fqn": info.fqn,
            "base_iri": info.base_iri,
            "prefixes": dict(info.prefixes),
            "exports": exports,
            "model_version": version,
            "om_version": caps.version,
        }
    )


def base_snapshot(
    p: Proposal,
    approval: ReviewRecord,
    publication_id: str,
    *,
    info: ModelInfo | None = None,
) -> Snapshot:
    """The snapshot of applied ``p`` without anything read from OM (``exports`` is ``None``).

    ``info`` (the model as the approve saw it) fills the manifest fields.
    """
    changes = applied_change_sets(p)
    return Snapshot(
        model=p.model,
        glossary_id=info.glossary_id if info is not None else "",
        glossary_fqn=info.fqn if info is not None else p.model,
        base_iri=info.base_iri if info is not None else None,
        prefixes=dict(info.prefixes) if info is not None else {},
        model_version="",
        last_change_set=changes[-1] if changes else None,
        proposal_id=p.id,
        source_tables=list(p.provenance.tables),
        provenance=provenance_record(p, approval, publication_id),
        message=build_message(p, approval, publication_id),
    )


async def take_snapshot(
    models: ModelService,
    om: OMClient,
    p: Proposal,
    approval: ReviewRecord,
    publication_id: str,
    *,
    info: ModelInfo | None = None,
) -> Snapshot:
    """The snapshot of applied ``p``; ``exports`` is ``None`` when the export failed.

    ``info`` (the model as the approve saw it) fills the manifest fields when OM is gone.
    """
    base = base_snapshot(p, approval, publication_id, info=info)
    return await exports_or_base(models, om, base, what=f"proposal {p.id}")


async def exports_or_base(
    models: ModelService, om: OMClient, base: Snapshot, *, what: str
) -> Snapshot:
    """``base`` with :func:`fill_exports`, or ``base`` itself when the export fails.

    The one export step of every publication snapshot (an applied proposal, a reconcile
    commit), so the same OM state always renders the same files (FR-GIT-03). A row without
    exports stays valid: the publisher exports at publish time. ``what`` names the snapshot
    in the warning (an id, never term text).
    """
    try:
        return await fill_exports(models, om, base)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        code = exc.code if isinstance(exc, LakeloomError) else type(exc).__name__
        _log.warning("%s: export for Git failed (%s)", what, code)
        return base
