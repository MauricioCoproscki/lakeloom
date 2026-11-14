"""SHACL shapes generated from a model graph (FR-PUB-04, ADR 0014).

Pure function of the merged graph of ``ModelService.graph`` (glossary SKOS plus native OWL):
no I/O, no OpenMetadata calls. One ``sh:NodeShape`` ``<C>/shape`` per class, one
``sh:PropertyShape`` ``<P>/shape`` per attribute or relation, shared by all its domains.
Shapes are open (no ``sh:closed``); units are not constrained.
"""

from collections.abc import Iterable
from urllib.parse import quote

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD
from rdflib.term import Node

from lakeloom.core.models import ModelInfo
from lakeloom.rdf.canonical import serialize

__all__ = [
    "SH",
    "VOCABULARY_NAMESPACES",
    "build_shapes",
    "is_vocabulary",
    "model_classes",
    "shapes_turtle",
]

SH = Namespace("http://www.w3.org/ns/shacl#")
_XSD = str(XSD)
_COUNT_PREDICATES = (
    OWL.minCardinality,
    OWL.maxCardinality,
    OWL.cardinality,
    OWL.qualifiedCardinality,
)


# Vocabulary namespaces whose classes the merged graph declares (``skos:Concept``,
# ``om:Glossary``...) but which are not concepts of the model. A denylist, not a base-IRI
# allowlist: paired native ontologies keep their own IRIs.
VOCABULARY_NAMESPACES = (
    str(SKOS),
    str(OWL),
    str(RDF),
    str(RDFS),
    str(XSD),
    "https://open-metadata.org/ontology/",  # the `om` prefix of rdf/canonical.py
)


def is_vocabulary(iri: URIRef) -> bool:
    """Whether ``iri`` lies in one of the ``VOCABULARY_NAMESPACES``."""
    return str(iri).startswith(VOCABULARY_NAMESPACES)


def model_classes(graph: Graph) -> list[URIRef]:
    """Sorted IRIs typed ``owl:Class`` or ``skos:Concept``; blank nodes and vocabulary
    classes (``VOCABULARY_NAMESPACES``) excluded."""
    found = {
        s
        for t in (OWL.Class, SKOS.Concept)
        for s in graph.subjects(RDF.type, t)
        if isinstance(s, URIRef) and not is_vocabulary(s)
    }
    return sorted(found)


def _first(values: Iterable[Node]) -> Literal | None:
    literals = sorted(
        (v for v in values if isinstance(v, Literal)), key=lambda v: (str(v), v.language or "")
    )
    return literals[0] if literals else None


def _class_label(graph: Graph, cls: URIRef) -> Literal | None:
    return _first(graph.objects(cls, SKOS.prefLabel)) or _first(graph.objects(cls, RDFS.label))


def _int(value: Node) -> int | None:
    """A non-negative integer literal's value; anything else (2.5, -1, text) is ignored."""
    if isinstance(value, Literal):
        py = value.toPython()
        if isinstance(py, int) and not isinstance(py, bool) and py >= 0:
            return py
    return None


def _list_values(graph: Graph, head: Node) -> list[Literal] | None:
    """Literals of a well-formed RDF list; ``None`` when malformed (cycle, gap, non-literal)."""
    out: list[Literal] = []
    seen: set[Node] = set()
    node = head
    while node != RDF.nil:
        if node in seen:
            return None
        seen.add(node)
        firsts = list(graph.objects(node, RDF.first))
        rests = list(graph.objects(node, RDF.rest))
        if len(firsts) != 1 or len(rests) != 1 or not isinstance(firsts[0], Literal):
            return None
        out.append(firsts[0])
        node = rests[0]
    return out


def _enum_values(graph: Graph, prop: URIRef) -> list[Literal] | None:
    """The values of the sorted-first well-formed non-empty ``owl:oneOf`` list (deterministic)."""
    lists = [v for h in graph.objects(prop, OWL.oneOf) if (v := _list_values(graph, h))]
    if not lists:
        return None
    return min(lists, key=lambda v: ("\x00".join(sorted(map(str, v))), [str(x) for x in v]))


