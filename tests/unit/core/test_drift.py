"""Drift: the pure scan diff and its operations (FR-PRO-07)."""

from datetime import UTC, datetime
from typing import Any

from lakeloom.core.drift import (
    TableBinding,
    TableDrift,
    bind_tables,
    comment_changes,
    diff_scans,
    drift_operations,
    resolve_moves,
)
from lakeloom.core.import_terms import (
    AttributeTerm,
    AxiomTerm,
    ConceptTerm,
    RelationTerm,
    TermSet,
)
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
    op_key,
)
from lakeloom.core.proposals import Proposal, ProposalPart, Provenance
from lakeloom.sources.models import (
    ColumnInfo,
    Constraint,
    TableDescription,
    TableRef,
    TableVersion,
)
from lakeloom.state.scans import ScannedTable, ScanRecord
from lakeloom.untrusted import UntrustedText

NS = "http://x/ontology/shop/"
NOW = datetime(2026, 11, 16, tzinfo=UTC)
POLICY = MappingPolicy(model="shop")

type Col = tuple[str, str] | tuple[str, str, str]


def table(
    name: str,
    cols: list[Col],
    *,
    version: int = 2,
    pk: list[str] | None = None,
    fks: list[tuple[list[str], str]] | None = None,
) -> ScannedTable:
    ref = TableRef(catalog="c", schema="s", name=name)
    columns = [
        ColumnInfo(
            name=c[0],
            data_type=c[1],
            nullable=True,
            position=i,
            comment=UntrustedText(text=c[2], source="lake") if len(c) > 2 else None,
        )
        for i, c in enumerate(cols)
    ]
    constraints = [Constraint(kind="primary_key", columns=pk)] if pk else []
    for cols_, target in fks or []:
        constraints.append(
            Constraint(
                kind="foreign_key",
                columns=cols_,
                ref_table=TableRef(catalog="c", schema="s", name=target),
                ref_columns=["id"],
            )
        )
    return ScannedTable(
        ref=ref,
        version=TableVersion(version=version),
        om_fqn=f"svc.c.s.{name}",
        description=TableDescription(
            ref=ref, columns=columns, constraints=constraints, version=TableVersion(version=version)
        ),
    )


def by_fqn(*tables: ScannedTable) -> dict[str, ScannedTable]:
    return {t.ref.full_name: t for t in tables}


def record(*tables: ScannedTable) -> ScanRecord:
    return ScanRecord(
        id="scan_new",
        source="lake",
        created_at=NOW,
        principal_subject="agent-7",
        principal_type="agent",
        status="completed",
        tables=list(tables),
    )


def terms(
    concepts: list[str],
    attributes: list[tuple[str, str, str]] = (),  # type: ignore[assignment]
    relations: list[tuple[str, str, str]] = (),  # type: ignore[assignment]
    axioms: dict[str, str] | None = None,
    *,
    unit: dict[tuple[str, str], str] | None = None,
    identifiers: set[tuple[str, str]] | None = None,
) -> TermSet:
    return TermSet(
        concepts={
            c: ConceptTerm(
                name=c,
                display_name=None,
                description=None,
                parent=None,
                synonyms=(),
                iri=f"{NS}{c}",
            )
            for c in concepts
        },
        attributes={
            (c, a): AttributeTerm(
                concept=c,
                name=a,
                datatype=dt,
                is_identifier=(c, a) in (identifiers or set()),
                allowed_values=(),
                unit=(unit or {}).get((c, a)),
                description=None,
                iri=f"{NS}{c}.{a}",
            )
            for c, a, dt in attributes
        },
        relations={
            (s, r, t): RelationTerm(
                source=s, relation_type=r, target=t, description=None, iri=f"{NS}{s}.{r}"
            )
            for s, r, t in relations
        },
        mappings=set(),
        axioms={
            c: AxiomTerm(concept=c, name=n, expressions=(("id", "EXACT", 1),))
            for c, n in (axioms or {}).items()
        },
        namespace=NS,
    )


def ops_for(
    baseline: dict[str, ScannedTable],
    current: dict[str, ScannedTable],
    bindings: dict[str, TableBinding],
    model_terms: TermSet,
    *,
    errors: dict[str, str] | None = None,
    versions: dict[str, float] | None = None,
) -> tuple[list[TableDrift], list[ProposalOperation], list[ProposalOperation], list[str]]:
    scan = record(*current.values())
    drifts = diff_scans(baseline, current, errors or {})
    drifts, bindings, move_warnings = resolve_moves(
        drifts, baseline, current, bindings, scan, POLICY
    )
    rewritten, added, warnings = drift_operations(
        drifts, scan, bindings, model_terms, POLICY, versions or {}
    )
    return drifts, rewritten, added, sorted([*move_warnings, *warnings])


