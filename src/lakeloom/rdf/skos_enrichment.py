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
  ``deprecate_concept`` →  ``owl:deprecated true``, ``skos:changeNote`` (reason) and, with a
                          successor, ``dct:isReplacedBy`` plus the deprecated prefLabel as the
                          successor's ``skos:hiddenLabel`` (never on the concept itself: a
                          prefLabel that is also a hiddenLabel violates SKOS S13)
  ``add_mapping``      → ``skos:exactMatch`` / ``skos:closeMatch`` / …
                         (already written by OM but kept here for completeness)
"""

from __future__ import annotations

from rdflib import OWL, Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF, SKOS, XSD

from lakeloom.core.operations import (
    AddHistoryNote,
    AddMapping,
    AddScopeNote,
    DeprecateConcept,
    ProposalOperation,
)

__all__ = [
    "concept_iri_index",
    "concept_labels",
    "derive_related",
    "enrich_graph_from_ops",
]

_MAPPING_PRED: dict[str, URIRef] = {
    "exactMatch": SKOS.exactMatch,
    "closeMatch": SKOS.closeMatch,
    "broadMatch": SKOS.broadMatch,
    "narrowMatch": SKOS.narrowMatch,
    "relatedMatch": SKOS.relatedMatch,
}


OM_FQN = URIRef("https://open-metadata.org/ontology/fullyQualifiedName")
OM_RELATED_TO = URIRef("https://open-metadata.org/ontology/relatedTo")


def derive_related(graph: Graph) -> Graph:
    """Add ``skos:related`` for every OM glossary ``relatedTo`` link between concepts, both
    ways: OM exports the thesaurus RT under its own predicate, and SKOS requires symmetry."""
    concepts = set(graph.subjects(RDF.type, SKOS.Concept))
    for s, _, o in list(graph.triples((None, OM_RELATED_TO, None))):
        if s in concepts and o in concepts and s != o:
            graph.add((s, SKOS.related, o))
            graph.add((o, SKOS.related, s))
    return graph


def concept_iri_index(graph: Graph) -> dict[str, URIRef]:
    """Return ``{key: URIRef}`` for every ``skos:Concept`` in ``graph``.

    Keys are the concept's OpenMetadata FQN (exact, always unique) and, only when they
    identify a single concept, its IRI local name and its prefLabels. An ambiguous short
    key is dropped rather than guessed, so an annotation never lands on the wrong concept.
    """
    from urllib.parse import unquote

    exact: dict[str, URIRef] = {}
    short: dict[str, set[URIRef]] = {}
    for s in graph.subjects(RDF.type, SKOS.Concept):
        if not isinstance(s, URIRef):
            continue
        fqn = graph.value(s, OM_FQN)
        if fqn is not None:
            exact[str(fqn)] = s
        local = unquote(str(s).rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1])
        short.setdefault(local, set()).add(s)
        for label in graph.objects(s, SKOS.prefLabel):
            short.setdefault(str(label), set()).add(s)
    index = {k: next(iter(v)) for k, v in short.items() if len(v) == 1}
    index.update(exact)
    return index


def concept_labels(graph: Graph) -> dict[str, str]:
    """``{concept IRI: prefLabel}``, preferring an untagged or English label."""
    out: dict[str, str] = {}
    for s, o in graph.subject_objects(SKOS.prefLabel):
        if isinstance(o, Literal) and (str(s) not in out or o.language in (None, "en")):
            out[str(s)] = str(o)
    return out


def _resolve_fqn(fqn: str, index: dict[str, URIRef]) -> URIRef | None:
    """The IRI for ``fqn``: the exact FQN, an IRI, or else an unambiguous last segment."""
    if fqn in index:
        return index[fqn]
    if "://" in fqn:
        return next((v for v in index.values() if str(v) == fqn), None)
    return index.get(fqn.rsplit(".", 1)[-1])


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
            if op.reason:
                graph.add((iri, SKOS.changeNote, Literal(op.reason, lang=lang)))
            successor = _resolve_fqn(op.successor, idx) if op.successor else None
            if successor is not None and successor != iri:
                graph.add((iri, DCTERMS.isReplacedBy, successor))
                # The old name stays findable through its successor (SKOS S13-safe).
                for label in list(graph.objects(iri, SKOS.prefLabel)):
                    if label not in set(graph.objects(successor, SKOS.prefLabel)):
                        graph.add((successor, SKOS.hiddenLabel, label))

        elif isinstance(op, AddMapping):
            iri = _resolve_fqn(str(op.concept), idx)
            if iri is None:
                continue
            pred = _MAPPING_PRED.get(op.kind, SKOS.relatedMatch)
            graph.add((iri, pred, URIRef(str(op.target_iri))))

    return graph
