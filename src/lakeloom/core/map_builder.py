"""MAP (Metadata Application Profile) builder for the Ontology Pipeline (stage 2).

A MAP documents the semantic contract between a physical data source and an ontology:
which column/table in the lake is realized by which concept.  It bridges the physical
world (the lake scan record) and the conceptual world (the SKOS graph).

Stages of the Ontology Pipeline (ANSI Z39.19 / ISO 25964):
  CV → **MAP** → Taxonomy → Thesaurus → Ontology → Knowledge Graph

This module is I/O-free: all functions accept plain Python objects (a ScanRecord and a
realization dict).  The MCP tools in ``server/tools_map.py`` collect the live data from
``deps.scans`` and ``deps.models`` / ``deps.om`` and pass it here.

Terminology:
  canonical_layer     — concept-centric view: concept_fqn → list of physical fields
  implementation_layer — field-centric view: "table.column" → concept_fqn
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from lakeloom.state.scans import ScanRecord

__all__ = [
    "ColumnEntry",
    "TableMapEntry",
    "MapProfile",
    "MapIssue",
    "MapReport",
    "build_map",
    "validate_map",
    "export_map_markdown",
    "export_map_json",
]

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnEntry:
    """One column in the physical schema, optionally linked to a concept."""

    name: str
    data_type: str
    concept_fqn: str | None = None


@dataclass
class TableMapEntry:
    """Mapping for one table in the scan, correlating columns with concepts."""

    om_fqn: str
    local_name: str
    source: str
    columns: list[ColumnEntry] = field(default_factory=list)
    table_concept: str | None = None

    @property
    def mapped_columns(self) -> list[ColumnEntry]:
        return [c for c in self.columns if c.concept_fqn is not None]

    @property
    def unmapped_columns(self) -> list[ColumnEntry]:
        return [c for c in self.columns if c.concept_fqn is None]

    @property
    def is_mapped(self) -> bool:
        return self.table_concept is not None or bool(self.mapped_columns)


@dataclass
class MapProfile:
    """Complete MAP for one source-model pair.

    Built by :func:`build_map` from a :class:`~lakeloom.state.scans.ScanRecord` plus
    the realization index gathered from the live SKOS graph / OM.
    """

    source_id: str
    scan_id: str
    model_slug: str
    created_at: datetime
    table_entries: list[TableMapEntry] = field(default_factory=list)

    # ---------------------------------------------------------------------------
    # Derived views
    # ---------------------------------------------------------------------------

    def canonical_layer(self) -> dict[str, list[str]]:
        """concept_fqn → list of ``"om_fqn.column"`` or ``"om_fqn"`` strings."""
        result: dict[str, list[str]] = {}
        for entry in self.table_entries:
            if entry.table_concept:
                result.setdefault(entry.table_concept, []).append(entry.om_fqn)
            for col in entry.mapped_columns:
                assert col.concept_fqn is not None
                result.setdefault(col.concept_fqn, []).append(
                    f"{entry.om_fqn}.{col.name}"
                )
        return result

    def implementation_layer(self) -> dict[str, str]:
        """``"om_fqn.column"`` or ``"om_fqn"`` → concept_fqn."""
        result: dict[str, str] = {}
        for entry in self.table_entries:
            if entry.table_concept:
                result[entry.om_fqn] = entry.table_concept
            for col in entry.mapped_columns:
                assert col.concept_fqn is not None
                result[f"{entry.om_fqn}.{col.name}"] = col.concept_fqn
        return result

    @property
    def total_tables(self) -> int:
        return len(self.table_entries)

    @property
    def mapped_tables(self) -> int:
        return sum(1 for e in self.table_entries if e.is_mapped)

    @property
    def total_columns(self) -> int:
        return sum(len(e.columns) for e in self.table_entries)

    @property
    def mapped_columns_count(self) -> int:
        return sum(len(e.mapped_columns) for e in self.table_entries)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MapIssue:
    """One validation finding from :func:`validate_map`."""

    level: Literal["error", "warning"]
    rule: str
    message: str
    subject: str | None = None


@dataclass
class MapReport:
    """Aggregate result of :func:`validate_map`."""

    total_tables: int = 0
    mapped_tables: int = 0
    total_columns: int = 0
    mapped_columns: int = 0
    issues: list[MapIssue] = field(default_factory=list)

    @property
    def coverage_ratio(self) -> float:
        if self.total_columns == 0:
            return 1.0
        return self.mapped_columns / self.total_columns

    @property
    def errors(self) -> list[MapIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[MapIssue]:
        return [i for i in self.issues if i.level == "warning"]

    def to_markdown(self) -> str:
        lines = [
            "## MAP Validation Report",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Tables | {self.total_tables} |",
            f"| Mapped tables | {self.mapped_tables} |",
            f"| Columns | {self.total_columns} |",
            f"| Mapped columns | {self.mapped_columns} |",
            f"| Coverage | {self.coverage_ratio:.0%} |",
            f"| Errors | {len(self.errors)} |",
            f"| Warnings | {len(self.warnings)} |",
            "",
        ]
        if not self.issues:
            lines.append("✅ No issues found.")
            return "\n".join(lines)
        for issue in self.issues:
            icon = "❌" if issue.level == "error" else "⚠️"
            subject = f" (`{issue.subject}`)" if issue.subject else ""
            lines.append(f"- {icon} **{issue.rule}**{subject}: {issue.message}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_map(
    scan_record: ScanRecord,
    *,
    model_slug: str,
    realizations: dict[str, list[str]],
    created_at: datetime | None = None,
) -> MapProfile:
    """Build a :class:`MapProfile` from a scan record plus a realization index.

    Parameters
    ----------
    scan_record:
        A :class:`~lakeloom.state.scans.ScanRecord` with the physical schema.
    model_slug:
        The target ontology model (e.g. ``"retail"``).
    realizations:
        ``{concept_fqn: [asset_fqn, ...]}`` — the asset FQNs realizing each
        concept.  An asset FQN can be a table FQN or a ``"table.column"``-style
        string.  Collected by the caller from the live graph + OM (see
        ``server/tools_map.py``).
    created_at:
        Optional timestamp for the MAP profile; defaults to now.
    """
    at = created_at or datetime.now(UTC)

    # Invert: asset_fqn → concept_fqn (last concept wins if there are multiple)
    asset_to_concept: dict[str, str] = {}
    for concept_fqn, asset_fqns in realizations.items():
        for asset_fqn in asset_fqns:
            asset_to_concept[asset_fqn] = concept_fqn

    table_entries: list[TableMapEntry] = []
    for scanned in scan_record.tables:
        om_fqn = scanned.om_fqn or scanned.ref.full_name
        local_name = scanned.ref.full_name

        # Table-level concept (the whole table is the asset).
        table_concept = asset_to_concept.get(om_fqn)

        columns: list[ColumnEntry] = []
        if scanned.description is not None:
            for col_info in scanned.description.columns:
                col_key = f"{om_fqn}.{col_info.name}"
                concept = asset_to_concept.get(col_key)
                columns.append(
                    ColumnEntry(
                        name=col_info.name,
                        data_type=col_info.data_type,
                        concept_fqn=concept,
                    )
                )

        table_entries.append(
            TableMapEntry(
                om_fqn=om_fqn,
                local_name=local_name,
                source=scan_record.source,
                columns=columns,
                table_concept=table_concept,
            )
        )

    return MapProfile(
        source_id=scan_record.source,
        scan_id=scan_record.id,
        model_slug=model_slug,
        created_at=at,
        table_entries=table_entries,
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_map(profile: MapProfile) -> MapReport:
    """Validate a :class:`MapProfile` and return a :class:`MapReport`.

    Rules:
      - MAP-01 (warning): tables with no concept mapping at all.
      - MAP-02 (warning): columns with no concept mapping.
      - MAP-03 (warning): model is empty (zero total tables).
    """
    issues: list[MapIssue] = []

    if profile.total_tables == 0:
        issues.append(
            MapIssue(
                level="warning",
                rule="MAP-03",
                message="Scan record contains no tables; nothing to map.",
            )
        )

    for entry in profile.table_entries:
        if not entry.is_mapped:
            issues.append(
                MapIssue(
                    level="warning",
                    rule="MAP-01",
                    subject=entry.om_fqn,
                    message=f"table {entry.om_fqn!r} has no concept mapping",
                )
            )
        elif entry.unmapped_columns:
            unmapped = ", ".join(c.name for c in entry.unmapped_columns[:5])
            extra = f" and {len(entry.unmapped_columns) - 5} more" if len(entry.unmapped_columns) > 5 else ""
            issues.append(
                MapIssue(
                    level="warning",
                    rule="MAP-02",
                    subject=entry.om_fqn,
                    message=f"{entry.om_fqn!r}: unmapped columns: {unmapped}{extra}",
                )
            )

    return MapReport(
        total_tables=profile.total_tables,
        mapped_tables=profile.mapped_tables,
        total_columns=profile.total_columns,
        mapped_columns=profile.mapped_columns_count,
        issues=issues,
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_map_markdown(profile: MapProfile) -> str:
    """Human-readable Markdown document for a :class:`MapProfile`."""
    lines = [
        f"# MAP: `{profile.source_id}` → `{profile.model_slug}`",
        "",
        f"- **Scan**: `{profile.scan_id}`",
        f"- **Generated**: {profile.created_at.strftime('%Y-%m-%d %H:%M UTC')}",
        f"- **Tables**: {profile.total_tables} ({profile.mapped_tables} mapped)",
        f"- **Columns**: {profile.total_columns} ({profile.mapped_columns_count} mapped)",
        "",
    ]

    canon = profile.canonical_layer()
    if canon:
        lines += ["## Canonical Layer (concept → fields)", ""]
        for concept, fields in sorted(canon.items()):
            cname = concept.rsplit(".", 1)[-1]
            lines.append(f"### `{cname}` (`{concept}`)")
            for f_ in sorted(fields):
                lines.append(f"- `{f_}`")
            lines.append("")

    lines += ["## Implementation Layer (table by table)", ""]
    for entry in profile.table_entries:
        mapped = len(entry.mapped_columns)
        total = len(entry.columns)
        lines.append(f"### `{entry.om_fqn}` ({mapped}/{total} columns mapped)")
        if entry.table_concept:
            lines.append(f"- Table concept: `{entry.table_concept}`")
        for col in entry.columns:
            status = f"`{col.concept_fqn}`" if col.concept_fqn else "_unmapped_"
            lines.append(f"- `{col.name}` ({col.data_type}) → {status}")
        lines.append("")

    return "\n".join(lines)


def export_map_json(profile: MapProfile) -> str:
    """JSON representation of a :class:`MapProfile` (canonical + implementation layers)."""
    data = {
        "source_id": profile.source_id,
        "scan_id": profile.scan_id,
        "model_slug": profile.model_slug,
        "created_at": profile.created_at.isoformat(),
        "canonical_layer": profile.canonical_layer(),
        "implementation_layer": profile.implementation_layer(),
        "tables": [
            {
                "om_fqn": e.om_fqn,
                "local_name": e.local_name,
                "source": e.source,
                "table_concept": e.table_concept,
                "columns": [
                    {
                        "name": c.name,
                        "data_type": c.data_type,
                        "concept_fqn": c.concept_fqn,
                    }
                    for c in e.columns
                ],
            }
            for e in profile.table_entries
        ],
    }
    return json.dumps(data, indent=2, ensure_ascii=False)
