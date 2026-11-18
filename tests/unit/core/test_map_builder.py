"""Unit tests for map_builder (M5c)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lakeloom.core.map_builder import (
    ColumnEntry,
    MapProfile,
    TableMapEntry,
    build_map,
    export_map_json,
    export_map_markdown,
    validate_map,
)
from lakeloom.sources.models import ColumnInfo, TableDescription, TableVersion
from lakeloom.sources.models import TableRef as SourceTableRef
from lakeloom.state.scans import ScannedTable, ScanRecord


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _col(name: str, data_type: str = "string", position: int = 0) -> ColumnInfo:
    return ColumnInfo(name=name, data_type=data_type, nullable=True, position=position)


def _table(name: str, columns: list[ColumnInfo], om_fqn: str | None = None) -> ScannedTable:
    ref = SourceTableRef(schema="sales", name=name)
    t = ScannedTable(
        ref=ref,
        version=TableVersion(version=1),
        description=TableDescription(ref=ref, columns=columns, version=TableVersion(version=1)),
    )
    if om_fqn:
        t.om_fqn = om_fqn
    return t


def _scan(*tables: ScannedTable) -> ScanRecord:
    return ScanRecord(
        id="scan-1",
        source="lake",
        created_at=NOW,
        principal_subject="agent-1",
        principal_type="agent",
        status="completed",
        tables=list(tables),
    )


# ---------------------------------------------------------------------------
# build_map
# ---------------------------------------------------------------------------


class TestBuildMap:
    def test_empty_scan(self) -> None:
        scan = _scan()
        profile = build_map(scan, model_slug="retail", realizations={})
        assert profile.total_tables == 0
        assert profile.mapped_tables == 0

    def test_single_table_no_realizations(self) -> None:
        scan = _scan(_table("orders", [_col("order_id", "bigint"), _col("status")]))
        profile = build_map(scan, model_slug="retail", realizations={})
        assert profile.total_tables == 1
        assert profile.mapped_tables == 0
        assert profile.total_columns == 2
        assert profile.mapped_columns_count == 0

    def test_column_level_realization(self) -> None:
        scan = _scan(
            _table(
                "orders",
                [_col("order_id", "bigint"), _col("status")],
                om_fqn="svc.sales.orders",
            )
        )
        realizations = {"retail.Order": ["svc.sales.orders.order_id"]}
        profile = build_map(scan, model_slug="retail", realizations=realizations)
        entry = profile.table_entries[0]
        mapped = {c.name: c.concept_fqn for c in entry.columns}
        assert mapped["order_id"] == "retail.Order"
        assert mapped["status"] is None

    def test_table_level_realization(self) -> None:
        scan = _scan(_table("customers", [_col("id", "bigint")], om_fqn="svc.sales.customers"))
        realizations = {"retail.Customer": ["svc.sales.customers"]}
        profile = build_map(scan, model_slug="retail", realizations=realizations)
        entry = profile.table_entries[0]
        assert entry.table_concept == "retail.Customer"

    def test_canonical_layer_populated(self) -> None:
        scan = _scan(
            _table("orders", [_col("id", "bigint")], om_fqn="svc.sales.orders"),
            _table("customers", [_col("id", "bigint")], om_fqn="svc.sales.customers"),
        )
        realizations = {
            "retail.Order": ["svc.sales.orders"],
            "retail.Customer": ["svc.sales.customers.id"],
        }
        profile = build_map(scan, model_slug="retail", realizations=realizations)
        canon = profile.canonical_layer()
        assert "retail.Order" in canon
        assert "retail.Customer" in canon

    def test_implementation_layer_populated(self) -> None:
        scan = _scan(_table("orders", [_col("id", "bigint")], om_fqn="svc.sales.orders"))
        realizations = {"retail.Order": ["svc.sales.orders.id"]}
        profile = build_map(scan, model_slug="retail", realizations=realizations)
        impl = profile.implementation_layer()
        assert impl["svc.sales.orders.id"] == "retail.Order"

    def test_uses_ref_full_name_when_no_om_fqn(self) -> None:
        scan = _scan(_table("orders", []))
        profile = build_map(scan, model_slug="retail", realizations={})
        assert profile.table_entries[0].om_fqn == "sales.orders"

    def test_mapped_tables_count(self) -> None:
        scan = _scan(
            _table("orders", [_col("id")], om_fqn="svc.sales.orders"),
            _table("products", [_col("id")], om_fqn="svc.sales.products"),
        )
        realizations = {"retail.Order": ["svc.sales.orders.id"]}
        profile = build_map(scan, model_slug="retail", realizations=realizations)
        assert profile.mapped_tables == 1
        assert profile.total_tables == 2


# ---------------------------------------------------------------------------
# validate_map
# ---------------------------------------------------------------------------


class TestValidateMap:
    def test_all_mapped_no_issues(self) -> None:
        scan = _scan(_table("orders", [_col("id")], om_fqn="svc.sales.orders"))
        realizations = {"retail.Order": ["svc.sales.orders.id"]}
        profile = build_map(scan, model_slug="retail", realizations=realizations)
        report = validate_map(profile)
        assert not report.issues

    def test_unmapped_table_warns(self) -> None:
        scan = _scan(_table("orders", [_col("id")], om_fqn="svc.sales.orders"))
        profile = build_map(scan, model_slug="retail", realizations={})
        report = validate_map(profile)
        assert any(i.rule == "MAP-01" for i in report.issues)

    def test_unmapped_columns_warns(self) -> None:
        scan = _scan(
            _table(
                "orders",
                [_col("id"), _col("extra_col")],
                om_fqn="svc.sales.orders",
            )
        )
        # Only id is mapped, extra_col is not.
        realizations = {"retail.Order": ["svc.sales.orders.id"]}
        profile = build_map(scan, model_slug="retail", realizations=realizations)
        report = validate_map(profile)
        assert any(i.rule == "MAP-02" for i in report.issues)

    def test_empty_scan_warns(self) -> None:
        profile = build_map(_scan(), model_slug="retail", realizations={})
        report = validate_map(profile)
        assert any(i.rule == "MAP-03" for i in report.issues)

    def test_coverage_ratio(self) -> None:
        scan = _scan(
            _table("orders", [_col("id"), _col("status")], om_fqn="svc.sales.orders")
        )
        realizations = {"retail.Order": ["svc.sales.orders.id"]}
        profile = build_map(scan, model_slug="retail", realizations=realizations)
        report = validate_map(profile)
        assert abs(report.coverage_ratio - 0.5) < 0.01

    def test_to_markdown_contains_header(self) -> None:
        profile = build_map(_scan(), model_slug="retail", realizations={})
        report = validate_map(profile)
        assert "MAP Validation Report" in report.to_markdown()


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


class TestExport:
    def _profile(self) -> MapProfile:
        scan = _scan(
            _table("orders", [_col("id", "bigint"), _col("status")], om_fqn="svc.sales.orders")
        )
        return build_map(
            scan,
            model_slug="retail",
            realizations={"retail.Order": ["svc.sales.orders.id"]},
            created_at=NOW,
        )

    def test_markdown_contains_canonical_layer(self) -> None:
        md = export_map_markdown(self._profile())
        assert "Canonical Layer" in md
        assert "retail.Order" in md

    def test_markdown_contains_implementation_layer(self) -> None:
        md = export_map_markdown(self._profile())
        assert "Implementation Layer" in md
        assert "svc.sales.orders" in md

    def test_json_is_parseable(self) -> None:
        import json

        data = json.loads(export_map_json(self._profile()))
        assert data["model_slug"] == "retail"
        assert "canonical_layer" in data
        assert "implementation_layer" in data

    def test_json_implementation_layer(self) -> None:
        import json

        data = json.loads(export_map_json(self._profile()))
        impl = data["implementation_layer"]
        assert impl.get("svc.sales.orders.id") == "retail.Order"
