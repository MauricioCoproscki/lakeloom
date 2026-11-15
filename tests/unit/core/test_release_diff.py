import pytest
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from lakeloom.core.release_diff import (
    diff_graphs,
    diff_markdown,
    format_version,
    next_version,
    parse_version,
)
from lakeloom.errors import LakeloomError

B = "https://example.org/m/"


def u(name: str) -> URIRef:
    return URIRef(B + name)


def model() -> Graph:
    g = Graph()
    g.add((u("Customer"), RDF.type, OWL.Class))
    g.add((u("Customer"), SKOS.prefLabel, Literal("Customer")))
    g.add((u("Order"), RDF.type, OWL.Class))
    g.add((u("email"), RDF.type, OWL.DatatypeProperty))
    g.add((u("email"), RDFS.range, XSD.string))
    return g


def restricted(n: int, label: str) -> Graph:
    g = model()
    r = BNode(label)
    g.add((u("Customer"), RDFS.subClassOf, r))
    g.add((r, RDF.type, OWL.Restriction))
    g.add((r, OWL.onProperty, u("email")))
    g.add((r, OWL.minCardinality, Literal(n)))
    return g


def d(old: Graph | None, new: Graph):
    return diff_graphs(old, new, from_version="1.0.0", to_version="1.1.0")


@pytest.mark.parametrize("text", ["0.0.1", "1.10.0"])
def test_parse_version_accepts(text: str) -> None:
    v = parse_version(text)
    assert format_version(v) == text


@pytest.mark.parametrize(
    "text", ["1.0", "01.0.0", "1.0.0-rc1", "v1.0.0", "../x", "", "1." + "9" * 300 + ".0", "1.0.0\n"]
)
def test_parse_version_rejects(text: str) -> None:
    with pytest.raises(LakeloomError) as e:
        parse_version(text)
    assert e.value.code == "validation_error"


def test_versions_order_numerically() -> None:
    assert parse_version("1.10.0") > parse_version("1.9.0")


def test_removed_term_is_major() -> None:
    new = model()
    new.remove((u("Order"), None, None))
    r = d(model(), new)
    assert r.bump == "major"
    assert [t.iri for t in r.removed] == [B + "Order"]


def test_iri_change_is_major() -> None:
    new = model()
    new.remove((u("Order"), None, None))
    new.add((u("Purchase"), RDF.type, OWL.Class))
    r = d(model(), new)
    assert r.bump == "major"
    assert [t.iri for t in r.added] == [B + "Purchase"]
    assert [t.iri for t in r.removed] == [B + "Order"]


def test_removed_range_is_major() -> None:
    new = model()
    new.remove((u("email"), RDFS.range, None))
    r = d(model(), new)
    assert r.bump == "major"
    assert r.removed == []
    assert [t.iri for t in r.changed] == [B + "email"]


def test_added_term_is_minor() -> None:
    new = model()
    new.add((u("Invoice"), RDF.type, OWL.Class))
    r = d(model(), new)
    assert r.bump == "minor"
    assert [t.iri for t in r.added] == [B + "Invoice"]


def test_added_broader_is_minor() -> None:
    new = model()
    new.add((u("Order"), SKOS.broader, u("Customer")))
    r = d(model(), new)
    assert r.bump == "minor"
    assert r.added == []
    assert [t.iri for t in r.changed] == [B + "Order"]


def test_label_or_definition_only_is_patch() -> None:
    new = model()
    new.set((u("Customer"), SKOS.prefLabel, Literal("Client")))
    new.add((u("Order"), SKOS.definition, Literal("An order")))
    r = d(model(), new)
    assert r.bump == "patch"
    assert r.annotation_only
    assert {t.iri for t in r.changed} == {B + "Customer", B + "Order"}
    assert r.added == [] and r.removed == []


def test_isomorphic_graphs_have_no_bump() -> None:
    r = d(restricted(1, "a"), restricted(1, "zzz"))
    assert r.bump is None
    assert r.added == r.changed == r.removed == []
    assert r.added_triples == r.removed_triples == 0


def test_restriction_change_attributed_to_term() -> None:
    r = d(restricted(1, "a"), restricted(2, "a"))
    assert r.bump == "major"
    assert [t.iri for t in r.changed] == [B + "Customer"]


def test_first_release_lists_all_terms_added() -> None:
    r = diff_graphs(model(), model(), from_version=None, to_version="1.0.0")
    assert r.bump is None
    r = diff_graphs(None, model(), from_version=None, to_version="1.0.0")
    assert r.bump is None
    assert {t.iri for t in r.added} == {B + "Customer", B + "Order", B + "email"}
    assert r.removed == [] and r.changed == []
    assert r.added_triples == len(model())
    assert {t.label for t in r.added} == {"Customer", "Order", "email"}


def test_changed_term_listed_once() -> None:
    new = model()
    new.add((u("Customer"), SKOS.definition, Literal("x")))
    new.add((u("Customer"), SKOS.note, Literal("y")))
    new.add((u("Customer"), SKOS.broader, u("Order")))
    r = d(model(), new)
    assert [t.iri for t in r.changed] == [B + "Customer"]


def test_next_version_rules() -> None:
    assert next_version((1, 4, 2), "major") == (2, 0, 0)
    assert next_version((1, 4, 2), "minor") == (1, 5, 0)
    assert next_version((1, 4, 2), "patch") == (1, 4, 3)
    assert next_version(None, None) == (1, 0, 0)
    assert next_version(None, "patch") == (1, 0, 0)
    with pytest.raises(LakeloomError):
        next_version((1, 0, 0), None)


def test_diff_markdown_quotes_labels() -> None:
    new = model()
    new.add((u("Evil"), RDF.type, OWL.Class))
    new.add((u("Evil"), RDFS.label, Literal("a`b\n## Injected")))
    md = diff_markdown(d(model(), new))
    assert "\n## Injected" not in md
    assert "a'b ## Injected" in md
    assert "`a'b ## Injected`" in md
    assert "### Added" in md


OM_GLOSSARY = URIRef("https://open-metadata.org/ontology/Glossary")


def with_vocabulary(g: Graph) -> Graph:
    """The merged graph also declares vocabulary classes (skos:Concept, om:Glossary...)."""
    g.add((SKOS.Concept, RDF.type, OWL.Class))
    g.add((OM_GLOSSARY, RDF.type, OWL.Class))
    g.add((SKOS.prefLabel, RDF.type, OWL.DatatypeProperty))
    return g


def test_first_release_excludes_vocabulary_terms() -> None:
    r = diff_graphs(None, with_vocabulary(model()), from_version=None, to_version="1.0.0")
    assert {t.iri for t in r.added} == {B + "Customer", B + "Order", B + "email"}


def test_later_diff_never_lists_vocabulary_terms() -> None:
    old = model()
    old.add((SKOS.Concept, RDF.type, OWL.Class))
    old.add((SKOS.Concept, RDFS.label, Literal("Concept")))
    new = model()
    new.add((OM_GLOSSARY, RDF.type, OWL.Class))
    new.add((SKOS.prefLabel, RDF.type, OWL.DatatypeProperty))
    r = d(old, new)
    listed = {t.iri for t in r.added + r.changed + r.removed}
    assert listed == set()
    # and a vocabulary annotation change is not a "changed" term either
    commented = with_vocabulary(model())
    commented.add((SKOS.Concept, RDFS.comment, Literal("c")))
    r = d(with_vocabulary(model()), commented)
    assert r.changed == []
