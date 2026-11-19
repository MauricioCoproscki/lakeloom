"""Unit tests for cv_builder, thesaurus_builder and coverage modules (M5a)."""

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, SKOS

from lakeloom.core.coverage import (
    CoverageModel,
    assess_coverage,
    hierarchy_depth,
)
from lakeloom.core.cv_builder import (
    TermDraft,
    batch_to_ops,
    harvest_folksonomy,
    normalize_term_name,
)
from lakeloom.core.operations import (
    AddConcept,
    AddHistoryNote,
    AddScopeNote,
    DeprecateConcept,
    ProposalOperation,
    SetCoverageModel,
)
from lakeloom.core.thesaurus_builder import (
    IssueLevel,
    suggest_related_from_cooccurrence,
    validate_ansi_z39,
    validate_bt_nt_cycles,
    validate_rt_symmetry,
)

# ---------------------------------------------------------------------------
# New operation types
# ---------------------------------------------------------------------------


class TestNewOps:
    def test_add_scope_note_roundtrip(self) -> None:
        from pydantic import TypeAdapter

        adapter: TypeAdapter[ProposalOperation] = TypeAdapter(ProposalOperation)
        op = AddScopeNote(op="add_scope_note", concept_fqn="retail.Customer", note="Note here")
        dumped = op.model_dump()
        loaded = adapter.validate_python(dumped)
        assert isinstance(loaded, AddScopeNote)
        assert loaded.note == "Note here"
        assert loaded.lang == "en"

    def test_add_history_note(self) -> None:
        op = AddHistoryNote(op="add_history_note", concept_fqn="retail.Order", note="Created in M5")
        assert op.lang == "en"

    def test_deprecate_concept(self) -> None:
        op = DeprecateConcept(
            op="deprecate_concept",
            fqn="retail.OldConcept",
            base_version="1.0",
            reason="Replaced by NewConcept",
            successor="retail.NewConcept",
        )
        assert op.successor == "retail.NewConcept"

    def test_set_coverage_model(self) -> None:
        op = SetCoverageModel(
            op="set_coverage_model",
            topics=["Customer", "Order"],
            depth=3,
            granularity="medium",
        )
        assert op.granularity == "medium"
        assert len(op.topics) == 2

    def test_new_ops_return_empty_om_ops(self) -> None:
        from lakeloom.core.operations import OMContext, to_om

        ctx = OMContext(
            glossary_name="retail", glossary_id="g-1", base_iri="http://x/", ontology_id="o-1"
        )
        for op in [
            AddScopeNote(op="add_scope_note", concept_fqn="retail.X", note="n"),
            AddHistoryNote(op="add_history_note", concept_fqn="retail.X", note="n"),
            DeprecateConcept(op="deprecate_concept", fqn="retail.X", base_version="1", reason="r"),
            SetCoverageModel(op="set_coverage_model", topics=["T"], depth=2, granularity="coarse"),
        ]:
            result = to_om(op, ctx)
            assert result.glossary == [], f"{type(op).__name__} should produce no glossary ops"
            assert result.native == [], f"{type(op).__name__} should produce no native ops"


# ---------------------------------------------------------------------------
# cv_builder
# ---------------------------------------------------------------------------


class TestNormalizeTermName:
    def test_strips_prefix(self) -> None:
        assert normalize_term_name("PII.Email") == "Email"

    def test_domain_prefix(self) -> None:
        assert normalize_term_name("Domain.Order Management") == "Order Management"

    def test_underscores(self) -> None:
        assert normalize_term_name("order_line_item") == "Order Line Item"

    def test_invalid_starts_with_digit(self) -> None:
        assert normalize_term_name("123bad") is None

    def test_empty_after_strip(self) -> None:
        assert normalize_term_name("!!!") is None

    def test_plain_label(self) -> None:
        assert normalize_term_name("Invoice") == "Invoice"


