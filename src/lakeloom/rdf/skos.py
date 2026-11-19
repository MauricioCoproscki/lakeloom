"""SKOS-only projection of a model graph (ADR 0009).

Keeps ``skos:ConceptScheme`` / ``skos:Concept`` typing and the SKOS predicates listed in
``SKOS_PREDICATES``. Where two ``skos:Concept`` classes are linked by ``rdfs:subClassOf`` and the
child has no ``skos:broader`` at all, a ``skos:broader`` is derived from it.

M6c: ``SKOS_PREDICATES`` now includes the annotation and editorial note predicates
(``scopeNote``, ``historyNote``, ``changeNote``, ``editorialNote``, ``hiddenLabel``) and
``owl:deprecated`` (with ``dct:isReplacedBy``), so they are preserved when the SKOS
projection is built from an enriched graph (see :mod:`lakeloom.rdf.skos_enrichment`).
"""

from rdflib import OWL, RDF, RDFS, Graph, URIRef
from rdflib.namespace import DCTERMS, SKOS

__all__ = ["SKOS_PREDICATES", "skos_projection"]

SKOS_PREDICATES: frozenset[URIRef] = frozenset(
    {
        SKOS.prefLabel,
        SKOS.altLabel,
        SKOS.hiddenLabel,
        SKOS.definition,
        SKOS.scopeNote,
        SKOS.historyNote,
        SKOS.changeNote,
        SKOS.editorialNote,
        SKOS.broader,
        SKOS.narrower,
        SKOS.related,
        SKOS.exactMatch,
        SKOS.closeMatch,
        SKOS.broadMatch,
        SKOS.narrowMatch,
        SKOS.relatedMatch,
        SKOS.inScheme,
        SKOS.topConceptOf,
        SKOS.hasTopConcept,
        # owl:deprecated and dct:isReplacedBy are not SKOS predicates but carry a
        # deprecation and its successor, which belong in the SKOS export.
        OWL.deprecated,
        DCTERMS.isReplacedBy,
    }
)
_SKOS_TYPES: frozenset[URIRef] = frozenset({SKOS.Concept, SKOS.ConceptScheme})


def skos_projection(graph: Graph) -> Graph:
    """Return a new graph holding only the SKOS view of ``graph``."""
    out = Graph(bind_namespaces="none")
    for prefix, ns in graph.namespaces():
        out.bind(prefix, ns, override=True)
    for s, p, o in graph:
        if p in SKOS_PREDICATES or (p == RDF.type and o in _SKOS_TYPES):
            out.add((s, p, o))
    concepts = set(graph.subjects(RDF.type, SKOS.Concept))
    has_broader = {s for s in concepts if (s, SKOS.broader, None) in graph}
    for child, _, parent in graph.triples((None, RDFS.subClassOf, None)):
        if child in concepts and parent in concepts and child not in has_broader:
            out.add((child, SKOS.broader, parent))
    return out
