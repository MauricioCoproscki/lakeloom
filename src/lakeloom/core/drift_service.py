"""Drift detection (FR-PRO-07, FR-AGT-04): the lake now against the scans a model was built
from, proposed as a **draft**.

Baseline: per table, the table as recorded in the newest scan referenced by an ``applied``
proposal of the model that names it in its provenance (a proposal without provenance tables
counts for its whole scan). A table such a scan no longer holds, while it holds other tables of
the same schema, was removed by that (drift) proposal and leaves the baseline.

Fresh scan: one :meth:`ScanService.scan` per baseline source over the catalogs and schemas of
its baseline tables, without profiling, as the caller; ``ScanService`` filters catalogs and
schemas independently, so the result is cut back to the baseline ``(catalog, schema)`` pairs.
A table in error, or every table of a source that could not be enumerated, is reported as
``error`` and never removed. The new scan records stay in the state store.

Operations come from :func:`~lakeloom.core.drift.drift_operations`; those of added tables go
through ``dedupe`` like ``propose_from_scan`` (only with ``include_new_tables``). Operations
re-creating a deleted native definition (an rc1 tombstone, ADR 0016 R4) and operations
already pending in an open proposal are left out with a warning. With anything left,
:meth:`ProposalService.propose_operations` creates the draft (``origin="drift"``, the new
scan id, provenance tables = the tables with operations at their new versions), re-reading
the pending operations under the model lock (``recheck_pending``): a concurrent drift run
that proposed them first makes this one a ``conflict``. No drift is not an error: the report
has ``proposal=None``.

:meth:`DriftService.check` is the report-only run of the ``drift-schedule`` job: the same
plan (fresh scan records included) and never a proposal. :class:`DriftSchedule` checks every
model with an applied scan as ``system:drift`` and reports through the gauge
``lakeloom_drift_tables{model}``, one log line per model (counts only: table names come from
the lake, NFR-SEC-11) and ``jobs.drift`` in ``get_server_info``.

Audit: ``propose_operations`` audits a created draft as ``detect_drift``. The no-drift path
and refusals before it are audited here when the service owns auditing (the CLI); MCP passes
``audit=None`` and ``guarded`` writes the only row.
"""

import asyncio
import logging
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol
from urllib.parse import quote

from pydantic import BaseModel, Field

from lakeloom import metrics
from lakeloom.core.audit import AuditRecord, current_audit, record
from lakeloom.core.callers import Actor, Caller
from lakeloom.core.dedupe import dedupe
from lakeloom.core.drift import (
    TableBinding,
    TableDrift,
    bind_ops,
    bind_tables,
    comment_changes,
    diff_scans,
    drift_operations,
    resolve_moves,
    table_of,
)
from lakeloom.core.import_terms import TermSet, extract_terms
from lakeloom.core.imports import _fqn  # pyright: ignore[reportPrivateUsage]
from lakeloom.core.mapper import map_scan
from lakeloom.core.model_create import validate_slug
from lakeloom.core.model_policy import (
    MappingPolicy,
    ModelPolicyEntry,
    access_for,
    policy_for,
    require_group,
)
from lakeloom.core.models import ModelService
from lakeloom.core.operations import (
    AddAttribute,
    AddAxiom,
    AddConcept,
    AddRelation,
    ProposalOperation,
    RemoveConcept,
    native_member_key,
    op_key,
)
from lakeloom.core.proposals import (
    Proposal,
    ProposalMatch,
    ProposalService,
    TableProvenance,
    map_om_error,
    summary_markdown,
)
from lakeloom.core.scans import ScanService
from lakeloom.errors import LakeloomError
from lakeloom.om.client import OMClient, OMError
from lakeloom.state.audit import AuditRepository
from lakeloom.state.proposals import ProposalRepository
from lakeloom.state.scans import ScannedTable, ScanRecord, ScanRepository
from lakeloom.untrusted import code_span

