"""Privacy rules for table profiles, shared by every profiler (FR-SRC-08, FR-SRC-09).

No pyarrow here: the Arrow profiler (``profiling.py``) and SQL profilers that push the
aggregates down to the source (Snowflake) make the same decisions from these helpers, so
the two paths cannot drift (ADR 0008):

* a column is *sensitive* when any of its tags matches a ``policy.sensitive_tags`` glob;
  sensitive columns never expose values: no top-k and no min/max of any type;
* with ``policy.samples`` off no top-k is produced and string columns get no min/max;
  number and temporal min/max are aggregates and stay;
* rendered values are truncated to ``value_max_length`` characters plus ``…``.
"""

from collections.abc import Mapping
from fnmatch import fnmatchcase
from typing import Literal

from lakeloom.sources.base import ProfilePolicy

__all__ = [
    "ValueKind",
    "allow_counts",
    "allow_minmax",
    "is_sensitive",
    "rank_top_k",
    "render",
    "truncate",
    "withheld_reason",
]

ValueKind = Literal["number", "temporal", "string", "boolean", "other"]

_ELLIPSIS = "…"


def is_sensitive(tags: list[str], patterns: list[str]) -> bool:
    folded = [p.strip().casefold() for p in patterns]
    return any(fnmatchcase(tag.strip().casefold(), pat) for tag in tags for pat in folded)


def allow_minmax(kind: ValueKind, *, sensitive: bool, policy: ProfilePolicy) -> bool:
    if sensitive:
        return False
    if kind == "string":
        return policy.samples
    return kind in ("number", "temporal")


def allow_counts(kind: ValueKind) -> bool:
    return kind != "other"


def withheld_reason(
    *, sensitive: bool, policy: ProfilePolicy
) -> Literal["sensitive", "samples_disabled"] | None:
    if sensitive:
        return "sensitive"
    if not policy.samples:
        return "samples_disabled"
    return None


def render(value: object) -> str:
    iso = getattr(value, "isoformat", None)
    return str(iso()) if callable(iso) else str(value)


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + _ELLIPSIS


def rank_top_k(counts: Mapping[object, int], policy: ProfilePolicy) -> list[tuple[str, int]]:
    """The ``top_k`` most frequent values, rendered and truncated; ties by rendered value."""
    ranked = sorted(((render(v), c) for v, c in counts.items()), key=lambda t: (-t[1], t[0]))
    return [(truncate(v, policy.value_max_length), c) for v, c in ranked[: policy.top_k]]
