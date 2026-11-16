"""Drift between the lake now and the scans a model was built from (FR-PRO-07), pure part.

:func:`diff_scans` compares the baseline tables (as recorded in the newest applied scan that
holds each) with a fresh scan; :func:`drift_operations` turns the drift into proposal
operations on the concept and attribute names the model uses (:func:`bind_tables`).

========================  ==================================================================
added / type-changed      the mapper's ``AddAttribute`` for the column (fresh scan), concept
column                    rewritten to the bound one; for a type change the name is the bound
                          attribute's and the model's ``unit`` / ``is_identifier`` /
                          ``allowed_values`` are kept when the mapper has none (the upsert
                          replaces every field in OpenMetadata, ADR 0016 R2). FK relations
                          whose columns are new are kept the same way.
comment change            reported only (:func:`comment_changes`): rc1 ignores description
                          changes on an existing native property (ADR 0016 R2)
removed column            ``RemoveAttribute`` on the bound name, only when the model has it
added table               the mapper's operations (the caller runs them through ``dedupe``)
removed table             its relations (either end), axiom, attributes, then
                          ``RemoveConcept``, only for terms the model has
renames                   remove plus add, both with ``possible rename of <old> to <new>``
                          evidence; a column candidate has the same raw type and position
                          (index in the column list), a table candidate the same
                          ``(name, type)`` column list, both matched 1:1
========================  ==================================================================

Rule ids: the mapper's for additions, ``R-DRIFT-REMOVE`` and ``R-DRIFT-RENAME`` otherwise.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from lakeloom.core.import_terms import TermSet
from lakeloom.core.mapper import map_scan
from lakeloom.core.model_policy import MappingPolicy
from lakeloom.core.operations import (
    AddAttribute,
    AddConcept,
    AddRelation,
    Evidence,
    ProposalOperation,
    RemoveAttribute,
    RemoveAxiom,
    RemoveConcept,
    RemoveRelation,
)
from lakeloom.sources.models import ColumnInfo
from lakeloom.state.scans import ScannedTable, ScanRecord

__all__ = [
    "REMOVE_RULE",
    "RENAME_RULE",
    "TableBinding",
    "TableDrift",
    "bind_ops",
    "bind_tables",
    "comment_changes",
    "diff_scans",
    "drift_operations",
    "table_of",
]

REMOVE_RULE = "R-DRIFT-REMOVE"
RENAME_RULE = "R-DRIFT-RENAME"
_OM = "OpenMetadata 2.1.0-rc1"
_MAX_LISTED = 10


@dataclass(frozen=True)
class TableBinding:
    """The concept a table is bound to in the model, and its columns' attribute names."""

    concept: str
    attributes: dict[str, str] = field(default_factory=dict[str, str])


class TableDrift(BaseModel):
    fqn: str
    status: Literal["unchanged", "changed", "added", "removed", "error"]
    added_columns: list[str] = []
    removed_columns: list[str] = []
    changed_columns: list[str] = []
    renames: list[tuple[str, str]] = []
    error: str | None = None


def _bare(ref: str) -> str:
    return ref.rsplit(".", 1)[-1]


def _first(op: ProposalOperation, kind: str) -> str | None:
    return next((e.ref for e in op.evidence if e.kind == kind), None)


