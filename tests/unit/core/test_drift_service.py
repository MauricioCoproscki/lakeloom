"""DriftService: baseline, fresh scan and the draft proposal (FR-PRO-07), over fakes.

The tool and prompt tests (``test_detect_drift_tool_scope_and_audit``,
``test_drift_check_prompt_registered``) need the MCP server fixtures, so they live in
tests/unit/server/test_tools_import.py and tests/unit/server/test_prompts.py.
"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from rdflib import Graph

from lakeloom.core import drift_service
from lakeloom.core.callers import Actor
from lakeloom.core.dedupe import DedupeResult, Match
from lakeloom.core.drift_service import DriftService
from lakeloom.core.model_policy import ModelAccess, ModelPolicyEntry
from lakeloom.core.operations import (
    AddAttribute,
    AddConcept,
    Evidence,
    ProposalOperation,
    op_key,
)
from lakeloom.core.proposals import (
    NothingToPropose,
    Proposal,
    ProposalPart,
    Provenance,
    TableProvenance,
)
from lakeloom.errors import LakeloomError
from lakeloom.om.models import OMGlossaryTerm
from lakeloom.sources.models import ColumnInfo, TableDescription, TableRef, TableVersion
from lakeloom.state.scans import ScannedTable, ScanRecord

NS = "http://x/ontology/shop/"
NOW = datetime(2026, 11, 16, tzinfo=UTC)
AGENT = Actor(subject="agent-7", type="agent")
ORDERS = "c.s.orders"

TTL = f"""\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<{NS}Order> a owl:Class, skos:Concept ; skos:prefLabel "Order" .
<{NS}Order.id> a owl:DatatypeProperty ; rdfs:domain <{NS}Order> ; rdfs:range xsd:long .
<{NS}Order.note> a owl:DatatypeProperty ; rdfs:domain <{NS}Order> ; rdfs:range xsd:string .
"""


def table(
    name: str, cols: list[tuple[str, str]], *, catalog: str = "c", schema: str = "s",
    version: int = 1, error: bool = False,
) -> ScannedTable:  # fmt: skip
    ref = TableRef(catalog=catalog, schema=schema, name=name)
    if error:
        return ScannedTable(
            ref=ref, version=TableVersion(), error={"code": "upstream_unavailable", "message": "x"}
        )
    columns = [
        ColumnInfo(name=n, data_type=t, nullable=True, position=i) for i, (n, t) in enumerate(cols)
    ]
    return ScannedTable(
        ref=ref,
        version=TableVersion(version=version),
        om_fqn=f"svc.{ref.full_name}",
        description=TableDescription(
            ref=ref, columns=columns, version=TableVersion(version=version)
        ),
    )


def scan(
    sid: str, *tables: ScannedTable, source: str = "lake", warnings: list[str] | None = None
) -> ScanRecord:
    return ScanRecord(
        id=sid, source=source, created_at=NOW, principal_subject="a", principal_type="agent",
        status="completed", tables=list(tables), warnings=warnings or [],
    )  # fmt: skip


def applied(pid: str, scan_id: str, ops: list[ProposalOperation], tables: list[str]) -> Proposal:
    data: dict[str, Any] = {
        "id": pid,
        "model": "shop",
        "status": "applied",
        "scan_id": scan_id,
        "parts": [ProposalPart(n=1, operations=ops)],
        "provenance": Provenance(
            scan_id=scan_id,
            tables=[TableProvenance(fqn=t) for t in tables],
            principal_subject="a",
            principal_type="agent",
            at=NOW,
        ),
        "created_at": NOW,
        "updated_at": NOW,
    }
    return Proposal.model_validate(data)


def attr(concept: str, name: str, ref: str, dt: str = "xsd:long") -> AddAttribute:
    return AddAttribute(
        op="add_attribute", concept=concept, name=name, datatype=dt,
        evidence=[Evidence(kind="column", ref=ref)],
    )  # fmt: skip


ORDER_OPS: list[ProposalOperation] = [
    AddConcept(op="add_concept", name="Order", evidence=[Evidence(kind="table", ref=ORDERS)]),
    attr("Order", "id", f"{ORDERS}.id"),
    attr("Order", "note", f"{ORDERS}.note", "xsd:string"),
]


class FakeScanRepo:
    def __init__(self) -> None:
        self.records: dict[str, ScanRecord] = {}

    async def get(self, scan_id: str) -> ScanRecord:
        if scan_id not in self.records:
            raise LakeloomError("not_found", f"scan {scan_id!r} does not exist", "x")
        return self.records[scan_id]


class FakeScans:
    def __init__(self, repo: FakeScanRepo) -> None:
        self.repo = repo
        self.lake: dict[str, list[ScannedTable] | Exception] = {}
        self.warnings: dict[str, list[str]] = {}
        self.calls: list[dict[str, Any]] = []

    async def scan(self, source: str, **kw: Any) -> ScanRecord:
        self.calls.append({"source": source, **kw})
        found = self.lake[source]
        if isinstance(found, Exception):
            raise found
        rec = scan(f"scan_new_{source}", *found, source=source, warnings=self.warnings.get(source))
        self.repo.records[rec.id] = rec
        return rec


class FakeProposalRepo:
    def __init__(self) -> None:
        self.proposals: list[Proposal] = []

    async def applied_scan_ids(self, model: str) -> list[str]:
        out = [p.scan_id for p in sorted(self.proposals, key=lambda p: p.id, reverse=True)]
        return list(dict.fromkeys(s for s in out if s))

    async def list_by_status(
        self, statuses: Any, model: str | None, cursor: str | None, limit: int
    ) -> tuple[list[Proposal], str | None]:
        rows = sorted(self.proposals, key=lambda p: p.id)
        start = int(cursor or 0)
        page = rows[start : start + limit]
        more = start + limit < len(rows)
        return page, str(start + limit) if more else None


class FakeProposals:
    """Records calls; like ProposalService, holds the created operations as pending and
    re-checks them (``recheck_pending``) under a model lock."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.pending: dict[str, str] = {}
        self.lock = asyncio.Lock()

    async def pending_op_keys(self, model: str) -> dict[str, str]:
        return dict(self.pending)

    async def propose_operations(self, model: str, operations: Any, **kw: Any) -> Proposal:
        async with self.lock:
            return self._create(model, list(operations), **kw)

    def _create(self, model: str, ops: list[ProposalOperation], **kw: Any) -> Proposal:
        if not ops:
            raise NothingToPropose([], [])
        if kw.get("recheck_pending") and any(op_key(op) in self.pending for op in ops):
            raise LakeloomError("conflict", "operations now pending", "run detect_drift again")
        self.calls.append({"model": model, "operations": ops, **kw})
        self.pending.update({op_key(op): "prop_drift" for op in ops})
        data: dict[str, Any] = {
            "id": "prop_drift",
            "model": model,
            "status": "draft",
            "scan_id": kw["scan_id"],
            "parts": [ProposalPart(n=1, operations=ops)],
            "provenance": Provenance(
                scan_id=kw["scan_id"], tables=list(kw["tables"]), principal_subject="a",
                principal_type="agent", at=NOW, origin=kw["origin"],
            ),
            "created_at": NOW,
            "updated_at": NOW,
        }  # fmt: skip
        return Proposal.model_validate(data)


