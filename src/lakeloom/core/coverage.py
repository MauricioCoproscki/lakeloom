"""Taxonomy coverage model: define the intended scope of an ontology and measure gaps.

The coverage model answers the four questions from Stage 3 of the Ontology Pipeline:
  1. What topics should this taxonomy cover?
  2. How deep should the hierarchy go?
  3. At what granularity (coarse/medium/fine)?
  4. What is explicitly excluded?

:func:`assess_coverage` compares the declared coverage model against the actual taxonomy
in the model's RDF graph and returns a :class:`CoverageReport` listing matched topics,
gaps, and depth violations.

Stage 3 of the Ontology Pipeline (ANSI Z39.19):
  Controlled Vocabulary → MAP → Taxonomy → Thesaurus → Ontology → Knowledge Graph
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from rdflib import Graph, URIRef
from rdflib.namespace import RDF, SKOS

__all__ = [
    "CoverageModel",
    "TopicMatch",
    "CoverageGap",
    "DepthViolation",
    "CoverageReport",
    "assess_coverage",
    "hierarchy_depth",
]


@dataclass(frozen=True)
class CoverageModel:
    """The intended scope of a taxonomy.

    ``topics`` names the subject areas the taxonomy must cover (e.g. ``["Customer",
    "Order", "Product"]``).  ``depth`` is the maximum BT/NT nesting level.
    ``granularity`` controls how strict the depth check is:
      - ``"coarse"`` (1-3 levels): top-level categories only
      - ``"medium"`` (3-6 levels): intermediate classification
      - ``"fine"`` (6+ levels): detailed faceted taxonomy
    ``exclusions`` lists topics deliberately out of scope.
    """

    topics: tuple[str, ...]
    depth: int
    granularity: Literal["coarse", "medium", "fine"]
    exclusions: tuple[str, ...] = ()

    @classmethod
    def from_lists(
        cls,
        topics: list[str],
        depth: int,
        granularity: Literal["coarse", "medium", "fine"],
        exclusions: list[str] | None = None,
    ) -> "CoverageModel":
        return cls(
            topics=tuple(topics),
            depth=depth,
            granularity=granularity,
            exclusions=tuple(exclusions or []),
        )


@dataclass(frozen=True)
class TopicMatch:
    """A required topic that was found in the taxonomy."""

    topic: str
    matched_concept: str
    match_score: float  # 1.0 = exact name match; < 1.0 = fuzzy


@dataclass(frozen=True)
class CoverageGap:
    """A required topic that has no corresponding concept in the taxonomy."""

    topic: str
    closest_concept: str | None = None
    closest_score: float = 0.0


@dataclass(frozen=True)
class DepthViolation:
    """A branch that exceeds the declared maximum depth."""

    concept: str
    actual_depth: int
    max_depth: int


@dataclass
class CoverageReport:
    """Result of :func:`assess_coverage`."""

    total_concepts: int = 0
    actual_max_depth: int = 0
    declared_max_depth: int = 0
    root_concepts: list[str] = field(default_factory=list)
    matched_topics: list[TopicMatch] = field(default_factory=list)
    gaps: list[CoverageGap] = field(default_factory=list)
    depth_violations: list[DepthViolation] = field(default_factory=list)

    @property
    def coverage_ratio(self) -> float:
        total = len(self.matched_topics) + len(self.gaps)
        return len(self.matched_topics) / total if total else 1.0

    def to_markdown(self) -> str:
        lines = [
            "## Coverage Assessment Report",
            "",
            f"| Metric | Value |",
            f"|---|---|",
            f"| Total concepts | {self.total_concepts} |",
            f"| Actual max depth | {self.actual_max_depth} |",
            f"| Declared max depth | {self.declared_max_depth} |",
            f"| Topics matched | {len(self.matched_topics)} |",
            f"| Gaps | {len(self.gaps)} |",
            f"| Depth violations | {len(self.depth_violations)} |",
            f"| Coverage ratio | {self.coverage_ratio:.0%} |",
            "",
        ]
        if self.root_concepts:
            roots = ", ".join(f"`{r}`" for r in self.root_concepts[:10])
            if len(self.root_concepts) > 10:
                roots += f" … (+{len(self.root_concepts) - 10} more)"
            lines += [f"**Root concepts:** {roots}", ""]

        if self.matched_topics:
            lines.append("### Matched Topics")
            for m in self.matched_topics:
                score = f" ({m.match_score:.0%})" if m.match_score < 1.0 else ""
                lines.append(f"- ✅ `{m.topic}` → `{m.matched_concept}`{score}")
            lines.append("")

        if self.gaps:
            lines.append("### Gaps (topics not found in taxonomy)")
            for g in self.gaps:
                hint = (
                    f" — closest: `{g.closest_concept}` ({g.closest_score:.0%})"
                    if g.closest_concept
                    else ""
                )
                lines.append(f"- ❌ `{g.topic}`{hint}")
            lines.append("")

        if self.depth_violations:
            lines.append("### Depth Violations")
            for v in self.depth_violations:
                lines.append(
                    f"- ⚠️ `{v.concept}`: depth {v.actual_depth} > declared max {v.max_depth}"
                )
            lines.append("")

        return "\n".join(lines)


def _label(node: URIRef, graph: Graph) -> str:
    for lbl in graph.objects(node, SKOS.prefLabel):
        return str(lbl)
    return str(node).rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def hierarchy_depth(graph: Graph) -> dict[URIRef, int]:
    """Compute the BT/NT depth of every concept in the graph.

    Root concepts (no skos:broader) have depth 1.  Returns a mapping from concept
    URIRef to its depth.  Cycles are handled by capping at the number of concepts.
    """
    concepts: set[URIRef] = {
        s for s, _, _ in graph.triples((None, RDF.type, SKOS.Concept))
        if isinstance(s, URIRef)
    }
    # Build parent map: concept → set of its broader concepts.
    broader: dict[URIRef, set[URIRef]] = {c: set() for c in concepts}
    for s, _, o in graph.triples((None, SKOS.broader, None)):
        if isinstance(s, URIRef) and isinstance(o, URIRef) and s in broader:
            broader[s].add(o)

    depth: dict[URIRef, int] = {}
    max_iter = len(concepts) + 1  # guard against cycles

    def _depth(node: URIRef, seen: frozenset[URIRef] = frozenset()) -> int:
        if node in depth:
            return depth[node]
        if node in seen or len(seen) > max_iter:
            return 1  # cycle or too deep — treat as root
        parents = broader.get(node, set()) - seen
        if not parents:
            depth[node] = 1
            return 1
        d = 1 + max(_depth(p, seen | {node}) for p in parents)
        depth[node] = d
        return d

    for c in concepts:
        _depth(c)
    return depth


def _simple_match_score(topic: str, label: str) -> float:
    """Fuzzy similarity between a topic string and a concept label (no NLP required).

    Returns 1.0 for exact case-insensitive match, a partial score for substring or
    word overlap, 0.0 for no overlap.
    """
    t = topic.casefold()
    l = label.casefold()
    if t == l:
        return 1.0
    if t in l or l in t:
        return 0.8
    # Word overlap (Jaccard on word sets).
    t_words = set(re.split(r"\W+", t)) - {""}
    l_words = set(re.split(r"\W+", l)) - {""}
    if not t_words or not l_words:
        return 0.0
    intersection = t_words & l_words
    union = t_words | l_words
    return len(intersection) / len(union)


def assess_coverage(
    graph: Graph,
    model: CoverageModel,
) -> CoverageReport:
    """Compare a taxonomy graph against a :class:`CoverageModel` and return gaps.

    For each required topic, the taxonomy's concept labels are searched with a
    simple string-similarity scorer (no NLP/embeddings needed).  A topic is
    considered "matched" when a concept with score ≥ 0.8 is found.

    ``depth_violations`` lists concepts whose BT/NT depth exceeds ``model.depth``.
    """
    report = CoverageReport(declared_max_depth=model.depth)

    concepts: list[tuple[URIRef, str]] = [
        (s, _label(s, graph))
        for s, _, _ in graph.triples((None, RDF.type, SKOS.Concept))
        if isinstance(s, URIRef)
    ]
    report.total_concepts = len(concepts)

    # Root concepts (no broader).
    broader_subjects = {
        s for s, _, _ in graph.triples((None, SKOS.broader, None)) if isinstance(s, URIRef)
    }
    report.root_concepts = [lbl for uri, lbl in concepts if uri not in broader_subjects]

    # Depth analysis.
    depths = hierarchy_depth(graph)
    report.actual_max_depth = max(depths.values(), default=0)
    for uri, lbl in concepts:
        d = depths.get(uri, 1)
        if d > model.depth:
            report.depth_violations.append(
                DepthViolation(concept=lbl, actual_depth=d, max_depth=model.depth)
            )

    # Topic matching.
    excluded = {e.casefold() for e in model.exclusions}
    for topic in model.topics:
        if topic.casefold() in excluded:
            continue
        best_score = 0.0
        best_label: str | None = None
        for _, lbl in concepts:
            score = _simple_match_score(topic, lbl)
            if score > best_score:
                best_score = score
                best_label = lbl
        if best_score >= 0.8 and best_label is not None:
            report.matched_topics.append(
                TopicMatch(topic=topic, matched_concept=best_label, match_score=best_score)
            )
        else:
            report.gaps.append(
                CoverageGap(
                    topic=topic,
                    closest_concept=best_label if best_score > 0.0 else None,
                    closest_score=best_score,
                )
            )

    return report