def bind_ops(ops: Iterable[ProposalOperation]) -> dict[str, TableBinding]:
    """Bindings from operations in order (later ones win): a table's concept is the concept
    of its ``AddAttribute`` operations (evidence ``column`` ``<table>.<col>``), else of its
    ``AddConcept`` (evidence ``table``); a column's attribute is that ``AddAttribute``'s name.
    A ``RemoveAttribute`` with column evidence unbinds the column, a ``RemoveConcept`` with
    table evidence the table."""
    by_attr: dict[str, str] = {}
    by_concept: dict[str, str] = {}
    attrs: dict[str, dict[str, str]] = defaultdict(dict)
    for op in ops:
        match op:
            case AddAttribute():
                ref = _first(op, "column")
                if ref is not None and "." in ref:
                    tbl, col = ref.rsplit(".", 1)
                    by_attr[tbl] = _bare(op.concept)
                    attrs[tbl][col] = op.name
            case RemoveAttribute():
                ref = _first(op, "column")
                if ref is not None and "." in ref:
                    tbl, col = ref.rsplit(".", 1)
                    attrs[tbl].pop(col, None)
            case AddConcept():
                ref = _first(op, "table")
                if ref is not None:
                    by_concept[ref] = op.name
            case RemoveConcept():
                ref = _first(op, "table")
                if ref is not None:
                    by_attr.pop(ref, None)
                    by_concept.pop(ref, None)
                    attrs.pop(ref, None)
            case _:
                pass
    out: dict[str, TableBinding] = {}
    for tbl in sorted(set(by_attr) | set(by_concept)):
        concept = by_attr.get(tbl) or by_concept[tbl]
        out[tbl] = TableBinding(concept=concept, attributes=dict(attrs.get(tbl, {})))
    return out


def bind_tables(proposals: Sequence[Any]) -> dict[str, TableBinding]:
    """The bindings of the model's applied proposals, oldest first (ids are ULIDs), so a
    newer proposal's names win. ``proposals`` are :class:`~lakeloom.core.proposals.Proposal`."""
    ordered = sorted(proposals, key=lambda p: str(p.id))
    return bind_ops(op for p in ordered for op in p.operations())


def _columns(t: ScannedTable) -> list[ColumnInfo]:
    return list(t.description.columns) if t.description is not None else []


def _comment(c: ColumnInfo) -> str | None:
    return c.comment.text if c.comment is not None else None


def _column_renames(
    fqn: str, old: list[ColumnInfo], new: list[ColumnInfo], removed: list[str], added: list[str]
) -> list[tuple[str, str]]:
    """Removed/added pairs with the same raw type and index, matched 1:1."""
    old_at = {c.name: (i, c.data_type) for i, c in enumerate(old)}
    new_at = {c.name: (i, c.data_type) for i, c in enumerate(new)}
    cands = {r: [a for a in added if new_at[a] == old_at[r]] for r in removed}
    back = {a: [r for r in removed if new_at[a] == old_at[r]] for a in added}
    return [
        (f"{fqn}.{r}", f"{fqn}.{a[0]}")
        for r, a in cands.items()
        if len(a) == 1 and len(back[a[0]]) == 1
    ]


def _table_drift(fqn: str, old: ScannedTable, new: ScannedTable) -> TableDrift:
    before, after = _columns(old), _columns(new)
    b = {c.name: c for c in before}
    a = {c.name: c for c in after}
    added = [c.name for c in after if c.name not in b]
    removed = [c.name for c in before if c.name not in a]
    changed = [
        c.name
        for c in after
        if c.name in b
        and (c.data_type != b[c.name].data_type or _comment(c) != _comment(b[c.name]))
    ]
    status: Literal["unchanged", "changed"] = (
        "changed" if added or removed or changed else "unchanged"
    )
    return TableDrift(
        fqn=fqn,
        status=status,
        added_columns=added,
        removed_columns=removed,
        changed_columns=changed,
        renames=_column_renames(fqn, before, after, removed, added),
    )


def _signature(t: ScannedTable) -> tuple[tuple[str, str], ...]:
    return tuple((c.name, c.data_type) for c in _columns(t))


def diff_scans(
    baseline: Mapping[str, ScannedTable],
    current: Mapping[str, ScannedTable],
    errors: Mapping[str, str],
) -> list[TableDrift]:
    """The drift of every table, sorted by full name. A table in ``errors`` is ``error`` and
    never ``removed``; a table rename candidate is a ``renames`` entry on the added table."""
    out: list[TableDrift] = []
    for fqn in sorted(set(baseline) | set(current) | set(errors)):
        old, new = baseline.get(fqn), current.get(fqn)
        if fqn in errors:
            out.append(TableDrift(fqn=fqn, status="error", error=errors[fqn]))
        elif old is not None and new is not None:
            out.append(_table_drift(fqn, old, new))
        elif old is not None:
            out.append(TableDrift(fqn=fqn, status="removed"))
        else:
            out.append(TableDrift(fqn=fqn, status="added"))
    gone = [d.fqn for d in out if d.status == "removed"]
    new_ones = [d for d in out if d.status == "added"]
    for d in new_ones:
        sig = _signature(current[d.fqn])
        cands = [g for g in gone if _signature(baseline[g]) == sig]
        if len(cands) != 1:
            continue
        back = [n for n in new_ones if _signature(current[n.fqn]) == sig]
        if len(back) == 1:
            d.renames = [(cands[0], d.fqn)]
    return out