class TestHarvestFolksonomy:
    def test_basic(self) -> None:
        tags = ["PII.Email", "Domain.Order", "PII.Name"]
        freq = {"PII.Email": 10, "Domain.Order": 5, "PII.Name": 3}
        candidates = harvest_folksonomy(tags, frequency_map=freq)
        names = [c.name for c in candidates]
        assert "Email" in names
        assert "Order" in names
        assert "Name" in names

    def test_sorted_by_frequency(self) -> None:
        tags = ["PII.Email", "Domain.Order"]
        freq = {"PII.Email": 10, "Domain.Order": 20}
        candidates = harvest_folksonomy(tags, frequency_map=freq)
        assert candidates[0].name == "Order"

    def test_deduplicates_same_name(self) -> None:
        tags = ["Dept.Customer", "Domain.Customer"]
        freq = {"Dept.Customer": 5, "Domain.Customer": 15}
        candidates = harvest_folksonomy(tags, frequency_map=freq)
        assert len([c for c in candidates if c.name == "Customer"]) == 1
        assert next(c for c in candidates if c.name == "Customer").frequency == 15

    def test_skips_invalid_tags(self) -> None:
        # "!!!" normalises to empty → no valid name → skipped
        candidates = harvest_folksonomy(["!!!"])
        assert candidates == []


class TestBatchToOps:
    def test_single_term(self) -> None:
        draft = TermDraft(name="Customer", definition="A buyer of goods")
        ops = batch_to_ops([draft])
        concepts = [op for op in ops if isinstance(op, AddConcept)]
        assert len(concepts) == 1
        assert concepts[0].name == "Customer"

    def test_scope_note_added(self) -> None:
        draft = TermDraft(name="Order", scope_note="Refers only to confirmed orders")
        ops = batch_to_ops([draft])
        notes = [op for op in ops if isinstance(op, AddScopeNote)]
        assert len(notes) == 1
        assert notes[0].note == "Refers only to confirmed orders"

    def test_history_note_added(self) -> None:
        draft = TermDraft(name="Product", history_note="Added in M5a")
        ops = batch_to_ops([draft])
        notes = [op for op in ops if isinstance(op, AddHistoryNote)]
        assert len(notes) == 1

    def test_broader_set(self) -> None:
        draft = TermDraft(name="LineItem", broader="retail.Order")
        ops = batch_to_ops([draft])
        concept = next(op for op in ops if isinstance(op, AddConcept))
        assert concept.parent == "retail.Order"

    def test_exceeds_limit_raises(self) -> None:
        from lakeloom.core.cv_builder import MAX_TERMS_PER_BATCH

        terms = [TermDraft(name=f"T{i}") for i in range(MAX_TERMS_PER_BATCH + 1)]
        with pytest.raises(ValueError, match="limit"):
            batch_to_ops(terms)

    def test_concepts_before_enrichments(self) -> None:
        drafts = [
            TermDraft(name="A", scope_note="note A"),
            TermDraft(name="B"),
        ]
        ops = batch_to_ops(drafts)
        concept_indices = [i for i, op in enumerate(ops) if isinstance(op, AddConcept)]
        note_indices = [i for i, op in enumerate(ops) if isinstance(op, AddScopeNote)]
        assert all(c < n for c in concept_indices for n in note_indices)


# ---------------------------------------------------------------------------
# thesaurus_builder
# ---------------------------------------------------------------------------


def _make_graph(triples: list[tuple]) -> Graph:
    g = Graph()
    for s, p, o in triples:
        g.add((s, p, o))
    return g


A = URIRef("http://x/A")
B = URIRef("http://x/B")
C = URIRef("http://x/C")


class TestValidateBtNtCycles:
    def test_no_cycle(self) -> None:
        g = _make_graph(
            [
                (A, RDF.type, SKOS.Concept),
                (B, RDF.type, SKOS.Concept),
                (B, SKOS.broader, A),
            ]
        )
        issues = validate_bt_nt_cycles(g)
        assert issues == []

    def test_direct_cycle(self) -> None:
        g = _make_graph(
            [
                (A, RDF.type, SKOS.Concept),
                (B, RDF.type, SKOS.Concept),
                (A, SKOS.broader, B),
                (B, SKOS.broader, A),
            ]
        )
        issues = validate_bt_nt_cycles(g)
        assert any(i.level == IssueLevel.ERROR for i in issues)


class TestValidateRtSymmetry:
    def test_symmetric(self) -> None:
        g = _make_graph(
            [
                (A, SKOS.related, B),
                (B, SKOS.related, A),
            ]
        )
        assert validate_rt_symmetry(g) == []

    def test_asymmetric(self) -> None:
        g = _make_graph([(A, SKOS.related, B)])
        issues = validate_rt_symmetry(g)
        assert len(issues) == 1
        assert issues[0].level == IssueLevel.WARNING