def _restriction_counts(graph: Graph, cls: URIRef) -> dict[URIRef, tuple[int | None, int | None]]:
    """Per property, the (min, max) implied by the cardinality restrictions on ``cls``."""
    out: dict[URIRef, tuple[int | None, int | None]] = {}
    for r in graph.objects(cls, RDFS.subClassOf):
        if not isinstance(r, BNode) or (r, RDF.type, OWL.Restriction) not in graph:
            continue
        for p in graph.objects(r, OWL.onProperty):
            if not isinstance(p, URIRef):
                continue
            lo, hi = out.get(p, (None, None))
            for pred in _COUNT_PREDICATES:
                for raw in graph.objects(r, pred):
                    n = _int(raw)
                    if n is None:
                        continue
                    if pred in (OWL.minCardinality, OWL.cardinality, OWL.qualifiedCardinality):
                        lo = n if lo is None else max(lo, n)
                    if pred in (OWL.maxCardinality, OWL.cardinality, OWL.qualifiedCardinality):
                        hi = n if hi is None else min(hi, n)
            out[p] = (lo, hi)
    return out


def _merge(a: int | None, b: int | None, *, stricter_is_max: bool) -> int | None:
    if a is None or b is None:
        return b if a is None else a
    return max(a, b) if stricter_is_max else min(a, b)


def build_shapes(graph: Graph, info: ModelInfo) -> Graph:
    """The SHACL shapes graph of ``graph``; ``info`` is reserved for model-level metadata."""
    del info
    shapes = Graph()
    classes = model_classes(graph)
    class_set = set(classes)
    for c in classes:
        node = URIRef(f"{c}/shape")
        shapes.add((node, RDF.type, SH.NodeShape))
        shapes.add((node, SH.targetClass, c))
        label = _class_label(graph, c)
        if label is not None:
            shapes.add((node, SH.name, label))

    def base(shape: URIRef, prop: URIRef, lo: int | None, hi: int | None) -> None:
        """Write the property shape ``shape``: base constraints of ``prop`` plus counts."""
        shapes.add((shape, RDF.type, SH.PropertyShape))
        shapes.add((shape, SH.path, prop))
        label = _first(graph.objects(prop, RDFS.label))
        if label is not None:
            shapes.add((shape, SH.name, label))
        ranges = sorted(r for r in graph.objects(prop, RDFS.range) if isinstance(r, URIRef))
        if (prop, RDF.type, OWL.ObjectProperty) in graph:
            for r in ranges:
                shapes.add((shape, SH["class"], r))
        elif (prop, RDF.type, OWL.DatatypeProperty) in graph:
            datatypes = [r for r in ranges if str(r).startswith(_XSD)]
            if len(datatypes) == 1:
                shapes.add((shape, SH.datatype, datatypes[0]))
            values = _enum_values(graph, prop)
            if values:
                lst = BNode()
                Collection(shapes, lst, list[Node](values))
                shapes.add((shape, SH["in"], lst))
        if (prop, RDF.type, OWL.FunctionalProperty) in graph:
            lo = _merge(lo, 1, stricter_is_max=True)
            hi = _merge(hi, 1, stricter_is_max=False)
        if lo is not None:
            shapes.add((shape, SH.minCount, Literal(lo)))
        if hi is not None:
            shapes.add((shape, SH.maxCount, Literal(hi)))

    restricted = {c: _restriction_counts(graph, c) for c in classes}
    properties = sorted(
        {
            s
            for t in (OWL.DatatypeProperty, OWL.ObjectProperty)
            for s in graph.subjects(RDF.type, t)
            if isinstance(s, URIRef)
        }
    )
    for p in properties:
        domains = sorted(
            d for d in graph.objects(p, RDFS.domain) if isinstance(d, URIRef) and d in class_set
        )
        plain = [d for d in domains if p not in restricted[d]]
        if not plain:
            continue
        shared = URIRef(f"{p}/shape")
        base(shared, p, None, None)
        for d in plain:
            shapes.add((URIRef(f"{d}/shape"), SH.property, shared))
    for c in classes:
        for p, (lo, hi) in restricted[c].items():
            own = URIRef(f"{c}/shape/{quote(str(p), safe='')}")
            base(own, p, lo, hi)
            shapes.add((URIRef(f"{c}/shape"), SH.property, own))
    return shapes


def shapes_turtle(shapes: Graph, info: ModelInfo) -> bytes:
    """Canonical turtle of ``shapes`` (prefix ``sh`` plus the model prefixes), for HTTP and Git."""
    return serialize(shapes, "turtle", prefixes={"sh": str(SH), **info.prefixes})