__all__ = [
    "DRIFT_ACTOR",
    "DRIFT_TOOL",
    "DriftReport",
    "DriftRun",
    "DriftSchedule",
    "DriftService",
    "drift_text",
]

_log = logging.getLogger("lakeloom.drift")

DRIFT_TOOL = "detect_drift"
DRIFT_ACTOR: Final = Actor(subject="system:drift", type="system")
_PAGE = 200
_TERM_READS = 8
_MAX_LISTED = 10
_NO_BASELINE_HINT = "run propose_from_scan and get it approved first"


class DriftReport(BaseModel):
    model: str
    baseline_scan_ids: list[str]
    scan_ids: list[str]
    tables: list[TableDrift]
    proposal: Proposal | None
    warnings: list[str]


def _now() -> datetime:
    return datetime.now(UTC)


def _schema_of(fqn: str) -> str:
    return fqn.rsplit(".", 1)[0]


def _bare(ref: str) -> str:
    return ref.rsplit(".", 1)[-1]


def _error_text(t: ScannedTable) -> str:
    err = t.error or {}
    return f"{err.get('code', 'error')}: {err.get('message', 'no description in scan')}"


class _Baseline:
    def __init__(self) -> None:
        self.tables: dict[str, ScannedTable] = {}
        self.source_of: dict[str, str] = {}
        self.scan_ids: list[str] = []


class _Fresh:
    def __init__(self) -> None:
        self.tables: dict[str, ScannedTable] = {}
        self.errors: dict[str, str] = {}
        self.records: list[ScanRecord] = []
        self.source_of: dict[str, str] = {}


