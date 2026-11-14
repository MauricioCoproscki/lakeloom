"""Model validation report (``validate_model``, FR-PUB-04 companion; ADR 0014).

Pure functions of the merged model graph (``ModelService.graph``) and its generated shapes:
no I/O, no OpenMetadata calls. Structured messages are plain strings; Markdown rendering
makes every name inert (code spans, escaped message text) because labels come from agents.

Counts: ``error``, ``warning`` and ``info`` count every issue found (before the 200 cap);
``concepts`` and ``properties`` are the number of model concepts and of datatype/object
properties inspected.
"""

import re
from collections import defaultdict
from typing import Literal

from pydantic import BaseModel
from rdflib import Graph, URIRef
from rdflib import Literal as RdfLiteral
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from lakeloom.core.models import ModelInfo
from lakeloom.publish.shacl_gen import SH, model_classes
from lakeloom.untrusted import code_span, single_line

__all__ = [
    "MAX_ISSUES",
    "ValidationIssue",
    "ValidationReport",
    "validate_model",
    "validation_markdown",
]

MAX_ISSUES = 200
Severity = Literal["error", "warning", "info"]
_RANK = {"error": 0, "warning": 1, "info": 2}
_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>&|#~])")


class ValidationIssue(BaseModel):
    severity: Severity
    code: str
    subject: str
    message: str


class ValidationReport(BaseModel):
    model: str
    conforms: bool
    counts: dict[str, int]
    issues: list[ValidationIssue]
    truncated: bool = False


def _cycles(edges: dict[URIRef, set[URIRef]]) -> list[list[URIRef]]:
    """Strongly connected components with a cycle (size > 1 or a self-loop), sorted."""
    index: dict[URIRef, int] = {}
    low: dict[URIRef, int] = {}
    on_stack: set[URIRef] = set()
    stack: list[URIRef] = []
    found: list[list[URIRef]] = []
    counter = 0
    for root in sorted(edges):
        if root in index:
            continue
        work: list[tuple[URIRef, list[URIRef]]] = [(root, sorted(edges.get(root, ())))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, todo = work[-1]
            if todo:
                nxt = todo.pop(0)
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, sorted(edges.get(nxt, ()))))
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp: list[URIRef] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    comp.append(member)
                    if member == node:
                        break
                if len(comp) > 1 or node in edges.get(node, ()):
                    found.append(sorted(comp))
    return sorted(found)


def _label(graph: Graph, concept: URIRef) -> list[RdfLiteral]:
    return [o for o in graph.objects(concept, SKOS.prefLabel) if isinstance(o, RdfLiteral)]


