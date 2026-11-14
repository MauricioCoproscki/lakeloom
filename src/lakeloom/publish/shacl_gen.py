"""SHACL shapes generated from a model graph (FR-PUB-04, ADR 0014).

Pure function of the merged graph of ``ModelService.graph`` (glossary SKOS plus native OWL):
no I/O, no OpenMetadata calls. One ``sh:NodeShape`` ``<C>/shape`` per class, one
``sh:PropertyShape`` ``<P>/shape`` per attribute or relation, shared by all its domains.
Shapes are open (no ``sh:closed``); units are not constrained.
"""

from collections.abc import Iterable

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD
from rdflib.term import Node

from lakeloom.core.models import ModelInfo
from lakeloom.rdf.canonical import serialize

__all__ = ["SH", "build_shapes", "model_classes", "shapes_turtle"]

SH = Namespace("http://www.w3.org/ns/shacl#")
_XSD = str(XSD)
_COUNT_PREDICATES = (
    OWL.minCardinality,
    OWL.maxCardinality,
    OWL.cardinality,
    OWL.qualifiedCardinality,
)


def model_classes(graph: Graph) -> list[URIRef]:
    """Sorted IRIs typed ``owl:Class`` or ``skos:Concept`` (blank nodes excluded)."""
    found = {
        s
        for t in (OWL.Class, SKOS.Concept)
        for s in graph.subjects(RDF.type, t)
        if isinstance(s, URIRef)
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
    if isinstance(value, Literal):
        try:
            return int(value.toPython())
        except (TypeError, ValueError):
            return None
    return None


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


def _tighten(
    shapes: Graph, shape: URIRef, pred: URIRef, new: int, *, stricter_is_max: bool
) -> None:
    old = [_int(v) for v in shapes.objects(shape, pred)]
    olds = [o for o in old if o is not None]
    if olds:
        pick = max if stricter_is_max else min
        new = pick([*olds, new])
        shapes.remove((shape, pred, None))
    shapes.add((shape, pred, Literal(new)))


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

    def ensure(prop: URIRef) -> URIRef:
        shape = URIRef(f"{prop}/shape")
        if (shape, RDF.type, SH.PropertyShape) not in shapes:
            shapes.add((shape, RDF.type, SH.PropertyShape))
            shapes.add((shape, SH.path, prop))
        return shape

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
        if not domains:
            continue
        shape = ensure(p)
        for d in domains:
            shapes.add((URIRef(f"{d}/shape"), SH.property, shape))
        label = _first(graph.objects(p, RDFS.label))
        if label is not None:
            shapes.add((shape, SH.name, label))
        ranges = sorted(r for r in graph.objects(p, RDFS.range) if isinstance(r, URIRef))
        if (p, RDF.type, OWL.ObjectProperty) in graph:
            for r in ranges:
                shapes.add((shape, SH["class"], r))
        else:
            datatypes = [r for r in ranges if str(r).startswith(_XSD)]
            if len(datatypes) == 1:
                shapes.add((shape, SH.datatype, datatypes[0]))
            for head in sorted(graph.objects(p, OWL.oneOf), key=str):
                values: list[Node] = [v for v in Collection(graph, head) if isinstance(v, Literal)]
                if values:
                    lst = BNode()
                    Collection(shapes, lst, values)
                    shapes.add((shape, SH["in"], lst))
                    break
        if (p, RDF.type, OWL.FunctionalProperty) in graph:
            shapes.add((shape, SH.minCount, Literal(1)))
            shapes.add((shape, SH.maxCount, Literal(1)))
    for c in classes:
        for p, (lo, hi) in _restriction_counts(graph, c).items():
            shape = ensure(p)
            shapes.add((URIRef(f"{c}/shape"), SH.property, shape))
            if lo is not None:
                _tighten(shapes, shape, SH.minCount, lo, stricter_is_max=True)
            if hi is not None:
                _tighten(shapes, shape, SH.maxCount, hi, stricter_is_max=False)
    return shapes


def shapes_turtle(shapes: Graph, info: ModelInfo) -> bytes:
    """Canonical turtle of ``shapes`` (prefix ``sh`` plus the model prefixes), for HTTP and Git."""
    return serialize(shapes, "turtle", prefixes={"sh": str(SH), **info.prefixes})
