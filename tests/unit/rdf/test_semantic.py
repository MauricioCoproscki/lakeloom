from rdflib import Graph
from rdflib.compare import isomorphic

from lakeloom.rdf.semantic import semantic_view
from lakeloom.rdf.untrusted import BOOKKEEPING

BASE = "http://localhost:8000/ontology/m/"

TEMPLATE = """
@prefix om: <https://open-metadata.org/ontology/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix m: <http://localhost:8000/ontology/m/> .

m:Customer a owl:Class, skos:Concept ;
    skos:prefLabel "Customer" ;
    skos:inScheme <GLOSS> ;
    skos:topConceptOf <GLOSS> ;
    om:fullyQualifiedName "m.Customer" ;
    rdfs:subClassOf [ a owl:Restriction ; owl:cardinality "1"^^xsd:nonNegativeInteger ;
                      owl:onProperty m:Customer.id ] .
<GLOSS> a skos:ConceptScheme ; skos:hasTopConcept m:Customer ; om:fullyQualifiedName "m" .
""".replace("@prefix m:", "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n@prefix m:")


def _g(gloss: str = "https://open-metadata.org/entity/glossary/aaa") -> Graph:
    return Graph().parse(data=TEMPLATE.replace("GLOSS", gloss), format="turtle")


def test_bookkeeping_has_three_predicates() -> None:
    assert len(BOOKKEEPING) == 3


def test_semantic_view_drops_bookkeeping_and_foreign_subjects() -> None:
    view = semantic_view(_g(), BASE)
    preds = {str(p) for _, p, _ in view}
    assert not {str(p) for p in BOOKKEEPING} & preds
    assert all(str(s).startswith(BASE) or not str(s).startswith("http") for s, _, _ in view)
    assert not any("glossary" in str(s) for s, _, _ in view)
    assert any(str(p).endswith("prefLabel") for p in preds)


def test_semantic_view_keeps_restriction_closure() -> None:
    view = semantic_view(_g(), BASE)
    preds = {str(p) for _, p, _ in view}
    assert "http://www.w3.org/2002/07/owl#onProperty" in preds
    assert "http://www.w3.org/2002/07/owl#cardinality" in preds


def test_semantic_view_ignores_glossary_id() -> None:
    a = semantic_view(_g("https://open-metadata.org/entity/glossary/aaa"), BASE)
    b = semantic_view(_g("https://open-metadata.org/entity/glossary/bbb"), BASE)
    assert isomorphic(a, b)
    assert len(a) > 0
