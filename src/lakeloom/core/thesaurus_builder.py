"""Thesaurus validation and relationship-suggestion utilities (Stage 4 of the Ontology Pipeline).

Validates a model's RDF graph against ANSI Z39.19 / ISO 25964 structural rules and
suggests ``skos:related`` (RT) candidates based on co-occurrence of asset realizations.

All functions are pure (read-only on the graph); they produce reports and suggestion lists.
Callers decide whether to turn suggestions into proposal operations.

Stage 4 of the Ontology Pipeline (ANSI Z39.19 / ISO 25964):
  Controlled Vocabulary → MAP → Taxonomy → Thesaurus → Ontology → Knowledge Graph
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from rdflib import Graph, URIRef
from rdflib.namespace import SKOS

__all__ = [
    "IssueLevel",
    "RelatedSuggestion",
    "ThesaurusIssue",
    "ThesaurusReport",
    "suggest_related_from_cooccurrence",
    "validate_ansi_z39",
    "validate_bt_nt_cycles",
    "validate_rt_symmetry",
    "validate_scope_notes",
]


class IssueLevel(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class ThesaurusIssue:
    """A single validation finding."""

    level: IssueLevel
    rule: str
    message: str
    subject: str | None = None


@dataclass
class ThesaurusReport:
    """Aggregate result of :func:`validate_ansi_z39`."""

    total_concepts: int = 0
    concepts_with_scope_note: int = 0
    concepts_with_definition: int = 0
    total_bt_nt_pairs: int = 0
    total_rt_pairs: int = 0
    issues: list[ThesaurusIssue] = field(default_factory=list["ThesaurusIssue"])

    @property
    def errors(self) -> list[ThesaurusIssue]:
        return [i for i in self.issues if i.level == IssueLevel.ERROR]

    @property
    def warnings(self) -> list[ThesaurusIssue]:
        return [i for i in self.issues if i.level == IssueLevel.WARNING]

    def to_markdown(self) -> str:
        lines = [
            "## Thesaurus Validation Report",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Total concepts | {self.total_concepts} |",
            f"| With definition | {self.concepts_with_definition} |",
            f"| With scope note | {self.concepts_with_scope_note} |",
            f"| BT/NT pairs | {self.total_bt_nt_pairs} |",
            f"| RT pairs | {self.total_rt_pairs} |",
            f"| Errors | {len(self.errors)} |",
            f"| Warnings | {len(self.warnings)} |",
            "",
        ]
        if not self.issues:
            lines.append("✅ No issues found.")
            return "\n".join(lines)
        for issue in self.issues:
            icon = "❌" if issue.level == IssueLevel.ERROR else "⚠️"
            subject = f" (`{issue.subject}`)" if issue.subject else ""
            lines.append(f"- {icon} **{issue.rule}**{subject}: {issue.message}")
        return "\n".join(lines)


@dataclass(frozen=True)
class RelatedSuggestion:
    """A candidate ``skos:related`` pair derived from asset co-occurrence."""

    concept_a_fqn: str
    concept_b_fqn: str
    # Number of assets tagged with both concepts.
    shared_asset_count: int
    # Fraction of concept_a's assets that also have concept_b (Jaccard-like).
    overlap_score: float


def _label(node: URIRef, graph: Graph) -> str:
    """Best short label for a node: prefLabel > local name."""
    for label in graph.objects(node, SKOS.prefLabel):
        return str(label)
    return str(node).rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def validate_bt_nt_cycles(graph: Graph) -> list[ThesaurusIssue]:
    """Detect cycles in the ``skos:broader`` / ``skos:narrower`` hierarchy (ANSI Z39.19 §8.3).

    A cycle means concept A is an ancestor of itself, which makes the taxonomy
    malformed and breaks reasoners.  Returns one issue per cycle (the full path).
    """
    issues: list[ThesaurusIssue] = []
    # Build adjacency: concept → set of its broader concepts.
    broader: dict[URIRef, set[URIRef]] = {}
    for s, _, o in graph.triples((None, SKOS.broader, None)):
        if isinstance(s, URIRef) and isinstance(o, URIRef):
            broader.setdefault(s, set()).add(o)
    # DFS cycle detection.
    visited: set[URIRef] = set()
    in_stack: set[URIRef] = set()

    def dfs(node: URIRef, path: list[URIRef]) -> None:
        if node in in_stack:
            cycle = [*path[path.index(node) :], node]
            cycle_str = " → ".join(_label(n, graph) for n in cycle)
            issues.append(
                ThesaurusIssue(
                    level=IssueLevel.ERROR,
                    rule="ANSI-Z39-BT-CYCLE",
                    message=f"Broader/narrower cycle detected: {cycle_str}",
                    subject=_label(node, graph),
                )
            )
            return
        if node in visited:
            return
        visited.add(node)
        in_stack.add(node)
        for parent in broader.get(node, set()):
            dfs(parent, [*path, node])
        in_stack.discard(node)

    for concept in broader:
        dfs(concept, [])
    return issues


def validate_rt_symmetry(graph: Graph) -> list[ThesaurusIssue]:
    """Check that ``skos:related`` is symmetric (ANSI Z39.19 §8.4).

    If A skos:related B, then B skos:related A must also be asserted or inferrable.
    Returns a warning for each asymmetric pair.
    """
    issues: list[ThesaurusIssue] = []
    pairs: set[tuple[URIRef, URIRef]] = set()
    for s, _, o in graph.triples((None, SKOS.related, None)):
        if isinstance(s, URIRef) and isinstance(o, URIRef):
            pairs.add((s, o))
    for a, b in pairs:
        if (b, a) not in pairs:
            issues.append(
                ThesaurusIssue(
                    level=IssueLevel.WARNING,
                    rule="ANSI-Z39-RT-ASYMMETRIC",
                    message=(
                        f"`{_label(a, graph)}` skos:related `{_label(b, graph)}` "
                        f"but the inverse is missing"
                    ),
                    subject=_label(a, graph),
                )
            )
    return issues


def validate_scope_notes(graph: Graph) -> list[ThesaurusIssue]:
    """Warn on concepts with no ``skos:definition`` and no ``skos:scopeNote`` (ISO 25964 §8.2).

    A term without any definition is ambiguous; this is a best-practice warning.
    Concepts that are purely structural (OWL restrictions, blank nodes) are skipped.
    """
    issues: list[ThesaurusIssue] = []
    for concept in graph.subjects(SKOS.inScheme, None):
        if not isinstance(concept, URIRef):
            continue
        has_def = any(True for _ in graph.objects(concept, SKOS.definition))
        has_scope = any(True for _ in graph.objects(concept, SKOS.scopeNote))
        if not has_def and not has_scope:
            issues.append(
                ThesaurusIssue(
                    level=IssueLevel.WARNING,
                    rule="ISO-25964-NO-DEFINITION",
                    message="Concept has no skos:definition or skos:scopeNote",
                    subject=_label(concept, graph),
                )
            )
    return issues


def validate_ansi_z39(graph: Graph) -> ThesaurusReport:
    """Run all ANSI Z39.19 / ISO 25964 checks and return a consolidated report."""
    report = ThesaurusReport()

    concepts = list(graph.subjects(SKOS["type"] if False else None, SKOS.Concept))
    # rdflib: iterate via RDF.type
    from rdflib.namespace import RDF

    concepts = [s for s, _, _ in graph.triples((None, RDF.type, SKOS.Concept))]
    report.total_concepts = len(concepts)

    for c in concepts:
        if any(True for _ in graph.objects(c, SKOS.definition)):
            report.concepts_with_definition += 1
        if any(True for _ in graph.objects(c, SKOS.scopeNote)):
            report.concepts_with_scope_note += 1

    report.total_bt_nt_pairs = sum(1 for _ in graph.triples((None, SKOS.broader, None)))
    report.total_rt_pairs = (
        sum(1 for _ in graph.triples((None, SKOS.related, None))) // 2
    )  # each pair counted twice

    report.issues.extend(validate_bt_nt_cycles(graph))
    report.issues.extend(validate_rt_symmetry(graph))
    report.issues.extend(validate_scope_notes(graph))

    return report


def suggest_related_from_cooccurrence(
    concept_realizations: dict[str, set[str]],
    *,
    min_shared: int = 2,
    min_score: float = 0.1,
    max_suggestions: int = 50,
) -> list[RelatedSuggestion]:
    """Suggest ``skos:related`` pairs using asset co-occurrence (no embeddings required).

    ``concept_realizations`` maps each concept FQN to the set of asset FQNs that realize
    it (from :func:`lakeloom.core.concepts.find_assets_for_concept`).

    Two concepts are suggested as RT when they share at least ``min_shared`` assets and
    their overlap score (shared / union) ≥ ``min_score``.

    Returns up to ``max_suggestions`` pairs sorted by overlap score descending.
    """
    suggestions: list[RelatedSuggestion] = []
    concepts = list(concept_realizations)
    for i, a in enumerate(concepts):
        assets_a = concept_realizations[a]
        if not assets_a:
            continue
        for b in concepts[i + 1 :]:
            assets_b = concept_realizations[b]
            shared = assets_a & assets_b
            if len(shared) < min_shared:
                continue
            union = assets_a | assets_b
            score = len(shared) / len(union) if union else 0.0
            if score < min_score:
                continue
            suggestions.append(
                RelatedSuggestion(
                    concept_a_fqn=a,
                    concept_b_fqn=b,
                    shared_asset_count=len(shared),
                    overlap_score=round(score, 4),
                )
            )
    suggestions.sort(key=lambda s: -s.overlap_score)
    return suggestions[:max_suggestions]
