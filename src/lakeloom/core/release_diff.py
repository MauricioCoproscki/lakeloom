"""Semantic diff of two ontology graphs and the semver bump rules (FR-GIT-06).

Pure functions: no I/O. A *term* is a URIRef subject typed ``owl:Class``, ``skos:Concept``,
``owl:DatatypeProperty`` or ``owl:ObjectProperty`` outside the vocabulary namespaces
(``publish.shacl_gen.VOCABULARY_NAMESPACES``). Blank-node restrictions are compared up to
isomorphism and attributed to the named term that owns them.
"""

import re
from typing import Literal

from pydantic import BaseModel
from rdflib import BNode, Graph, URIRef
from rdflib.compare import graph_diff, to_isomorphic
from rdflib.namespace import DCTERMS, OWL, RDF, RDFS, SKOS
from rdflib.term import Node

from lakeloom.errors import LakeloomError
from lakeloom.publish.shacl_gen import is_vocabulary
from lakeloom.untrusted import code_span

__all__ = [
    "Bump",
    "ReleaseDiff",
    "TermChange",
    "Version",
    "diff_graphs",
    "diff_markdown",
    "format_version",
    "next_version",
    "parse_version",
]

Version = tuple[int, int, int]
Bump = Literal["major", "minor", "patch"]

_VERSION = re.compile(r"([0-9]|[1-9][0-9]*)\.([0-9]|[1-9][0-9]*)\.([0-9]|[1-9][0-9]*)")
_MAX_VERSION_CHARS = 40
_MAX_LISTED = 50

TERM_TYPES = frozenset({OWL.Class, SKOS.Concept, OWL.DatatypeProperty, OWL.ObjectProperty})
ANNOTATION_PREDICATES = frozenset(
    {
        SKOS.prefLabel,
        SKOS.altLabel,
        SKOS.hiddenLabel,
        SKOS.definition,
        SKOS.scopeNote,
        SKOS.note,
        SKOS.example,
        RDFS.label,
        RDFS.comment,
        DCTERMS.description,
    }
)


def parse_version(text: str) -> Version:
    """Strict ``MAJOR.MINOR.PATCH``; anything else is a ``validation_error``."""
    m = _VERSION.fullmatch(text) if len(text) <= _MAX_VERSION_CHARS else None
    if m is None:
        raise LakeloomError(
            "validation_error",
            "version must be MAJOR.MINOR.PATCH (for example 1.2.0)",
            "use digits only, no leading zeros, no 'v' prefix and no pre-release suffix",
        )
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def format_version(v: Version) -> str:
    return f"{v[0]}.{v[1]}.{v[2]}"


class TermChange(BaseModel):
    iri: str
    label: str
    change: Literal["added", "changed", "removed"]


class ReleaseDiff(BaseModel):
    from_version: str | None
    to_version: str
    added: list[TermChange]
    changed: list[TermChange]
    removed: list[TermChange]
    removed_triples: int
    added_triples: int
    annotation_only: bool
    bump: Bump | None
    reason: str


def next_version(previous: Version | None, bump: Bump | None) -> Version:
    """The version after ``previous``; the first release is ``1.0.0``."""
    if previous is None:
        return (1, 0, 0)
    major, minor, patch = previous
    if bump == "major":
        return (major + 1, 0, 0)
    if bump == "minor":
        return (major, minor + 1, 0)
    if bump == "patch":
        return (major, minor, patch + 1)
    raise LakeloomError(
        "validation_error",
        f"nothing changed since {format_version(previous)}",
        "approve a proposal first",
    )


def _terms(g: Graph) -> set[URIRef]:
    """Typed model terms; vocabulary IRIs (``skos:Concept``, ``om:Glossary``...) are not."""
    return {
        s
        for t in TERM_TYPES
        for s in g.subjects(RDF.type, t)
        if isinstance(s, URIRef) and not is_vocabulary(s)
    }


def _local_name(iri: str) -> str:
    return re.split(r"[#/]", iri.rstrip("#/"))[-1] or iri