class FakeModels:
    def __init__(self, ttl: str = TTL) -> None:
        self.ttl = ttl

    async def get_model(self, slug: str, **_: Any) -> Any:
        return SimpleNamespace(base_iri=NS, name=slug)

    async def graph(self, slug: str) -> Graph:
        return Graph().parse(data=self.ttl, format="turtle")


class FakeOM:
    def __init__(self) -> None:
        self.tombstones: list[dict[str, Any]] = []
        self.term_reads: list[str] = []

    async def get_ontology_by_name(self, name: str, **_: Any) -> Any:
        return SimpleNamespace(id="onto-1", name=name)

    async def list_native_members(
        self, name: str, *, include_deleted: bool = False
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        if include_deleted:
            return [], list(self.tombstones), []
        return [], [], []

    async def get_term(self, fqn: str, fields: object = ()) -> OMGlossaryTerm | None:
        self.term_reads.append(fqn)
        name = fqn.rsplit(".", 1)[-1]
        return OMGlossaryTerm.model_validate(
            {"id": f"t-{name}", "name": name, "fullyQualifiedName": fqn, "version": 0.3,
             "description": "d", "glossary": {"id": "g-1", "type": "glossary"}}
        )  # fmt: skip


class Env:
    def __init__(self, policies: list[ModelPolicyEntry] | None = None) -> None:
        self.scan_repo = FakeScanRepo()
        self.scans = FakeScans(self.scan_repo)
        self.repo = FakeProposalRepo()
        self.proposals = FakeProposals()
        self.models = FakeModels()
        self.om = FakeOM()
        self.svc = DriftService(
            self.scans,  # type: ignore[arg-type]
            self.scan_repo,  # type: ignore[arg-type]
            self.proposals,  # type: ignore[arg-type]
            self.repo,  # type: ignore[arg-type]
            self.models,  # type: ignore[arg-type]
            self.om,  # type: ignore[arg-type]
            policies or [],
        )

    def baseline(
        self, pid: str, rec: ScanRecord, ops: list[ProposalOperation], tables: list[str]
    ) -> None:
        self.scan_repo.records[rec.id] = rec
        self.repo.proposals.append(applied(pid, rec.id, ops, tables))

    async def detect(self, **kw: Any) -> Any:
        return await self.svc.detect(AGENT, "pytest", "shop", **kw)


ORDERS_V1 = table("orders", [("id", "bigint"), ("note", "string")])


async def test_no_applied_scan_is_validation_error() -> None:
    env = Env()
    with pytest.raises(LakeloomError) as info:
        await env.detect()

    assert info.value.code == "validation_error"
    assert info.value.message == "no applied scan-based proposal for shop"
    assert info.value.hint == "run propose_from_scan and get it approved first"
    assert env.scans.calls == []


async def test_propose_groups_are_enforced() -> None:
    env = Env([ModelPolicyEntry(name="shop", access=ModelAccess(propose_groups=["stewards"]))])
    principal = SimpleNamespace(
        subject="bob", type="human", groups=frozenset[str](), scopes=frozenset[str]()
    )
    with pytest.raises(LakeloomError) as info:
        await env.svc.detect(principal, None, "shop")  # type: ignore[arg-type]
    assert info.value.code == "forbidden"


async def test_baseline_is_newest_applied_scan_per_table() -> None:
    env = Env()
    customers = table("customers", [("id", "bigint")])
    env.baseline(
        "prop_1",
        scan("scan_1", table("orders", [("id", "bigint")]), customers),
        [],
        [ORDERS, "c.s.customers"],
    )
    env.baseline("prop_2", scan("scan_2", ORDERS_V1), ORDER_OPS, [ORDERS])
    env.scans.lake["lake"] = [
        ORDERS_V1.model_copy(update={"version": TableVersion(version=5)}),
        customers,
    ]

    report = await env.detect()

    assert report.baseline_scan_ids == ["scan_2", "scan_1"]
    assert {d.fqn: d.status for d in report.tables} == {
        "c.s.customers": "unchanged",
        ORDERS: "unchanged",  # against scan_2, which already has the note column
    }


async def test_table_removed_by_an_applied_drift_is_not_baselined_again() -> None:
    env = Env()
    env.baseline(
        "prop_1",
        scan("scan_1", ORDERS_V1, table("gone", [("id", "bigint")])),
        ORDER_OPS,
        [ORDERS, "c.s.gone"],
    )
    env.baseline("prop_2", scan("scan_2", ORDERS_V1), [], [ORDERS, "c.s.gone"])  # drift removed it
    env.scans.lake["lake"] = [ORDERS_V1]

    report = await env.detect()

    assert [d.fqn for d in report.tables] == [ORDERS]


async def test_one_scan_per_source_limited_to_baseline_schemas() -> None:
    env = Env()
    items = table("items", [("id", "bigint")], catalog="d", schema="t")
    users = table("users", [("id", "bigint")], catalog="u", schema="v")
    env.baseline("prop_1", scan("scan_1", ORDERS_V1, items), ORDER_OPS, [ORDERS, "d.t.items"])
    env.baseline("prop_2", scan("scan_2", users, source="uc"), [], ["u.v.users"])
    cross = table(
        "extra", [("id", "bigint")], catalog="c", schema="t"
    )  # (c, t): not a baseline pair
    env.scans.lake["lake"] = [ORDERS_V1, items, cross]
    env.scans.lake["uc"] = [users]

    report = await env.detect()

    assert [(c["source"], c["catalogs"], c["schemas"], c["profile"]) for c in env.scans.calls] == [
        ("lake", ["c", "d"], ["s", "t"], False),
        ("uc", ["u"], ["v"], False),
    ]
    assert all(
        c["principal_subject"] == "agent-7" and c["client"] == "pytest" for c in env.scans.calls
    )
    assert "c.t.extra" not in {d.fqn for d in report.tables}
    assert report.scan_ids == ["scan_new_lake", "scan_new_uc"]


async def test_no_drift_returns_report_without_proposal() -> None:
    env = Env()
    env.baseline("prop_1", scan("scan_1", ORDERS_V1), ORDER_OPS, [ORDERS])
    env.scans.lake["lake"] = [ORDERS_V1]

    report = await env.detect()

    assert report.proposal is None and env.proposals.calls == []
    assert report.scan_ids == ["scan_new_lake"]
    assert "scan_new_lake" in env.scan_repo.records  # the new scan record still exists
    assert [d.status for d in report.tables] == ["unchanged"]


async def test_drift_creates_draft_with_drift_origin_and_new_versions() -> None:
    env = Env()
    env.baseline("prop_1", scan("scan_1", ORDERS_V1), ORDER_OPS, [ORDERS])
    other = table("customers", [("id", "bigint")])
    env.baseline("prop_0", scan("scan_0", other), [], ["c.s.customers"])
    now = table("orders", [("id", "bigint"), ("total", "decimal")], version=7)
    env.scans.lake["lake"] = [now, other]

    report = await env.detect()

    (call,) = env.proposals.calls
    assert call["origin"] == "drift" and call["tool"] == "detect_drift"
    assert call["scan_id"] == "scan_new_lake" and call["principal"] is AGENT
    assert call["client"] == "pytest"
    assert [op_key(op) for op in call["operations"]] == [
        "remove-attr:Order.note", "attr:Order.total",
    ]  # fmt: skip
    # only the tables with operations, at their new versions
    assert call["tables"] == [TableProvenance(fqn=ORDERS, om_fqn="svc.c.s.orders", version=7)]
    assert report.proposal is not None and report.proposal.id == "prop_drift"
    assert report.baseline_scan_ids == ["scan_1", "scan_0"]
    changed = next(d for d in report.tables if d.fqn == ORDERS)
    assert (changed.added_columns, changed.removed_columns) == (["total"], ["note"])


async def test_removed_table_reads_the_concept_version() -> None:
    env = Env()
    env.baseline(
        "prop_1",
        scan("scan_1", ORDERS_V1, table("keep", [("id", "bigint")])),
        ORDER_OPS,
        [ORDERS, "c.s.keep"],
    )
    env.scans.lake["lake"] = [table("keep", [("id", "bigint")])]

    report = await env.detect()

    (call,) = env.proposals.calls
    assert [op_key(op) for op in call["operations"]][-1] == "remove-concept:Order"
    assert env.om.term_reads == ["shop.Order"]
    assert call["tables"] == [TableProvenance(fqn=ORDERS, om_fqn="svc.c.s.orders", version=None)]
    assert {d.fqn: d.status for d in report.tables}[ORDERS] == "removed"


async def test_unreachable_source_tables_are_errors_not_removed() -> None:
    env = Env()
    env.baseline("prop_1", scan("scan_1", ORDERS_V1), ORDER_OPS, [ORDERS])
    env.scans.lake["lake"] = []
    env.scans.warnings["lake"] = ["enumeration failed: upstream_unavailable: down"]

    report = await env.detect()

    assert [(d.fqn, d.status) for d in report.tables] == [(ORDERS, "error")]
    assert report.proposal is None


async def test_error_table_in_fresh_scan_is_reported() -> None:
    env = Env()
    env.baseline("prop_1", scan("scan_1", ORDERS_V1), ORDER_OPS, [ORDERS])
    env.scans.lake["lake"] = [table("orders", [], error=True)]

    report = await env.detect()

    (d,) = report.tables
    assert d.status == "error" and d.error is not None and "upstream_unavailable" in d.error
    assert env.proposals.calls == []


async def test_new_tables_go_through_dedupe(monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env()
    env.baseline("prop_1", scan("scan_1", ORDERS_V1), ORDER_OPS, [ORDERS])
    env.scans.lake["lake"] = [ORDERS_V1, table("customers", [("id", "bigint")])]
    seen: list[list[str]] = []

    async def fake_dedupe(ops: list[ProposalOperation], **kw: Any) -> DedupeResult:
        seen.append([op_key(op) for op in ops])
        assert (
            kw["model"] == "shop"
            and kw["asset_by_table"]["c.s.customers"][0] == "svc.c.s.customers"
        )
        kept: list[ProposalOperation] = [op for op in ops if not isinstance(op, AddConcept)]
        return DedupeResult(
            operations=kept,
            matches=[Match(op_key="concept:Customer", candidate_fqn="shop.Client", confidence=0.95,
                           matched_on="name", action="realized")],
            warnings=["deduped"],
        )  # fmt: skip

    monkeypatch.setattr(drift_service, "dedupe", fake_dedupe)
    report = await env.detect()

    assert seen == [["concept:Customer", "attr:Customer.id"]]
    (call,) = env.proposals.calls
    assert [op_key(op) for op in call["operations"]] == ["attr:Customer.id"]
    assert [m.candidate_fqn for m in call["matches"]] == ["shop.Client"]
    assert "deduped" in call["warnings"]
    assert {d.fqn: d.status for d in report.tables}["c.s.customers"] == "added"


async def test_include_new_tables_false_only_reports() -> None:
    env = Env()
    env.baseline("prop_1", scan("scan_1", ORDERS_V1), ORDER_OPS, [ORDERS])
    env.scans.lake["lake"] = [ORDERS_V1, table("customers", [("id", "bigint")])]

    report = await env.detect(include_new_tables=False)

    assert {d.fqn: d.status for d in report.tables}["c.s.customers"] == "added"
    assert report.proposal is None and env.proposals.calls == []


async def test_tombstoned_and_pending_operations_are_left_out() -> None:
    env = Env()
    env.baseline("prop_1", scan("scan_1", ORDERS_V1), ORDER_OPS, [ORDERS])
    now = table(
        "orders", [("id", "bigint"), ("note", "string"), ("total", "decimal"), ("qty", "int")]
    )
    env.scans.lake["lake"] = [now]
    env.om.tombstones = [{"iri": f"{NS}Order.total", "deleted": True}]
    env.proposals.pending = {"attr:Order.qty": "prop_open"}

    report = await env.detect()

    assert report.proposal is None
    assert any("deleted in OpenMetadata" in w and "Order.total" in w for w in report.warnings)
    assert any("already pending in prop_open" in w for w in report.warnings)


async def test_dedupe_match_of_a_removed_concept_keeps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env()
    env.baseline("prop_1", scan("scan_1", ORDERS_V1), ORDER_OPS, [ORDERS])
    env.scans.lake["lake"] = [table("purchases", [("id", "bigint"), ("note", "string")])]

    async def fake_dedupe(ops: list[ProposalOperation], **kw: Any) -> DedupeResult:
        kept: list[ProposalOperation] = [op for op in ops if not isinstance(op, AddConcept)]
        return DedupeResult(
            operations=kept,
            matches=[Match(op_key="concept:Purchase", candidate_fqn="shop.Order", confidence=0.93,
                           matched_on="name", action="realized")],
        )  # fmt: skip

    monkeypatch.setattr(drift_service, "dedupe", fake_dedupe)
    report = await env.detect()

    (call,) = env.proposals.calls
    keys = [op_key(op) for op in call["operations"]]
    assert not any(k.startswith("remove-") for k in keys), keys
    assert any("c.s.orders" in w and "Order" in w and "not proposed" in w for w in report.warnings)


async def test_table_moved_between_baseline_schemas_keeps_its_concept() -> None:
    env = Env()
    items = table("items", [("id", "bigint")], schema="t")
    env.baseline("prop_1", scan("scan_1", ORDERS_V1, items), ORDER_OPS, [ORDERS, "c.t.items"])
    moved = table(
        "orders", [("id", "bigint"), ("note", "string"), ("total", "decimal")], schema="t"
    )
    env.scans.lake["lake"] = [moved, items]

    report = await env.detect()

    status = {d.fqn: (d.status, d.renames) for d in report.tables}
    assert status["c.t.orders"] == ("changed", [(ORDERS, "c.t.orders")])
    (call,) = env.proposals.calls
    assert [op_key(op) for op in call["operations"]] == ["attr:Order.total"]
    assert env.om.term_reads == []  # no RemoveConcept, so no version read


def test_scan_id_is_none_without_a_fresh_scan() -> None:
    """No fresh scan record: the provenance holds no scan id (``None``), never ``""``."""
    from lakeloom.core.drift_service import _Baseline, _Fresh  # pyright: ignore[reportPrivateUsage]

    warnings: list[str] = []
    assert DriftService._scan_id(_Fresh(), _Baseline(), [], warnings) is None  # pyright: ignore[reportPrivateUsage]
    assert warnings == []


async def test_concurrent_detects_create_one_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both detects plan before either takes the model lock; the pending re-check under the
    lock (recheck_pending) turns the second into a ``conflict``, not a duplicate draft."""
    env = Env()
    env.baseline("prop_1", scan("scan_1", ORDERS_V1), ORDER_OPS, [ORDERS])
    env.scans.lake["lake"] = [table("orders", [("id", "bigint"), ("total", "decimal")])]
    real = env.proposals.propose_operations
    arrived: list[None] = []
    both = asyncio.Event()

    async def barrier(*args: Any, **kw: Any) -> Any:
        arrived.append(None)
        if len(arrived) == 2:
            both.set()
        await both.wait()
        return await real(*args, **kw)

    monkeypatch.setattr(env.proposals, "propose_operations", barrier)
    results = await asyncio.gather(env.detect(), env.detect(), return_exceptions=True)

    reports = [r for r in results if not isinstance(r, BaseException)]
    errors = [r for r in results if isinstance(r, LakeloomError)]
    assert len(reports) == 1 and reports[0].proposal is not None
    assert [e.code for e in errors] == ["conflict"]
    (call,) = env.proposals.calls
    assert call["recheck_pending"] is True
