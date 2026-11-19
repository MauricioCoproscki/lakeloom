"""Unit tests for graph_analytics module (M6b)."""

from __future__ import annotations

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, SKOS

from lakeloom.core.graph_analytics import (
    analytics_markdown,
    concept_stats,
    hierarchy_report,
    realization_coverage,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NS = "http://example.org/concepts/"


def _c(name: str) -> URIRef:
    return URIRef(f"{_NS}{name}")


def _graph(*concepts: str) -> Graph:
    """Minimal SKOS graph with the given concept names."""
    g = Graph()
    for name in concepts:
        g.add((_c(name), RDF.type, SKOS.Concept))
        g.add((_c(name), SKOS.prefLabel, Literal(name, lang="en")))
    return g


def _hierarchy(parent: str, *children: str) -> Graph:
    """Graph: parent → children via skos:broader."""
    g = _graph(parent, *children)
    for child in children:
        g.add((_c(child), SKOS.broader, _c(parent)))
    return g


# ---------------------------------------------------------------------------
# concept_stats
# ---------------------------------------------------------------------------


class TestConceptStats:
    def test_empty_graph(self) -> None:
        stats = concept_stats(Graph())
        assert stats.total_concepts == 0
        assert stats.max_depth == 0

    def test_single_concept(self) -> None:
        stats = concept_stats(_graph("Animal"))
        assert stats.total_concepts == 1
        assert stats.isolated_concepts == 1

    def test_isolated_count(self) -> None:
        # Two concepts with no relations.
        stats = concept_stats(_graph("A", "B"))
        assert stats.isolated_concepts == 2

    def test_connected_not_isolated(self) -> None:
        g = _hierarchy("Animal", "Dog")
        stats = concept_stats(g)
        assert stats.isolated_concepts == 0

    def test_max_depth_chain(self) -> None:
        # A → B → C (depth 0, 1, 2).
        g = _graph("A", "B", "C")
        g.add((_c("B"), SKOS.broader, _c("A")))
        g.add((_c("C"), SKOS.broader, _c("B")))
        stats = concept_stats(g)
        assert stats.max_depth == 2

    def test_total_relations_counts_broader_and_narrower(self) -> None:
        g = _hierarchy("Animal", "Dog")
        stats = concept_stats(g)
        # broader(Dog→Animal) + narrower(Animal→Dog) inferred = 2 minimum
        assert stats.total_relations >= 2

    def test_hub_concept_detected(self) -> None:
        # One node connected to 5+ others.
        g = _graph("Hub", "A", "B", "C", "D", "E")
        for child in ["A", "B", "C", "D", "E"]:
            g.add((_c(child), SKOS.broader, _c("Hub")))
        stats = concept_stats(g)
        assert f"{_NS}Hub" in stats.hub_concepts

    def test_avg_depth_flat(self) -> None:
        # All roots, avg depth = 0.
        stats = concept_stats(_graph("A", "B", "C"))
        assert stats.avg_depth == 0.0


# ---------------------------------------------------------------------------
# hierarchy_report
# ---------------------------------------------------------------------------


class TestHierarchyReport:
    def test_empty_graph(self) -> None:
        report = hierarchy_report(Graph())
        assert report.roots == []
        assert report.leaves == []

    def test_single_root(self) -> None:
        report = hierarchy_report(_graph("Animal"))
        assert f"{_NS}Animal" in report.roots
        assert f"{_NS}Animal" in report.leaves  # also a leaf (no children)

    def test_root_and_leaf(self) -> None:
        g = _hierarchy("Animal", "Dog")
        report = hierarchy_report(g)
        assert f"{_NS}Animal" in report.roots
        assert f"{_NS}Dog" in report.leaves
        assert f"{_NS}Animal" not in report.leaves

    def test_polyhierarchy(self) -> None:
        g = _graph("A", "B", "C")
        g.add((_c("C"), SKOS.broader, _c("A")))
        g.add((_c("C"), SKOS.broader, _c("B")))
        report = hierarchy_report(g)
        assert f"{_NS}C" in report.polyhierarchy_nodes

    def test_orphan_concept(self) -> None:
        g = _hierarchy("Animal", "Dog")
        g.add((_c("Orphan"), RDF.type, SKOS.Concept))
        report = hierarchy_report(g)
        assert f"{_NS}Orphan" in report.orphan_concepts

    def test_no_orphans_when_connected(self) -> None:
        g = _hierarchy("Animal", "Dog")
        report = hierarchy_report(g)
        assert report.orphan_concepts == []


# ---------------------------------------------------------------------------
# realization_coverage
# ---------------------------------------------------------------------------


class TestRealizationCoverage:
    def _graph(self) -> Graph:
        return _graph("A", "B", "C")

    def test_zero_realizations(self) -> None:
        cov = realization_coverage(self._graph(), {})
        assert cov.coverage_ratio == 0.0
        assert cov.concepts_with_realizations == 0
        assert cov.concepts_without_realizations == 3

    def test_full_coverage(self) -> None:
        cov = realization_coverage(
            self._graph(),
            {
                f"{_NS}A": ["tbl.a"],
                f"{_NS}B": ["tbl.b"],
                f"{_NS}C": ["tbl.c"],
            },
        )
        assert cov.coverage_ratio == 1.0
        assert cov.concepts_with_realizations == 3
        assert cov.concepts_without_realizations == 0

    def test_partial_coverage(self) -> None:
        cov = realization_coverage(
            self._graph(),
            {f"{_NS}A": ["tbl.a"]},
        )
        assert abs(cov.coverage_ratio - 1 / 3) < 0.01
        assert cov.concepts_with_realizations == 1

    def test_top_realized_sorted(self) -> None:
        cov = realization_coverage(
            self._graph(),
            {
                f"{_NS}A": ["x", "y", "z"],
                f"{_NS}B": ["p"],
            },
        )
        assert cov.top_realized[0] == f"{_NS}A"

    def test_unknown_iri_ignored(self) -> None:
        cov = realization_coverage(
            self._graph(),
            {"http://unknown/X": ["tbl.x"]},
        )
        assert cov.concepts_with_realizations == 0

    def test_empty_graph(self) -> None:
        cov = realization_coverage(Graph(), {})
        assert cov.total_concepts == 0
        assert cov.coverage_ratio == 0.0


# ---------------------------------------------------------------------------
# analytics_markdown
# ---------------------------------------------------------------------------


class TestAnalyticsMarkdown:
    def test_contains_all_sections(self) -> None:
        g = _hierarchy("Animal", "Dog")
        stats = concept_stats(g)
        hier = hierarchy_report(g)
        cov = realization_coverage(g, {f"{_NS}Dog": ["tbl.dogs"]})
        md = analytics_markdown(stats, hier, cov)
        assert "Concept Stats" in md
        assert "Hierarchy Report" in md
        assert "Realization Coverage" in md

    def test_coverage_percent_shown(self) -> None:
        g = _graph("A", "B")
        stats = concept_stats(g)
        hier = hierarchy_report(g)
        cov = realization_coverage(g, {f"{_NS}A": ["tbl.a"]})
        md = analytics_markdown(stats, hier, cov)
        assert "50.0%" in md