def comment_changes(
    baseline: Mapping[str, ScannedTable], current: Mapping[str, ScannedTable]
) -> list[str]:
    """The warning for changed column comments (reported, never proposed: ADR 0016 R2)."""
    changed: list[str] = []
    for fqn in sorted(set(baseline) & set(current)):
        before = {c.name: c for c in _columns(baseline[fqn])}
        for c in _columns(current[fqn]):
            if c.name in before and _comment(c) != _comment(before[c.name]):
                changed.append(f"{fqn}.{c.name}")
    if not changed:
        return []
    more = f" and {len(changed) - _MAX_LISTED} more" if len(changed) > _MAX_LISTED else ""
    return [
        f"{len(changed)} column comments changed; attribute descriptions cannot be changed on "
        f"{_OM}: {', '.join(changed[:_MAX_LISTED])}{more}"
    ]


def table_of(op: ProposalOperation, tables: Iterable[str]) -> str | None:
    """The table an operation belongs to, from its evidence (the longest name first)."""
    by_length = sorted(tables, key=len, reverse=True)
    for evidence in op.evidence:
        for name in by_length:
            if evidence.ref == name or evidence.ref.startswith(name + "."):
                return name
    return None


def _rename_detail(old: str, new: str) -> str:
    return f"possible rename of {old} to {new}"


def _with(op: ProposalOperation, **update: Any) -> Any:
    """A validated copy of ``op`` with ``update`` applied."""
    data = op.model_dump()
    data.update(update)
    return type(op).model_validate(data)


def _plus_evidence(op: ProposalOperation, evidence: Evidence, **update: Any) -> Any:
    return _with(
        op, evidence=[*(e.model_dump() for e in op.evidence), evidence.model_dump()], **update
    )


@dataclass
class _Mapped:
    """The mapper's view of the fresh scan, indexed by table and column."""

    by_table: dict[str, list[ProposalOperation]]
    concept_of: dict[str, str]  # table -> the mapper's concept name
    table_of_concept: dict[str, str]  # the mapper's concept name -> table
    attribute: dict[str, AddAttribute]  # "<table>.<col>" -> the mapper's AddAttribute


def _mapped(scan: ScanRecord, policy: MappingPolicy, warnings: list[str]) -> _Mapped:
    result = map_scan(scan, policy)
    warnings.extend(result.warnings)
    names = [t.ref.full_name for t in scan.tables]
    out = _Mapped(by_table={}, concept_of={}, table_of_concept={}, attribute={})
    for op in result.operations:
        tbl = table_of(op, names)
        if tbl is None:
            continue
        out.by_table.setdefault(tbl, []).append(op)
        if isinstance(op, AddConcept) and op.parent is None and _first(op, "table") == tbl:
            out.concept_of[tbl] = op.name
            out.table_of_concept[op.name] = tbl
        elif isinstance(op, AddAttribute):
            ref = _first(op, "column")
            if ref is not None:
                out.attribute[ref] = op
    return out


