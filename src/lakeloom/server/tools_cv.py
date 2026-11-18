"""MCP tools for direct CV, Taxonomy and Thesaurus authoring (M5b).

Provides two groups of tools:

READ tools (scope ``lakeloom:read``, read_only_hint=True):
  - ``list_cv_candidates``    — normalise tag strings into term candidates
  - ``validate_thesaurus``    — ANSI Z39.19 / ISO 25964 structural checks
  - ``suggest_related_terms`` — RT candidates from asset co-occurrence
  - ``assess_taxonomy_coverage`` — gap analysis against a declared coverage model

WRITE tools (scope ``lakeloom:propose``, pass through proposal → review):
  - ``propose_add_term``       — add one or more concepts directly (without a lake scan)
  - ``propose_add_hierarchy``  — add a parent + children block at once
  - ``propose_deprecate``      — mark a concept as deprecated
  - ``propose_add_scope_note`` — attach a skos:scopeNote to an existing concept

All writes create or modify a proposal exactly like ``propose_from_scan``; nothing is
applied until a human reviewer approves (``review_proposal``).
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from lakeloom.core.coverage import CoverageModel, assess_coverage
from lakeloom.core.cv_builder import (
    FolksonomyCandidate,
    TermDraft,
    batch_to_ops,
    harvest_folksonomy,
    normalize_term_name,
)
from lakeloom.core.operations import (
    AddConcept,
    AddRelation,
    AddScopeNote,
    DeprecateConcept,
    ProposalOperation,
)
from lakeloom.core.proposals import Proposal
from lakeloom.core.thesaurus_builder import (
    RelatedSuggestion,
    ThesaurusReport,
    suggest_related_from_cooccurrence,
    validate_ansi_z39,
)
from lakeloom.errors import LakeloomError
from lakeloom.server.auth import Principal
from lakeloom.server.http_app import AppDeps
from lakeloom.server.tools_common import (
    PROPOSE_SCOPE,
    READ_SCOPE,
    client_name,
    guarded,
)
from lakeloom.server.tools_proposals import NEXT_STEP, ProposalResult, with_line
from lakeloom.untrusted import code_span as _code

__all__ = ["register_cv_tools"]

_log = logging.getLogger(__name__)

MAX_TERMS_PER_CALL = 500
MAX_TAGS = 1000
MAX_TOPICS = 50

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)

_SCOPE_READ = "Requires scope `lakeloom:read`."
_SCOPE_PROPOSE = "Requires scope `lakeloom:propose`."
_DRAFT_NOTE = (
    "Nothing is applied: the change sets stay drafts until a human reviewer approves "
    "(review_proposal)."
)

LIST_CV_CANDIDATES_DESCRIPTION = f"""\
Normalise a list of raw tag strings (OM classification FQNs, column names, free labels) \
into candidate term names for a Controlled Vocabulary.  Strips classification prefixes \
(`PII.`, `Domain.`), replaces underscores with spaces, title-cases, and validates the \
OM name pattern.  Tags that cannot be normalised are silently dropped.

Pass `frequency_map` to rank candidates by asset count.  Returns candidates sorted by \
frequency descending.  Read-only, no side effects.

{_SCOPE_READ}"""

VALIDATE_THESAURUS_DESCRIPTION = f"""\
Validate the thesaurus structure of a semantic model against ANSI Z39.19 / ISO 25964 rules:

- **BT/NT cycle check** (ERROR): detects cycles in the skos:broader/narrower hierarchy.
- **RT symmetry check** (WARNING): skos:related must be symmetric (A RT B → B RT A).
- **Definition coverage** (WARNING): concepts with no skos:definition or skos:scopeNote.

Returns a report with counts (total concepts, BT/NT pairs, RT pairs, defined concepts) \
and a list of issues.  Read-only, no side effects.  Unknown model → `not_found`.

{_SCOPE_READ}"""

SUGGEST_RELATED_TERMS_DESCRIPTION = f"""\
Suggest `skos:related` (RT) pairs for a model's concepts based on asset co-occurrence: \
two concepts that tag many of the same tables/columns are likely related.  Requires \
no embeddings or NLP — uses only the existing realization data.

`min_shared` (default 2): minimum shared assets for a pair to be suggested.
`min_score` (default 0.1): minimum Jaccard overlap score.
`limit` (default 20): maximum pairs returned.

