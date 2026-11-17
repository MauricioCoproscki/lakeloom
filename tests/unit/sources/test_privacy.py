"""Profiling privacy rules shared by the Arrow and SQL profilers (FR-SRC-08, FR-SRC-09)."""

import pytest

from lakeloom.sources.base import ProfilePolicy
from lakeloom.sources.privacy import (
    ValueKind,
    allow_counts,
    allow_minmax,
    is_sensitive,
    rank_top_k,
    withheld_reason,
)


def policy(*, samples: bool = False, top_k: int = 5, value_max_length: int = 64) -> ProfilePolicy:
    return ProfilePolicy(
        samples=samples,
        sensitive_tags=["pii.*"],
        top_k=top_k,
        low_cardinality=50,
        value_max_length=value_max_length,
    )


KINDS: tuple[ValueKind, ...] = ("number", "temporal", "string", "boolean", "other")


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("sensitive", [False, True])
@pytest.mark.parametrize("samples", [False, True])
def test_minmax_matrix(kind: ValueKind, sensitive: bool, samples: bool) -> None:
    expected = not sensitive and (kind in ("number", "temporal") or (kind == "string" and samples))
    assert allow_minmax(kind, sensitive=sensitive, policy=policy(samples=samples)) is expected


def test_counts_excludes_other() -> None:
    assert [allow_counts(k) for k in KINDS] == [True, True, True, True, False]


def test_withheld_reason() -> None:
    assert withheld_reason(sensitive=True, policy=policy(samples=False)) == "sensitive"
    assert withheld_reason(sensitive=True, policy=policy(samples=True)) == "sensitive"
    assert withheld_reason(sensitive=False, policy=policy(samples=False)) == "samples_disabled"
    assert withheld_reason(sensitive=False, policy=policy(samples=True)) is None


def test_is_sensitive_glob_casefold() -> None:
    assert is_sensitive(["PII.Email"], ["pii.*"])
    assert not is_sensitive(["Tier.Gold"], ["pii.*"])
    assert not is_sensitive([], ["pii.*"])


def test_rank_top_k_orders_and_truncates() -> None:
    ranked = rank_top_k({"bb": 2, "a": 2, "c": 5}, policy(top_k=2, value_max_length=1))
    assert ranked == [("c", 5), ("a", 2)]