ORDERS = "c.s.orders"
BOUND = {ORDERS: TableBinding(concept="PurchaseOrder", attributes={"id": "orderId", "x": "x"})}


def test_added_column_yields_add_attribute_on_bound_concept() -> None:
    old = table("orders", [("id", "bigint")])
    new = table("orders", [("id", "bigint"), ("total", "decimal(10,2)")])
    drifts, rewritten, added, _ = ops_for(
        by_fqn(old),
        by_fqn(new),
        BOUND,
        terms(["PurchaseOrder"], [("PurchaseOrder", "orderId", "xsd:long")]),
    )

    (d,) = drifts
    assert (d.fqn, d.status, d.added_columns) == (ORDERS, "changed", ["total"])
    assert added == []
    (op,) = rewritten
    assert isinstance(op, AddAttribute)
    assert (op.concept, op.name, op.datatype) == ("PurchaseOrder", "total", "xsd:decimal")
    assert "R-COLUMN" in op.rule_ids
    assert op.evidence[0] == Evidence(kind="column", ref=f"{ORDERS}.total", detail="decimal(10,2)")


def test_removed_column_yields_remove_attribute_only_if_in_model() -> None:
    old = table("orders", [("id", "bigint"), ("x", "string"), ("gone", "string")])
    new = table("orders", [("id", "bigint")])
    binding = {
        ORDERS: TableBinding(
            concept="PurchaseOrder", attributes={"id": "orderId", "x": "x", "gone": "gone"}
        )
    }
    model = terms(["PurchaseOrder"], [("PurchaseOrder", "x", "xsd:string")])
    drifts, rewritten, _, _ = ops_for(by_fqn(old), by_fqn(new), binding, model)

    assert drifts[0].removed_columns == ["x", "gone"]
    (op,) = rewritten  # "gone" is not in the model graph
    assert isinstance(op, RemoveAttribute)
    assert (op.concept, op.name, op.rule_ids) == ("PurchaseOrder", "x", ["R-DRIFT-REMOVE"])
    assert op.evidence == [Evidence(kind="column", ref=f"{ORDERS}.x")]


def test_type_change_is_upsert() -> None:
    old = table("orders", [("id", "int"), ("qty", "int")], pk=["id"])
    new = table("orders", [("id", "bigint"), ("qty", "int")])  # the PK is no longer declared
    binding = {ORDERS: TableBinding(concept="PurchaseOrder", attributes={"id": "orderId"})}
    model = terms(
        ["PurchaseOrder"],
        [("PurchaseOrder", "orderId", "xsd:int")],
        unit={("PurchaseOrder", "orderId"): "unit:NUM"},
        identifiers={("PurchaseOrder", "orderId")},
    )
    drifts, rewritten, _, _ = ops_for(by_fqn(old), by_fqn(new), binding, model)

    assert drifts[0].changed_columns == ["id"]
    (op,) = rewritten
    assert isinstance(op, AddAttribute)
    assert (op.concept, op.name, op.datatype) == ("PurchaseOrder", "orderId", "xsd:long")
    # the upsert replaces every field in OM: the model's unit and identifier flag are kept
    assert op.is_identifier is True and op.unit == "unit:NUM"


def test_type_change_to_same_datatype_proposes_nothing() -> None:
    old = table("orders", [("id", "varchar(10)")])
    new = table("orders", [("id", "varchar(20)")])
    model = terms(["PurchaseOrder"], [("PurchaseOrder", "orderId", "xsd:string")])
    drifts, rewritten, _, warnings = ops_for(by_fqn(old), by_fqn(new), BOUND, model)

    assert drifts[0].changed_columns == ["id"]
    assert rewritten == []
    assert any("c.s.orders.id" in w and "already matches" in w for w in warnings)


def test_comment_change_updates_description() -> None:
    """Amended by Task 1 (ADR 0016 R2): rc1 ignores description changes on an existing
    native property, so a comment change is reported and warned, never proposed."""
    old = table("orders", [("id", "bigint", "old text")])
    new = table("orders", [("id", "bigint", "new text")])
    model = terms(["PurchaseOrder"], [("PurchaseOrder", "orderId", "xsd:long")])
    drifts, rewritten, _, _ = ops_for(by_fqn(old), by_fqn(new), BOUND, model)

    assert (drifts[0].status, drifts[0].changed_columns) == ("changed", ["id"])
    assert rewritten == []
    (w,) = comment_changes(by_fqn(old), by_fqn(new))
    assert "1 column comments changed" in w and f"{ORDERS}.id" in w
    assert "OpenMetadata 2.1.0-rc1" in w


