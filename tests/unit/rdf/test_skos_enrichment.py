"""Unit tests for skos_enrichment (M6c)."""

from __future__ import annotations

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, SKOS

from lakeloom.rdf.skos_enrichment import concept_iri_index, enrich_graph_from_ops

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NS = "http://example.org/concepts/"


def _c(name: str) -> URIRef:
    return URIRef(f"{_NS}{name}")


def _graph(*concepts: str) -> Graph:
    g = Graph()
    for name in concepts:
        g.add((_c(name), RDF.type, SKOS.Concept))
        g.add((_c(name), SKOS.prefLabel, Literal(name, lang="en")))
    return g


def _scope_note_op(fqn: str, note: str, lang: str = "en"):
    from lakeloom.core.operations import AddScopeNote

    return AddScopeNote(op="add_scope_note", concept_fqn=fqn, note=note, lang=lang)


def _history_note_op(fqn: str, note: str, lang: str = "en"):
    from lakeloom.core.operations import AddHistoryNote

    return AddHistoryNote(op="add_history_note", concept_fqn=fqn, note=note, lang=lang)


def _deprecate_op(fqn: str, reason: str = "replaced by successor"):
    from lakeloom.core.operations import DeprecateConcept

    return DeprecateConcept(op="deprecate_concept", fqn=fqn, base_version="1.0", reason=reason)


def _mapping_op(concept_fqn: str, kind: str, target: str):
    from pydantic import AnyUrl

    from lakeloom.core.operations import AddMapping

    return AddMapping(
        op="add_mapping",
        concept=concept_fqn,
        kind=kind,  # type: ignore[arg-type]
        target_iri=AnyUrl(target),
    )


# ---------------------------------------------------------------------------
# concept_iri_index
# ---------------------------------------------------------------------------


class TestConceptIriIndex:
    def test_empty_graph(self) -> None:
        assert concept_iri_index(Graph()) == {}

    def test_indexes_by_local_name(self) -> None:
        idx = concept_iri_index(_graph("Animal"))
        assert "Animal" in idx
        assert idx["Animal"] == _c("Animal")

    def test_indexes_by_pref_label(self) -> None:
        idx = concept_iri_index(_graph("Animal"))
        assert "Animal" in idx  # prefLabel "Animal" also indexes

    def test_exact_fqn_wins_and_ambiguous_short_names_are_dropped(self) -> None:
        from lakeloom.rdf.skos_enrichment import OM_FQN

        g = Graph()
        for path, fqn in (("a/Item", "m.A.Item"), ("b/Item", "m.B.Item")):
            g.add((_c(path), RDF.type, SKOS.Concept))
            g.add((_c(path), SKOS.prefLabel, Literal("Item", lang="en")))
            g.add((_c(path), OM_FQN, Literal(fqn)))
        idx = concept_iri_index(g)
        assert idx["m.B.Item"] == _c("b/Item") and "Item" not in idx
        enrich_graph_from_ops(g, [_scope_note_op("m.B.Item", "only B")])
        assert list(g.objects(_c("a/Item"), SKOS.scopeNote)) == []
        assert len(list(g.objects(_c("b/Item"), SKOS.scopeNote))) == 1

    def test_multiple_concepts(self) -> None:
        idx = concept_iri_index(_graph("Dog", "Cat", "Bird"))
        assert len(idx) >= 3


# ---------------------------------------------------------------------------
# enrich_graph_from_ops — scope notes
# ---------------------------------------------------------------------------


class TestScopeNote:
    def test_adds_scope_note_triple(self) -> None:
        g = _graph("Animal")
        enrich_graph_from_ops(g, [_scope_note_op("Animal", "Applies to all living fauna.")])
        notes = list(g.objects(_c("Animal"), SKOS.scopeNote))
        assert len(notes) == 1
        assert str(notes[0]) == "Applies to all living fauna."

    def test_scope_note_lang_tag(self) -> None:
        g = _graph("Animal")
        enrich_graph_from_ops(g, [_scope_note_op("Animal", "Fauna", lang="pt")])
        notes = list(g.objects(_c("Animal"), SKOS.scopeNote))
        assert notes[0].language == "pt"  # type: ignore[attr-defined]

    def test_multiple_scope_notes(self) -> None:
        g = _graph("Animal")
        enrich_graph_from_ops(
            g,
            [
                _scope_note_op("Animal", "Note 1"),
                _scope_note_op("Animal", "Note 2"),
            ],
        )
        notes = list(g.objects(_c("Animal"), SKOS.scopeNote))
        assert len(notes) == 2

    def test_unknown_concept_skipped(self) -> None:
        g = _graph("Animal")
        enrich_graph_from_ops(g, [_scope_note_op("NoSuchConcept", "nope")])
        notes = list(g.objects(_c("Animal"), SKOS.scopeNote))
        assert notes == []


# ---------------------------------------------------------------------------
# enrich_graph_from_ops — history notes
# ---------------------------------------------------------------------------


class TestHistoryNote:
    def test_adds_history_note_triple(self) -> None:
        g = _graph("Revenue")
        enrich_graph_from_ops(g, [_history_note_op("Revenue", "Renamed from NetRevenue in v2.")])
        notes = list(g.objects(_c("Revenue"), SKOS.historyNote))
        assert len(notes) == 1
        assert "Renamed" in str(notes[0])

    def test_unknown_concept_skipped(self) -> None:
        g = _graph("Revenue")
        enrich_graph_from_ops(g, [_history_note_op("Ghost", "doesn't exist")])
        # graph unchanged for Revenue
        assert list(g.objects(_c("Revenue"), SKOS.historyNote)) == []


