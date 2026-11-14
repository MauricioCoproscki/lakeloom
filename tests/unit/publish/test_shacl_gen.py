import random

from hypothesis import given, settings
from hypothesis import strategies as st
from pyshacl import validate
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from lakeloom.core.models import ModelInfo
from lakeloom.om.native_rdf import build_graph
from lakeloom.publish.shacl_gen import SH, build_shapes, model_classes, shapes_turtle

B = "https://example.org/m/"
INFO = ModelInfo(slug="m", glossary_id="g", fqn="g", base_iri=B, prefixes={"ex": B})


def u(name: str) -> URIRef:
    return URIRef(B + name)


def cls(name: str, label: str | None = None) -> dict:
    return {"iri": B + name, "conceptType": "OWL_CLASS", "displayName": label or name}


def data(name: str, domain: list[str], range_: str, **extra) -> dict:
    return {
        "iri": B + name,
        "kind": "DATA",
        "domain": [B + d for d in domain],
        "range": [range_],
        "displayName": name,
        **extra,
    }


def rel(name: str, domain: str, range_: str) -> dict:
    return {
        "iri": B + name,
        "kind": "OBJECT",
        "domain": [B + domain],
        "range": [B + range_],
        "displayName": name,
    }


def restriction(subject: str, prop: str, kind: str, n: int) -> dict:
    return {
        "subjectIri": B + subject,
        "axiomType": "SUBCLASS_OF",
        "expressions": [
            {
                "kind": "RESTRICTION",
                "propertyIri": B + prop,
                "restrictionKind": kind,
                "cardinality": n,
            }
        ],
    }


def shapes(concepts=(), props=(), axioms=()) -> Graph:
    return build_shapes(build_graph(list(concepts), list(props), list(axioms)), INFO)


def prop_shape(g: Graph, prop: str) -> URIRef:
    return URIRef(B + prop + "/shape")


def test_node_shape_per_class() -> None:
    g = build_graph([cls("A", "Alpha"), cls("B")], [], [])
    g.add((u("S"), RDF.type, SKOS.Concept))
    g.add((u("S"), SKOS.prefLabel, Literal("Skos S")))
    s = build_shapes(g, INFO)
    for c in ("A", "B", "S"):
        assert (u(c + "/shape"), RDF.type, SH.NodeShape) in s
        assert (u(c + "/shape"), SH.targetClass, u(c)) in s
    assert (u("A/shape"), SH.name, Literal("Alpha")) in s
    assert (u("S/shape"), SH.name, Literal("Skos S")) in s
    assert model_classes(g) == [u("A"), u("B"), u("S")]


def test_class_label_prefers_preflabel() -> None:
    g = build_graph([cls("A", "Rdfs A")], [], [])
    g.add((u("A"), SKOS.prefLabel, Literal("Pref A")))
    g.add((u("A"), SKOS.prefLabel, Literal("Zed A")))
    s = build_shapes(g, INFO)
    assert list(s.objects(u("A/shape"), SH.name)) == [Literal("Pref A")]


def test_datatype_attribute_shape() -> None:
    s = shapes([cls("A")], [data("A.price", ["A"], str(XSD.decimal))])
    ps = u("A.price/shape")
    assert (u("A/shape"), SH.property, ps) in s
    assert (ps, RDF.type, SH.PropertyShape) in s
    assert (ps, SH.path, u("A.price")) in s
    assert (ps, SH.datatype, XSD.decimal) in s
    assert (ps, SH.name, Literal("A.price")) in s


def test_literal_range_has_no_datatype() -> None:
    s = shapes([cls("A")], [data("A.note", ["A"], str(RDFS.Literal))])
    assert (u("A.note/shape"), SH.path, u("A.note")) in s
    assert list(s.objects(u("A.note/shape"), SH.datatype)) == []


