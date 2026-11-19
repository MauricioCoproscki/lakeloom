"""Stage deliverables: what each step of the ontology pipeline gives people right away.

A model does not need to reach the knowledge-graph stage to be useful. Each renderer here
turns the *effective graph* (OpenMetadata's terms plus the governance annotations of the
proposal log, see :mod:`lakeloom.core.annotations`) into a file people and ordinary tools
can use:

==================  ===================================  =============================
Stage               Deliverable                          Formats
==================  ===================================  =============================
1 Vocabulary        business glossary, quality score     Markdown, CSV
2 MAP               data dictionary (column → term)      Markdown, CSV
3 Taxonomy          navigation tree                      Markdown, Mermaid, CSV
4 Thesaurus         search synonyms, deprecation map     Solr/Elasticsearch, CSV
==================  ===================================  =============================

Everything is pure (no I/O) and deterministic: the same graph gives the same bytes. Text
from OpenMetadata is untrusted: in Markdown it is folded to one line and table pipes are
escaped, so a definition can never add headings, rows or links of its own.
"""

import csv
import io
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from rdflib import OWL, Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF, SKOS

from lakeloom.core.map_profile import MapProfile
from lakeloom.rdf.skos_enrichment import OM_FQN

__all__ = [
    "DELIVERABLES",
    "ConceptEntry",
    "Deliverable",
    "Expansion",
    "QualityScore",
    "data_dictionary_csv",
    "data_dictionary_markdown",
    "deliverable",
    "deprecations_csv",
    "expand_query",
    "glossary_csv",
    "glossary_markdown",
    "quality_score",
    "render",
    "synonyms_csv",
    "synonyms_solr",
    "taxonomy_csv",
    "taxonomy_markdown",
    "taxonomy_mermaid",
    "vocabulary_entries",
]

_SEP = "; "  # multi-valued CSV cells


@dataclass(frozen=True)
class Deliverable:
    """One file a stage delivers: its kind, format, file name and media type."""

    kind: str  # glossary, taxonomy, synonyms, data_dictionary
    format: str
    filename: str  # also its path under ``deliverables/`` in a release
    media_type: str
    stage: int
    needs_scan: bool = False


DELIVERABLES: tuple[Deliverable, ...] = (
    Deliverable("glossary", "markdown", "glossary.md", "text/markdown", 1),
    Deliverable("glossary", "csv", "glossary.csv", "text/csv", 1),
    Deliverable("data_dictionary", "markdown", "data-dictionary.md", "text/markdown", 2, True),
    Deliverable("data_dictionary", "csv", "data-dictionary.csv", "text/csv", 2, True),
    Deliverable("taxonomy", "markdown", "taxonomy.md", "text/markdown", 3),
    Deliverable("taxonomy", "mermaid", "taxonomy.mmd", "text/plain", 3),
    Deliverable("taxonomy", "csv", "taxonomy.csv", "text/csv", 3),
    Deliverable("synonyms", "solr", "synonyms.txt", "text/plain", 4),
    Deliverable("synonyms", "csv", "synonyms.csv", "text/csv", 4),
    Deliverable("synonyms", "deprecations", "deprecations.csv", "text/csv", 4),
)


def deliverable(kind: str, fmt: str) -> Deliverable:
    """The deliverable ``kind`` in format ``fmt`` (``KeyError`` when there is none)."""
    for d in DELIVERABLES:
        if d.kind == kind and d.format == fmt:
            return d
    raise KeyError((kind, fmt))


def render(
    d: Deliverable,
    entries: "list[ConceptEntry]",
    model: str,
    profile: MapProfile | None = None,
) -> str:
    """The text of deliverable ``d``; a data dictionary needs the MAP ``profile``."""
    match (d.kind, d.format):
        case ("glossary", "markdown"):
            return glossary_markdown(entries, model)
        case ("glossary", "csv"):
            return glossary_csv(entries)
        case ("taxonomy", "markdown"):
            return taxonomy_markdown(entries, model)
        case ("taxonomy", "mermaid"):
            return taxonomy_mermaid(entries)
        case ("taxonomy", "csv"):
            return taxonomy_csv(entries)
        case ("synonyms", "solr"):
            return synonyms_solr(entries)
        case ("synonyms", "csv"):
            return synonyms_csv(entries)
        case ("synonyms", "deprecations"):
            return deprecations_csv(entries)
        case ("data_dictionary", fmt):
            if profile is None:
                raise ValueError("a data dictionary needs a MAP profile")
            if fmt == "csv":
                return data_dictionary_csv(profile, entries)
            return data_dictionary_markdown(profile, entries)
        case _:
            raise KeyError((d.kind, d.format))


