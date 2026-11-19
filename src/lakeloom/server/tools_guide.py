"""The ``get_next_steps`` MCP tool: gathers a :class:`ModelState` and ranks what to do next.

Every source is read with what already exists (validation, proposal log, release tags,
triple-store sink, the last scheduled drift run, the latest scan, graph analytics). A source
that fails is listed in ``unavailable`` and its signals are skipped, so one broken upstream
never hides the rest of the guidance. Nothing is scanned, fetched externally or written.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field
from rdflib import Graph
from rdflib.namespace import RDF, SKOS

from lakeloom.core.annotations import deprecated_concepts
from lakeloom.core.callers import Actor, Caller
from lakeloom.core.graph_analytics import concept_stats, hierarchy_report, realization_coverage
from lakeloom.core.map_profile import build_map, validate_map
from lakeloom.core.model_policy import AccessAction, access_for, may
from lakeloom.core.next_steps import Caller as GuideCaller
from lakeloom.core.next_steps import (
    DriftSummary,
    Goal,
    ModelState,
    NextSteps,
    ProposalRef,
    next_steps,
)
from lakeloom.core.proposals import Proposal
from lakeloom.core.thesaurus import validate_ansi_z39
from lakeloom.server.auth import Principal
from lakeloom.server.http_app import AppDeps
from lakeloom.server.tools_analytics import load_graph_and_realizations
from lakeloom.server.tools_common import READ_SCOPE, guarded

__all__ = ["gather_state", "guide_caller", "register_guide_tools"]

_log = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
_PAGES = 20
_PAGE_SIZE = 200
_ACTIONS: tuple[AccessAction, ...] = ("propose", "review", "publish")

NEXT_STEPS_DESCRIPTION = """\
Recommend what to do next on a semantic model, in plain business language. Start here when \
the user is unsure how to build or maintain a model, or asks "what now?".

Reads the model's current state (structural errors, proposals waiting for authors or \
reviewers, applied changes not yet released, triple-store sync, lake drift, scanned columns \
without a concept, concepts without data or definitions, loose concepts, hierarchy shape) and \
returns the `stage` the model is in plus up to `limit` ranked steps. Each step has a `title` \
and `why` to show the user, and a `tool` with `args` to call when they agree. Steps with \
`actionable: false` describe work for someone else (another author, a reviewer, an operator): \
tell the user, do not call a tool for them.

`goal`: `auto` (default, everything), `build` (structure and content gaps), `maintain` \
(blockers, reviews, releases, lake changes) or `publish` (what stands between the model and a \
release). `deliverables` says what each stage can already hand to people (glossary, data \
dictionary, taxonomy, synonym file), with `ready`, `partial` or `unavailable` and the tool \
that produces it: offer these early, the user need not finish the pipeline first. \
`unavailable` lists sources that could not be read; their signals are missing. \
Never call the suggested tools without the user's agreement. Read-only, no side effects. \
Unknown model → `not_found`.

Requires scope `lakeloom:read`."""


class StepResult(BaseModel):
    id: str
    title: str
    why: str
    tier: int
    impact: str
    effort: int
    who: str
    actionable: bool
    tool: str | None
    args: dict[str, Any]
    evidence: dict[str, Any]


class DeliverableResult(BaseModel):
    kind: str
    stage: int
    status: str
    why: str
    tool: str
    args: dict[str, Any]


class NextStepsResult(BaseModel):
    model: str
    stage: str
    health: dict[str, Any]
    steps: list[StepResult]
    deliverables: list[DeliverableResult] = Field(
        default_factory=list[DeliverableResult],
        description="What each stage can already hand out (ready, partial or unavailable).",
    )
    unavailable: list[str] = Field(
        default_factory=list[str], description="Sources that could not be read."
    )


def guide_caller(principal: Caller, deps: AppDeps, model: str) -> GuideCaller:
    """What ``principal`` may do on ``model``: the scope and the model's access groups."""
    scopes: frozenset[str] = getattr(principal, "scopes", frozenset[str]())
    access = access_for(deps.policies, model)
    can: frozenset[AccessAction] = frozenset(
        a for a in _ACTIONS if f"lakeloom:{a}" in scopes and may(principal, access, a)
    )
    operator = isinstance(principal, Actor) or "lakeloom:admin" in scopes
    return GuideCaller(id=principal.subject, can=can, operator=operator)


