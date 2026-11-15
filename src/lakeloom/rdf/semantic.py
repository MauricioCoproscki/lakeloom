"""The semantic view of an exported model graph, for "no semantic diff" comparisons."""

from rdflib import BNode, Graph
from rdflib.term import Node

from lakeloom.rdf.untrusted import BOOKKEEPING

__all__ = ["semantic_view"]


def semantic_view(graph: Graph, base_iri: str) -> Graph:
    """Triples of subjects under ``base_iri`` minus bookkeeping, plus reachable blank nodes."""
    view = Graph()
    pending: list[Node] = []
    for s, p, o in graph:
        if p in BOOKKEEPING or isinstance(s, BNode) or not str(s).startswith(base_iri):
            continue
        view.add((s, p, o))
        if isinstance(o, BNode):
            pending.append(o)
    seen: set[Node] = set()
    while pending:
        node = pending.pop()
        if node in seen:
            continue
        seen.add(node)
        for s, p, o in graph.triples((node, None, None)):  # type: ignore[arg-type]
            view.add((s, p, o))
            if isinstance(o, BNode):
                pending.append(o)
    return view