class _Builder:
    def __init__(
        self,
        drifts: Sequence[TableDrift],
        scan: ScanRecord,
        bindings: Mapping[str, TableBinding],
        terms: TermSet,
        policy: MappingPolicy,
        versions: Mapping[str, float],
    ) -> None:
        self.drifts = drifts
        self.bindings = bindings
        self.terms = terms
        self.versions = versions
        self.warnings: list[str] = []
        self.mapper_warnings: list[str] = []
        self.m = _mapped(scan, policy, self.mapper_warnings)
        self.rewritten: list[ProposalOperation] = []
        self.additions: list[ProposalOperation] = []
        # removed table -> added table (a table rename candidate)
        self.table_renames = {
            old: new for d in drifts for old, new in d.renames if d.status == "added"
        }

    def concept(self, tbl: str) -> str | None:
        bound = self.bindings.get(tbl)
        return bound.concept if bound is not None else self.m.concept_of.get(tbl)

    def run(self) -> None:
        for d in self.drifts:
            match d.status:
                case "changed":
                    self.changed(d)
                case "removed":
                    self.removed(d)
                case "added":
                    self.added(d)
                case _:
                    pass

    # -- changed tables ----------------------------------------------------------------------

    def changed(self, d: TableDrift) -> None:
        concept = self.concept(d.fqn)
        if concept is None:
            self.warnings.append(f"table {d.fqn} has no concept in the model; drift not proposed")
            return
        bound = self.bindings.get(d.fqn, TableBinding(concept=concept))
        renamed = {old.rsplit(".", 1)[1]: new.rsplit(".", 1)[1] for old, new in d.renames}
        renamed_to = {new: old for old, new in renamed.items()}
        for col in d.removed_columns:
            new = renamed.get(col)
            detail = _rename_detail(f"{d.fqn}.{col}", f"{d.fqn}.{new}") if new else None
            self.remove_column(d.fqn, concept, bound, col, detail)
        for col in d.added_columns:
            old = renamed_to.get(col)
            detail = _rename_detail(f"{d.fqn}.{old}", f"{d.fqn}.{col}") if old else None
            self.add_column(d.fqn, concept, col, detail)
        self.new_relations(d.fqn, concept, set(d.added_columns))
        for col in d.changed_columns:
            self.change_column(d.fqn, concept, bound, col)

    def remove_column(
        self, tbl: str, concept: str, bound: TableBinding, col: str, detail: str | None
    ) -> None:
        name = bound.attributes.get(col)
        if name is None:
            self.warnings.append(f"column {tbl}.{col} has no attribute in the model; not removed")
            return
        if (concept, name) not in self.terms.attributes:
            return
        self.rewritten.append(
            RemoveAttribute(
                op="remove_attribute",
                concept=concept,
                name=name,
                rule_ids=[RENAME_RULE if detail else REMOVE_RULE],
                evidence=[Evidence(kind="column", ref=f"{tbl}.{col}", detail=detail)],
            )
        )

    def add_column(self, tbl: str, concept: str, col: str, detail: str | None) -> None:
        op = self.m.attribute.get(f"{tbl}.{col}")
        if op is None:
            self.warnings.append(f"column {tbl}.{col} could not be mapped; not proposed")
            return
        if detail is None:
            self.rewritten.append(_with(op, concept=concept))
        else:
            ev = Evidence(kind="column", ref=f"{tbl}.{col}", detail=detail)
            self.rewritten.append(_plus_evidence(op, ev, concept=concept))

    def new_relations(self, tbl: str, concept: str, added: set[str]) -> None:
        for op in self.m.by_table.get(tbl, []):
            if not isinstance(op, AddRelation):
                continue
            cols = {e.ref.rsplit(".", 1)[1] for e in op.evidence if e.kind == "fk"}
            if not cols or not cols <= added:
                continue
            target_tbl = self.m.table_of_concept.get(op.target)
            target = (self.concept(target_tbl) if target_tbl else None) or op.target
            if (concept, op.relation_type, target) in self.terms.relations:
                continue
            self.rewritten.append(_with(op, source=concept, target=target))

    def change_column(self, tbl: str, concept: str, bound: TableBinding, col: str) -> None:
        op = self.m.attribute.get(f"{tbl}.{col}")
        if op is None:
            self.warnings.append(f"column {tbl}.{col} could not be mapped; not proposed")
            return
        name = bound.attributes.get(col, op.name)
        term = self.terms.attributes.get((concept, name))
        update: dict[str, Any] = {"concept": concept, "name": name}
        if term is not None:
            update["is_identifier"] = op.is_identifier or term.is_identifier
            update["unit"] = op.unit or term.unit
            update["allowed_values"] = list(op.allowed_values) or list(term.allowed_values)
        new: AddAttribute = _with(op, **update)
        if term is not None and (
            new.datatype,
            new.is_identifier,
            tuple(new.allowed_values),
            new.unit,
        ) == (term.datatype, term.is_identifier, term.allowed_values, term.unit):
            self.warnings.append(
                f"column {tbl}.{col} changed but attribute {concept}.{name} already matches it"
            )
            return
        self.rewritten.append(new)

    # -- removed and added tables ------------------------------------------------------------

    def removed(self, d: TableDrift) -> None:
        bound = self.bindings.get(d.fqn)
        if bound is None:
            self.warnings.append(f"table {d.fqn} has no concept in the model; not removed")
            return
        c = bound.concept
        if c not in self.terms.concepts:
            return
        new = self.table_renames.get(d.fqn)
        detail = _rename_detail(d.fqn, new) if new else None
        common: dict[str, Any] = {
            "rule_ids": [RENAME_RULE if detail else REMOVE_RULE],
            "evidence": [Evidence(kind="table", ref=d.fqn, detail=detail)],
        }
        for s, r, t in sorted(k for k in self.terms.relations if c in (k[0], k[2])):
            self.rewritten.append(
                RemoveRelation(op="remove_relation", source=s, relation_type=r, target=t, **common)
            )
        axiom = self.terms.axioms.get(c)
        if axiom is not None:
            self.rewritten.append(
                RemoveAxiom(op="remove_axiom", concept=c, name=axiom.name, **common)
            )
        for concept, name in sorted(k for k in self.terms.attributes if k[0] == c):
            self.rewritten.append(
                RemoveAttribute(op="remove_attribute", concept=concept, name=name, **common)
            )
        children = sorted(n for n, t in self.terms.concepts.items() if t.parent == c)
        if children:
            self.warnings.append(
                f"concept {c} of removed table {d.fqn} has child concepts "
                f"({', '.join(children[:_MAX_LISTED])}); it is not removed"
            )
            return
        version = self.versions.get(c)
        if version is None:
            self.warnings.append(f"concept {c} has no known OpenMetadata version; not removed")
            return
        self.rewritten.append(
            RemoveConcept(op="remove_concept", fqn=c, base_version=f"{version}", **common)
        )

    def added(self, d: TableDrift) -> None:
        old = d.renames[0][0] if d.renames else None
        for op in self.m.by_table.get(d.fqn, []):
            is_root = isinstance(op, AddConcept) and op.parent is None
            if old is not None and is_root and _first(op, "table") == d.fqn:
                ev = Evidence(kind="table", ref=d.fqn, detail=_rename_detail(old, d.fqn))
                self.additions.append(_plus_evidence(op, ev))
            else:
                self.additions.append(op)


def drift_operations(
    drifts: Sequence[TableDrift],
    current_scan: ScanRecord,
    bindings: Mapping[str, TableBinding],
    model_terms: TermSet,
    policy: MappingPolicy,
    versions: Mapping[str, float],
) -> tuple[list[ProposalOperation], list[ProposalOperation], list[str]]:
    """The operations of ``drifts``: removals and rewritten additions on the bound names, the
    mapper's operations for added tables (still to go through ``dedupe``), and warnings.

    ``versions`` holds the OpenMetadata version of each concept a removed table's
    ``RemoveConcept`` needs, by concept name."""
    b = _Builder(drifts, current_scan, bindings, model_terms, policy, versions)
    b.run()
    # the mapper's own warnings only for the tables that drifted (not the unchanged ones)
    drifted = [d.fqn for d in drifts if d.status in ("changed", "added")]
    mapper = {w for w in b.mapper_warnings if any(f in w for f in drifted)}
    return b.rewritten, b.additions, sorted(set(b.warnings) | mapper)