@dataclass(frozen=True)
class ConceptEntry:
    """One concept as a deliverable sees it."""

    iri: str
    fqn: str
    label: str
    definition: str | None = None
    synonyms: tuple[str, ...] = ()
    scope_notes: tuple[str, ...] = ()
    history_notes: tuple[str, ...] = ()
    deprecated: bool = False
    replaced_by: str | None = None  # FQN of the successor
    broader: tuple[str, ...] = ()  # FQNs
    related: tuple[str, ...] = ()  # FQNs


@dataclass(frozen=True)
class QualityScore:
    """How usable the vocabulary is as a glossary today."""

    total: int
    defined: int
    with_synonyms: int
    with_scope_note: int
    deprecated: int
    undefined: tuple[str, ...] = field(default=())  # labels, first 20

    @property
    def defined_ratio(self) -> float:
        return self.defined / self.total if self.total else 0.0


# --- reading the graph ----------------------------------------------------------------


def _text(values: Iterable[object]) -> list[str]:
    """Literal values, English or untagged first, then the rest; each once."""
    lits = [v for v in values if isinstance(v, Literal)]
    lits.sort(key=lambda v: (v.language not in (None, "en"), v.language or "", str(v)))
    out: list[str] = []
    for v in lits:
        if str(v) not in out:
            out.append(str(v))
    return out


def vocabulary_entries(graph: Graph) -> list[ConceptEntry]:
    """Every ``skos:Concept`` of ``graph``, sorted by FQN."""
    nodes = [n for n in graph.subjects(RDF.type, SKOS.Concept) if isinstance(n, URIRef)]
    fqn = {n: str(graph.value(n, OM_FQN) or n) for n in nodes}
    concepts = set(nodes)

    def links(node: URIRef, *preds: URIRef) -> tuple[str, ...]:
        found = {o for p in preds for o in graph.objects(node, p) if o in concepts}
        return tuple(sorted(fqn[o] for o in found if isinstance(o, URIRef)))

    narrower_of: dict[URIRef, set[URIRef]] = {}
    for child, parent in graph.subject_objects(SKOS.narrower):
        if isinstance(child, URIRef) and isinstance(parent, URIRef):
            narrower_of.setdefault(parent, set()).add(child)

    entries: list[ConceptEntry] = []
    for n in nodes:
        labels = _text(graph.objects(n, SKOS.prefLabel))
        definition = _text(graph.objects(n, SKOS.definition))
        successor = graph.value(n, DCTERMS.isReplacedBy)
        broader = set(links(n, SKOS.broader))
        broader |= {fqn[p] for p, kids in narrower_of.items() if n in kids and p in concepts}
        entries.append(
            ConceptEntry(
                iri=str(n),
                fqn=fqn[n],
                label=labels[0] if labels else fqn[n].rsplit(".", 1)[-1],
                definition=definition[0] if definition else None,
                synonyms=tuple(_text(graph.objects(n, SKOS.altLabel))),
                scope_notes=tuple(_text(graph.objects(n, SKOS.scopeNote))),
                history_notes=tuple(_text(graph.objects(n, SKOS.historyNote))),
                deprecated=any(
                    isinstance(o, Literal) and o.toPython() is True
                    for o in graph.objects(n, OWL.deprecated)
                ),
                replaced_by=fqn.get(successor) if isinstance(successor, URIRef) else None,
                broader=tuple(sorted(broader)),
                related=links(n, SKOS.related),
            )
        )
    return sorted(entries, key=lambda e: (e.fqn.casefold(), e.fqn))


# --- shared helpers -------------------------------------------------------------------


def _cell(text: str | None) -> str:
    """Untrusted text as one inert Markdown table cell."""
    if not text:
        return ""
    flat = " ".join(text.split())
    return flat.replace("\\", "\\\\").replace("|", "\\|")