def _label(g: Graph, iri: URIRef) -> str:
    for pred in (SKOS.prefLabel, RDFS.label):
        values = sorted(str(o) for o in g.objects(iri, pred))
        if values:
            return values[0]
    return _local_name(str(iri))


def _owner(g: Graph, node: Node, seen: frozenset[Node] = frozenset()) -> URIRef | None:
    """The named subject that reaches blank node ``node`` (the term owning a restriction)."""
    if isinstance(node, URIRef):
        return node
    if not isinstance(node, BNode) or node in seen:
        return None
    for s in sorted(set(g.subjects(None, node)), key=str):
        found = _owner(g, s, seen | {node})
        if found is not None:
            return found
    return None


def _changes(g: Graph, iris: list[URIRef], change: Literal["added", "changed", "removed"]):
    return [TermChange(iri=str(i), label=_label(g, i), change=change) for i in sorted(iris)]


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def diff_graphs(
    old: Graph | None, new: Graph, *, from_version: str | None, to_version: str
) -> ReleaseDiff:
    """Compare ``old`` with ``new``; ``old=None`` is a first release (everything added)."""
    if old is None:
        return ReleaseDiff(
            from_version=from_version,
            to_version=to_version,
            added=_changes(new, list(_terms(new)), "added"),
            changed=[],
            removed=[],
            removed_triples=0,
            added_triples=len(new),
            annotation_only=False,
            bump=None,
            reason="initial release",
        )
    both, in_old, in_new = graph_diff(to_isomorphic(old), to_isomorphic(new))
    old_full, new_full = both + in_old, both + in_new
    old_terms, new_terms = _terms(old_full), _terms(new_full)
    removed = sorted(old_terms - new_terms)
    added = sorted(new_terms - old_terms)
    touched: set[URIRef] = set()
    for graph, full in ((in_old, old_full), (in_new, new_full)):
        for s, _p, _o in graph:
            owner = _owner(full, s)
            if owner is not None and not is_vocabulary(owner):
                touched.add(owner)
    changed = sorted(touched & old_terms & new_terms)
    structural_removed = any(p not in ANNOTATION_PREDICATES for _s, p, _o in in_old)
    structural_added = any(p not in ANNOTATION_PREDICATES for _s, p, _o in in_new)
    bump: Bump | None
    if not in_old and not in_new:
        bump, reason = None, "nothing changed"
    elif removed or structural_removed:
        bump = "major"
        reason = (
            _plural(len(removed), "term") + " removed"
            if removed
            else "a non-annotation triple was removed"
        )
    elif added or structural_added:
        bump = "minor"
        reason = (
            _plural(len(added), "term") + " added" if added else "a non-annotation triple was added"
        )
    else:
        bump, reason = "patch", "only annotations changed"
    return ReleaseDiff(
        from_version=from_version,
        to_version=to_version,
        added=_changes(new_full, list(added), "added"),
        changed=_changes(new_full, list(changed), "changed"),
        removed=_changes(old_full, list(removed), "removed"),
        removed_triples=len(in_old),
        added_triples=len(in_new),
        annotation_only=bump == "patch",
        bump=bump,
        reason=reason,
    )


def _group(title: str, items: list[TermChange]) -> list[str]:
    if not items:
        return []
    lines = ["", f"### {title}"]
    lines += [f"- {code_span(t.label)} — {code_span(t.iri)}" for t in items[:_MAX_LISTED]]
    if len(items) > _MAX_LISTED:
        lines.append(f"- … and {len(items) - _MAX_LISTED} more")
    return lines


def diff_markdown(d: ReleaseDiff) -> str:
    """A Markdown summary; labels and IRIs come from the model and are quoted as code spans."""
    head = (
        f"## {d.to_version}" if d.from_version is None else f"## {d.from_version} → {d.to_version}"
    )
    bump = d.bump or "none"
    lines = [
        head,
        "",
        f"Bump: {bump} ({d.reason}). Triples: +{d.added_triples} / -{d.removed_triples}.",
    ]
    lines += _group("Added", d.added)
    lines += _group("Changed", d.changed)
    lines += _group("Removed", d.removed)
    return "\n".join(lines) + "\n"