class DriftService:
    def __init__(
        self,
        scans: ScanService,
        scan_repo: ScanRepository,
        proposals: ProposalService,
        proposal_repo: ProposalRepository,
        models: ModelService,
        om: OMClient,
        policies: Sequence[ModelPolicyEntry] = (),
        *,
        audit: AuditRepository | None = None,
    ) -> None:
        self._scans = scans
        self._scan_repo = scan_repo
        self._proposals = proposals
        self._repo = proposal_repo
        self._models = models
        self._om = om
        self._policies = list(policies)
        self._audit = audit

    async def detect(
        self,
        principal: Caller,
        client: str | None,
        model: str,
        *,
        include_new_tables: bool = True,
    ) -> DriftReport:
        """Scan the model's baseline tables again and propose the drift as a draft."""
        validate_slug(model)
        current_audit().model = model
        try:
            require_group(principal, access_for(self._policies, model), "propose", model=model)
            plan = await self._plan(principal, client, model, include_new_tables)
        except LakeloomError as exc:
            await self._write_audit(principal, client, model, exc.code)
            raise
        report, ops, tables, matches, scan_id = plan
        if not ops:
            await self._write_audit(principal, client, model, None)
            return report
        proposal = await self._proposals.propose_operations(
            model,
            ops,
            origin="drift",
            principal=principal,
            client=client,
            tool=DRIFT_TOOL,
            scan_id=scan_id,
            tables=tables,
            matches=matches,
            warnings=report.warnings,
            recheck_pending=True,  # dedupe and the pending filter ran before the model lock
        )
        return report.model_copy(update={"proposal": proposal})

    async def check(self, principal: Caller, client: str | None, model: str) -> DriftReport:
        """Scan the model's baseline tables again and report the drift; never a proposal.

        The fresh scan records are stored, as with :meth:`detect`."""
        validate_slug(model)
        require_group(principal, access_for(self._policies, model), "propose", model=model)
        report, *_ = await self._plan(principal, client, model, True)
        return report

    # --- planning -------------------------------------------------------------------------

    async def _plan(
        self, principal: Caller, client: str | None, model: str, include_new_tables: bool
    ) -> tuple[
        DriftReport,
        list[ProposalOperation],
        list[TableProvenance],
        list[ProposalMatch],
        str | None,
    ]:
        scan_ids = await self._repo.applied_scan_ids(model)
        if not scan_ids:
            raise LakeloomError(
                "validation_error", f"no applied scan-based proposal for {model}", _NO_BASELINE_HINT
            )
        applied = await self._applied(model)
        warnings: list[str] = []
        base = await self._baseline(scan_ids, applied, warnings)
        if not base.tables:
            raise LakeloomError(
                "validation_error",
                f"the applied scans of {model} hold no described table",
                _NO_BASELINE_HINT,
            )
        fresh = await self._rescan(principal, client, base, warnings)
        drifts = diff_scans(base.tables, fresh.tables, fresh.errors)

        policy = policy_for(self._policies, model)
        bindings = self._bindings(base, applied, policy)
        combined = ScanRecord(
            id=fresh.records[0].id if fresh.records else "scan_none",
            source=fresh.records[0].source if fresh.records else "-",
            created_at=_now(),
            principal_subject=principal.subject,
            principal_type=str(principal.type),
            status="completed",
            tables=[fresh.tables[k] for k in sorted(fresh.tables)],
        )
        drifts, bindings, move_warnings = resolve_moves(
            drifts, base.tables, fresh.tables, bindings, combined, policy
        )
        warnings += move_warnings
        terms, base_iri = await self._terms(model)
        versions = await self._versions(model, terms, drifts, bindings)
        rewritten, additions, op_warnings = drift_operations(
            drifts, combined, bindings, terms, policy, versions
        )
        warnings += comment_changes(base.tables, fresh.tables)
        warnings += op_warnings

        matches: list[ProposalMatch] = []
        added = [d.fqn for d in drifts if d.status == "added"]
        if not include_new_tables:
            if added:
                warnings.append(
                    f"{len(added)} added tables are reported but not proposed "
                    "(include_new_tables=false)"
                )
            additions = []
        elif additions:
            result = await dedupe(
                additions,
                model=model,
                models=self._models,
                om=self._om,
                pending=self._proposals.pending_op_keys,
                asset_by_table={k: (t.om_fqn, None) for k, t in fresh.tables.items() if t.om_fqn},
            )
            additions = result.operations
            warnings += result.warnings
            folded = {_bare(m.candidate_fqn) for m in result.matches if m.action != "kept"}
            rewritten = self._keep_folded(rewritten, folded, warnings)
            matches = [
                ProposalMatch(
                    op_key=m.op_key,
                    candidate_fqn=m.candidate_fqn,
                    confidence=m.confidence,
                    matched_on=m.matched_on,
                    action=m.action,
                )
                for m in result.matches
            ]
        ops = await self._without_tombstones(
            model, terms, base_iri, [*rewritten, *additions], warnings
        )
        ops = await self._without_pending(model, ops, warnings)

        names = sorted(set(base.tables) | set(fresh.tables))
        used = sorted({t for op in ops if (t := table_of(op, names)) is not None})
        tables = [
            TableProvenance(
                fqn=t, om_fqn=fresh.tables[t].om_fqn, version=fresh.tables[t].version.version
            )
            if t in fresh.tables
            else TableProvenance(fqn=t, om_fqn=base.tables[t].om_fqn, version=None)
            for t in used
        ]
        scan_id = self._scan_id(fresh, base, used, warnings)
        report = DriftReport(
            model=model,
            baseline_scan_ids=base.scan_ids,
            scan_ids=[r.id for r in fresh.records],
            tables=drifts,
            proposal=None,
            warnings=list(dict.fromkeys(warnings)),
        )
        return report, ops, tables, matches, scan_id

    @staticmethod
    def _keep_folded(
        ops: list[ProposalOperation], folded: set[str], warnings: list[str]
    ) -> list[ProposalOperation]:
        """Drop the removal of a table whose concept an added table was folded into by
        ``dedupe``: that table most likely moved, and the draft must not delete the concept."""
        gone: dict[str, str] = {}  # removed table -> concept
        for op in ops:
            if isinstance(op, RemoveConcept) and _bare(op.fqn) in folded:
                ref = next((e.ref for e in op.evidence if e.kind == "table"), None)
                if ref is not None:
                    gone[ref] = _bare(op.fqn)
        if not gone:
            return ops
        for tbl, concept in sorted(gone.items()):
            warnings.append(
                f"table {tbl} may have moved: an added table matched its concept {concept}; "
                "its removal is not proposed"
            )
        return [
            op for op in ops if not any(e.kind == "table" and e.ref in gone for e in op.evidence)
        ]

    async def _applied(self, model: str) -> list[Proposal]:
        out: list[Proposal] = []
        cursor: str | None = None
        while True:
            page, cursor = await self._repo.list_by_status(["applied"], model, cursor, _PAGE)
            out.extend(page)
            if cursor is None:
                return out

    async def _baseline(
        self, scan_ids: Sequence[str], applied: Sequence[Proposal], warnings: list[str]
    ) -> _Baseline:
        wanted_by_scan: dict[str, set[str]] = {}
        for p in applied:
            if p.scan_id:
                wanted_by_scan.setdefault(p.scan_id, set()).update(
                    t.fqn for t in p.provenance.tables
                )
        out = _Baseline()
        seen: set[str] = set()
        for sid in scan_ids:
            try:
                rec = await self._scan_repo.get(sid)
            except LakeloomError as exc:
                if exc.code != "not_found":
                    raise
                warnings.append(f"scan {sid} of an applied proposal is gone; skipped")
                continue
            by_name = {t.ref.full_name: t for t in rec.tables}
            covered = {_schema_of(n) for n in by_name}
            wanted = wanted_by_scan.get(sid) or set(by_name)
            contributed = False
            for fqn in sorted(wanted - seen):
                t = by_name.get(fqn)
                if t is None:
                    if _schema_of(fqn) in covered:
                        seen.add(fqn)  # removed by the proposal of this scan (a drift)
                    continue
                if t.error or t.description is None:
                    continue  # an older scan may hold it
                seen.add(fqn)
                out.tables[fqn] = t
                out.source_of[fqn] = rec.source
                contributed = True
            if contributed:
                out.scan_ids.append(sid)
        return out

    async def _rescan(
        self, principal: Caller, client: str | None, base: _Baseline, warnings: list[str]
    ) -> _Fresh:
        out = _Fresh()
        by_source: dict[str, list[str]] = {}
        for fqn, src in base.source_of.items():
            by_source.setdefault(src, []).append(fqn)
        for source in sorted(by_source):
            mine = by_source[source]
            pairs = {(base.tables[f].ref.catalog, base.tables[f].ref.schema_) for f in mine}
            named = sorted({c for c, _ in pairs if c is not None})
            catalogs = named if all(c is not None for c, _ in pairs) else None
            try:
                rec = await self._scans.scan(
                    source,
                    catalogs=catalogs,
                    schemas=sorted({s for _, s in pairs}),
                    tables=None,
                    profile=False,
                    principal_subject=principal.subject,
                    principal_type=str(principal.type),
                    client=client,
                )
            except LakeloomError as exc:
                warnings.append(
                    f"source {source} could not be scanned ({exc.code}); its tables are "
                    "reported as errors"
                )
                for fqn in mine:
                    out.errors[fqn] = f"{exc.code}: {exc.message}"
                continue
            out.records.append(rec)
            warnings.extend(rec.warnings)
            for t in rec.tables:
                if (t.ref.catalog, t.ref.schema_) not in pairs:
                    continue
                fqn = t.ref.full_name
                out.source_of[fqn] = source
                if t.error or t.description is None:
                    out.errors[fqn] = _error_text(t)
                else:
                    out.tables[fqn] = t
            failed = next((w for w in rec.warnings if w.startswith("enumeration failed")), None)
            if failed is not None:
                for fqn in mine:
                    if fqn not in out.tables:
                        out.errors.setdefault(fqn, failed.removeprefix("enumeration failed: "))
        return out

    def _bindings(
        self, base: _Baseline, applied: Sequence[Proposal], policy: MappingPolicy
    ) -> dict[str, TableBinding]:
        """The applied proposals' names; the mapper's names for baseline tables without."""
        bound = bind_tables(applied)
        missing = [t for k, t in sorted(base.tables.items()) if k not in bound]
        if missing:
            rec = ScanRecord(
                id="scan_baseline",
                source="-",
                created_at=_now(),
                principal_subject="-",
                principal_type="-",
                status="completed",
                tables=missing,
            )
            fallback = bind_ops(map_scan(rec, policy).operations)
            bound = {**{k: v for k, v in fallback.items() if k in base.tables}, **bound}
        return bound

    async def _terms(self, model: str) -> tuple[TermSet, str]:
        info = await self._models.get_model(model)
        if info.base_iri is None:
            raise LakeloomError(
                "validation_error",
                f"model {model!r} has no base IRI",
                "configure LAKELOOM_PUBLIC_URL so model IRIs can be derived",
            )
        graph = await self._models.graph(model)
        terms = await asyncio.to_thread(extract_terms, graph, info.base_iri)
        return terms, info.base_iri

    async def _versions(
        self,
        model: str,
        terms: TermSet,
        drifts: Sequence[TableDrift],
        bindings: Mapping[str, TableBinding],
    ) -> dict[str, float]:
        """OM versions of the concepts of removed tables (those the model has)."""
        moved = {
            old for d in drifts if d.status == "changed" for old, new in d.renames if new == d.fqn
        }
        names = sorted(
            {
                b.concept
                for d in drifts
                if d.status == "removed"
                and d.fqn not in moved
                and (b := bindings.get(d.fqn)) is not None
                and b.concept in terms.concepts
            }
        )
        gate = asyncio.Semaphore(_TERM_READS)

        async def one(name: str) -> tuple[str, float | None]:
            async with gate:
                fqn = _fqn(model, terms, name)
                try:
                    term = await self._om.get_term(fqn)
                    if term is None and fqn != f"{model}.{name}":
                        term = await self._om.get_term(f"{model}.{name}")
                except OMError as exc:
                    raise map_om_error(exc, f"reading term {name!r} of {model!r}") from None
            version = getattr(term, "version", None) if term is not None else None
            return name, version if isinstance(version, float | int) else None

        found = await asyncio.gather(*(one(n) for n in names))
        return {n: float(v) for n, v in found if v is not None}

    async def _without_tombstones(
        self,
        model: str,
        terms: TermSet,
        base_iri: str,
        ops: list[ProposalOperation],
        warnings: list[str],
    ) -> list[ProposalOperation]:
        """Leave out additions whose native definition is an rc1 tombstone (ADR 0016 R4)."""
        if not any(
            isinstance(op, AddConcept | AddAttribute | AddAxiom | AddRelation) for op in ops
        ):
            return ops
        try:
            if await self._om.get_ontology_by_name(model) is None:
                return ops
            live, tombs = await asyncio.gather(
                self._om.list_native_members(model),
                self._om.list_native_members(model, include_deleted=True),
            )
        except OMError as exc:
            raise map_om_error(exc, f"listing the native members of {model!r}") from None
        prefixes: tuple[Literal["concept", "attr", "axiom"], ...] = ("concept", "attr", "axiom")
        alive = {native_member_key(p, m) for p, ms in zip(prefixes, live, strict=True) for m in ms}
        deleted = {
            k
            for p, ms in zip(prefixes, tombs, strict=True)
            for m in ms
            if (k := native_member_key(p, m)) not in alive
        }
        if not deleted:
            return ops

        def iri(name: str) -> str:
            known = terms.concepts.get(_bare(name))
            return known.iri if known is not None else f"{base_iri}{quote(_bare(name), safe='')}"

        kept: list[ProposalOperation] = []
        skipped: list[str] = []
        for op in ops:
            match op:
                case AddConcept():
                    key, label = f"concept:{iri(op.name)}", f"concept {op.name}"
                case AddAttribute():
                    key = f"attr:{iri(op.concept)}.{quote(op.name, safe='')}"
                    label = f"attribute {_bare(op.concept)}.{op.name}"
                case AddAxiom():
                    name = str(op.axiom.get("name"))
                    key, label = f"axiom:{iri(op.concept)}.{quote(name, safe='')}", f"axiom {name}"
                case AddRelation():
                    key = f"attr:{iri(op.source)}.{quote(op.relation_type, safe='')}"
                    label = f"relation {_bare(op.source)}.{op.relation_type}"
                case _:
                    kept.append(op)
                    continue
            if key in deleted:
                skipped.append(label)
            else:
                kept.append(op)
        if skipped:
            warnings.append(
                f"{len(skipped)} definitions were deleted in OpenMetadata and cannot be created "
                f"again: {', '.join(sorted(skipped)[:_MAX_LISTED])}"
            )
        return kept

    async def _without_pending(
        self, model: str, ops: list[ProposalOperation], warnings: list[str]
    ) -> list[ProposalOperation]:
        if not ops:
            return ops
        pending = await self._proposals.pending_op_keys(model)
        kept = [op for op in ops if op_key(op) not in pending]
        ids = sorted({pending[op_key(op)] for op in ops if op_key(op) in pending})
        if len(kept) < len(ops):
            warnings.append(
                f"{len(ops) - len(kept)} operations already pending in {', '.join(ids)}"
            )
        return kept

    @staticmethod
    def _scan_id(
        fresh: _Fresh, base: _Baseline, used: Sequence[str], warnings: list[str]
    ) -> str | None:
        """The new scan the proposal records: that of the source of its tables (``None``
        without a fresh scan). Provenance holds one scan id; drift over several sources names
        the others in a warning."""
        if not fresh.records:
            return None
        sources = Counter(fresh.source_of.get(t) or base.source_of.get(t) for t in used)
        ids = {r.source: r.id for r in fresh.records}
        chosen = next(
            (
                ids[s]
                for s, _ in sorted(sources.items(), key=lambda x: (-x[1], str(x[0])))
                if s in ids
            ),
            fresh.records[0].id,
        )
        others = sorted(ids[s] for s in sources if s in ids and ids[s] != chosen)
        if others:
            warnings.append(
                f"the drift spans several sources; the proposal records scan {chosen} "
                f"(also scanned: {', '.join(others)})"
            )
        return chosen

    async def _write_audit(
        self, principal: Caller, client: str | None, model: str, code: str | None
    ) -> None:
        """No drift (``code`` None) or a refusal (CLI mode only; see the module doc)."""
        if self._audit is None:
            return
        rec = AuditRecord(
            at=_now(),
            principal_subject=principal.subject,
            principal_type=str(principal.type),
            client=client,
            tool=DRIFT_TOOL,
            model=model,
            result="ok" if code is None else "forbidden" if code == "forbidden" else "error",
            error_code=code,
        )
        await record(self._audit, rec)