def _ref(p: Proposal, now: datetime) -> ProposalRef:
    return ProposalRef(
        id=p.id,
        author=p.provenance.principal_subject,
        age_days=max((now - p.updated_at).days, 0),
    )


async def _proposals(deps: AppDeps, model: str, statuses: list[str]) -> list[Proposal]:
    out: list[Proposal] = []
    cursor: str | None = None
    for _ in range(_PAGES):
        page, cursor = await deps.proposal_repo.list_by_status(statuses, model, cursor, _PAGE_SIZE)
        out.extend(page)
        if cursor is None:
            break
    return out


async def gather_state(
    deps: AppDeps, model: str, *, now: datetime | None = None
) -> tuple[ModelState, list[str]]:
    """The model's :class:`ModelState` and the names of the sources that failed.

    The model itself must exist (``not_found`` propagates); every other source is optional.
    """
    at = now or datetime.now(UTC)
    await deps.models.get_model(model)
    fields: dict[str, Any] = {"model": model}
    unavailable: list[str] = []

    async def source(name: str, read: Callable[[], Awaitable[None]]) -> None:
        try:
            await read()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.warning("next_steps: %s unavailable for %s (%s)", name, model, type(exc).__name__)
            unavailable.append(name)

    async def validation() -> None:
        report = await deps.models.validation(model)
        fields["structural_errors"] = [
            f"{i.code}: {i.subject}" for i in report.issues if i.severity == "error"
        ]

    proposals: list[Proposal] = []

    async def proposal_log() -> None:
        proposals.extend(
            await _proposals(deps, model, ["changes_requested", "submitted", "draft", "applied"])
        )
        by: dict[str, list[ProposalRef]] = {"changes_requested": [], "submitted": [], "draft": []}
        for p in proposals:
            if p.status in by:
                by[p.status].append(_ref(p, at))
        fields["changes_requested"] = by["changes_requested"]
        fields["submitted"] = by["submitted"]
        fields["drafts"] = by["draft"]

    async def releases() -> None:
        if deps.reader is None:
            return
        listed = await deps.reader.list(model)
        fields["has_release"] = bool(listed)
        since = listed[0].released_at if listed else None
        fields["unreleased_changes"] = sum(
            1
            for p in proposals
            if p.status == "applied" and (since is None or p.updated_at > since)
        )

    async def sink() -> None:
        if deps.sink_worker is None:
            return
        counts = await deps.releases_repo.sink_counts(deps.sink_worker.repo_key)
        fields["sink_failed"] = counts.get("failed", 0)

    async def drift() -> None:
        run = deps.drift_schedule.last_run
        if run is not None and model in run.tables:
            fields["drift"] = DriftSummary(unclassified=run.tables[model])

    graph: list[Graph] = []
    realizations: dict[str, list[str]] = {}

    async def analytics() -> None:
        g, real = await load_graph_and_realizations(deps, model)
        graph.append(g)
        realizations.update(real)
        stats = concept_stats(g)
        cov = realization_coverage(g, real)
        fields["concepts"] = stats.total_concepts
        fields["max_depth"] = stats.max_depth
        fields["orphan_concepts"] = len(hierarchy_report(g).orphan_concepts)
        fields["realized_concepts"] = cov.concepts_with_realizations
        fields["defined_concepts"] = validate_ansi_z39(g).concepts_with_definition
        concepts = set(g.subjects(RDF.type, SKOS.Concept))
        fields["synonym_concepts"] = len({c for c in g.subjects(SKOS.altLabel) if c in concepts})
        fields["deprecated_concepts"] = len(deprecated_concepts(g))

    async def latest_scan() -> None:
        from lakeloom.server.deliverables import latest_scan_id

        latest = await latest_scan_id(deps, model, realizations)
        if latest is None:
            return
        fields["latest_scan_id"] = latest
        record = await deps.scans.get(latest)
        report = validate_map(build_map(record, model_slug=model, realizations=realizations))
        fields["total_columns"] = report.total_columns
        fields["mapped_columns"] = report.mapped_columns
        fields["unmapped_tables"] = sorted(
            {i.subject for i in report.issues if i.rule in ("MAP-01", "MAP-02") and i.subject}
        )

    await source("validation", validation)
    await source("proposals", proposal_log)
    await source("releases", releases)
    await source("triplestore", sink)
    await source("drift", drift)
    await source("analytics", analytics)
    if graph:
        await source("scan", latest_scan)
    return ModelState(**fields), unavailable