Returns pairs sorted by overlap score descending.  Read-only, no side effects.  Unknown \
model → `not_found`.

{_SCOPE_READ}"""

ASSESS_TAXONOMY_COVERAGE_DESCRIPTION = f"""\
Measure how well a model's taxonomy covers a declared set of topics.  For each topic \
in `topics`, searches the model's concepts by name similarity.  Reports matched topics, \
gaps (topics with no concept), and depth violations (branches deeper than `depth`).

`depth` (default 5): maximum BT/NT hierarchy depth the taxonomy should have.
`granularity`: `"coarse"` (1-3 levels), `"medium"` (3-6), `"fine"` (6+) — affects \
context in the report only.
`exclusions`: topics explicitly out of scope (skipped in gap analysis).

Read-only, no side effects.  Unknown model → `not_found`.

{_SCOPE_READ}"""

PROPOSE_ADD_TERM_DESCRIPTION = f"""\
Add one or more new concepts directly to a semantic model — no lake scan required.  \
Each term in `terms` is a `TermDraft`:
- `name` (required): OM term name pattern (letter, then letters/digits/spaces/underscores, ≤128).
- `definition`: skos:definition text.
- `broader`: FQN of the parent concept (sets skos:broader).
- `synonyms`: list of altLabel strings.
- `scope_note`: skos:scopeNote text.
- `history_note`: skos:historyNote text.
- `authority_source`: free-text source reference (recorded in evidence).

Creates a new draft proposal with the terms.  At most {MAX_TERMS_PER_CALL} terms per call. \
{_DRAFT_NOTE}

{_SCOPE_PROPOSE}"""

PROPOSE_ADD_HIERARCHY_DESCRIPTION = f"""\
Add a parent concept and its children to a model in one call.  Creates the parent (if it \
does not already exist) and all child concepts, each with skos:broader pointing at the parent.

`parent_name`: name of the parent concept (created if absent; pass its FQN if it exists).
`children`: list of child term names (strings) or TermDraft objects for richer definitions.
`parent_definition`: optional definition for the parent concept.
`note`: free-text rationale recorded in the proposal.

{_DRAFT_NOTE}

{_SCOPE_PROPOSE}"""

PROPOSE_DEPRECATE_DESCRIPTION = f"""\
Mark a concept as deprecated.  The concept stays in the model (so existing realizations \
are not broken) but agents are directed to `successor` when one is given.  The deprecation \
is recorded as a `DeprecateConcept` operation in a new draft proposal.

`concept`: name or FQN of the concept to deprecate.
`reason`: why it is deprecated (required, ≤1000 chars).
`successor`: name or FQN of the replacement concept (optional).
`model`: target model slug.

{_DRAFT_NOTE}

{_SCOPE_PROPOSE}"""

PROPOSE_ADD_SCOPE_NOTE_DESCRIPTION = f"""\
Attach a `skos:scopeNote` to an existing concept.  Scope notes clarify the intended use \
of a term, disambiguate it from similar concepts, or document governance decisions.

`concept`: name or FQN of the existing concept.
`note`: the scope note text (≤2000 chars).
`lang`: BCP 47 language tag (default `"en"`).
`model`: target model slug.

{_DRAFT_NOTE}