def test_identifier_is_required_single() -> None:
    s = shapes([cls("A")], [data("A.id", ["A"], str(XSD.string), isIdentifier=True)])
    ps = u("A.id/shape")
    assert (ps, SH.minCount, Literal(1)) in s
    assert (ps, SH.maxCount, Literal(1)) in s


def test_enum_values_roundtrip_and_validate() -> None:
    values = ['a"b', "ünï", "007"]
    s = shapes([cls("A")], [data("A.code", ["A"], str(XSD.string), enumValues=values)])
    (head,) = s.objects(u("A.code/shape"), SH["in"])
    assert [str(v) for v in Collection(s, head)] == values
    turtle = shapes_turtle(s, INFO)
    reparsed = Graph().parse(data=turtle, format="turtle")
    (head2,) = reparsed.objects(u("A.code/shape"), SH["in"])
    assert [str(v) for v in Collection(reparsed, head2)] == values

    def conforms(value: str) -> bool:
        d = Graph()
        d.add((u("x"), RDF.type, u("A")))
        d.add((u("x"), u("A.code"), Literal(value)))
        return bool(validate(d, shacl_graph=s)[0])

    assert conforms("007")
    assert not conforms("7")


def test_relation_shape_has_class() -> None:
    s = shapes([cls("A"), cls("B")], [rel("A.b", "A", "B")])
    ps = u("A.b/shape")
    assert (u("A/shape"), SH.property, ps) in s
    assert (ps, SH.path, u("A.b")) in s
    assert (ps, SH["class"], u("B")) in s
    assert list(s.objects(ps, SH.datatype)) == []


def _shape_of(s: Graph, cls_: str, prop: str) -> URIRef:
    (ps,) = [x for x in s.objects(u(cls_ + "/shape"), SH.property) if (x, SH.path, u(prop)) in s]
    assert isinstance(ps, URIRef)
    return ps


def _counts(s: Graph, prop: str, cls_: str = "A") -> tuple[int | None, int | None]:
    ps = _shape_of(s, cls_, prop)
    lo = s.value(ps, SH.minCount)
    hi = s.value(ps, SH.maxCount)
    return (
        None if lo is None else int(str(lo)),
        None if hi is None else int(str(hi)),
    )


def test_cardinality_restrictions() -> None:
    props = [data(f"A.p{i}", ["A"], str(XSD.string)) for i in range(4)]
    axioms = [
        restriction("A", "A.p0", "MIN", 2),
        restriction("A", "A.p1", "MAX", 3),
        restriction("A", "A.p2", "EXACT", 2),
    ]
    g = build_graph([cls("A")], props, axioms)
    # a qualified cardinality (not produced by build_graph) on p3
    r = BNode()
    g.add((r, RDF.type, OWL.Restriction))
    g.add((r, OWL.onProperty, u("A.p3")))
    g.add((r, OWL.qualifiedCardinality, Literal(4, datatype=XSD.nonNegativeInteger)))
    g.add((u("A"), RDFS.subClassOf, r))
    s = build_shapes(g, INFO)
    assert _counts(s, "A.p0") == (2, None)
    assert _counts(s, "A.p1") == (None, 3)
    assert _counts(s, "A.p2") == (2, 2)
    assert _counts(s, "A.p3") == (4, 4)


def test_stricter_count_wins_over_functional() -> None:
    s = shapes(
        [cls("A")],
        [
            data("A.id", ["A"], str(XSD.string), isIdentifier=True),
            data("A.id2", ["A"], str(XSD.string), isIdentifier=True),
        ],
        [restriction("A", "A.id", "MIN", 2), restriction("A", "A.id2", "MAX", 0)],
    )
    assert _counts(s, "A.id") == (2, 1)
    assert _counts(s, "A.id2") == (1, 0)


def test_restriction_creates_missing_shape() -> None:
    s = shapes([cls("A")], [], [restriction("A", "A.ghost", "MIN", 1)])
    assert (_shape_of(s, "A", "A.ghost"), RDF.type, SH.PropertyShape) in s
    assert _counts(s, "A.ghost") == (1, None)


