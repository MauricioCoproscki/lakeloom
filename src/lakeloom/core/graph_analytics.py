"""Graph analytics for SKOS ontology models (M6b).

Provides structural analysis of a SKOS graph — depth, hierarchy shape, hubs,
orphans, and realization coverage — without any I/O.  All public functions
accept a pre-loaded ``rdflib.Graph`` so the MCP tools can inject a live graph
while tests supply a minimal in-memory one.

Public API:
  concept_stats(graph)              → ConceptStats
  hierarchy_report(graph)           → HierarchyReport
  realization_coverage(graph, real) → CoverageStats
  analytics_markdown(stats, hier, cov) → str
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

from rdflib import Graph, URIRef
from rdflib.namespace import RDF, SKOS

__all__ = [
    "ConceptStats",
    "CoverageStats",
    "HierarchyReport",
    "analytics_markdown",
    "concept_stats",
    "hierarchy_report",
    "realization_coverage",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConceptStats:
    """Structural statistics about concept count and graph depth."""

    total_concepts: int
    total_relations: int  # all skos:broader + skos:narrower + skos:related
    avg_depth: float  # mean BFS depth from root concepts
    max_depth: int
    isolated_concepts: int  # no BT, NT or RT
    hub_concepts: list[str]  # IRIs with ≥ 5 outgoing broader/narrower/related


@dataclass(frozen=True)
class HierarchyReport:
    """Shape of the broader/narrower hierarchy."""

    roots: list[str]  # concepts with no skos:broader
    leaves: list[str]  # concepts with no skos:narrower
    polyhierarchy_nodes: list[str]  # concepts with ≥ 2 distinct broader parents
    orphan_concepts: list[str]  # concepts completely disconnected (no BT/NT/RT)
    total_top_concepts: int  # skos:topConceptOf / scheme has topConcept


@dataclass(frozen=True)
class CoverageStats:
    """Realization coverage — how many concepts have physical assets."""

    total_concepts: int
    concepts_with_realizations: int
    concepts_without_realizations: int
    coverage_ratio: float  # 0.0-1.0
    top_realized: list[str]  # top-10 concept IRIs by realization count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _concepts(graph: Graph) -> list[URIRef]:
    return [s for s in graph.subjects(RDF.type, SKOS.Concept) if isinstance(s, URIRef)]


def _broader_map(graph: Graph, concepts: list[URIRef]) -> dict[URIRef, set[URIRef]]:
    """concept → set of its skos:broader parents."""
    bmap: dict[URIRef, set[URIRef]] = defaultdict(set)
    for c in concepts:
        for b in graph.objects(c, SKOS.broader):
            if isinstance(b, URIRef):
                bmap[c].add(b)
    return bmap


def _narrower_map(graph: Graph, concepts: list[URIRef]) -> dict[URIRef, set[URIRef]]:
    """concept → set of its skos:narrower children."""
    nmap: dict[URIRef, set[URIRef]] = defaultdict(set)
    for c in concepts:
        for n in graph.objects(c, SKOS.narrower):
            if isinstance(n, URIRef):
                nmap[c].add(n)
    # Also infer from broader (some graphs only record one direction).
    for c in concepts:
        for b in graph.objects(c, SKOS.broader):
            if isinstance(b, URIRef):
                nmap[b].add(c)
    return nmap


def _related_map(graph: Graph, concepts: list[URIRef]) -> dict[URIRef, set[URIRef]]:
    rmap: dict[URIRef, set[URIRef]] = defaultdict(set)
    for c in concepts:
        for r in graph.objects(c, SKOS.related):
            if isinstance(r, URIRef):
                rmap[c].add(r)
                rmap[r].add(c)  # symmetric
    return rmap


def _bfs_depths(
    roots: list[URIRef],
    narrower: dict[URIRef, set[URIRef]],
) -> dict[URIRef, int]:
    """Return BFS depth from roots (0 = root)."""
    depths: dict[URIRef, int] = {}
    queue: deque[tuple[URIRef, int]] = deque((r, 0) for r in roots)
    while queue:
        node, d = queue.popleft()
        if node in depths:
            continue
        depths[node] = d
        for child in narrower.get(node, ()):
            if child not in depths:
                queue.append((child, d + 1))
    return depths


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def concept_stats(graph: Graph) -> ConceptStats:
    """Compute structural statistics for the concept graph."""
    concepts = _concepts(graph)
    n = len(concepts)
    if n == 0:
        return ConceptStats(
            total_concepts=0,
            total_relations=0,
            avg_depth=0.0,
            max_depth=0,
            isolated_concepts=0,
            hub_concepts=[],
        )

    broader = _broader_map(graph, concepts)
    narrower = _narrower_map(graph, concepts)
    related = _related_map(graph, concepts)

    total_relations = (
        sum(len(v) for v in broader.values())
        + sum(len(v) for v in narrower.values())
        + sum(len(v) for v in related.values())
    )

    roots = [c for c in concepts if not broader.get(c)]
    depths = _bfs_depths(roots, narrower)
    # Assign depth 0 to unreachable concepts (disconnected sub-graphs).
    all_depths = [depths.get(c, 0) for c in concepts]
    avg_depth = sum(all_depths) / n if n else 0.0
    max_depth = max(all_depths, default=0)

    isolated = [
        c for c in concepts if not broader.get(c) and not narrower.get(c) and not related.get(c)
    ]

    degree: dict[URIRef, int] = defaultdict(int)
    for c in concepts:
        degree[c] += len(broader.get(c, ()))
        degree[c] += len(narrower.get(c, ()))
        degree[c] += len(related.get(c, ()))
    hubs = [str(c) for c, d in degree.items() if d >= 5]

    return ConceptStats(
        total_concepts=n,
        total_relations=total_relations,
        avg_depth=round(avg_depth, 2),
        max_depth=max_depth,
        isolated_concepts=len(isolated),
        hub_concepts=sorted(hubs),
    )


def hierarchy_report(graph: Graph) -> HierarchyReport:
    """Describe the shape of the broader/narrower hierarchy."""
    concepts = _concepts(graph)
    if not concepts:
        return HierarchyReport(
            roots=[],
            leaves=[],
            polyhierarchy_nodes=[],
            orphan_concepts=[],
            total_top_concepts=0,
        )

    broader = _broader_map(graph, concepts)
    narrower = _narrower_map(graph, concepts)
    related = _related_map(graph, concepts)

    roots = [str(c) for c in concepts if not broader.get(c)]
    leaves = [str(c) for c in concepts if not narrower.get(c)]
    polyhierarchy = [str(c) for c in concepts if len(broader.get(c, ())) >= 2]
    orphans = [
        str(c)
        for c in concepts
        if not broader.get(c) and not narrower.get(c) and not related.get(c)
    ]

    # Count scheme-level top concepts.
    top_count = sum(1 for _ in graph.subject_objects(SKOS.topConceptOf)) + sum(
        1 for _ in graph.subject_objects(SKOS.hasTopConcept)
    )

    return HierarchyReport(
        roots=sorted(roots),
        leaves=sorted(leaves),
        polyhierarchy_nodes=sorted(polyhierarchy),
        orphan_concepts=sorted(orphans),
        total_top_concepts=top_count,
    )


def realization_coverage(
    graph: Graph,
    realizations: dict[str, list[str]],
) -> CoverageStats:
    """Compute concept coverage based on physical asset realizations.

    Parameters
    ----------
    graph:
        SKOS graph for the model.
    realizations:
        Mapping ``concept_iri → list[asset_fqn]``.  Keys that are not in the
        graph are ignored.
    """
    concepts = _concepts(graph)
    n = len(concepts)
    if n == 0:
        return CoverageStats(
            total_concepts=0,
            concepts_with_realizations=0,
            concepts_without_realizations=0,
            coverage_ratio=0.0,
            top_realized=[],
        )

    concept_iris = {str(c) for c in concepts}
    counts: dict[str, int] = {}
    for iri, assets in realizations.items():
        if iri in concept_iris:
            counts[iri] = len(assets)

    with_real = len(counts)
    without_real = n - with_real
    ratio = round(with_real / n, 4) if n else 0.0
    top = sorted(counts, key=lambda k: -counts[k])[:10]

    return CoverageStats(
        total_concepts=n,
        concepts_with_realizations=with_real,
        concepts_without_realizations=without_real,
        coverage_ratio=ratio,
        top_realized=top,
    )


# ---------------------------------------------------------------------------
# Markdown export
# ---------------------------------------------------------------------------


def analytics_markdown(
    stats: ConceptStats,
    hier: HierarchyReport,
    cov: CoverageStats,
) -> str:
    """Format all three analytics objects as a Markdown report."""
    lines: list[str] = ["# Ontology Graph Analytics\n"]

    lines.append("## Concept Stats")
    lines.append(f"- **Total concepts:** {stats.total_concepts}")
    lines.append(f"- **Total relations:** {stats.total_relations}")
    lines.append(f"- **Avg depth:** {stats.avg_depth}")
    lines.append(f"- **Max depth:** {stats.max_depth}")
    lines.append(f"- **Isolated concepts:** {stats.isolated_concepts}")
    if stats.hub_concepts:
        lines.append(f"- **Hub concepts ({len(stats.hub_concepts)}):**")
        for iri in stats.hub_concepts[:5]:
            lines.append(f"  - `{iri}`")
        if len(stats.hub_concepts) > 5:
            lines.append(f"  - … ({len(stats.hub_concepts) - 5} more)")

    lines.append("\n## Hierarchy Report")
    lines.append(f"- **Root concepts:** {len(hier.roots)}")
    lines.append(f"- **Leaf concepts:** {len(hier.leaves)}")
    lines.append(f"- **Polyhierarchy nodes:** {len(hier.polyhierarchy_nodes)}")
    lines.append(f"- **Orphan concepts:** {len(hier.orphan_concepts)}")
    lines.append(f"- **Top concepts (scheme):** {hier.total_top_concepts}")
    if hier.orphan_concepts:
        lines.append("\n**Orphans:**")
        for iri in hier.orphan_concepts[:5]:
            lines.append(f"  - `{iri}`")

    lines.append("\n## Realization Coverage")
    pct = round(cov.coverage_ratio * 100, 1)
    lines.append(f"- **Coverage:** {cov.concepts_with_realizations}/{cov.total_concepts} ({pct}%)")
    lines.append(f"- **Without realizations:** {cov.concepts_without_realizations}")
    if cov.top_realized:
        lines.append("\n**Top realized concepts:**")
        for iri in cov.top_realized[:5]:
            lines.append(f"  - `{iri}`")

    return "\n".join(lines)