class TestValidateAnsiZ39:
    def test_basic_report(self) -> None:
        g = _make_graph(
            [
                (A, RDF.type, SKOS.Concept),
                (A, SKOS.prefLabel, Literal("Alpha", lang="en")),
                (A, SKOS.definition, Literal("Definition of Alpha", lang="en")),
                (B, RDF.type, SKOS.Concept),
                (B, SKOS.broader, A),
            ]
        )
        report = validate_ansi_z39(g)
        assert report.total_concepts == 2
        assert report.total_bt_nt_pairs == 1


class TestSuggestRelated:
    def test_suggests_pair(self) -> None:
        realizations = {
            "retail.Order": {"a1", "a2", "a3"},
            "retail.Customer": {"a1", "a2", "a4"},
            "retail.Product": {"a5", "a6"},
        }
        suggestions = suggest_related_from_cooccurrence(realizations, min_shared=2)
        assert any(
            s.concept_a_fqn in ("retail.Order", "retail.Customer")
            and s.concept_b_fqn in ("retail.Order", "retail.Customer")
            for s in suggestions
        )

    def test_no_overlap(self) -> None:
        realizations = {
            "retail.A": {"x1"},
            "retail.B": {"x2"},
        }
        assert suggest_related_from_cooccurrence(realizations, min_shared=2) == []


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------


class TestHierarchyDepth:
    def test_root_depth_1(self) -> None:
        g = _make_graph([(A, RDF.type, SKOS.Concept)])
        depths = hierarchy_depth(g)
        assert depths[A] == 1

    def test_child_depth_2(self) -> None:
        g = _make_graph(
            [
                (A, RDF.type, SKOS.Concept),
                (B, RDF.type, SKOS.Concept),
                (B, SKOS.broader, A),
            ]
        )
        depths = hierarchy_depth(g)
        assert depths[A] == 1
        assert depths[B] == 2


class TestAssessCoverage:
    def _graph(self) -> Graph:
        g = Graph()
        for concept, label in [(A, "Customer"), (B, "Order"), (C, "Product")]:
            g.add((concept, RDF.type, SKOS.Concept))
            g.add((concept, SKOS.prefLabel, Literal(label, lang="en")))
        g.add((B, SKOS.broader, A))
        return g

    def test_matched_topics(self) -> None:
        model = CoverageModel.from_lists(["Customer", "Order"], depth=3, granularity="medium")
        report = assess_coverage(self._graph(), model)
        assert len(report.matched_topics) == 2
        assert report.gaps == []

    def test_gap_detected(self) -> None:
        model = CoverageModel.from_lists(["Customer", "Invoice"], depth=3, granularity="medium")
        report = assess_coverage(self._graph(), model)
        gap_topics = [g.topic for g in report.gaps]
        assert "Invoice" in gap_topics

    def test_depth_violation(self) -> None:
        model = CoverageModel.from_lists(["Customer"], depth=1, granularity="coarse")
        report = assess_coverage(self._graph(), model)
        # B (Order) has depth 2, which exceeds max 1
        assert any(v.actual_depth > 1 for v in report.depth_violations)

    def test_exclusions_skipped(self) -> None:
        model = CoverageModel.from_lists(
            ["Customer", "Order"], depth=3, granularity="medium", exclusions=["Order"]
        )
        report = assess_coverage(self._graph(), model)
        # "Order" is excluded, so only "Customer" is checked
        assert all(m.topic != "Order" for m in report.matched_topics)
        assert all(g.topic != "Order" for g in report.gaps)

    def test_coverage_ratio(self) -> None:
        model = CoverageModel.from_lists(
            ["Customer", "Order", "Invoice"], depth=3, granularity="medium"
        )
        report = assess_coverage(self._graph(), model)
        # 2 matched, 1 gap → 2/3
        assert abs(report.coverage_ratio - 2 / 3) < 0.01


def test_batch_orders_parents_before_children() -> None:
    from lakeloom.core.cv_builder import TermDraft, batch_to_ops

    ops = batch_to_ops(
        [
            TermDraft(name="Puppy", broader="Dog"),
            TermDraft(name="Dog", broader="Animal", authority_source="AKC"),
            TermDraft(name="Animal"),
        ]
    )
    assert [o.name for o in ops] == ["Animal", "Dog", "Puppy"]  # type: ignore[attr-defined]
    assert ops[1].evidence[0].detail == "authority: AKC"