def test_multi_domain_property_shared_shape() -> None:
    s = shapes([cls("A"), cls("B")], [data("shared", ["A", "B"], str(XSD.string))])
    ps = u("shared/shape")
    assert (u("A/shape"), SH.property, ps) in s
    assert (u("B/shape"), SH.property, ps) in s
    assert len(list(s.subjects(SH.path, u("shared")))) == 1


def test_class_without_attributes_has_empty_node_shape() -> None:
    s = shapes([cls("A")])
    assert (u("A/shape"), RDF.type, SH.NodeShape) in s
    assert list(s.objects(u("A/shape"), SH.property)) == []
    assert list(s.objects(u("A/shape"), SH.closed)) == []


def test_same_name_on_two_classes() -> None:
    s = shapes(
        [cls("A"), cls("B")],
        [data("A.name", ["A"], str(XSD.string)), data("B.name", ["B"], str(XSD.integer))],
    )
    assert (u("A/shape"), SH.property, u("A.name/shape")) in s
    assert (u("B/shape"), SH.property, u("B.name/shape")) in s
    assert (u("A.name/shape"), SH.datatype, XSD.string) in s
    assert (u("B.name/shape"), SH.datatype, XSD.integer) in s


def test_shapes_validate_instance_data() -> None:
    s = shapes(
        [cls("Customer"), cls("Order")],
        [
            data("Customer.id", ["Customer"], str(XSD.string), isIdentifier=True),
            data("Customer.age", ["Customer"], str(XSD.integer)),
            rel("Order.customer", "Order", "Customer"),
        ],
    )

    def instance(age: Literal | None, ident: bool, target: str) -> Graph:
        d = Graph()
        d.add((u("c1"), RDF.type, u("Customer")))
        if ident:
            d.add((u("c1"), u("Customer.id"), Literal("c-1")))
        if age is not None:
            d.add((u("c1"), u("Customer.age"), age))
        d.add((u("o1"), RDF.type, u("Order")))
        d.add((u("o1"), u("Order.customer"), u(target)))
        d.add((u("p1"), RDF.type, u("Product")))
        return d

    ok = Literal(30, datatype=XSD.integer)
    assert validate(instance(ok, True, "c1"), shacl_graph=s)[0]
    assert not validate(instance(Literal("old"), True, "c1"), shacl_graph=s)[0]
    assert not validate(instance(ok, False, "c1"), shacl_graph=s)[0]
    assert not validate(instance(ok, True, "p1"), shacl_graph=s)[0]


_NAMES = st.lists(st.integers(0, 6), unique=True, min_size=0, max_size=7)


@settings(max_examples=40, deadline=None)
@given(
    ids=_NAMES,
    seed=st.integers(0, 10_000),
    enum=st.lists(st.text(min_size=1, max_size=5), unique=True, max_size=4),
)
def test_shapes_deterministic(ids: list[int], seed: int, enum: list[str]) -> None:
    concepts = [cls("A"), cls("B")]
    props = [
        data(
            f"A.p{i}",
            ["A", "B"] if i % 2 else ["A"],
            str(XSD.string),
            isIdentifier=i % 3 == 0,
            enumValues=enum if i % 4 == 0 else None,
        )
        for i in ids
    ]
    axioms = [restriction("A", f"A.p{i}", "MIN", 1) for i in ids if i % 2]
    rng = random.Random(seed)  # noqa: S311
    outputs = set()
    for _ in range(2):
        c, p, a = concepts[:], props[:], axioms[:]
        rng.shuffle(c)
        rng.shuffle(p)
        rng.shuffle(a)
        outputs.add(shapes_turtle(shapes(c, p, a), INFO))
    assert len(outputs) == 1


def test_shapes_turtle_prefixes() -> None:
    out = shapes_turtle(shapes([cls("A")]), INFO).decode()
    assert "@prefix sh: <http://www.w3.org/ns/shacl#>" in out
    assert f"@prefix ex: <{B}>" in out


