"""Guided next steps for a semantic model: signals S1-S12, stage detection and ranking.

Pure: the server layer gathers a :class:`ModelState` from the existing reports (validation,
proposals, releases, triple-store sink, drift, MAP, coverage, graph analytics) and this module
turns it into a short, deterministic, ranked list of :class:`Step` in business language.

Ranking: a lower ``tier`` always wins (1 blockers, 2 governance flow, 3 lake changes,
4 basic gaps); inside a tier, steps the caller can act on come first, then the higher
``impact * log2(1 + reach) / effort`` score, then ``id``.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from lakeloom.core.model_policy import AccessAction

__all__ = [
    "Caller",
    "DriftSummary",
    "Goal",
    "ModelState",
    "NextSteps",
    "ProposalRef",
    "Stage",
    "Step",
    "Thresholds",
    "detect_stage",
    "next_steps",
]

Stage = Literal["bootstrap", "vocabulary", "taxonomy", "thesaurus", "ontology", "maintenance"]
Goal = Literal["auto", "build", "maintain", "publish"]
Level = Literal["high", "medium", "low"]
Who = Literal["propose", "review", "publish", "operator"]

_IMPACT: dict[Level, int] = {"high": 3, "medium": 2, "low": 1}
_GOAL_TIERS: dict[Goal, frozenset[int]] = {
    "auto": frozenset({1, 2, 3, 4}),
    "build": frozenset({1, 4}),
    "maintain": frozenset({1, 2, 3}),
    "publish": frozenset({1, 2}),
}
_MAX_LISTED = 5


@dataclass(frozen=True)
class Thresholds:
    coverage: float = 0.6  # S9: share of concepts realized by an asset
    defined: float = 0.7  # S10: share of concepts with a definition
    vocabulary_min_concepts: int = 20
    vocabulary_defined: float = 0.5
    unmapped_columns: float = 0.1  # S8: share of scanned columns without a concept
    orphans: float = 0.15  # S11: share of concepts with no BT/NT/RT
    flat_min_concepts: int = 30  # S12: a flat list this large needs a hierarchy
    max_depth: int = 6  # S12
    stale_draft_days: int = 7  # S4


@dataclass(frozen=True)
class ProposalRef:
    id: str
    author: str
    age_days: int


@dataclass(frozen=True)
class DriftSummary:
    added_tables: list[str] = field(default_factory=list[str])
    removed_tables: list[str] = field(default_factory=list[str])
    changed_tables: list[str] = field(default_factory=list[str])
    unclassified: int = 0  # the scheduled drift run only keeps a count per model

    @property
    def total(self) -> int:
        named = len(self.added_tables) + len(self.removed_tables) + len(self.changed_tables)
        return named + self.unclassified


@dataclass(frozen=True)
class ModelState:
    """Everything the detectors read; filled by the server layer, never fetched here."""

    model: str
    concepts: int = 0
    latest_scan_id: str | None = None
    structural_errors: list[str] = field(default_factory=list[str])  # S1
    changes_requested: list[ProposalRef] = field(default_factory=list[ProposalRef])  # S2
    submitted: list[ProposalRef] = field(default_factory=list[ProposalRef])  # S3
    drafts: list[ProposalRef] = field(default_factory=list[ProposalRef])  # S4
    has_release: bool = False
    unreleased_changes: int = 0  # S5
    sink_failed: int = 0  # S6
    drift: DriftSummary | None = None  # S7; None when drift never ran
    total_columns: int = 0  # S8
    mapped_columns: int = 0
    unmapped_tables: list[str] = field(default_factory=list[str])
    realized_concepts: int = 0  # S9
    defined_concepts: int = 0  # S10
    orphan_concepts: int = 0  # S11
    max_depth: int = 0  # S12


@dataclass(frozen=True)
class Caller:
    id: str
    can: frozenset[AccessAction] = frozenset({"propose", "review", "publish"})
    operator: bool = False


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    why: str
    tier: int
    impact: Level
    effort: int  # 1-3
    who: Who
    reach: int = 1
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict[str, Any])
    evidence: dict[str, Any] = field(default_factory=dict[str, Any])
    actionable: bool = True

    @property
    def score(self) -> float:
        return _IMPACT[self.impact] * math.log2(1 + max(self.reach, 1)) / self.effort


@dataclass(frozen=True)
class NextSteps:
    model: str
    stage: Stage
    steps: list[Step]
    health: dict[str, Any]


def _ratio(part: int, whole: int) -> float:
    return part / whole if whole else 1.0


def _listed(items: list[str]) -> str:
    shown = ", ".join(f"`{i}`" for i in items[:_MAX_LISTED])
    more = len(items) - _MAX_LISTED
    return shown + (f" and {more} more" if more > 0 else "")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


DEFAULT_THRESHOLDS = Thresholds()


def detect_stage(s: ModelState, t: Thresholds = DEFAULT_THRESHOLDS) -> Stage:
    if s.concepts == 0:  # models authored directly may never be scanned
        return "bootstrap"
    defined = _ratio(s.defined_concepts, s.concepts)
    if s.concepts < t.vocabulary_min_concepts or defined < t.vocabulary_defined:
        return "vocabulary"
    if _orphans_high(s, t) or _depth_off(s, t):
        return "taxonomy"
    if _ratio(s.realized_concepts, s.concepts) < t.coverage or defined < t.defined:
        return "thesaurus"
    return "maintenance" if s.has_release else "ontology"


def _orphans_high(s: ModelState, t: Thresholds) -> bool:
    return s.concepts > 0 and _ratio(s.orphan_concepts, s.concepts) > t.orphans


def _depth_off(s: ModelState, t: Thresholds) -> bool:
    flat = s.max_depth <= 1 and s.concepts > t.flat_min_concepts
    return flat or s.max_depth > t.max_depth


# --- detectors: one per signal, each (state, thresholds, caller) -> steps ---------------------


def _s1_structural(s: ModelState, t: Thresholds, c: Caller) -> list[Step]:
    if not s.structural_errors:
        return []
    n = len(s.structural_errors)
    return [Step(
        id="structure.errors",
        title=f"Fix {_plural(n, 'structural error')} in the model",
        why="Errors such as hierarchy loops make the model inconsistent; releases and "
        "agent answers built on it can be wrong.",
        tier=1, impact="high", effort=2, who="propose", reach=n,
        tool="validate_model", args={"model": s.model},
        evidence={"errors": s.structural_errors[:_MAX_LISTED]},
    )]  # fmt: skip


def _s2_changes_requested(s: ModelState, t: Thresholds, c: Caller) -> list[Step]:
    mine = [p for p in s.changes_requested if p.author == c.id]
    others = [p for p in s.changes_requested if p.author != c.id]
    out: list[Step] = []
    if mine:
        out.append(Step(
            id="proposals.changes_requested.mine",
            title=f"Address reviewer feedback on {_plural(len(mine), 'proposal')}",
            why="A reviewer sent these back; they stay blocked until you revise them.",
            tier=1, impact="high", effort=2, who="propose", reach=len(mine),
            tool="get_proposal", args={"proposal_id": mine[0].id},
            evidence={"proposals": [p.id for p in mine]},
        ))  # fmt: skip
    if others:
        out.append(Step(
            id="proposals.changes_requested.others",
            title=f"{_plural(len(others), 'proposal')} wait for their authors to revise",
            why="Reviewers asked for changes; the authors need to act.",
            tier=1, impact="medium", effort=1, who="propose", reach=len(others),
            evidence={"proposals": [p.id for p in others]},
            actionable=False,
        ))  # fmt: skip
    return out


def _s3_submitted(s: ModelState, t: Thresholds, c: Caller) -> list[Step]:
    reviewable = sorted(
        (p for p in s.submitted if p.author != c.id), key=lambda p: (-p.age_days, p.id)
    )
    if not reviewable:
        return []
    can = "review" in c.can
    oldest = reviewable[0]
    return [Step(
        id="proposals.review",
        title=(
            f"Review {_plural(len(reviewable), 'pending proposal')}"
            if can
            else f"{_plural(len(reviewable), 'proposal')} wait for a reviewer"
        ),
        why=f"Approved work only reaches agents once reviewed; the oldest has waited "
        f"{_plural(oldest.age_days, 'day')}.",
        tier=2, impact="high", effort=1, who="review", reach=len(reviewable),
        tool="list_pending_reviews" if can else None,
        args={"model": s.model} if can else {},
        evidence={"proposals": [p.id for p in reviewable], "oldest_days": oldest.age_days},
        actionable=can,
    )]  # fmt: skip


def _s4_drafts(s: ModelState, t: Thresholds, c: Caller) -> list[Step]:
    stale = sorted(
        (p for p in s.drafts if p.author == c.id and p.age_days > t.stale_draft_days),
        key=lambda p: (-p.age_days, p.id),
    )
    if not stale:
        return []
    return [Step(
        id="proposals.stale_drafts",
        title=f"Submit or discard {_plural(len(stale), 'forgotten draft')}",
        why="Old drafts drift away from the model and become harder to apply.",
        tier=2, impact="low", effort=1, who="propose", reach=len(stale),
        tool="get_proposal", args={"proposal_id": stale[0].id},
        evidence={"drafts": [p.id for p in stale]},
    )]  # fmt: skip


def _s5_unreleased(s: ModelState, t: Thresholds, c: Caller) -> list[Step]:
    if s.unreleased_changes <= 0:
        return []
    can = "publish" in c.can
    return [Step(
        id="release.unreleased",
        title=(
            f"Publish a release with {_plural(s.unreleased_changes, 'applied change')}"
            if can
            else f"{_plural(s.unreleased_changes, 'applied change')} wait for a release"
        ),
        why="Consumers of released versions (Git, triple store) do not see these changes yet.",
        tier=2, impact="medium", effort=1, who="publish", reach=s.unreleased_changes,
        tool="create_release" if can else None,
        args={"model": s.model} if can else {},
        evidence={"changes": s.unreleased_changes},
        actionable=can,
    )]  # fmt: skip


def _s6_sink(s: ModelState, t: Thresholds, c: Caller) -> list[Step]:
    if s.sink_failed <= 0:
        return []
    return [Step(
        id="release.sink_failed",
        title=f"Triple-store sync failed for {_plural(s.sink_failed, 'release')}",
        why="SPARQL consumers are serving an outdated model. An operator can retry with "
        "`lakeloom release sink-retry` once the triple store is reachable.",
        tier=1, impact="high", effort=1, who="operator", reach=s.sink_failed,
        evidence={"failed": s.sink_failed},
        actionable=c.operator,
    )]  # fmt: skip


def _s7_drift(s: ModelState, t: Thresholds, c: Caller) -> list[Step]:
    d = s.drift
    if d is None or d.total == 0:
        return []
    parts = [
        f"{len(d.added_tables)} new" if d.added_tables else "",
        f"{len(d.changed_tables)} changed" if d.changed_tables else "",
        f"{len(d.removed_tables)} removed" if d.removed_tables else "",
        f"{d.unclassified} drifted" if d.unclassified else "",
    ]
    can = "propose" in c.can
    return [Step(
        id="lake.drift",
        title=f"The lake changed: {', '.join(p for p in parts if p)} tables",
        why="The model no longer matches the data; new or changed columns have no meaning "
        "attached and removed ones leave dangling links.",
        tier=3, impact="high", effort=2, who="propose", reach=d.total,
        tool="detect_drift" if can else None,
        args={"model": s.model} if can else {},
        evidence={
            "added": d.added_tables[:_MAX_LISTED],
            "changed": d.changed_tables[:_MAX_LISTED],
            "removed": d.removed_tables[:_MAX_LISTED],
        },
        actionable=can,
    )]  # fmt: skip


def _s8_unmapped(s: ModelState, t: Thresholds, c: Caller) -> list[Step]:
    unmapped = s.total_columns - s.mapped_columns
    if s.total_columns == 0 or _ratio(unmapped, s.total_columns) <= t.unmapped_columns:
        return []
    where = f" in {_listed(s.unmapped_tables)}" if s.unmapped_tables else ""
    args = {"model": s.model} | ({"scan_id": s.latest_scan_id} if s.latest_scan_id else {})
    return [Step(
        id="map.unmapped_columns",
        title=f"Link {_plural(unmapped, 'column')}{where} to concepts",
        why=f"Agents cannot explain these columns; they are "
        f"{_ratio(unmapped, s.total_columns):.0%} of the scanned lake.",
        tier=3, impact="high", effort=2, who="propose", reach=unmapped,
        tool="build_map_from_scan", args=args,
        evidence={"unmapped_columns": unmapped, "tables": s.unmapped_tables[:_MAX_LISTED]},
        actionable="propose" in c.can,
    )]  # fmt: skip


def _s9_coverage(s: ModelState, t: Thresholds, c: Caller) -> list[Step]:
    ratio = _ratio(s.realized_concepts, s.concepts)
    if s.concepts == 0 or ratio >= t.coverage:
        return []
    missing = s.concepts - s.realized_concepts
    return [Step(
        id="coverage.unrealized_concepts",
        title=f"Connect {_plural(missing, 'concept')} to real tables or columns",
        why=f"Only {ratio:.0%} of the concepts point at data; the rest cannot be used to "
        "find assets.",
        tier=4, impact="medium", effort=2, who="propose", reach=missing,
        tool="assess_coverage", args={"model": s.model},
        evidence={"coverage": round(ratio, 3), "unrealized": missing},
        actionable="propose" in c.can,
    )]  # fmt: skip


def _s10_definitions(s: ModelState, t: Thresholds, c: Caller) -> list[Step]:
    ratio = _ratio(s.defined_concepts, s.concepts)
    if s.concepts == 0 or ratio >= t.defined:
        return []
    missing = s.concepts - s.defined_concepts
    return [Step(
        id="vocabulary.definitions",
        title=f"Write definitions for {_plural(missing, 'concept')}",
        why=f"Only {ratio:.0%} of the concepts say what they mean; without a definition "
        "people and agents guess.",
        tier=4, impact="high", effort=3, who="propose", reach=missing,
        tool="validate_thesaurus", args={"model": s.model},
        evidence={"defined": round(ratio, 3), "undefined": missing},
        actionable="propose" in c.can,
    )]  # fmt: skip


def _s11_orphans(s: ModelState, t: Thresholds, c: Caller) -> list[Step]:
    if not _orphans_high(s, t):
        return []
    return [Step(
        id="taxonomy.orphans",
        title=f"Place {_plural(s.orphan_concepts, 'loose concept')} in the hierarchy",
        why="These concepts have no broader, narrower or related term, so browsing and "
        "\"include narrower\" searches never reach them.",
        tier=4, impact="medium", effort=2, who="propose", reach=s.orphan_concepts,
        tool="graph_stats", args={"model": s.model},
        evidence={"orphans": s.orphan_concepts, "concepts": s.concepts},
        actionable="propose" in c.can,
    )]  # fmt: skip


def _s12_depth(s: ModelState, t: Thresholds, c: Caller) -> list[Step]:
    if not _depth_off(s, t):
        return []
    flat = s.max_depth <= 1
    return [Step(
        id="taxonomy.flat" if flat else "taxonomy.too_deep",
        title=(
            f"Group {_plural(s.concepts, 'concept')} under broader terms"
            if flat
            else f"Simplify a hierarchy {s.max_depth} levels deep"
        ),
        why=(
            "A long flat list is hard to browse; a few broader groupings make it navigable."
            if flat
            else f"More than {t.max_depth} levels is hard to navigate and usually mixes "
            "classification with detail."
        ),
        tier=4, impact="medium", effort=3, who="propose", reach=s.concepts,
        tool="propose_add_hierarchy" if flat else "graph_stats", args={"model": s.model},
        evidence={"max_depth": s.max_depth, "concepts": s.concepts},
        actionable="propose" in c.can,
    )]  # fmt: skip


_DETECTORS: tuple[Callable[[ModelState, Thresholds, Caller], list[Step]], ...] = (
    _s1_structural,
    _s2_changes_requested,
    _s3_submitted,
    _s4_drafts,
    _s5_unreleased,
    _s6_sink,
    _s7_drift,
    _s8_unmapped,
    _s9_coverage,
    _s10_definitions,
    _s11_orphans,
    _s12_depth,
)


def _rank_key(step: Step) -> tuple[int, bool, float, str]:
    return (step.tier, not step.actionable, -step.score, step.id)


def next_steps(
    state: ModelState,
    caller: Caller,
    *,
    goal: Goal = "auto",
    limit: int = 3,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> NextSteps:
    tiers = _GOAL_TIERS[goal]
    steps = [
        step
        for detect in _DETECTORS
        for step in detect(state, thresholds, caller)
        if step.tier in tiers
    ]
    steps.sort(key=_rank_key)
    health = {
        "concepts": state.concepts,
        "defined": round(_ratio(state.defined_concepts, state.concepts), 3),
        "coverage": round(_ratio(state.realized_concepts, state.concepts), 3),
        "open_reviews": len(state.submitted),
        "unreleased_changes": state.unreleased_changes,
    }
    return NextSteps(
        model=state.model,
        stage=detect_stage(state, thresholds),
        steps=steps[: max(limit, 0)],
        health=health,
    )