def test_rename_candidate_same_type_and_position() -> None:
    old = table("orders", [("id", "bigint"), ("a", "int"), ("b", "string")])
    new = table("orders", [("id", "bigint"), ("a2", "int"), ("b2", "date")])
    binding = {ORDERS: TableBinding(concept="PurchaseOrder", attributes={"a": "a", "b": "b"})}
    model = terms(
        ["PurchaseOrder"], [("PurchaseOrder", "a", "xsd:int"), ("PurchaseOrder", "b", "xsd:string")]
    )
    drifts, rewritten, _, _ = ops_for(by_fqn(old), by_fqn(new), binding, model)

    (d,) = drifts
    assert d.renames == [(f"{ORDERS}.a", f"{ORDERS}.a2")]  # b -> b2 changes type: no candidate
    keys = [op_key(op) for op in rewritten]
    assert keys == [
        "remove-attr:PurchaseOrder.a",
        "remove-attr:PurchaseOrder.b",
        "attr:PurchaseOrder.a2",
        "attr:PurchaseOrder.b2",
    ]
    detail = f"possible rename of {ORDERS}.a to {ORDERS}.a2"
    remove_a, remove_b, add_a2, add_b2 = rewritten
    assert remove_a.rule_ids == ["R-DRIFT-RENAME"]
    assert Evidence(kind="column", ref=f"{ORDERS}.a", detail=detail) in remove_a.evidence
    assert Evidence(kind="column", ref=f"{ORDERS}.a2", detail=detail) in add_a2.evidence
    assert remove_b.rule_ids == ["R-DRIFT-REMOVE"]
    assert all(e.detail is None or "rename" not in e.detail for e in add_b2.evidence)


def test_table_rename_candidate() -> None:
    cols: list[Col] = [("id", "bigint"), ("total", "decimal")]
    old = table("orders", cols)
    new = table("purchase_orders", cols)
    other = table("customers", [("id", "bigint")])
    binding = {ORDERS: TableBinding(concept="Order", attributes={"id": "id", "total": "total"})}
    model = terms(["Order"], [("Order", "id", "xsd:long"), ("Order", "total", "xsd:decimal")])
    drifts, rewritten, added, _ = ops_for(
        by_fqn(old), by_fqn(new, other), binding, model, versions={"Order": 0.4}
    )

    status = {d.fqn: (d.status, d.renames) for d in drifts}
    assert status == {
        "c.s.customers": ("added", []),
        ORDERS: ("removed", []),
        "c.s.purchase_orders": ("added", [(ORDERS, "c.s.purchase_orders")]),
    }
    detail = f"possible rename of {ORDERS} to c.s.purchase_orders"
    assert all(op.rule_ids == ["R-DRIFT-RENAME"] for op in rewritten)
    assert all(Evidence(kind="table", ref=ORDERS, detail=detail) in op.evidence for op in rewritten)
    concept = next(op for op in added if isinstance(op, AddConcept) and op.name == "PurchaseOrder")
    assert Evidence(kind="table", ref="c.s.purchase_orders", detail=detail) in concept.evidence
    customer = next(op for op in added if isinstance(op, AddConcept) and op.name == "Customer")
    assert all(e.detail is None or "rename" not in e.detail for e in customer.evidence)


def test_removed_table_removes_members_then_concept() -> None:
    old = table("orders", [("id", "bigint"), ("customer_id", "bigint")])
    binding = {ORDERS: TableBinding(concept="Order", attributes={"id": "id"})}
    model = terms(
        ["Order", "Customer", "Invoice"],
        [("Order", "id", "xsd:long"), ("Order", "customerId", "xsd:long")],
        [("Order", "hasCustomer", "Customer"), ("Invoice", "hasOrder", "Order")],
        {"Order": "OrderPrimaryKey"},
    )
    drifts, rewritten, added, _ = ops_for(by_fqn(old), {}, binding, model, versions={"Order": 0.3})

    assert [(d.fqn, d.status) for d in drifts] == [(ORDERS, "removed")]
    assert added == []
    assert [op_key(op) for op in rewritten] == [
        "remove-rel:Invoice|hasOrder|Order",
        "remove-rel:Order|hasCustomer|Customer",
        "remove-axiom:Order.OrderPrimaryKey",
        "remove-attr:Order.customerId",
        "remove-attr:Order.id",
        "remove-concept:Order",
    ]
    assert isinstance(rewritten[0], RemoveRelation) and isinstance(rewritten[2], RemoveAxiom)
    concept = rewritten[-1]
    assert isinstance(concept, RemoveConcept) and concept.base_version == "0.3"
    assert all(op.rule_ids == ["R-DRIFT-REMOVE"] for op in rewritten)
    assert all(op.evidence == [Evidence(kind="table", ref=ORDERS)] for op in rewritten)


