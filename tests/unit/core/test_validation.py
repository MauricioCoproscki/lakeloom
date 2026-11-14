from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from lakeloom.core.models import ModelInfo
from lakeloom.core.validation import validate_model, validation_markdown
from lakeloom.publish.shacl_gen import build_shapes

B = "https://example.org/m/"
INFO = ModelInfo(slug="m", glossary_id="g", fqn="g", base_iri=B, prefixes={"ex": B})


def u(name: str) -> URIRef:
    return URIRef(B + name)


def concept(g: Graph, name: str, label: str | None = None, defined: bool = True, lang=None):
    g.add((u(name), RDF.type, OWL.Class))
    g.add((u(name), SKOS.prefLabel, Literal(label or name, lang=lang)))
    if defined:
        g.add((u(name), SKOS.definition, Literal("d")))


def prop(g: Graph, name: str, domain, range_, kind=OWL.DatatypeProperty) -> None:
    g.add((u(name), RDF.type, kind))
    g.add((u(name), RDFS.domain, domain))
    g.add((u(name), RDFS.range, range_))


def run(g: Graph):
    return validate_model(g, INFO, build_shapes(g, INFO))


def codes(r, severity=None) -> list[str]:
    return [i.code for i in r.issues if severity in (None, i.severity)]


def clean() -> Graph:
    g = Graph()
    concept(g, "A")
    concept(g, "B")
    g.add((u("B"), SKOS.broader, u("A")))
    prop(g, "a.x", u("A"), XSD.string)
    return g


def test_clean_model_conforms() -> None:
    r = run(clean())
    assert r.conforms and codes(r, "error") == [] and codes(r, "warning") == []
    assert r.counts["error"] == 0 and r.truncated is False


def test_duplicate_label_is_error() -> None:
    g = clean()
    concept(g, "C", "alpha")
    concept(g, "D", "ALPHA")
    g.add((u("C"), SKOS.broader, u("A")))
    g.add((u("D"), SKOS.broader, u("A")))
    r = run(g)
    assert "duplicate_label" in codes(r, "error") and not r.conforms
    g2 = clean()
    concept(g2, "C", "alpha", lang="en")
    concept(g2, "D", "alpha", lang="pt")
    g2.add((u("C"), SKOS.broader, u("A")))
    g2.add((u("D"), SKOS.broader, u("A")))
    assert "duplicate_label" not in codes(run(g2))


def test_hierarchy_cycle_is_error() -> None:
    g = clean()
    g.add((u("A"), SKOS.broader, u("B")))
    assert "hierarchy_cycle" in codes(run(g), "error")
    g2 = clean()
    g2.add((u("A"), RDFS.subClassOf, u("B")))
    g2.add((u("B"), RDFS.subClassOf, u("A")))
    assert codes(run(g2), "error") == ["hierarchy_cycle"]
    g3 = clean()  # narrower implies inverse broader; restriction bnodes are ignored
    g3.add((u("B"), SKOS.narrower, u("A")))
    g3.add((u("A"), RDFS.subClassOf, BNode()))
    assert "hierarchy_cycle" in codes(run(g3))


def test_dangling_domain_is_error() -> None:
    g = clean()
    prop(g, "z.p", u("Nope"), XSD.string)
    r = run(g)
    assert [i.subject for i in r.issues if i.code == "dangling_domain"] == [B + "z.p"]


def test_external_range_is_warning() -> None:
    g = clean()
    prop(g, "a.rel", u("A"), URIRef("https://other.org/X"), OWL.ObjectProperty)
    r = run(g)
    assert "external_range" in codes(r, "warning") and r.conforms


def test_isolated_concept_warning() -> None:
    g = clean()
    concept(g, "Lonely")
    r = run(g)
    assert [i.subject for i in r.issues if i.code == "isolated_concept"] == [B + "Lonely"]


def test_missing_definition_warning() -> None:
    g = clean()
    g.remove((u("A"), SKOS.definition, None))
    r = run(g)
    assert [i.subject for i in r.issues if i.code == "missing_definition"] == [B + "A"]


def test_untyped_attribute_warning() -> None:
    g = clean()
    prop(g, "a.free", u("A"), RDFS.Literal)
    assert "untyped_attribute" in codes(run(g), "warning")


def test_shapes_info_counts() -> None:
    r = run(clean())
    info = [i for i in r.issues if i.code == "shapes"]
    assert len(info) == 1 and info[0].severity == "info" and info[0].subject == "m"
    assert "2 node shapes, 1 property shapes" in info[0].message
    assert r.counts["info"] == 1 and r.counts["concepts"] == 2 and r.counts["properties"] == 1


def test_issues_sorted_deterministically() -> None:
    g = clean()
    concept(g, "Z", defined=False)
    concept(g, "Y", defined=False)
    prop(g, "a.free", u("A"), RDFS.Literal)
    prop(g, "z.p", u("Nope"), XSD.string)
    r = run(g)
    keys = [
        ({"error": 0, "warning": 1, "info": 2}[i.severity], i.code, i.subject) for i in r.issues
    ]
    assert (
        keys == sorted(keys) and r.issues[0].severity == "error" and r.issues[-1].severity == "info"
    )
    assert run(g).model_dump() == r.model_dump()


def test_report_capped_at_200() -> None:
    g = Graph()
    for n in range(250):
        concept(g, f"C{n:03}", defined=False)
        g.add((u(f"C{n:03}"), SKOS.related, u(f"C{n:03}")))
    r = run(g)
    assert len(r.issues) == 200 and r.truncated
    assert r.counts["warning"] == 250 and r.conforms


def test_markdown_quotes_untrusted_names() -> None:
    g = clean()
    evil = Graph()
    for n in ("x`**bold**\n# h", "y"):
        evil.add((URIRef(B + n.replace("\n", "%0A")), RDF.type, OWL.Class))
    g += evil
    g.add((u("A"), SKOS.prefLabel, Literal("a")))
    concept(g, "W", "[l](http://e/) `*`")
    r = run(g)
    r.issues[0].message = "bad `tick` **b**\n- fake bullet"
    md = validation_markdown(r)
    first = next(ln for ln in md.splitlines() if ln.startswith("- **"))
    assert "\\`tick\\`" in first and "\\*\\*b\\*\\*" in first
    assert "\n- fake bullet" not in md
    assert md.startswith("# Validation of model `m`")
    assert "- Conforms:" in md and "Errors:" in md


def test_large_chain_cycle_is_linear_and_capped() -> None:
    import time

    g = Graph()
    n = 5000
    for i in range(n):
        concept(g, f"N{i}")
    for i in range(n - 1):
        g.add((u(f"N{i}"), SKOS.broader, u(f"N{i + 1}")))
    g.add((u(f"N{n - 1}"), SKOS.broader, u("N0")))
    start = time.monotonic()
    r = run(g)
    assert time.monotonic() - start < 2
    cyc = [i for i in r.issues if i.code == "hierarchy_cycle"]
    assert len(cyc) == 1 and "and 4980 more" in cyc[0].message and len(cyc[0].message) < 3000


def test_self_loop_is_a_cycle() -> None:
    g = clean()
    g.add((u("A"), SKOS.broader, u("A")))
    assert codes(run(g), "error") == ["hierarchy_cycle"]


def test_duplicate_group_message_capped() -> None:
    g = clean()
    for i in range(30):
        concept(g, f"D{i}", "same")
        g.add((u(f"D{i}"), SKOS.broader, u("A")))
    dup = [i for i in run(g).issues if i.code == "duplicate_label"]
    assert len(dup) == 1 and "and 10 more" in dup[0].message
