"""MCP tools for stage 4 of the ontology pipeline: the thesaurus (RT, notes, deprecation).

All writes create a draft proposal; nothing is applied until a reviewer approves it.
"""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult
from pydantic import BaseModel, Field

from lakeloom.core.annotations import effective_graph
from lakeloom.core.operations import (
    AddHistoryNote,
    AddRelation,
    AddScopeNote,
    DeprecateConcept,
    ProposalOperation,
    UpdateConcept,
)
from lakeloom.core.thesaurus import (
    RelatedSuggestion,
    ThesaurusReport,
    suggest_related_from_cooccurrence,
    validate_ansi_z39,
)
from lakeloom.errors import LakeloomError
from lakeloom.rdf.skos_enrichment import concept_labels
from lakeloom.server.auth import Principal
from lakeloom.server.http_app import AppDeps
from lakeloom.server.tools_authoring import (
    DRAFT_NOTE,
    READ_ONLY,
    SCOPE_PROPOSE_TEXT,
    SCOPE_READ_TEXT,
    WRITE,
    proposal_result,
    resolve_in_model,
    tool_result,
    vocabulary_gap_warning,
)
from lakeloom.server.tools_common import (
    PROPOSE_SCOPE,
    READ_SCOPE,
    client_name,
    guarded,
)
from lakeloom.server.tools_proposals import ProposalResult
from lakeloom.untrusted import code_span as _code

__all__ = ["register_thesaurus_tools"]

VALIDATE_THESAURUS_DESCRIPTION = f"""\
Validate the thesaurus structure of a semantic model against ANSI Z39.19 / ISO 25964 rules:

- **BT/NT cycle check** (ERROR): detects cycles in the skos:broader/narrower hierarchy.
- **RT symmetry check** (WARNING): skos:related must be symmetric (A RT B → B RT A).
- **Definition coverage** (WARNING): concepts with no skos:definition or skos:scopeNote.

Returns a report with counts (total concepts, BT/NT pairs, RT pairs, defined concepts) \
and a list of issues.  Read-only, no side effects.  Unknown model → `not_found`.

{SCOPE_READ_TEXT}"""

SUGGEST_RELATED_TERMS_DESCRIPTION = f"""\
Suggest `skos:related` (RT) pairs for a model's concepts based on asset co-occurrence: \
two concepts that tag many of the same tables/columns are likely related.  Requires \
no embeddings or NLP — uses only the existing realization data.

`min_shared` (default 2): minimum shared assets for a pair to be suggested.
`min_score` (default 0.1): minimum Jaccard overlap score.
`limit` (default 20): maximum pairs returned.

Returns pairs sorted by overlap score descending.  Read-only, no side effects.  Unknown \
model → `not_found`.

{SCOPE_READ_TEXT}"""

PROPOSE_DEPRECATE_DESCRIPTION = f"""\
Mark a concept as deprecated.  The concept stays in the model (so existing realizations \
are not broken) but agents are directed to `successor` when one is given.  The deprecation \
is recorded as a `DeprecateConcept` operation in a new draft proposal.

`concept`: name or FQN of the concept to deprecate.
`reason`: why it is deprecated (required, ≤1000 chars).
`successor`: name or FQN of the replacement concept (optional).
`model`: target model slug.

{DRAFT_NOTE}

{SCOPE_PROPOSE_TEXT}"""

PROPOSE_ADD_SCOPE_NOTE_DESCRIPTION = f"""\
Attach a `skos:scopeNote` to an existing concept.  Scope notes clarify the intended use \
of a term, disambiguate it from similar concepts, or document governance decisions.

`concept`: name or FQN of the existing concept.
`note`: the scope note text (≤2000 chars).
`lang`: BCP 47 language tag (default `"en"`).
`model`: target model slug.

{DRAFT_NOTE}

{SCOPE_PROPOSE_TEXT}"""


PROPOSE_ADD_RELATION_DESCRIPTION = f"""\
Link two existing concepts of a model, as a thesaurus would:

- `related` (RT): an associative link, symmetric by definition (`skos:related` both ways). \
Stored as OpenMetadata's glossary `relatedTo` relationship, without a native ontology \
property. Use it for pairs from `suggest_related_terms` the user agrees with.
- `broader` (BT/NT): makes `target` the parent of `source` (the inverse NT follows). A \
concept has one parent: an existing parent is replaced. Cycles are refused.

`source`, `target`: name, FQN or IRI of concepts of `model`.

{DRAFT_NOTE}

{SCOPE_PROPOSE_TEXT}"""