# ---------------------------------------------------------------------------
# enrich_graph_from_ops — deprecation
# ---------------------------------------------------------------------------


class TestDeprecateConcept:
    def test_adds_deprecated_triple(self) -> None:
        g = _graph("OldTerm")
        enrich_graph_from_ops(g, [_deprecate_op("OldTerm")])
        deprecated = list(g.objects(_c("OldTerm"), OWL.deprecated))
        assert len(deprecated) == 1
        assert deprecated[0].toPython() is True  # type: ignore[attr-defined]

    def test_no_hidden_label_on_the_deprecated_concept(self) -> None:
        g = _graph("OldTerm")
        enrich_graph_from_ops(g, [_deprecate_op("OldTerm")])
        # SKOS S13: a prefLabel must not also be a hiddenLabel of the same concept
        assert list(g.objects(_c("OldTerm"), SKOS.hiddenLabel)) == []

    def test_successor_gets_replaced_by_and_hidden_label(self) -> None:
        from rdflib.namespace import DCTERMS

        from lakeloom.core.operations import DeprecateConcept

        g = _graph("OldTerm", "NewTerm")
        op = DeprecateConcept(
            op="deprecate_concept", fqn="OldTerm", base_version="1", reason="r", successor="NewTerm"
        )
        enrich_graph_from_ops(g, [op])
        assert (_c("OldTerm"), DCTERMS.isReplacedBy, _c("NewTerm")) in g
        assert str(next(g.objects(_c("NewTerm"), SKOS.hiddenLabel))) == "OldTerm"

    def test_adds_change_note_for_reason(self) -> None:
        g = _graph("OldTerm")
        enrich_graph_from_ops(g, [_deprecate_op("OldTerm", reason="Superseded by NewTerm")])
        change_notes = list(g.objects(_c("OldTerm"), SKOS.changeNote))
        assert any("Superseded" in str(cn) for cn in change_notes)

    def test_unknown_concept_skipped(self) -> None:
        g = _graph("OldTerm")
        enrich_graph_from_ops(g, [_deprecate_op("Ghost")])
        assert list(g.objects(_c("OldTerm"), OWL.deprecated)) == []


# ---------------------------------------------------------------------------
# enrich_graph_from_ops — mappings
# ---------------------------------------------------------------------------


class TestAddMappingOp:
    def test_adds_exact_match_triple(self) -> None:
        g = _graph("Revenue")
        enrich_graph_from_ops(
            g, [_mapping_op("Revenue", "exactMatch", "https://schema.org/MonetaryAmount")]
        )
        matches = list(g.objects(_c("Revenue"), SKOS.exactMatch))
        assert URIRef("https://schema.org/MonetaryAmount") in matches

    def test_adds_close_match_triple(self) -> None:
        g = _graph("Revenue")
        enrich_graph_from_ops(
            g, [_mapping_op("Revenue", "closeMatch", "http://www.wikidata.org/entity/Q1")]
        )
        matches = list(g.objects(_c("Revenue"), SKOS.closeMatch))
        assert len(matches) == 1

    def test_unknown_concept_skipped(self) -> None:
        g = _graph("Revenue")
        enrich_graph_from_ops(
            g, [_mapping_op("Ghost", "exactMatch", "https://schema.org/MonetaryAmount")]
        )
        assert list(g.objects(_c("Revenue"), SKOS.exactMatch)) == []


# ---------------------------------------------------------------------------
# Mixed ops in one call
# ---------------------------------------------------------------------------


class TestMixedOps:
    def test_multiple_op_types_in_one_call(self) -> None:
        g = _graph("Revenue", "OldTerm")
        ops = [
            _scope_note_op("Revenue", "Gross revenue from all channels"),
            _history_note_op("Revenue", "Formerly called Turnover"),
            _deprecate_op("OldTerm"),
        ]
        enrich_graph_from_ops(g, ops)
        assert list(g.objects(_c("Revenue"), SKOS.scopeNote))
        assert list(g.objects(_c("Revenue"), SKOS.historyNote))
        assert list(g.objects(_c("OldTerm"), OWL.deprecated))

    def test_returns_same_graph_object(self) -> None:
        g = _graph("Animal")
        result = enrich_graph_from_ops(g, [_scope_note_op("Animal", "note")])
        assert result is g

    def test_empty_ops_list_no_change(self) -> None:
        g = _graph("Animal")
        before = len(g)
        enrich_graph_from_ops(g, [])
        assert len(g) == before

    def test_unrelated_ops_ignored(self) -> None:
        from lakeloom.core.operations import AddConcept

        g = _graph("Animal")
        op = AddConcept(
            op="add_concept",
            model="retail",
            name="NewThing",
            definition="A new thing",
            parent_fqn=None,
            synonyms=[],
        )
        before = len(g)
        enrich_graph_from_ops(g, [op])  # type: ignore[arg-type]
        assert len(g) == before


def test_derive_related_makes_om_related_to_symmetric() -> None:
    from lakeloom.rdf.skos_enrichment import OM_RELATED_TO, derive_related

    g = _graph("A", "B")
    g.add((_c("A"), OM_RELATED_TO, _c("B")))
    derive_related(g)
    assert (_c("A"), SKOS.related, _c("B")) in g and (_c("B"), SKOS.related, _c("A")) in g
