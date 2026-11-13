"""The snapshot an applied proposal is published from (ADR 0013 § Snapshot at apply).

:func:`take_snapshot` runs right after ``applied`` is stored: the commit message and the
provenance record are always built; the three exports, the model version and the OM version
come from OpenMetadata (bot client, reads only) and are left empty when that fails, so the
publisher exports at publish time through :func:`fill_exports` (its ``exporter``).
"""

import asyncio
import logging

from lakeloom.core.models import ModelInfo, ModelService
from lakeloom.core.proposals import Proposal, ReviewRecord
from lakeloom.errors import LakeloomError
from lakeloom.gitstore.layout import (
    Snapshot,
    applied_change_sets,
    build_message,
    provenance_record,
)
from lakeloom.om.client import OMClient

__all__ = ["fill_exports", "take_snapshot"]

_log = logging.getLogger(__name__)


async def fill_exports(models: ModelService, om: OMClient, snapshot: Snapshot) -> Snapshot:
    """``snapshot`` with the model's current exports, model version and OM version.

    Raises whatever the export raises (the publisher treats it as transient).
    """
    slug = snapshot.model
    info = await models.get_model(slug)
    ttl = await models.export(slug, "turtle")
    jsonld = await models.export(slug, "jsonld")
    owl = await models.export(slug, "rdfxml")
    version = await models.version(slug)
    caps = await om.get_capabilities()
    return snapshot.model_copy(
        update={
            "glossary_id": info.glossary_id,
            "glossary_fqn": info.fqn,
            "base_iri": info.base_iri,
            "prefixes": dict(info.prefixes),
            "exports": {
                "ontology.ttl": ttl.decode("utf-8"),
                "ontology.jsonld": jsonld.decode("utf-8"),
                "ontology.owl": owl.decode("utf-8"),
            },
            "model_version": version,
            "om_version": caps.version,
        }
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
    changes = applied_change_sets(p)
    base = Snapshot(
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
    try:
        return await fill_exports(models, om, base)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        what = exc.code if isinstance(exc, LakeloomError) else type(exc).__name__
        _log.warning("proposal %s: export for Git failed at apply (%s)", p.id, what)
        return base