def _csv(header: list[str], rows: Iterable[list[str]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


def _status(e: ConceptEntry) -> str:
    return "deprecated" if e.deprecated else "current"


# --- stage 1: glossary and quality ----------------------------------------------------


def glossary_markdown(entries: list[ConceptEntry], model: str) -> str:
    """A business glossary: one row per current term, deprecated terms listed apart."""
    current = [e for e in entries if not e.deprecated]
    old = [e for e in entries if e.deprecated]
    lines = [
        f"# Business glossary: {_cell(model)}",
        "",
        f"{len(current)} terms" + (f", {len(old)} deprecated" if old else "") + ".",
        "",
        "| Term | Definition | Also called | Use it for |",
        "|------|------------|-------------|------------|",
    ]
    for e in current:
        lines.append(
            f"| **{_cell(e.label)}** | {_cell(e.definition) or '_no definition yet_'} "
            f"| {_cell(', '.join(e.synonyms))} | {_cell(' '.join(e.scope_notes))} |"
        )
    if old:
        lines += ["", "## Deprecated terms", "", "| Term | Use instead |", "|------|------|"]
        by_fqn = {e.fqn: e for e in entries}
        for e in old:
            succ = by_fqn.get(e.replaced_by or "")
            lines.append(f"| {_cell(e.label)} | {_cell(succ.label) if succ else '—'} |")
    return "\n".join(lines) + "\n"


def glossary_csv(entries: list[ConceptEntry]) -> str:
    header = [
        "fqn",
        "term",
        "definition",
        "synonyms",
        "scope_note",
        "history_note",
        "broader",
        "related",
        "status",
        "replaced_by",
        "iri",
    ]
    return _csv(
        header,
        (
            [
                e.fqn,
                e.label,
                e.definition or "",
                _SEP.join(e.synonyms),
                _SEP.join(e.scope_notes),
                _SEP.join(e.history_notes),
                _SEP.join(e.broader),
                _SEP.join(e.related),
                _status(e),
                e.replaced_by or "",
                e.iri,
            ]
            for e in entries
        ),
    )


def quality_score(entries: list[ConceptEntry]) -> QualityScore:
    current = [e for e in entries if not e.deprecated]
    undefined = [e.label for e in current if not (e.definition or "").strip()]
    return QualityScore(
        total=len(current),
        defined=len(current) - len(undefined),
        with_synonyms=sum(1 for e in current if e.synonyms),
        with_scope_note=sum(1 for e in current if e.scope_notes),
        deprecated=len(entries) - len(current),
        undefined=tuple(undefined[:20]),
    )


# --- stage 2: data dictionary ---------------------------------------------------------


def _dictionary_rows(profile: MapProfile, entries: list[ConceptEntry]) -> list[list[str]]:
    by_iri = {e.iri: e for e in entries}
    by_fqn = {e.fqn: e for e in entries}
    rows: list[list[str]] = []
    for table in sorted(profile.table_entries, key=lambda t: t.om_fqn):
        tconcept = by_iri.get(table.table_concept or "") or by_fqn.get(table.table_concept or "")
        for col in table.columns:
            c = by_iri.get(col.concept_fqn or "") or by_fqn.get(col.concept_fqn or "")
            rows.append(
                [
                    table.om_fqn,
                    tconcept.label if tconcept else "",
                    col.name,
                    col.data_type,
                    c.label if c else "",
                    c.fqn if c else "",
                    (c.definition or "") if c else "",
                    _status(c) if c else "unmapped",
                ]
            )
    return rows


def data_dictionary_csv(profile: MapProfile, entries: list[ConceptEntry]) -> str:
    header = [
        "table",
        "table_concept",
        "column",
        "data_type",
        "term",
        "term_fqn",
        "definition",
        "status",
    ]
    return _csv(header, _dictionary_rows(profile, entries))


def data_dictionary_markdown(profile: MapProfile, entries: list[ConceptEntry]) -> str:
    rows = _dictionary_rows(profile, entries)
    mapped = sum(1 for r in rows if r[7] != "unmapped")
    lines = [
        f"# Data dictionary: {_cell(profile.model_slug)}",
        "",
        f"Scan `{_cell(profile.scan_id)}` of source `{_cell(profile.source_id)}`: "
        f"{mapped} of {len(rows)} columns carry a business term.",
    ]
    table = None
    for r in rows:
        if r[0] != table:
            table = r[0]
            concept = f" — {_cell(r[1])}" if r[1] else ""
            lines += [
                "",
                f"## `{_cell(table)}`{concept}",
                "",
                "| Column | Type | Term | Definition |",
                "|--------|------|------|------------|",
            ]
        term = _cell(r[4]) or "_unmapped_"
        if r[7] == "deprecated":
            term += " (deprecated)"
        lines.append(f"| `{_cell(r[2])}` | {_cell(r[3])} | {term} | {_cell(r[6])} |")
    return "\n".join(lines) + "\n"


# --- stage 3: taxonomy ----------------------------------------------------------------


def _tree(entries: list[ConceptEntry]) -> tuple[list[ConceptEntry], dict[str, list[ConceptEntry]]]:
    """Roots and children (by parent FQN) of the current concepts. A concept with several
    parents (polyhierarchy) appears under each one."""
    current = [e for e in entries if not e.deprecated]
    known = {e.fqn for e in current}
    children: dict[str, list[ConceptEntry]] = {}
    roots: list[ConceptEntry] = []
    for e in current:
        parents = [p for p in e.broader if p in known]
        if not parents:
            roots.append(e)
        for p in parents:
            children.setdefault(p, []).append(e)
    for kids in children.values():
        kids.sort(key=lambda e: e.label.casefold())
    roots.sort(key=lambda e: e.label.casefold())
    return roots, children


def _walk(
    entries: list[ConceptEntry],
) -> Iterable[tuple[int, ConceptEntry, ConceptEntry | None]]:
    """(depth, concept, parent) in display order; a cycle is cut at its second visit."""
    roots, children = _tree(entries)

    def visit(
        e: ConceptEntry, depth: int, parent: ConceptEntry | None, path: frozenset[str]
    ) -> Iterator[tuple[int, ConceptEntry, ConceptEntry | None]]:
        yield depth, e, parent
        for kid in children.get(e.fqn, []):
            if kid.fqn not in path:
                yield from visit(kid, depth + 1, e, path | {kid.fqn})

    for r in roots:
        yield from visit(r, 0, None, frozenset({r.fqn}))


def taxonomy_markdown(entries: list[ConceptEntry], model: str) -> str:
    lines = [f"# Taxonomy: {_cell(model)}", ""]
    for depth, e, _ in _walk(entries):
        lines.append(
            f"{'  ' * depth}- **{_cell(e.label)}**"
            + (f" — {_cell(e.definition)}" if e.definition else "")
        )
    return "\n".join(lines) + "\n"


def taxonomy_mermaid(entries: list[ConceptEntry]) -> str:
    """A Mermaid ``flowchart`` (renders on GitHub and in most Markdown viewers)."""
    ids: dict[str, str] = {}

    def node(e: ConceptEntry) -> str:
        if e.fqn not in ids:
            ids[e.fqn] = f"c{len(ids)}"
        label = _cell(e.label).replace('"', "'")
        return f'{ids[e.fqn]}["{label}"]'

    lines = ["flowchart TD"]
    for _, e, parent in _walk(entries):
        lines.append(f"  {node(parent)} --> {node(e)}" if parent else f"  {node(e)}")
    return "\n".join(lines) + "\n"


def taxonomy_csv(entries: list[ConceptEntry]) -> str:
    return _csv(
        ["depth", "fqn", "term", "parent_fqn"],
        ([str(d), e.fqn, e.label, p.fqn if p else ""] for d, e, p in _walk(entries)),
    )


# --- stage 4: synonyms and deprecations -----------------------------------------------


def _solr_term(text: str) -> str:
    """One synonym-file term: commas and ``=>`` would change the rule, so they go."""
    return " ".join(text.replace(",", " ").replace("=>", " ").split())


def synonyms_solr(entries: list[ConceptEntry]) -> str:
    """Solr / Elasticsearch / OpenSearch synonym rules.

    A current term and its synonyms are equivalent (``a, b, c``). A deprecated term maps to
    its successor (``old => new``), so searches for the old name find the new term.
    """
    by_fqn = {e.fqn: e for e in entries}
    lines = ["# Generated by Lakeloom from the effective model; edit the model, not this file."]
    for e in entries:
        if e.deprecated:
            succ = by_fqn.get(e.replaced_by or "")
            if succ is not None:
                lines.append(f"{_solr_term(e.label)} => {_solr_term(succ.label)}")
            continue
        terms = [_solr_term(t) for t in (e.label, *e.synonyms)]
        terms = list(dict.fromkeys(t for t in terms if t))
        if len(terms) > 1:
            lines.append(", ".join(terms))
    return "\n".join(lines) + "\n"


def synonyms_csv(entries: list[ConceptEntry]) -> str:
    return _csv(
        ["term", "fqn", "synonym"],
        ([e.label, e.fqn, s] for e in entries if not e.deprecated for s in e.synonyms),
    )


def deprecations_csv(entries: list[ConceptEntry]) -> str:
    by_fqn = {e.fqn: e for e in entries}
    rows: list[list[str]] = []
    for e in entries:
        if e.deprecated:
            succ = by_fqn.get(e.replaced_by or "")
            rows.append(
                [
                    e.label,
                    e.fqn,
                    succ.label if succ else "",
                    e.replaced_by or "",
                    _SEP.join(e.history_notes),
                ]
            )
    return _csv(["deprecated_term", "fqn", "use_instead", "use_instead_fqn", "history"], rows)


# --- stage 4: query expansion ---------------------------------------------------------


@dataclass(frozen=True)
class Expansion:
    """One concept recognised in a query, with the terms a search should add."""

    matched: str  # the text of the query that matched
    fqn: str
    label: str
    synonyms: tuple[str, ...] = ()
    narrower: tuple[str, ...] = ()  # labels, up to ``depth`` levels down
    related: tuple[str, ...] = ()
    broader: tuple[str, ...] = ()
    redirected_from: str | None = None  # a deprecated term the query used


def _fold(text: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text.casefold()).split())


def _decamel(text: str) -> str:
    """``CardPayment`` → ``Card Payment``, ``order_date`` stays for :func:`_fold`."""
    out: list[str] = []
    for i, ch in enumerate(text):
        if i and ch.isupper() and (text[i - 1].islower() or text[i - 1].isdigit()):
            out.append(" ")
        out.append(ch)
    return "".join(out)


def expand_query(
    entries: list[ConceptEntry],
    text: str,
    *,
    depth: int = 1,
    related: bool = True,
) -> list[Expansion]:
    """The concepts ``text`` mentions (by label or synonym, longest match first) and, for
    each, its synonyms, narrower terms (``depth`` levels), related and broader terms. A
    deprecated term is replaced by its successor and reported as ``redirected_from``."""
    by_fqn = {e.fqn: e for e in entries}
    names: dict[str, ConceptEntry] = {}
    for e in entries:
        for name in (e.label, *e.synonyms):
            for key in {_fold(name), _fold(_decamel(name))}:
                if key and (key not in names or names[key].deprecated):
                    names[key] = e
    children: dict[str, list[ConceptEntry]] = {}
    for e in entries:
        if not e.deprecated:
            for p in e.broader:
                children.setdefault(p, []).append(e)

    words = _fold(text).split()
    longest = max((len(k.split()) for k in names), default=0)
    found: list[Expansion] = []
    seen: set[str] = set()
    i = 0
    while i < len(words):
        for n in range(min(longest, len(words) - i), 0, -1):
            phrase = " ".join(words[i : i + n])
            e = names.get(phrase)
            if e is None:
                continue
            origin: str | None = None
            if e.deprecated:
                successor = by_fqn.get(e.replaced_by or "")
                if successor is None:
                    break  # deprecated with no successor: nothing safe to expand
                origin, e = e.label, successor
            if e.fqn not in seen:
                seen.add(e.fqn)
                found.append(_expansion(phrase, e, origin, by_fqn, children, depth, related))
            i += n - 1
            break
        i += 1
    return found


def _expansion(
    phrase: str,
    e: ConceptEntry,
    origin: str | None,
    by_fqn: dict[str, ConceptEntry],
    children: dict[str, list[ConceptEntry]],
    depth: int,
    related: bool,
) -> Expansion:
    narrower: list[str] = []
    frontier, seen = [e.fqn], {e.fqn}
    for _ in range(max(depth, 0)):
        nxt: list[str] = []
        for f in frontier:
            for kid in sorted(children.get(f, []), key=lambda k: k.label.casefold()):
                if kid.fqn not in seen:
                    seen.add(kid.fqn)
                    narrower.append(kid.label)
                    nxt.append(kid.fqn)
        frontier = nxt

    def labels(fqns: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(by_fqn[f].label for f in fqns if f in by_fqn and not by_fqn[f].deprecated)

    return Expansion(
        matched=phrase,
        fqn=e.fqn,
        label=e.label,
        synonyms=e.synonyms,
        narrower=tuple(narrower),
        related=labels(e.related) if related else (),
        broader=labels(e.broader),
        redirected_from=origin,
    )
