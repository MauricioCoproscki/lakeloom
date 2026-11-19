from dataclasses import replace

import pytest

from lakeloom.core.next_steps import (
    Caller,
    DriftSummary,
    ModelState,
    ProposalRef,
    Thresholds,
    detect_stage,
    next_steps,
)

ME = Caller(id="alice")
NOBODY = Caller(id="bob", can=frozenset())

# A model with no signal firing: 100 concepts, well defined, realized, hierarchical.
HEALTHY = ModelState(
    model="retail",
    concepts=100,
    latest_scan_id="scan_1",
    has_release=True,
    total_columns=50,
    mapped_columns=50,
    realized_concepts=90,
    defined_concepts=90,
    orphan_concepts=2,
    max_depth=3,
)


def ids(state: ModelState, caller: Caller = ME, **kw: object) -> list[str]:
    return [s.id for s in next_steps(state, caller, limit=20, **kw).steps]  # type: ignore[arg-type]


def test_healthy_model_has_no_steps_and_is_in_maintenance() -> None:
    out = next_steps(HEALTHY, ME)
    assert out.steps == [] and out.stage == "maintenance"
    assert out.health["coverage"] == 0.9 and out.health["defined"] == 0.9


@pytest.mark.parametrize(
    ("change", "stage"),
    [
        ({"concepts": 0}, "bootstrap"),
        ({"concepts": 10, "defined_concepts": 10, "realized_concepts": 10}, "vocabulary"),
        ({"defined_concepts": 40}, "vocabulary"),
        ({"orphan_concepts": 30}, "taxonomy"),
        ({"max_depth": 1}, "taxonomy"),
        ({"max_depth": 9}, "taxonomy"),
        ({"realized_concepts": 40}, "thesaurus"),
        ({"defined_concepts": 60}, "thesaurus"),
        ({"has_release": False}, "ontology"),
    ],
)
def test_detect_stage(change: dict[str, object], stage: str) -> None:
    assert detect_stage(replace(HEALTHY, **change)) == stage  # type: ignore[arg-type]


def test_s1_structural_errors() -> None:
    (step,) = next_steps(replace(HEALTHY, structural_errors=["cycle A>B>A"]), ME).steps
    assert step.id == "structure.errors" and step.tier == 1
    assert step.tool == "validate_model" and step.args == {"model": "retail"}


def test_s2_changes_requested_split_by_author() -> None:
    state = replace(
        HEALTHY,
        changes_requested=[ProposalRef("p1", "alice", 1), ProposalRef("p2", "carol", 1)],
    )
    steps = {s.id: s for s in next_steps(state, ME, limit=5).steps}
    assert steps["proposals.changes_requested.mine"].args == {"proposal_id": "p1"}
    others = steps["proposals.changes_requested.others"]
    assert not others.actionable and others.tool is None


def test_s3_never_asks_to_review_own_proposal_and_oldest_first() -> None:
    state = replace(
        HEALTHY,
        submitted=[
            ProposalRef("p1", "alice", 9),
            ProposalRef("p2", "carol", 2),
            ProposalRef("p3", "dave", 5),
        ],
    )
    (step,) = next_steps(state, ME).steps
    assert step.evidence["proposals"] == ["p3", "p2"] and step.evidence["oldest_days"] == 5
    # only my own proposals wait: tell me, but never as something I can review
    own = next_steps(replace(state, submitted=[ProposalRef("p1", "alice", 9)]), ME).steps
    assert [(s.id, s.actionable, s.tool) for s in own] == [("proposals.review.mine", False, None)]


def test_s3_without_review_permission_is_informative() -> None:
    state = replace(HEALTHY, submitted=[ProposalRef("p2", "carol", 2)])
    (step,) = next_steps(state, NOBODY).steps
    assert not step.actionable and step.tool is None and "wait for a reviewer" in step.title


def test_s4_only_my_stale_drafts() -> None:
    drafts = [
        ProposalRef("d1", "alice", 3),
        ProposalRef("d2", "alice", 10),
        ProposalRef("d3", "carol", 30),
    ]
    (step,) = next_steps(replace(HEALTHY, drafts=drafts), ME).steps
    assert step.id == "proposals.stale_drafts" and step.evidence["drafts"] == ["d2"]


def test_s5_unreleased_needs_publish_right() -> None:
    state = replace(HEALTHY, unreleased_changes=4)
    (step,) = next_steps(state, ME).steps
    assert step.tool == "create_release" and step.actionable
    (info,) = next_steps(state, NOBODY).steps
    assert info.tool is None and not info.actionable


def test_s6_sink_failed_actionable_only_for_operator() -> None:
    state = replace(HEALTHY, sink_failed=2)
    (step,) = next_steps(state, ME).steps
    assert step.tier == 1 and not step.actionable and "sink-retry" in step.why
    (op,) = next_steps(state, Caller(id="ops", operator=True)).steps
    assert op.actionable


def test_s7_drift() -> None:
    assert ids(replace(HEALTHY, drift=DriftSummary())) == []
    drift = DriftSummary(added_tables=["a", "b"], removed_tables=["c"])
    (step,) = next_steps(replace(HEALTHY, drift=drift), ME).steps
    assert step.id == "lake.drift" and step.reach == 3
    assert step.title == "The lake changed: 2 new, 1 removed tables"
    assert step.tool == "detect_drift"
    (counted,) = next_steps(replace(HEALTHY, drift=DriftSummary(unclassified=4)), ME).steps
    assert counted.title == "The lake changed: 4 drifted tables" and counted.reach == 4


