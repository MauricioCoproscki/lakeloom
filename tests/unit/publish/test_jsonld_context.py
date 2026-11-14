import json

from _graphs import B, cls, data, rel
from hypothesis import given, settings
from hypothesis import strategies as st
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD

from lakeloom.core.context import (
    AssetContext,
    ResolvedAsset,
    minimal_jsonld_context,
    render_jsonld,
)
from lakeloom.core.models import ModelInfo
from lakeloom.om.native_rdf import build_graph
from lakeloom.publish.jsonld_context import build_context

INFO = ModelInfo(slug="m", glossary_id="g", fqn="g", base_iri=B, prefixes={"ex": B})
STR = str(XSD.string)


def ctx_of(concepts=(), props=(), info: ModelInfo = INFO) -> dict:
    return build_context(build_graph(list(concepts), list(props), []), info)


BASIC = (
    [cls("Customer"), cls("Order")],
    [
        data("Customer.email", ["Customer"], STR),
        data("Customer.age", ["Customer"], str(XSD.integer)),
        rel("Order.customer", "Order", "Customer"),
    ],
)


def test_context_terms_for_classes_and_properties() -> None:
    c = ctx_of(*BASIC)
    assert c["@version"] == 1.1
    assert c["@vocab"] == B and c["ex"] == B
    assert c["xsd"] == str(XSD)
    assert c["dcterms"] == "http://purl.org/dc/terms/"
    assert c["Customer"] == B + "Customer"
    assert c["Order"] == B + "Order"
    assert c["Customer.email"] == {"@id": B + "Customer.email", "@type": STR}
    assert c["Customer.age"] == {"@id": B + "Customer.age", "@type": str(XSD.integer)}


def test_literal_range_and_no_range_have_only_id() -> None:
    props = [
        data("Customer.note", ["Customer"], "http://www.w3.org/2000/01/rdf-schema#Literal"),
        data("Customer.misc", ["Customer"], STR),
    ]
    g = build_graph([cls("Customer")], props, [])
    g.remove((URIRef(B + "Customer.misc"), None, None))
    g.add(
        (
            URIRef(B + "Customer.misc"),
            RDF.type,
            URIRef("http://www.w3.org/2002/07/owl#DatatypeProperty"),
        )
    )
    c = build_context(g, INFO)
    assert c["Customer.note"] == {"@id": B + "Customer.note"}
    assert c["Customer.misc"] == {"@id": B + "Customer.misc"}


def test_relation_term_is_id_typed() -> None:
    c = ctx_of(*BASIC)
    assert c["Order.customer"] == {"@id": B + "Order.customer", "@type": "@id"}
    assert c["customer"] == c["Order.customer"]


def test_unique_alias_present() -> None:
    c = ctx_of(*BASIC)
    assert c["email"] == c["Customer.email"]
    assert c["age"] == c["Customer.age"]


def test_ambiguous_alias_is_omitted() -> None:
    props = [
        data("Customer.email", ["Customer"], STR),
        data("Supplier.email", ["Supplier"], STR),
    ]
    c = ctx_of([cls("Customer"), cls("Supplier")], props)
    assert "email" not in c
    assert "Customer.email" in c and "Supplier.email" in c


def test_keyword_and_prefix_collisions_skipped() -> None:
    # alias "ex" collides with a prefix; class "xsd" collides with the xsd prefix
    props = [data("Customer.ex", ["Customer"], STR)]
    c = ctx_of([cls("Customer"), cls("xsd"), cls("@type")], props)
    assert c["ex"] == B
    assert c["xsd"] == str(XSD)
    assert "@type" not in c
    assert c["Customer.ex"]["@id"] == B + "Customer.ex"


def test_collision_with_earlier_key_skipped() -> None:
    # alias "Order" would shadow the class term "Order"
    c = ctx_of([cls("Order")], [data("Customer.Order", ["Order"], STR)])
    assert c["Order"] == B + "Order"


def test_no_base_iri_uses_local_names() -> None:
    info = ModelInfo(slug="m", glossary_id="g", fqn="g")
    c = ctx_of(*BASIC, info=info)
    assert "@vocab" not in c
    assert c["Customer"] == B + "Customer"
    # split_uri("…/Customer.email") yields the local name "Customer.email"
    assert c["Customer.email"]["@id"] == B + "Customer.email"
    assert c["email"] == c["Customer.email"]


def test_unsplittable_iri_is_skipped() -> None:
    g = build_graph([cls("Customer")], [], [])
    g.add((URIRef("urn:x:1/"), RDF.type, URIRef("http://www.w3.org/2002/07/owl#Class")))
    info = ModelInfo(slug="m", glossary_id="g", fqn="g")
    c = build_context(g, info)
    assert c["Customer"] == B + "Customer"


def test_context_expands_to_model_iris() -> None:
    c = ctx_of(*BASIC)
    person = {"@id": "urn:x:1", "@type": "Customer", "email": "a@b", "age": "42"}
    order = {"@id": "urn:x:2", "@type": "Order", "customer": "urn:x:1"}
    doc = {"@context": c, "@graph": [person, order]}
    g = Graph().parse(data=json.dumps(doc), format="json-ld")
    s = URIRef("urn:x:1")
    assert (s, RDF.type, URIRef(B + "Customer")) in g
    assert (s, URIRef(B + "Customer.email"), Literal("a@b", datatype=XSD.string)) in g
    assert (s, URIRef(B + "Customer.age"), Literal("42", datatype=XSD.integer)) in g
    assert (URIRef("urn:x:2"), URIRef(B + "Order.customer"), s) in g


names = st.text(alphabet="ABCDEFGH", min_size=1, max_size=3)


@settings(max_examples=40, deadline=None)
@given(st.lists(st.tuples(names, names), min_size=1, max_size=6, unique=True), st.randoms())
def test_context_deterministic(pairs, rnd) -> None:
    classes = [cls(c) for c in sorted({c for c, _ in pairs})]
    props = [data(f"{c}.{p}", [c], STR) for c, p in pairs]
    shuffled = list(props)
    rnd.shuffle(shuffled)
    a = ctx_of(classes, props)
    b = ctx_of(list(reversed(classes)), shuffled)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _asset_ctx() -> AssetContext:
    return AssetContext(asset=ResolvedAsset(fqn="s.d.sc.t", table_fqn="s.d.sc.t", om_id="1"))


def test_asset_jsonld_uses_full_context() -> None:
    full = ctx_of(*BASIC)
    doc = render_jsonld(_asset_ctx(), INFO, max_tokens=None, context=full).payload
    assert doc["@context"] == full
    # without a full context the minimal one is kept
    assert render_jsonld(_asset_ctx(), INFO).payload["@context"] == minimal_jsonld_context(INFO)


def test_vocabulary_classes_get_no_context_term() -> None:
    from rdflib.namespace import OWL, SKOS

    g = build_graph([cls("Customer")], [], [])
    om = "https://open-metadata.org/ontology/"
    for v in (
        SKOS.Concept,
        SKOS.ConceptScheme,
        URIRef(om + "Glossary"),
        URIRef(om + "GlossaryTerm"),
    ):
        g.add((v, RDF.type, OWL.Class))
    c = build_context(g, INFO)
    assert c["Customer"] == B + "Customer"
    for key in ("Concept", "ConceptScheme", "Glossary", "GlossaryTerm"):
        assert key not in c, key
