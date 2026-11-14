"""Full JSON-LD ``@context`` for a model (FR-PUB-05).

Pure: reads the merged model graph and returns the inner ``@context`` object (the caller
wraps and serialises it). Precedence, earliest first: ``@version``, the minimal context
(``@vocab``, prefixes, ``dcterms``, ``skos``), ``xsd``, one term per class, one term per
property, then short aliases for property names that are unique in the model. A key that
collides with a keyword, a prefix or an earlier key is skipped.
"""

import logging
from typing import Any

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD, split_uri
from rdflib.term import Node

from lakeloom.core.context import minimal_jsonld_context
from lakeloom.core.models import ModelInfo
from lakeloom.publish.shacl_gen import model_classes

__all__ = ["build_context"]

_log = logging.getLogger(__name__)
_XSD = str(XSD)


def _key(iri: URIRef, base: str | None) -> str | None:
    text = str(iri)
    if base and text.startswith(base) and len(text) > len(base):
        return text[len(base) :]
    try:
        return split_uri(text)[1] or None
    except ValueError:
        return None


def _property_term(graph: Graph, prop: URIRef) -> dict[str, str]:
    term = {"@id": str(prop)}
    if (prop, RDF.type, OWL.DatatypeProperty) in graph:
        ranges = sorted(str(r) for r in graph.objects(prop, RDFS.range) if isinstance(r, URIRef))
        xsd = [r for r in ranges if r.startswith(_XSD)]
        if xsd:
            term["@type"] = xsd[0]
    else:
        term["@type"] = "@id"
    return term


def _properties(graph: Graph) -> list[URIRef]:
    found: set[Node] = set()
    for kind in (OWL.DatatypeProperty, OWL.ObjectProperty):
        found.update(s for s in graph.subjects(RDF.type, kind) if isinstance(s, URIRef))
    return sorted(p for p in found if isinstance(p, URIRef))


def build_context(graph: Graph, info: ModelInfo) -> dict[str, Any]:
    """The model's full JSON-LD context (the value of ``@context``)."""
    context: dict[str, Any] = {"@version": 1.1}
    context.update(minimal_jsonld_context(info))
    context.setdefault("xsd", _XSD)

    def add(key: str | None, value: Any) -> bool:
        if not key or key.startswith("@") or key in context:
            _log.debug("jsonld context: term skipped (collision or invalid key)")
            return False
        context[key] = value
        return True

    for cls in model_classes(graph):
        add(_key(cls, info.base_iri), str(cls))

    terms: dict[str, dict[str, str]] = {}
    for prop in _properties(graph):
        key = _key(prop, info.base_iri)
        term = _property_term(graph, prop)
        if add(key, term) and key:
            terms[key] = term

    aliases: dict[str, list[str]] = {}
    for key in terms:
        alias = key.rsplit(".", 1)[-1]
        if alias != key:
            aliases.setdefault(alias, []).append(key)
    for alias, keys in sorted(aliases.items()):
        if len(keys) == 1:
            add(alias, dict(terms[keys[0]]))
    return context
