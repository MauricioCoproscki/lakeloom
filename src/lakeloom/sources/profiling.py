# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportMissingTypeStubs=false, reportAttributeAccessIssue=false
"""Streaming table profiles with privacy controls (FR-SRC-08, FR-SRC-09).

Profiles are computed where the data lives: the dataset is scanned once, batch by batch
(never materialised as a whole table), and only aggregates leave this module. Privacy
rules (ADR 0008):

* a column is *sensitive* when any of its tags matches a ``policy.sensitive_tags`` glob;
  sensitive columns never expose values: no top-k and no min/max of any type;
* with ``policy.samples`` off (the default) no top-k is produced and string-like columns
  get no min/max; numeric, date and timestamp min/max are aggregates and stay;
* distinct values are tracked exactly up to ``low_cardinality + 1`` per column; past that
  the column reports ``distinct_count=None`` ("more than low_cardinality") and no top-k;
* rendered values are truncated to ``value_max_length`` characters plus ``…``.
"""

from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from lakeloom.sources.base import ProfilePolicy
from lakeloom.sources.models import ColumnProfile, TableDescription, TableProfile
from lakeloom.sources.privacy import (
    ValueKind,
    allow_counts,
    allow_minmax,
    is_sensitive,
    rank_top_k,
    render,
    truncate,
    withheld_reason,
)

__all__ = ["profile_arrow_dataset"]


def _arrow_kind(t: pa.DataType) -> ValueKind:
    if pa.types.is_integer(t) or pa.types.is_floating(t) or pa.types.is_decimal(t):
        return "number"
    if pa.types.is_date(t) or pa.types.is_timestamp(t) or pa.types.is_time(t):
        return "temporal"
    if pa.types.is_string(t) or pa.types.is_large_string(t) or pa.types.is_string_view(t):
        return "string"
    if pa.types.is_boolean(t):
        return "boolean"
    return "other"


@dataclass
class _ColumnState:
    name: str
    kind: ValueKind
    sensitive: bool
    want_minmax: bool
    want_counts: bool
    nulls: int = 0
    min: Any = None
    max: Any = None
    counts: dict[Any, int] | None = field(default_factory=dict)

    def update(self, array: pa.Array | pa.ChunkedArray) -> None:
        self.nulls += array.null_count
        if self.want_minmax and len(array) > array.null_count:
            mm = pc.min_max(array)
            lo, hi = mm["min"].as_py(), mm["max"].as_py()
            if lo is not None and (self.min is None or lo < self.min):
                self.min = lo
            if hi is not None and (self.max is None or hi > self.max):
                self.max = hi

    def update_counts(self, array: pa.Array | pa.ChunkedArray, cap: int) -> None:
        if self.counts is None:
            return
        for item in pc.value_counts(array.drop_null()).to_pylist():
            value = item["values"]
            self.counts[value] = self.counts.get(value, 0) + item["counts"]
        if len(self.counts) > cap:
            self.counts = None  # high cardinality: stop tracking, bounds memory


def profile_arrow_dataset(
    dataset: Any,
    description: TableDescription,
    policy: ProfilePolicy,
    *,
    batch_size: int = 65536,
) -> TableProfile:
    """Profile ``dataset`` in one streaming pass. Synchronous: wrap in ``to_thread``."""
    schema = dataset.schema
    cap = policy.low_cardinality + 1
    states: list[_ColumnState] = []
    for info in description.columns:
        if info.name not in schema.names:
            continue
        kind = _arrow_kind(schema.field(info.name).type)
        sensitive = is_sensitive(info.tags, policy.sensitive_tags)
        counts = allow_counts(kind)
        states.append(
            _ColumnState(
                name=info.name,
                kind=kind,
                sensitive=sensitive,
                want_minmax=allow_minmax(kind, sensitive=sensitive, policy=policy),
                want_counts=counts,
                counts={} if counts else None,
            )
        )
    columns = [s.name for s in states]
    row_count = 0
    for batch in dataset.to_batches(columns=columns, batch_size=batch_size):
        row_count += batch.num_rows
        for state in states:
            array = batch.column(state.name)
            state.update(array)
            state.update_counts(array, cap)
    out: list[ColumnProfile] = []
    for state in states:
        distinct = len(state.counts) if state.counts is not None else None
        withheld = withheld_reason(sensitive=state.sensitive, policy=policy)
        top_k: list[tuple[str, int]] | None = None
        if withheld is None and state.counts is not None:
            top_k = rank_top_k(state.counts, policy)
        lo = render(state.min) if state.min is not None else None
        hi = render(state.max) if state.max is not None else None
        if state.kind == "string":
            lo = truncate(lo, policy.value_max_length) if lo is not None else None
            hi = truncate(hi, policy.value_max_length) if hi is not None else None
        out.append(
            ColumnProfile(
                name=state.name,
                null_rate=state.nulls / row_count if row_count else None,
                distinct_count=distinct,
                min=lo,
                max=hi,
                top_k=top_k,
                values_withheld=withheld,
            )
        )
    return TableProfile(ref=description.ref, row_count=row_count, columns=out)
