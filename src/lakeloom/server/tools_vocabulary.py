"""MCP tools for stage 1 of the ontology pipeline: the controlled vocabulary.

All writes create a draft proposal; nothing is applied until a reviewer approves it.
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult
from pydantic import BaseModel, Field

from lakeloom.core.vocabulary import (
    FolksonomyCandidate,
    TermDraft,
    batch_to_ops,
    harvest_folksonomy,
)
from lakeloom.server.auth import Principal
from lakeloom.server.http_app import AppDeps
from lakeloom.server.tools_authoring import (
    DRAFT_NOTE,
    MAX_TAGS,
    MAX_TERMS_PER_CALL,
    READ_ONLY,
    SCOPE_PROPOSE_TEXT,
    SCOPE_READ_TEXT,
    WRITE,
    proposal_result,
    tool_result,
)
from lakeloom.server.tools_common import (
    PROPOSE_SCOPE,
    READ_SCOPE,
    client_name,
    guarded,
)
from lakeloom.server.tools_proposals import ProposalResult

__all__ = ["register_vocabulary_tools"]

LIST_CV_CANDIDATES_DESCRIPTION = f"""\
Normalise a list of raw tag strings (OM classification FQNs, column names, free labels) \
into candidate term names for a Controlled Vocabulary.  Strips classification prefixes \
(`PII.`, `Domain.`), replaces underscores with spaces, title-cases, and validates the \
OM name pattern.  Tags that cannot be normalised are silently dropped.

Pass `frequency_map` to rank candidates by asset count.  Returns candidates sorted by \
frequency descending.  Read-only, no side effects.

{SCOPE_READ_TEXT}"""

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
{DRAFT_NOTE}

{SCOPE_PROPOSE_TEXT}"""


class CVCandidatesResult(BaseModel):
    total_input: int
    total_valid: int
    candidates: list[FolksonomyCandidate]


def register_vocabulary_tools(mcp: MCPServer, deps: AppDeps) -> None:
    """Register the vocabulary tools on ``mcp``."""

    # ------------------------------------------------------------------
    # READ: harvest_vocabulary_candidates
    # ------------------------------------------------------------------

    async def harvest_vocabulary_candidates(
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
                "\nNext: pass the names you want to `propose_add_concepts` as `TermDraft` items."
            )
            data = CVCandidatesResult(
                total_input=len(tags),
                total_valid=valid,
                candidates=candidates,
            )
            return tool_result("\n".join(text_lines), data)

        return await guarded("harvest_vocabulary_candidates", ctx, READ_SCOPE, body)

    mcp.add_tool(
        harvest_vocabulary_candidates,
        name="harvest_vocabulary_candidates",
        title="List CV candidates",
        description=LIST_CV_CANDIDATES_DESCRIPTION,
        annotations=READ_ONLY,
    )

    # ------------------------------------------------------------------
    # WRITE: propose_add_concepts
    # ------------------------------------------------------------------

    async def propose_add_concepts(
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
            Field(
                max_length=1000, description="Rationale for this batch (recorded in provenance)."
            ),
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
                tool="propose_add_concepts",
                warnings=warnings,
                recheck_pending=True,
            )
            return proposal_result(p, deps)

        return await guarded(
            "propose_add_concepts", ctx, PROPOSE_SCOPE, body, audited=True, audit=deps.audit
        )

    mcp.add_tool(
        propose_add_concepts,
        name="propose_add_concepts",
        title="Propose add term",
        description=PROPOSE_ADD_TERM_DESCRIPTION,
        annotations=WRITE,
    )