def test_s8_unmapped_columns_threshold_and_scan_arg() -> None:
    assert ids(replace(HEALTHY, mapped_columns=45)) == []  # exactly 10%: not above
    state = replace(HEALTHY, mapped_columns=20, unmapped_tables=["orders", "customers"])
    (step,) = next_steps(state, ME).steps
    assert step.title == "Link 30 columns in `orders`, `customers` to concepts"
    assert step.args == {"model": "retail", "scan_id": "scan_1"}


def test_s9_and_s10_gaps() -> None:
    state = replace(HEALTHY, realized_concepts=50, defined_concepts=60)
    assert set(ids(state)) == {"coverage.unrealized_concepts", "vocabulary.definitions"}


def test_s11_orphans() -> None:
    (step,) = next_steps(replace(HEALTHY, orphan_concepts=20), ME).steps
    assert step.id == "taxonomy.orphans" and step.reach == 20


def test_s12_flat_and_too_deep() -> None:
    (flat,) = next_steps(replace(HEALTHY, max_depth=1), ME).steps
    assert flat.id == "taxonomy.flat" and flat.tool == "propose_add_hierarchy"
    (deep,) = next_steps(replace(HEALTHY, max_depth=8), ME).steps
    assert deep.id == "taxonomy.too_deep"
    assert ids(replace(HEALTHY, max_depth=1, concepts=25, defined_concepts=25)) == []


def test_lower_tier_always_wins_and_limit() -> None:
    state = replace(
        HEALTHY,
        structural_errors=["x"],
        unreleased_changes=1,
        drift=DriftSummary(added_tables=["a"]),
        realized_concepts=10,
    )
    out = next_steps(state, ME)
    assert [s.tier for s in out.steps] == [1, 2, 3]
    assert len(next_steps(state, ME, limit=1).steps) == 1


def test_actionable_first_inside_a_tier_then_score() -> None:
    state = replace(
        HEALTHY,
        submitted=[ProposalRef("p1", "carol", 1)],
        unreleased_changes=50,
    )
    reviewer = Caller(id="alice", can=frozenset({"review"}))
    assert ids(state, reviewer) == ["proposals.review", "release.unreleased"]
    # Both actionable: the release reaches more items, so it scores higher.
    assert ids(state, ME) == ["release.unreleased", "proposals.review"]


@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        ("auto", {1, 2, 3, 4}),
        ("build", {1, 4}),
        ("maintain", {1, 2, 3}),
        ("publish", {1, 2}),
    ],
)
def test_goal_filters_tiers(goal: str, expected: set[int]) -> None:
    state = replace(
        HEALTHY,
        structural_errors=["x"],
        unreleased_changes=1,
        drift=DriftSummary(added_tables=["a"]),
        realized_concepts=10,
    )
    out = next_steps(state, ME, goal=goal, limit=20)  # type: ignore[arg-type]
    assert {s.tier for s in out.steps} == expected


def test_output_is_deterministic() -> None:
    state = replace(
        HEALTHY, realized_concepts=10, defined_concepts=60, orphan_concepts=30, max_depth=9
    )
    assert next_steps(state, ME, limit=20) == next_steps(state, ME, limit=20)


def test_custom_thresholds() -> None:
    strict = Thresholds(coverage=0.95)
    assert ids(HEALTHY, thresholds=strict) == ["coverage.unrealized_concepts"]


# --- deliverables: value at every stage ---------------------------------------------------


def _status(state: ModelState) -> dict[str, str]:
    from lakeloom.core.next_steps import deliverables

    return {d.kind: d.status for d in deliverables(state)}


def test_deliverables_of_an_empty_model_are_unavailable() -> None:
    assert set(_status(ModelState(model="m")).values()) == {"unavailable"}


def test_a_partly_defined_vocabulary_already_gives_a_glossary() -> None:
    got = _status(ModelState(model="m", concepts=10, defined_concepts=3))
    assert got["glossary"] == "partial"
    assert got["taxonomy"] == "unavailable" and got["data_dictionary"] == "unavailable"


def test_each_stage_ready_independently() -> None:
    state = ModelState(
        model="m",
        concepts=10,
        defined_concepts=9,
        latest_scan_id="scan_1",
        total_columns=10,
        mapped_columns=10,
        max_depth=2,
        orphan_concepts=0,
        synonym_concepts=4,
        deprecated_concepts=1,
    )
    from lakeloom.core.next_steps import deliverables

    got = {d.kind: d for d in deliverables(state)}
    assert {k: d.status for k, d in got.items()} == {
        "glossary": "ready",
        "data_dictionary": "ready",
        "taxonomy": "ready",
        "synonyms": "ready",
    }
    assert got["data_dictionary"].args == {"model": "m", "scan_id": "scan_1"}
    assert "1 deprecated term redirected" in got["synonyms"].why


def test_next_steps_carries_deliverables() -> None:
    out = next_steps(ModelState(model="m", concepts=5, defined_concepts=5), Caller(id="u"))
    assert [d.kind for d in out.deliverables] == [
        "glossary",
        "data_dictionary",
        "taxonomy",
        "synonyms",
    ]