def _markdown(out: NextSteps, unavailable: list[str]) -> str:
    h = out.health
    lines = [
        f"## Next steps — {out.model}",
        "",
        f"Stage: **{out.stage}** · {h['concepts']} concepts · {h['defined']:.0%} defined · "
        f"{h['coverage']:.0%} linked to data · {h['open_reviews']} open reviews",
        "",
    ]
    if not out.steps:
        lines.append("Nothing needs attention right now.")
    for n, s in enumerate(out.steps, 1):
        call = f" → `{s.tool}`" if s.tool and s.actionable else " (for someone else)"
        lines.append(f"{n}. **{s.title}**{call}")
        lines.append(f"   {s.why}")
    usable = [d for d in out.deliverables if d.status != "unavailable"]
    if usable:
        lines += ["", "**You can already hand out:**"]
        for d in usable:
            mark = "" if d.status == "ready" else " (partial)"
            lines.append(f"- {d.kind.replace('_', ' ')}{mark} → `{d.tool}`: {d.why}")
    if unavailable:
        lines += ["", f"_Could not read: {', '.join(unavailable)}; related checks skipped._"]
    return "\n".join(lines)


def register_guide_tools(mcp: MCPServer, deps: AppDeps) -> None:
    async def next_steps_tool(
        model: Annotated[str, Field(description="Model slug, as returned by list_models.")],
        ctx: Context,
        goal: Annotated[
            Literal["auto", "build", "maintain", "publish"],
            Field(description="Which kind of work to recommend."),
        ] = "auto",
        limit: Annotated[int, Field(ge=1, le=10, description="Maximum steps returned.")] = 3,
    ) -> Annotated[CallToolResult, NextStepsResult]:
        async def body(principal: Principal) -> CallToolResult:
            state, unavailable = await gather_state(deps, model)
            chosen: Goal = goal
            out = next_steps(state, guide_caller(principal, deps, model), goal=chosen, limit=limit)
            result = NextStepsResult(
                model=out.model,
                stage=out.stage,
                health=out.health,
                steps=[
                    StepResult(**{k: getattr(s, k) for k in StepResult.model_fields})
                    for s in out.steps
                ],
                unavailable=unavailable,
                deliverables=[
                    DeliverableResult(**{k: getattr(d, k) for k in DeliverableResult.model_fields})
                    for d in out.deliverables
                ],
            )
            return CallToolResult(
                content=[TextContent(type="text", text=_markdown(out, unavailable))],
                structured_content=result.model_dump(mode="json"),
            )

        return await guarded("get_next_steps", ctx, READ_SCOPE, body)

    mcp.add_tool(
        next_steps_tool,
        name="get_next_steps",
        title="Get next steps",
        description=NEXT_STEPS_DESCRIPTION,
        annotations=READ_ONLY,
    )
