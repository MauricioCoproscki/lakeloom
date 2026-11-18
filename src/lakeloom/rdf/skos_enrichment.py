"""Enrich a SKOS graph with lakeloom-native annotation ops (M6c).

The OM native ontology API does not support SKOS annotation predicates
(``scopeNote``, ``historyNote``, ``changeNote``) or ``owl:deprecated``; they are
stored only in lakeloom's proposal log.  This module materialises those ops as
RDF triples so the SKOS export (and ``skos_projection``) can include them.

Main entry point: :func:`enrich_graph_from_ops`.  It is a pure function — no
I/O — so callers that have both a graph and a list of applied ops can call it
directly.  The result is the same graph object, mutated in-place, so it is safe
to call on a copy of the model graph.

The module also exports :func:`concept_iri_index`, a helper that builds a
``local-name → IRI`` mapping from an existing SKOS graph.

Supported ops:
  ``add_scope_note``   → ``skos:scopeNote``
  ``add_history_note`` → ``skos:historyNote``
  ``deprecate_concept`` →  ``owl:deprecated true``  +  ``skos:hiddenLabel`` (pref label)
  ``add_mapping``      → ``skos:exactMatch`` / ``skos:closeMatch`` / …
                         (already written by OM but kept here for completeness)
"""

from __future__ import annotations

from rdflib import OWL, Graph, Literal, URIRef
from rdflib.namespace import RDF, SKOS, XSD

from lakeloom.core.operations import (
    AddHistoryNote,
    AddMapping,
    AddScopeNote,
    DeprecateConcept,
    ProposalOperation,
)

__all__ = [
    "concept_iri_index",
    "enrich_graph_from_ops",
]

_MAPPING_PRED: dict[str, URIRef] = {
    "exactMatch": SKOS.exactMatch,
    "closeMatch": SKOS.closeMatch,
    "broadMatch": SKOS.broadMatch,
    "narrowMatch": SKOS.narrowMatch,
    "relatedMatch": SKOS.relatedMatch,
}


def concept_iri_index(graph: Graph) -> dict[str, URIRef]:
    """Return ``{local_name: URIRef}`` for every ``skos:Concept`` in ``graph``.

    The local name is the last path / fragment segment of the IRI, URL-decoded.
    If the concept has a ``skos:prefLabel`` its value is also indexed.
    """
    from urllib.parse import unquote

    index: dict[str, URIRef] = {}
    for s in graph.subjects(RDF.type, SKOS.Concept):
        if not isinstance(s, URIRef):
            continue
        raw = str(s)
        # Last path segment (handles both /path/Name and /path/Name#fragment).
        local = raw.rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        index[unquote(local)] = s
        # Also index by prefLabel for fuzzy FQN matching.
        for label in graph.objects(s, SKOS.prefLabel):
            index[str(label)] = s
    return index


def _resolve_fqn(fqn: str, index: dict[str, URIRef]) -> URIRef | None:
    """Return the IRI for ``fqn``, trying the full FQN then its last segment."""
    if fqn in index:
        return index[fqn]
    local = fqn.rsplit(".", 1)[-1]
    return index.get(local)


def enrich_graph_from_ops(
    graph: Graph,
    ops: list[ProposalOperation],
    *,
    lang: str = "en",
) -> Graph:
    """Add lakeloom-native annotation triples to ``graph`` for each applied op.

    Modifies ``graph`` in-place and returns it.

    Parameters
    ----------
    graph:
        SKOS graph for the model (already loaded from OM).  Concepts must be
        present as ``rdf:type skos:Concept`` subjects for the lookup to work.
    ops:
        Applied proposal operations.  Only ``AddScopeNote``, ``AddHistoryNote``,
        ``DeprecateConcept``, and ``AddMapping`` are handled; all others are ignored.
    lang:
        Fallback language tag for Literals when the op carries none.
    """
    idx = concept_iri_index(graph)

    for op in ops:
        if isinstance(op, AddScopeNote):
            iri = _resolve_fqn(op.concept_fqn, idx)
            if iri is None:
                continue
            graph.add((iri, SKOS.scopeNote, Literal(op.note, lang=op.lang or lang)))

        elif isinstance(op, AddHistoryNote):
            iri = _resolve_fqn(op.concept_fqn, idx)
            if iri is None:
                continue
            graph.add((iri, SKOS.historyNote, Literal(op.note, lang=op.lang or lang)))

        elif isinstance(op, DeprecateConcept):
            iri = _resolve_fqn(op.fqn, idx)
            if iri is None:
                continue
            graph.add((iri, OWL.deprecated, Literal(True, datatype=XSD.boolean)))
            # Collect the preferred label(s) as hidden labels so the concept
            # stays findable but is flagged as deprecated.
            for label in list(graph.objects(iri, SKOS.prefLabel)):
                graph.add((iri, SKOS.hiddenLabel, label))
            # Record reason as a changeNote.
            if op.reason:
                graph.add((iri, SKOS.changeNote, Literal(op.reason, lang=lang)))

        elif isinstance(op, AddMapping):
            iri = _resolve_fqn(str(op.concept), idx)
            if iri is None:
                continue
            pred = _MAPPING_PRED.get(op.kind, SKOS.relatedMatch)
            graph.add((iri, pred, URIRef(str(op.target_iri))))

    return graph