def test_removed_table_not_in_model_proposes_nothing() -> None:
    old = table("orders", [("id", "bigint")])
    binding = {ORDERS: TableBinding(concept="Order", attributes={})}
    drifts, rewritten, _, _ = ops_for(by_fqn(old), {}, binding, terms(["Customer"]))

    assert drifts[0].status == "removed" and rewritten == []


def test_error_table_never_removed() -> None:
    old = table("orders", [("id", "bigint")])
    binding = {ORDERS: TableBinding(concept="Order", attributes={"id": "id"})}
    model = terms(["Order"], [("Order", "id", "xsd:long")])
    drifts, rewritten, added, _ = ops_for(
        by_fqn(old), {}, binding, model, errors={ORDERS: "upstream_unavailable: down"},
        versions={"Order": 0.2},
    )  # fmt: skip

    (d,) = drifts
    assert (d.status, d.error) == ("error", "upstream_unavailable: down")
    assert rewritten == [] and added == []


def test_new_fk_column_adds_relation() -> None:
    customers = table("customers", [("id", "bigint")])
    old = table("orders", [("id", "bigint")])
    new = table(
        "orders",
        [("id", "bigint"), ("customer_id", "bigint")],
        fks=[(["customer_id"], "customers")],
    )
    binding = {
        ORDERS: TableBinding(concept="PurchaseOrder", attributes={"id": "id"}),
        "c.s.customers": TableBinding(concept="Client", attributes={"id": "id"}),
    }
    model = terms(["PurchaseOrder", "Client"])
    _, rewritten, _, _ = ops_for(by_fqn(old, customers), by_fqn(new, customers), binding, model)

    keys = [op_key(op) for op in rewritten]
    # the mapper's relation name (from the scan), with both ends on the bound concepts
    assert keys == ["attr:PurchaseOrder.customer_id", "rel:PurchaseOrder|hasCustomer|Client"]
    rel = rewritten[1]
    assert isinstance(rel, AddRelation)
    assert (rel.source, rel.target) == ("PurchaseOrder", "Client")
    assert rel.rule_ids == ["R-FK"]


def _proposal(pid: str, ops: list[ProposalOperation]) -> Proposal:
    data: dict[str, Any] = {
        "id": pid,
        "model": "shop",
        "status": "applied",
        "parts": [ProposalPart(n=1, operations=ops)],
        "scan_id": "scan_1",
        "provenance": Provenance(
            scan_id="scan_1", principal_subject="a", principal_type="agent", at=NOW
        ),
        "created_at": NOW,
        "updated_at": NOW,
    }
    return Proposal.model_validate(data)


def test_bind_tables_prefers_attribute_evidence() -> None:
    concept = AddConcept(
        op="add_concept", name="Order", evidence=[Evidence(kind="table", ref=ORDERS)]
    )
    attr = AddAttribute(
        op="add_attribute",
        concept="shop.PurchaseOrder",
        name="orderId",
        datatype="xsd:long",
        evidence=[Evidence(kind="column", ref=f"{ORDERS}.id", detail="bigint")],
    )
    lone = AddConcept(
        op="add_concept", name="Customer", evidence=[Evidence(kind="table", ref="c.s.customers")]
    )
    later = AddAttribute(
        op="add_attribute",
        concept="PurchaseOrder",
        name="total",
        datatype="xsd:decimal",
        evidence=[Evidence(kind="column", ref=f"{ORDERS}.total")],
    )
    dropped = RemoveAttribute(
        op="remove_attribute",
        concept="PurchaseOrder",
        name="orderId",
        evidence=[Evidence(kind="column", ref=f"{ORDERS}.id")],
    )
    got = bind_tables(
        [_proposal("prop_2", [later, dropped]), _proposal("prop_1", [concept, attr, lone])]
    )

    assert got == {
        ORDERS: TableBinding(concept="PurchaseOrder", attributes={"total": "total"}),
        "c.s.customers": TableBinding(concept="Customer", attributes={}),
    }