{_SCOPE_PROPOSE}"""


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class CVCandidatesResult(BaseModel):
    total_input: int
    total_valid: int
    candidates: list[FolksonomyCandidate]


class ThesaurusReportResult(BaseModel):
    model: str
    total_concepts: int
    concepts_with_definition: int
    concepts_with_scope_note: int
    total_bt_nt_pairs: int
    total_rt_pairs: int
    error_count: int
    warning_count: int
    summary: str


class RelatedSuggestionsResult(BaseModel):
    model: str
    total_suggestions: int
    suggestions: list[RelatedSuggestion]


class CoverageReportResult(BaseModel):
    model: str
    total_concepts: int
    actual_max_depth: int
    declared_max_depth: int
    coverage_ratio: float
    matched_count: int
    gap_count: int
    depth_violation_count: int
    summary: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(text: str, data: BaseModel) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=data.model_dump(mode="json"),
    )


def _proposal_result(
    p: Proposal, deps: AppDeps, *, next_step: bool = True
) -> CallToolResult:
    summary = deps.proposals.summary_markdown(p)
    text = f"{summary}\n{NEXT_STEP}\n" if next_step and p.status == "draft" else summary
    data = ProposalResult(
        proposal_id=p.id,
        status=p.status,
        summary=summary,
        proposal=p,
        matches=p.matches,
        warnings=p.warnings,
    )
    return _result(text, data)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_cv_tools(mcp: MCPServer, deps: AppDeps) -> None:
    """Register the eight CV/Thesaurus/Taxonomy tools on ``mcp``."""

    # ------------------------------------------------------------------
    # READ: list_cv_candidates
    # ------------------------------------------------------------------

    async def list_cv_candidates(
        tags: Annotated[
            list[str],
            Field(
                min_length=1,
                max_length=MAX_TAGS,
                description=(
                    "Raw tag strings to normalise: OM classification FQNs (e.g. "
                    "`PII.Email`), column names, free labels.  At most 1000."
                ),
            ),
        ],
        ctx: Context,
        frequency_map: Annotated[
            dict[str, int] | None,
            Field(
                description=(
                    "Optional map from tag string to asset count; used to rank candidates "
                    "by frequency.  Keys must match the `tags` list."
                ),
            ),
        ] = None,
    ) -> Annotated[CallToolResult, CVCandidatesResult]:
        async def body(_: Principal) -> CallToolResult:
            candidates = harvest_folksonomy(tags, frequency_map=frequency_map or {})
            valid = len(candidates)
            text_lines = [
                f"**{valid} valid candidate(s)** from {len(tags)} input tag(s).",
                "",
            ]
            for c in candidates[:20]:
                freq = f" (freq {c.frequency})" if c.frequency else ""
                text_lines.append(f"- `{c.name}`{freq} ← `{c.source_tag}`")
            if valid > 20:
                text_lines.append(f"… and {valid - 20} more (see structured content).")
            text_lines.append(
                "\nNext: pass the names you want to `propose_add_term` as `TermDraft` items."
            )
            data = CVCandidatesResult(
                total_input=len(tags),
                total_valid=valid,
                candidates=candidates,
            )
            return _result("\n".join(text_lines), data)

        return await guarded("list_cv_candidates", ctx, READ_SCOPE, body)

    mcp.add_tool(
        list_cv_candidates,
        name="list_cv_candidates",
        title="List CV candidates",
        description=LIST_CV_CANDIDATES_DESCRIPTION,
        annotations=READ_ONLY,
    )

    # ------------------------------------------------------------------
    # READ: validate_thesaurus
    # ------------------------------------------------------------------

    async def validate_thesaurus(
        model: Annotated[str, Field(description="Model slug (see list_models).")],
        ctx: Context,
    ) -> Annotated[CallToolResult, ThesaurusReportResult]:
        async def body(_: Principal) -> CallToolResult:
            graph = await deps.models.graph(model)
            report: ThesaurusReport = validate_ansi_z39(graph)
            data = ThesaurusReportResult(
                model=model,
                total_concepts=report.total_concepts,
                concepts_with_definition=report.concepts_with_definition,
                concepts_with_scope_note=report.concepts_with_scope_note,
                total_bt_nt_pairs=report.total_bt_nt_pairs,
                total_rt_pairs=report.total_rt_pairs,
                error_count=len(report.errors),
                warning_count=len(report.warnings),
                summary=report.to_markdown(),
            )
            return _result(report.to_markdown(), data)

        return await guarded("validate_thesaurus", ctx, READ_SCOPE, body)

    mcp.add_tool(
        validate_thesaurus,
        name="validate_thesaurus",
        title="Validate thesaurus",
        description=VALIDATE_THESAURUS_DESCRIPTION,
        annotations=READ_ONLY,
    )

    # ------------------------------------------------------------------
    # READ: suggest_related_terms
    # ------------------------------------------------------------------

    async def suggest_related_terms(
        model: Annotated[str, Field(description="Model slug.")],
        ctx: Context,
        min_shared: Annotated[
            int, Field(ge=1, le=100, description="Minimum shared assets for a pair.")
        ] = 2,
        min_score: Annotated[
            float, Field(ge=0.0, le=1.0, description="Minimum Jaccard overlap score.")
        ] = 0.1,
        limit: Annotated[int, Field(ge=1, le=200, description="Maximum pairs returned.")] = 20,
    ) -> Annotated[CallToolResult, RelatedSuggestionsResult]:
        async def body(_: Principal) -> CallToolResult:
            from rdflib.namespace import RDF, SKOS as _SKOS

            from lakeloom.core.concepts import find_assets_for_concept

            # Enumerate all concept IRIs from the SKOS graph (avoids empty-query restriction).
            graph = await deps.models.graph(model)
            concept_iris = [
                str(s) for s in graph.subjects(RDF.type, _SKOS.Concept)
            ]
            # Build realization map: concept_iri → set of asset FQNs (sample, ≤200 concepts).
            realization_map: dict[str, set[str]] = {}
            for iri in concept_iris[:200]:
                try:
                    assets_page = await find_assets_for_concept(
                        deps.models, deps.om, iri, limit=500
                    )
                    realization_map[iri] = {a.table_fqn for a in assets_page.items}
                except Exception:
                    pass

            suggestions = suggest_related_from_cooccurrence(
                realization_map,
                min_shared=min_shared,
                min_score=min_score,
                max_suggestions=limit,
            )
            lines = [f"**{len(suggestions)} RT suggestion(s)** for model {_code(model)}.", ""]
            for s in suggestions[:10]:
                a = s.concept_a_fqn.rsplit(".", 1)[-1]
                b = s.concept_b_fqn.rsplit(".", 1)[-1]
                lines.append(
                    f"- `{a}` ↔ `{b}` "
                    f"({s.shared_asset_count} shared assets, score {s.overlap_score:.2f})"
                )
            if len(suggestions) > 10:
                lines.append(f"… and {len(suggestions) - 10} more.")
            if suggestions:
                lines.append(
                    "\nNext: pass the pairs you agree with to `propose_add_relation` "
                    "with `relation_type=related`."
                )
            data = RelatedSuggestionsResult(
                model=model,
                total_suggestions=len(suggestions),
                suggestions=suggestions,
            )
            return _result("\n".join(lines), data)

        return await guarded("suggest_related_terms", ctx, READ_SCOPE, body)

    mcp.add_tool(
        suggest_related_terms,
        name="suggest_related_terms",
        title="Suggest related terms",
        description=SUGGEST_RELATED_TERMS_DESCRIPTION,
        annotations=READ_ONLY,
    )

    # ------------------------------------------------------------------
    # READ: assess_taxonomy_coverage
    # ------------------------------------------------------------------

    async def assess_taxonomy_coverage(
        model: Annotated[str, Field(description="Model slug.")],
        topics: Annotated[
            list[str],
            Field(
                min_length=1,
                max_length=MAX_TOPICS,
                description="Topic names the taxonomy should cover (e.g. ['Customer', 'Order']).",
            ),
        ],
        ctx: Context,
        depth: Annotated[
            int, Field(ge=1, le=20, description="Maximum BT/NT hierarchy depth declared.")
        ] = 5,
        granularity: Annotated[
            Literal["coarse", "medium", "fine"],
            Field(description="Intended taxonomy granularity."),
        ] = "medium",
        exclusions: Annotated[
            list[str],
            Field(max_length=50, description="Topics explicitly out of scope."),
        ] = [],
    ) -> Annotated[CallToolResult, CoverageReportResult]:
        async def body(_: Principal) -> CallToolResult:
            graph = await deps.models.graph(model)
            coverage_model = CoverageModel.from_lists(
                topics, depth=depth, granularity=granularity, exclusions=list(exclusions)
            )
            report = assess_coverage(graph, coverage_model)
            data = CoverageReportResult(
                model=model,
                total_concepts=report.total_concepts,
                actual_max_depth=report.actual_max_depth,
                declared_max_depth=report.declared_max_depth,
                coverage_ratio=report.coverage_ratio,
                matched_count=len(report.matched_topics),
                gap_count=len(report.gaps),
                depth_violation_count=len(report.depth_violations),
                summary=report.to_markdown(),
            )
            return _result(report.to_markdown(), data)

        return await guarded("assess_taxonomy_coverage", ctx, READ_SCOPE, body)

    mcp.add_tool(
        assess_taxonomy_coverage,
        name="assess_taxonomy_coverage",
        title="Assess taxonomy coverage",
        description=ASSESS_TAXONOMY_COVERAGE_DESCRIPTION,
        annotations=READ_ONLY,
    )

    # ------------------------------------------------------------------
    # WRITE: propose_add_term
    # ------------------------------------------------------------------

    async def propose_add_term(
        model: Annotated[str, Field(description="Target model slug.")],
        terms: Annotated[
            list[TermDraft],
            Field(
                min_length=1,
                max_length=MAX_TERMS_PER_CALL,
                description=(
                    f"Term drafts to add (1 to {MAX_TERMS_PER_CALL}).  Each has `name` "
                    "(required) plus optional `definition`, `broader`, `synonyms`, "
                    "`scope_note`, `history_note`, `authority_source`."
                ),
            ),
        ],
        ctx: Context,
        note: Annotated[
            str | None,
            Field(max_length=1000, description="Rationale for this batch (recorded in provenance)."),
        ] = None,
    ) -> Annotated[CallToolResult, ProposalResult]:
        async def body(principal: Principal) -> CallToolResult:
            ops = batch_to_ops(list(terms))
            warnings: list[str] = []
            if note:
                warnings.append(f"Author note: {note}")
            p = await deps.proposals.propose_operations(
                model,
                ops,
                origin="direct",
                principal=principal,
                client=client_name(ctx),
                tool="propose_add_term",
                warnings=warnings,
                recheck_pending=True,
            )
            return _proposal_result(p, deps)

        return await guarded(
            "propose_add_term", ctx, PROPOSE_SCOPE, body, audited=True, audit=deps.audit
        )

    mcp.add_tool(
        propose_add_term,
        name="propose_add_term",
        title="Propose add term",
        description=PROPOSE_ADD_TERM_DESCRIPTION,
        annotations=WRITE,
    )

    # ------------------------------------------------------------------
    # WRITE: propose_add_hierarchy
    # ------------------------------------------------------------------

    async def propose_add_hierarchy(
        model: Annotated[str, Field(description="Target model slug.")],
        parent_name: Annotated[
            str,
            Field(
                min_length=1,
                max_length=128,
                description=(
                    "Name of the parent concept.  Created if it does not exist in the model; "
                    "pass the FQN (e.g. `retail.Order`) if it already exists."
                ),
            ),
        ],
        children: Annotated[
            list[str],
            Field(
                min_length=1,
                max_length=200,
                description=(
                    "Child concept names.  Each becomes a new concept with skos:broader "
                    "pointing at the parent."
                ),
            ),
        ],
        ctx: Context,
        parent_definition: Annotated[
            str | None,
            Field(max_length=4000, description="Definition for the parent concept (optional)."),
        ] = None,
        note: Annotated[
            str | None,
            Field(max_length=1000, description="Rationale recorded in the proposal."),
        ] = None,
    ) -> Annotated[CallToolResult, ProposalResult]:
        async def body(principal: Principal) -> CallToolResult:
            from lakeloom.untrusted import UntrustedText

            ops: list[ProposalOperation] = []
            parent_bare = parent_name.rsplit(".", 1)[-1]  # strip FQN prefix if given
            # Create parent only when it looks like a bare name (no dot = not an FQN).
            if "." not in parent_name:
                ops.append(
                    AddConcept(
                        op="add_concept",
                        name=parent_name,
                        description=(
                            UntrustedText(text=parent_definition, source="user")
                            if parent_definition
                            else None
                        ),
                        rule_ids=["R-CV-DIRECT"],
                    )
                )
            for child in children:
                ops.append(
                    AddConcept(
                        op="add_concept",
                        name=child,
                        parent=parent_name,
                        rule_ids=["R-CV-DIRECT"],
                    )
                )
            warnings: list[str] = []
            if note:
                warnings.append(f"Author note: {note}")
            p = await deps.proposals.propose_operations(
                model,
                ops,
                origin="direct",
                principal=principal,
                client=client_name(ctx),
                tool="propose_add_hierarchy",
                warnings=warnings,
                recheck_pending=True,
            )
            return _proposal_result(p, deps)

        return await guarded(
            "propose_add_hierarchy", ctx, PROPOSE_SCOPE, body, audited=True, audit=deps.audit
        )

    mcp.add_tool(
        propose_add_hierarchy,
        name="propose_add_hierarchy",
        title="Propose add hierarchy",
        description=PROPOSE_ADD_HIERARCHY_DESCRIPTION,
        annotations=WRITE,
    )

    # ------------------------------------------------------------------
    # WRITE: propose_deprecate
    # ------------------------------------------------------------------

    async def propose_deprecate(
        model: Annotated[str, Field(description="Model slug.")],
        concept: Annotated[
            str,
            Field(description="Name or FQN of the concept to deprecate."),
        ],
        reason: Annotated[
            str,
            Field(min_length=1, max_length=1000, description="Why the concept is deprecated."),
        ],
        ctx: Context,
        successor: Annotated[
            str | None,
            Field(
                description=(
                    "Name or FQN of the replacement concept.  Agents will be directed here "
                    "instead of the deprecated one."
                )
            ),
        ] = None,
    ) -> Annotated[CallToolResult, ProposalResult]:
        async def body(principal: Principal) -> CallToolResult:
            from lakeloom.core.concepts import find_concepts

            page = await find_concepts(deps.models, deps.om, concept, model=model, limit=5)
            match = next((c for c in page.items if c.name == concept.rsplit(".", 1)[-1]), None)
            if match is None:
                raise LakeloomError(
                    "not_found",
                    f"concept {concept!r} not found in model {_code(model)}",
                    "use find_concepts to look up the exact name or FQN",
                )
            op = DeprecateConcept(
                op="deprecate_concept",
                fqn=match.fqn,
                base_version="1",  # lakeloom-native op; OM version not needed
                reason=reason,
                successor=successor,
                rule_ids=["R-CV-DIRECT"],
            )
            p = await deps.proposals.propose_operations(
                model,
                [op],
                origin="direct",
                principal=principal,
                client=client_name(ctx),
                tool="propose_deprecate",
                recheck_pending=True,
            )
            return _proposal_result(p, deps)

        return await guarded(
            "propose_deprecate", ctx, PROPOSE_SCOPE, body, audited=True, audit=deps.audit
        )

    mcp.add_tool(
        propose_deprecate,
        name="propose_deprecate",
        title="Propose deprecate concept",
        description=PROPOSE_DEPRECATE_DESCRIPTION,
        annotations=WRITE,
    )

    # ------------------------------------------------------------------
    # WRITE: propose_add_scope_note
    # ------------------------------------------------------------------

    async def propose_add_scope_note(
        model: Annotated[str, Field(description="Model slug.")],
        concept: Annotated[
            str, Field(description="Name or FQN of the existing concept.")
        ],
        note: Annotated[
            str,
            Field(min_length=1, max_length=2000, description="The scope note text."),
        ],
        ctx: Context,
        lang: Annotated[
            str,
            Field(pattern=r"^[a-z]{2,3}(-[A-Z]{2})?$", description="BCP 47 language tag."),
        ] = "en",
    ) -> Annotated[CallToolResult, ProposalResult]:
        async def body(principal: Principal) -> CallToolResult:
            from lakeloom.core.concepts import find_concepts

            page = await find_concepts(deps.models, deps.om, concept, model=model, limit=5)
            match = next((c for c in page.items if c.name == concept.rsplit(".", 1)[-1]), None)
            if match is None:
                raise LakeloomError(
                    "not_found",
                    f"concept {concept!r} not found in model {_code(model)}",
                    "use find_concepts to look up the exact name or FQN",
                )
            op = AddScopeNote(
                op="add_scope_note",
                concept_fqn=match.fqn,
                note=note,
                lang=lang,
                rule_ids=["R-CV-DIRECT"],
            )
            p = await deps.proposals.propose_operations(
                model,
                [op],
                origin="direct",
                principal=principal,
                client=client_name(ctx),
                tool="propose_add_scope_note",
                recheck_pending=True,
            )
            return _proposal_result(p, deps)

        return await guarded(
            "propose_add_scope_note", ctx, PROPOSE_SCOPE, body, audited=True, audit=deps.audit
        )

    mcp.add_tool(
        propose_add_scope_note,
        name="propose_add_scope_note",
        title="Propose add scope note",
        description=PROPOSE_ADD_SCOPE_NOTE_DESCRIPTION,
        annotations=WRITE,
    )