def validate_model(graph: Graph, info: ModelInfo, shapes: Graph) -> ValidationReport:
    """Check ``graph`` (the model's merged graph) and report issues; ``shapes`` is its shapes."""
    issues: list[ValidationIssue] = []

    def add(severity: Severity, code: str, subject: str, message: str) -> None:
        issues.append(
            ValidationIssue(severity=severity, code=code, subject=subject, message=message)
        )

    concepts = model_classes(graph)
    concept_set = set(concepts)
    props = sorted(
        {
            s
            for t in (OWL.DatatypeProperty, OWL.ObjectProperty)
            for s in graph.subjects(RDF.type, t)
            if isinstance(s, URIRef)
        }
    )

    # duplicate_label: same prefLabel, case-folded, same language
    by_label: dict[tuple[str, str], set[URIRef]] = defaultdict(set)
    for c in concepts:
        for lit in _label(graph, c):
            by_label[(str(lit).casefold(), (lit.language or "").lower())].add(c)
    for (text, lang), members in sorted(by_label.items()):
        if len(members) > 1:
            ordered = sorted(members)
            where = f" ({lang})" if lang else ""
            add(
                "error",
                "duplicate_label",
                str(ordered[0]),
                f"label '{text}'{where} is used by {len(ordered)} concepts: "
                + ", ".join(f"'{m}'" for m in ordered),
            )

    # hierarchy: skos:broader / skos:narrower (inverse) and rdfs:subClassOf among concepts
    broader: dict[URIRef, set[URIRef]] = defaultdict(set)
    sub: dict[URIRef, set[URIRef]] = defaultdict(set)
    for s, o in graph.subject_objects(SKOS.broader):
        if s in concept_set and o in concept_set:
            broader[s].add(o)  # type: ignore[arg-type]
    for s, o in graph.subject_objects(SKOS.narrower):
        if s in concept_set and o in concept_set:
            broader[o].add(s)  # type: ignore[arg-type,index]
    for s, o in graph.subject_objects(RDFS.subClassOf):
        if s in concept_set and o in concept_set:
            sub[s].add(o)  # type: ignore[arg-type,index]
    for kind, edges in (("skos:broader", broader), ("rdfs:subClassOf", sub)):
        for comp in _cycles(edges):
            add(
                "error",
                "hierarchy_cycle",
                str(comp[0]),
                f"cycle in {kind} among: " + ", ".join(f"'{m}'" for m in comp),
            )

    # property checks
    linked: set[URIRef] = set()
    for p in props:
        is_object = (p, RDF.type, OWL.ObjectProperty) in graph
        domains = list(graph.objects(p, RDFS.domain))
        ranges = list(graph.objects(p, RDFS.range))
        for d in domains:
            if isinstance(d, URIRef) and d in concept_set:
                linked.add(d)
            else:
                add(
                    "error",
                    "dangling_domain",
                    str(p),
                    f"domain '{d}' is not a concept of the model",
                )
        for r in ranges:
            if isinstance(r, URIRef) and r in concept_set:
                linked.add(r)
            elif is_object:
                add(
                    "warning",
                    "external_range",
                    str(p),
                    f"range '{r}' is not a concept of the model",
                )
            elif r == RDFS.Literal:
                add("warning", "untyped_attribute", str(p), "range is rdfs:Literal (no datatype)")

    related: set[URIRef] = set()
    for pred in (SKOS.related,):
        for s, o in graph.subject_objects(pred):
            related.update(x for x in (s, o) if isinstance(x, URIRef))
    for c in concepts:
        connected = (
            c in linked
            or c in related
            or bool(broader.get(c))
            or bool(sub.get(c))
            or any(c in parents for parents in broader.values())
            or any(c in parents for parents in sub.values())
        )
        if not connected:
            add(
                "warning",
                "isolated_concept",
                str(c),
                "concept has no hierarchy, related or property links",
            )
        if not any(True for _ in graph.objects(c, SKOS.definition)):
            add("warning", "missing_definition", str(c), "concept has no skos:definition")

    nodes = sum(1 for _ in shapes.subjects(RDF.type, SH.NodeShape))
    properties = sum(1 for _ in shapes.subjects(RDF.type, SH.PropertyShape))
    add("info", "shapes", info.slug, f"{nodes} node shapes, {properties} property shapes")

    issues.sort(key=lambda i: (_RANK[i.severity], i.code, i.subject, i.message))
    counts = {s: sum(1 for i in issues if i.severity == s) for s in _RANK}
    counts["concepts"] = len(concepts)
    counts["properties"] = len(props)
    return ValidationReport(
        model=info.slug,
        conforms=counts["error"] == 0,
        counts=counts,
        issues=issues[:MAX_ISSUES],
        truncated=len(issues) > MAX_ISSUES,
    )


def _inline(text: str) -> str:
    """Untrusted message text as one line of inert Markdown."""
    return _MD_SPECIAL.sub(r"\\\1", single_line(text))


def validation_markdown(report: ValidationReport) -> str:
    """Heading, conformance, counts by severity, then one bullet per issue."""
    c = report.counts
    lines = [
        f"# Validation of model {code_span(report.model)}",
        "",
        f"- Conforms: {'yes' if report.conforms else 'no'}",
        f"- Errors: {c.get('error', 0)}, warnings: {c.get('warning', 0)}, info: {c.get('info', 0)}",
        "",
        "## Issues",
    ]
    if not report.issues:
        lines.append("- none")
    for i in report.issues:
        lines.append(
            f"- **{i.severity}** {code_span(i.code)} {code_span(i.subject)}: {_inline(i.message)}"
        )
    if report.truncated:
        lines += ["", f"Showing the first {len(report.issues)} issues; more were found."]
    return "\n".join(lines) + "\n"