PROPOSE_ADD_HISTORY_NOTE_DESCRIPTION = f"""\
Attach a `skos:historyNote` to an existing concept: when and why its meaning or use \
changed (renames, merges, split definitions). History notes are how a vocabulary stays \
trustworthy across releases.

`concept`: name, FQN or IRI. `note`: the text (≤2000 chars). `lang`: BCP 47 tag.

{DRAFT_NOTE}

{SCOPE_PROPOSE_TEXT}"""


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


def register_thesaurus_tools(mcp: MCPServer, deps: AppDeps) -> None:
    """Register the thesaurus tools on ``mcp``."""

    # ------------------------------------------------------------------
    # READ: validate_thesaurus
    # ------------------------------------------------------------------

    async def validate_thesaurus(
        model: Annotated[str, Field(description="Model slug (see list_models).")],
        ctx: Context,
    ) -> Annotated[CallToolResult, ThesaurusReportResult]:
        async def body(_: Principal) -> CallToolResult:
            graph = await effective_graph(deps.models, deps.proposal_repo, model)
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
            return tool_result(report.to_markdown(), data)

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
            from lakeloom.server.tools_analytics import load_realization_index

            index = await load_realization_index(deps, model)
            labels = concept_labels(index.graph)
            suggestions = suggest_related_from_cooccurrence(
                index.tables(),
                min_shared=min_shared,
                min_score=min_score,
                max_suggestions=limit,
            )
            lines = [f"**{len(suggestions)} RT suggestion(s)** for model {_code(model)}.", ""]
            for s in suggestions[:10]:
                a = labels.get(s.concept_a_fqn, s.concept_a_fqn)
                b = labels.get(s.concept_b_fqn, s.concept_b_fqn)
                lines.append(
                    f"- {_code(a)} ↔ {_code(b)} "
                    f"({s.shared_asset_count} shared assets, score {s.overlap_score:.2f})"
                )
            if len(suggestions) > 10:
                lines.append(f"… and {len(suggestions) - 10} more.")
            if index.truncated:
                lines.append("\n_Large model: only part of the realizations were read._")
            if suggestions:
                lines.append(
                    "\nNext: for each pair the user agrees with, call `propose_add_relation` "
                    "(`relation=related`)."
                )
            data = RelatedSuggestionsResult(
                model=model,
                total_suggestions=len(suggestions),
                suggestions=suggestions,
            )
            return tool_result("\n".join(lines), data)

        return await guarded("suggest_related_terms", ctx, READ_SCOPE, body)

    mcp.add_tool(
        suggest_related_terms,
        name="suggest_related_terms",
        title="Suggest related terms",
        description=SUGGEST_RELATED_TERMS_DESCRIPTION,
        annotations=READ_ONLY,
    )

    # ------------------------------------------------------------------
    # WRITE: propose_deprecate_concept
    # ------------------------------------------------------------------

    async def propose_deprecate_concept(
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
            fqn = await resolve_in_model(deps, model, concept)
            successor_fqn = await resolve_in_model(deps, model, successor) if successor else None
            if successor_fqn == fqn:
                raise LakeloomError(
                    "validation_error",
                    "a concept cannot be its own successor",
                    "pass a different concept as successor, or none",
                )
            op = DeprecateConcept(
                op="deprecate_concept",
                fqn=fqn,
                base_version="1",  # lakeloom-native op: never applied to OM, no version check
                reason=reason,
                successor=successor_fqn,
                rule_ids=["R-CV-DIRECT"],
            )
            p = await deps.proposals.propose_operations(
                model,
                [op],
                origin="direct",
                principal=principal,
                client=client_name(ctx),
                tool="propose_deprecate_concept",
                recheck_pending=True,
            )
            return proposal_result(p, deps)

        return await guarded(
            "propose_deprecate_concept",
            ctx,
            PROPOSE_SCOPE,
            body,
            audited=True,
            audit=deps.audit,
        )

    mcp.add_tool(
        propose_deprecate_concept,
        name="propose_deprecate_concept",
        title="Propose deprecate concept",
        description=PROPOSE_DEPRECATE_DESCRIPTION,
        annotations=WRITE,
    )

    # ------------------------------------------------------------------
    # WRITE: propose_add_scope_note
    # ------------------------------------------------------------------

    async def propose_add_scope_note(
        model: Annotated[str, Field(description="Model slug.")],
        concept: Annotated[str, Field(description="Name or FQN of the existing concept.")],
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
            op = AddScopeNote(
                op="add_scope_note",
                concept_fqn=await resolve_in_model(deps, model, concept),
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
            return proposal_result(p, deps)

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

    # ------------------------------------------------------------------
    # WRITE: propose_add_relation
    # ------------------------------------------------------------------

    async def propose_add_relation(
        model: Annotated[str, Field(description="Model slug.")],
        source: Annotated[str, Field(description="Name, FQN or IRI of the first concept.")],
        target: Annotated[str, Field(description="Name, FQN or IRI of the second concept.")],
        ctx: Context,
        relation: Annotated[
            Literal["related", "broader"],
            Field(description="`related` (RT, symmetric) or `broader` (target becomes parent)."),
        ] = "related",
        note: Annotated[
            str | None,
            Field(max_length=1000, description="Rationale recorded in the proposal."),
        ] = None,
    ) -> Annotated[CallToolResult, ProposalResult]:
        async def body(principal: Principal) -> CallToolResult:
            src = await resolve_in_model(deps, model, source)
            dst = await resolve_in_model(deps, model, target)
            if src == dst:
                raise LakeloomError(
                    "validation_error",
                    "a concept cannot be related to itself",
                    "pass two different concepts",
                )
            op: ProposalOperation
            if relation == "related":
                op = AddRelation(
                    op="add_relation",
                    source=src,
                    relation_type="relatedTo",
                    target=dst,
                    glossary_only=True,
                    rule_ids=["R-CV-DIRECT"],
                )
            else:
                if dst.startswith(src + "."):
                    raise LakeloomError(
                        "validation_error",
                        f"{dst} is below {src}: making it the parent would create a cycle",
                        "pick a parent outside the concept's own subtree",
                    )
                term = await deps.om.get_term(src, ("parent",))
                if term is None or term.version is None:
                    raise LakeloomError(
                        "not_found",
                        f"glossary term {src!r} was not found in OpenMetadata",
                        "check the concept with explain_concept",
                    )
                op = UpdateConcept(
                    op="update_concept",
                    fqn=src,
                    base_version=str(term.version),
                    parent=dst,
                    rule_ids=["R-CV-DIRECT"],
                )
            p = await deps.proposals.propose_operations(
                model,
                [op],
                origin="direct",
                principal=principal,
                client=client_name(ctx),
                tool="propose_add_relation",
                warnings=[
                    *await vocabulary_gap_warning(deps, model, "linking concepts"),
                    *([f"Author note: {note}"] if note else []),
                ],
                recheck_pending=True,
            )
            return proposal_result(p, deps)

        return await guarded(
            "propose_add_relation", ctx, PROPOSE_SCOPE, body, audited=True, audit=deps.audit
        )

    mcp.add_tool(
        propose_add_relation,
        name="propose_add_relation",
        title="Propose add relation",
        description=PROPOSE_ADD_RELATION_DESCRIPTION,
        annotations=WRITE,
    )

    # ------------------------------------------------------------------
    # WRITE: propose_add_history_note
    # ------------------------------------------------------------------

    async def propose_add_history_note(
        model: Annotated[str, Field(description="Model slug.")],
        concept: Annotated[str, Field(description="Name, FQN or IRI of the existing concept.")],
        note: Annotated[
            str, Field(min_length=1, max_length=2000, description="The history note text.")
        ],
        ctx: Context,
        lang: Annotated[
            str,
            Field(pattern=r"^[a-z]{2,3}(-[A-Z]{2})?$", description="BCP 47 language tag."),
        ] = "en",
    ) -> Annotated[CallToolResult, ProposalResult]:
        async def body(principal: Principal) -> CallToolResult:
            op = AddHistoryNote(
                op="add_history_note",
                concept_fqn=await resolve_in_model(deps, model, concept),
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
                tool="propose_add_history_note",
                recheck_pending=True,
            )
            return proposal_result(p, deps)

        return await guarded(
            "propose_add_history_note", ctx, PROPOSE_SCOPE, body, audited=True, audit=deps.audit
        )

    mcp.add_tool(
        propose_add_history_note,
        name="propose_add_history_note",
        title="Propose add history note",
        description=PROPOSE_ADD_HISTORY_NOTE_DESCRIPTION,
        annotations=WRITE,
    )