# --- the drift-schedule job (report only) -----------------------------------------------------


class DriftRun(BaseModel):
    """One scheduled run: per model, the tables changed, added or removed, or the error code."""

    at: datetime
    tables: dict[str, int] = Field(default_factory=dict[str, int])
    errors: dict[str, str] = Field(default_factory=dict[str, str])

    def summary(self) -> dict[str, Any]:
        """The ``jobs.drift`` block of ``get_server_info`` (slugs and counts only)."""
        return {
            "last_run": self.at.isoformat(),
            "models": len(self.tables) + len(self.errors),
            "drifted": sorted(m for m, n in self.tables.items() if n),
            "tables": dict(self.tables),
            "errors": dict(self.errors),
        }


class _Checker(Protocol):
    async def check(self, principal: Caller, client: str | None, model: str) -> DriftReport: ...


class _Baselined(Protocol):
    async def models_with_applied_scans(self) -> list[str]: ...


class DriftSchedule:
    """Runs :meth:`DriftService.check` for every model with an applied scan, as
    ``system:drift``; one model's failure never stops the run."""

    def __init__(
        self,
        service: _Checker,
        proposals: _Baselined,
        *,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._service = service
        self._proposals = proposals
        self._clock = clock
        self.last_run: DriftRun | None = None

    async def run(self) -> DriftRun:
        run = DriftRun(at=self._clock())
        for model in await self._proposals.models_with_applied_scans():
            try:
                report = await self._service.check(DRIFT_ACTOR, None, model)
            except (LakeloomError, OMError) as exc:  # the message may carry lake text: code only
                code = (
                    exc.code
                    if isinstance(exc, LakeloomError)
                    else map_om_error(exc, f"checking {model!r}").code
                )
                run.errors[model] = code
                _log.warning("scheduled drift check failed", extra={"model": model, "error": code})
                continue
            except Exception as exc:
                run.errors[model] = "internal"
                _log.error(
                    "scheduled drift check failed",
                    extra={"model": model, "error": type(exc).__name__},
                )
                continue
            counts = Counter(t.status for t in report.tables)
            drifted = counts["changed"] + counts["added"] + counts["removed"]
            run.tables[model] = drifted
            metrics.DRIFT_TABLES.labels(model=model).set(drifted)
            _log.info(
                "scheduled drift check",
                extra={
                    "model": model,
                    "changed": counts["changed"],
                    "added": counts["added"],
                    "removed": counts["removed"],
                    "errors": counts["error"],
                },
            )
        self.last_run = run
        return run


def _cols(names: Sequence[str]) -> str:
    return ", ".join(code_span(n) for n in names)


def drift_text(report: DriftReport, *, submit: str = "submit_proposal") -> str:
    """The report as Markdown. Table and column names are lake data: inert code spans."""
    since = ", ".join(report.baseline_scan_ids)
    lines = [
        f"# Drift of model {code_span(report.model)}",
        "",
        f"- Baseline scans: {since or 'none'}",
        f"- New scans: {', '.join(report.scan_ids) or 'none'}",
    ]
    counts = Counter(d.status for d in report.tables)
    lines.append(
        "- Tables: " + ", ".join(f"{s} {counts[s]}" for s in sorted(counts))
        if counts
        else "- Tables: none"
    )
    drifted = [d for d in report.tables if d.status != "unchanged"]
    if drifted:
        lines += ["", "## Tables", ""]
        for d in drifted:
            lines.append(f"- {code_span(d.fqn)}: {d.status}")
            if d.added_columns:
                lines.append(f"  - added columns: {_cols(d.added_columns)}")
            if d.removed_columns:
                lines.append(f"  - removed columns: {_cols(d.removed_columns)}")
            if d.changed_columns:
                lines.append(f"  - changed columns: {_cols(d.changed_columns)}")
            if d.error:
                lines.append(f"  - error: {code_span(d.error)}")
    renames = [r for d in report.tables for r in d.renames]
    if renames:
        lines += ["", "## Possible renames", ""]
        lines += [f"- {code_span(old)} to {code_span(new)}" for old, new in renames]
    if report.warnings:
        lines += ["", "## Warnings", ""]
        lines += [f"- {code_span(w)}" for w in report.warnings]
    lines.append("")
    p = report.proposal
    if p is None:
        if drifted:
            lines.append(f"Drift found since {since}, but nothing to propose.")
        else:
            lines.append(f"no drift since {since}")
    else:
        lines += [
            summary_markdown(p).rstrip(),
            "",
            f"Draft {code_span(p.id)}; submit it with {submit}.",
        ]
    return "\n".join(lines) + "\n"