def test_blank_classes_excluded() -> None:
    g = Graph()
    g.add((BNode(), RDF.type, OWL.Class))
    assert model_classes(g) == []


def test_restriction_on_one_domain_does_not_leak() -> None:
    s = shapes(
        [cls("A"), cls("B")],
        [data("P", ["A", "B"], str(XSD.string))],
        [restriction("A", "P", "MIN", 1)],
    )
    assert _shape_of(s, "A", "P") != _shape_of(s, "B", "P")
    assert _counts(s, "P", "A") == (1, None)
    assert _counts(s, "P", "B") == (None, None)
    assert (_shape_of(s, "A", "P"), SH.datatype, XSD.string) in s

    def conforms(cname: str, values: list[str]) -> bool:
        d = Graph()
        d.add((u("x"), RDF.type, u(cname)))
        for v in values:
            d.add((u("x"), u("P"), Literal(v)))
        return bool(validate(d, shacl_graph=s)[0])

    assert conforms("B", [])
    assert not conforms("A", [])
    assert conforms("A", ["v"])


def test_two_classes_keep_independent_counts() -> None:
    s = shapes(
        [cls("A"), cls("B")],
        [data("P", ["A", "B"], str(XSD.string))],
        [restriction("A", "P", "MIN", 1), restriction("B", "P", "MAX", 2)],
    )
    assert _counts(s, "P", "A") == (1, None)
    assert _counts(s, "P", "B") == (None, 2)


def _enum_graph(lists: list[list[str]], cyclic: bool = False) -> Graph:
    g = build_graph([cls("A")], [data("A.e", ["A"], str(XSD.string))], [])
    for values in lists:
        head = BNode()
        Collection(g, head, [Literal(v) for v in values])
        g.add((u("A.e"), OWL.oneOf, head))
    if cyclic:
        head, tail = BNode(), BNode()
        g.add((head, RDF.first, Literal("x")))
        g.add((head, RDF.rest, tail))
        g.add((tail, RDF.first, Literal("y")))
        g.add((tail, RDF.rest, head))
        g.add((u("A.e"), OWL.oneOf, head))
    return g


def test_multiple_enum_lists_pick_is_deterministic() -> None:
    outs = set()
    for lists in ([["b", "c"], ["a", "z"]], [["a", "z"], ["b", "c"]]):
        for _ in range(3):
            outs.add(shapes_turtle(build_shapes(_enum_graph(lists), INFO), INFO))
    assert len(outs) == 1
    s = build_shapes(_enum_graph([["b", "c"], ["a", "z"]]), INFO)
    (head,) = s.objects(u("A.e/shape"), SH["in"])
    assert [str(v) for v in Collection(s, head)] == ["a", "z"]


def test_cyclic_enum_list_is_skipped() -> None:
    s = build_shapes(_enum_graph([], cyclic=True), INFO)
    assert list(s.objects(u("A.e/shape"), SH["in"])) == []
    s2 = build_shapes(_enum_graph([["ok"]], cyclic=True), INFO)
    (head,) = s2.objects(u("A.e/shape"), SH["in"])
    assert [str(v) for v in Collection(s2, head)] == ["ok"]


def test_invalid_counts_are_ignored() -> None:
    g = build_graph([cls("A")], [data("A.p", ["A"], str(XSD.string))], [])
    for pred, val in (
        (OWL.minCardinality, Literal(-1, datatype=XSD.integer)),
        (OWL.maxCardinality, Literal("2.5", datatype=XSD.decimal)),
        (OWL.cardinality, Literal("many")),
    ):
        r = BNode()
        g.add((r, RDF.type, OWL.Restriction))
        g.add((r, OWL.onProperty, u("A.p")))
        g.add((r, pred, val))
        g.add((u("A"), RDFS.subClassOf, r))
    s = build_shapes(g, INFO)
    assert _counts(s, "A.p") == (None, None)
