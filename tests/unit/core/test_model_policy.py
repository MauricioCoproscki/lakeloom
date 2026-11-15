"""Per-model access groups (NFR-SEC-07): empty list = no restriction, the CLI operator exempt."""

from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from lakeloom.core.callers import Actor
from lakeloom.core.model_policy import (
    ModelAccess,
    ModelPolicyEntry,
    TriplestorePolicy,
    access_for,
    may,
    require_group,
    triplestore_for,
)
from lakeloom.errors import LakeloomError


@dataclass(frozen=True)
class _Principal:
    subject: str
    type: str = "human"
    groups: frozenset[str] = frozenset()


def test_require_group_rules() -> None:
    open_access = ModelAccess()
    restricted = ModelAccess(propose_groups=["stewards"], review_groups=["reviewers"])
    member = _Principal("alice", groups=frozenset({"stewards", "staff"}))
    outsider = _Principal("bob", groups=frozenset({"staff"}))
    no_groups = _Principal("carol")
    cli = Actor(subject="operator")

    # empty list allows everyone
    require_group(outsider, open_access, "propose")
    require_group(no_groups, open_access, "review")
    assert may(no_groups, open_access, "propose")
    # a listed group allows
    require_group(member, restricted, "propose")
    assert may(member, restricted, "propose")
    assert not may(member, restricted, "review")
    # the CLI operator is exempt
    require_group(cli, restricted, "propose")
    require_group(cli, restricted, "review")
    assert may(cli, restricted, "review")
    # anyone else is refused
    assert not may(outsider, restricted, "propose")
    with pytest.raises(LakeloomError) as info:
        require_group(outsider, restricted, "review", model="shop")
    assert info.value.code == "forbidden"
    assert info.value.message == "model 'shop' restricts review to groups reviewers"
    with pytest.raises(LakeloomError) as bare:
        require_group(no_groups, restricted, "propose")
    assert bare.value.code == "forbidden" and "restricts propose to groups stewards" in (
        bare.value.message
    )


def test_access_from_policy_entries() -> None:
    entries = [
        ModelPolicyEntry.model_validate(
            {"name": "shop", "access": {"propose_groups": ["stewards"], "review_groups": ["r"]}}
        ),
        ModelPolicyEntry.model_validate({"name": "retail"}),
    ]
    assert access_for(entries, "shop") == ModelAccess(
        propose_groups=["stewards"], review_groups=["r"]
    )
    assert access_for(entries, "retail") == ModelAccess()
    assert access_for(entries, "unknown") == ModelAccess()
    with pytest.raises(ValidationError):
        ModelPolicyEntry.model_validate({"name": "x", "access": {"approve_groups": ["a"]}})
    with pytest.raises(ValidationError):
        ModelAccess.model_validate({"propose_groups": [""]})


def test_publish_groups_restrict_release() -> None:
    access = ModelAccess(publish_groups=["publishers"])
    member = _Principal("alice", type="agent", groups=frozenset({"publishers"}))
    outsider = _Principal("bot", type="agent", groups=frozenset({"stewards"}))
    require_group(member, access, "publish")
    require_group(Actor(subject="operator"), access, "publish")
    require_group(outsider, ModelAccess(), "publish")  # empty: no restriction
    with pytest.raises(LakeloomError) as info:
        require_group(outsider, access, "publish", model="shop")
    assert info.value.code == "forbidden"
    assert info.value.message == "model 'shop' restricts publish to groups publishers"
    # publish groups do not leak into the other actions
    assert may(outsider, access, "propose") and may(outsider, access, "review")
    entry = ModelPolicyEntry.model_validate(
        {"name": "shop", "access": {"publish_groups": ["publishers"]}}
    )
    assert access_for([entry], "shop").publish_groups == ["publishers"]


def test_triplestore_policy_defaults_and_overrides() -> None:
    entries = [
        ModelPolicyEntry.model_validate(
            {"name": "shop", "triplestore": {"enabled": False, "shapes": True}}
        ),
        ModelPolicyEntry.model_validate({"name": "retail"}),
    ]
    assert triplestore_for(entries, "shop") == TriplestorePolicy(enabled=False, shapes=True)
    assert triplestore_for(entries, "retail") == TriplestorePolicy(enabled=True, shapes=None)
    assert triplestore_for(entries, "unknown") == TriplestorePolicy()
    with pytest.raises(ValidationError):
        ModelPolicyEntry.model_validate({"name": "x", "triplestore": {"url": "http://x"}})