def test_unchanged_tables_produce_nothing() -> None:
    t = table("orders", [("id", "bigint", "same")])
    drifts, rewritten, added, warnings = ops_for(
        by_fqn(t), by_fqn(t.model_copy(update={"version": TableVersion(version=9)})), BOUND,
        terms(["PurchaseOrder"]),
    )  # fmt: skip

    assert [(d.fqn, d.status) for d in drifts] == [(ORDERS, "unchanged")]
    assert rewritten == [] and added == [] and warnings == []
    assert comment_changes(by_fqn(t), by_fqn(t)) == []


def test_moved_table_is_changed_not_removed() -> None:
    old = table("orders", [("id", "bigint"), ("total", "decimal")])
    moved = ScannedTable.model_validate(
        {
            **table(
                "orders", [("id", "bigint"), ("total", "decimal"), ("extra", "string")]
            ).model_dump(by_alias=True),
            "ref": {"catalog": "c", "schema": "t", "name": "orders"},
        }
    )
    binding = {ORDERS: TableBinding(concept="Order", attributes={"id": "id", "total": "total"})}
    model = terms(["Order"], [("Order", "id", "xsd:long"), ("Order", "total", "xsd:decimal")])
    drifts, rewritten, added, _ = ops_for(
        by_fqn(old), by_fqn(moved), binding, model, versions={"Order": 0.4}
    )

    status = {d.fqn: (d.status, d.renames, d.added_columns) for d in drifts}
    assert status == {
        ORDERS: ("removed", [], []),
        "c.t.orders": ("changed", [(ORDERS, "c.t.orders")], ["extra"]),
    }
    # the move keeps the concept: no removal, no new concept, only the new column
    assert [op_key(op) for op in rewritten] == ["attr:Order.extra"]
    assert added == []


def test_ambiguous_move_keeps_remove_and_add_with_warning() -> None:
    a = table("orders", [("id", "bigint")])
    b = table("orders_old", [("id", "bigint"), ("x", "int")])
    moved = ScannedTable.model_validate(
        {**table("orders", [("id", "bigint")]).model_dump(by_alias=True),
         "ref": {"catalog": "c", "schema": "t", "name": "orders"}}
    )  # fmt: skip
    binding = {
        ORDERS: TableBinding(concept="Order", attributes={"id": "id"}),
        "c.s.orders_old": TableBinding(concept="Order", attributes={"id": "id"}),
    }
    model = terms(["Order"], [("Order", "id", "xsd:long")])
    drifts, rewritten, added, warnings = ops_for(
        by_fqn(a, b), by_fqn(moved), binding, model, versions={"Order": 0.4}
    )

    assert {d.fqn: d.status for d in drifts}["c.t.orders"] == "added"
    assert "remove-concept:Order" in [op_key(op) for op in rewritten]
    assert "concept:Order" in [op_key(op) for op in added]
    assert any("may have moved" in w and "c.t.orders" in w for w in warnings)


def test_comment_only_change_never_recreates_a_removed_attribute() -> None:
    old = table("orders", [("id", "bigint", "old text")])
    new = table("orders", [("id", "bigint", "new text")])
    drifts, rewritten, _, _ = ops_for(by_fqn(old), by_fqn(new), BOUND, terms(["PurchaseOrder"]))

    assert drifts[0].changed_columns == ["id"] and drifts[0].type_changed_columns == []
    assert rewritten == []


def test_comment_only_change_never_reverts_model_edits() -> None:
    old = table("orders", [("id", "bigint", "old text")])
    new = table("orders", [("id", "bigint", "new text")])
    edited = terms(
        ["PurchaseOrder"],
        [("PurchaseOrder", "orderId", "xsd:decimal")],  # edited in the model after the scan
        unit={("PurchaseOrder", "orderId"): "unit:EUR"},
    )
    _, rewritten, _, _ = ops_for(by_fqn(old), by_fqn(new), BOUND, edited)

    assert rewritten == []


def test_type_change_of_removed_attribute_is_skipped() -> None:
    old = table("orders", [("id", "int")])
    new = table("orders", [("id", "bigint")])
    _, rewritten, _, warnings = ops_for(by_fqn(old), by_fqn(new), BOUND, terms(["PurchaseOrder"]))

    assert rewritten == []
    assert any("c.s.orders.id" in w and "not in the model" in w for w in warnings)


def test_type_change_of_unbound_column_is_added() -> None:
    old = table("orders", [("id", "int"), ("qty", "int")])
    new = table("orders", [("id", "int"), ("qty", "bigint")])
    _, rewritten, _, _ = ops_for(by_fqn(old), by_fqn(new), BOUND, terms(["PurchaseOrder"]))

    assert [op_key(op) for op in rewritten] == ["attr:PurchaseOrder.qty"]
