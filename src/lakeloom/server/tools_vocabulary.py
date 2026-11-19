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

HARVEST_VOCABULARY_CANDIDATES_DESCRIPTION = f"""\
Stage 1 of the ontology pipeline (controlled vocabulary): harvest candidate concept names \
from the folksonomy the organisation already uses. With no `tags`, reads OpenMetadata's \
classification tags and ranks them by how many assets use them; with `tags`, normalises \
the strings you pass (classification FQNs, column names, free labels).

Normalisation strips classification prefixes (`PII.`, `Domain.`), replaces underscores \
with spaces, title-cases and checks the OM term-name pattern; strings that cannot be \
normalised are dropped. Candidates are suggestions: agree on names and write a definition \
for each before calling `propose_add_concepts`. Read-only, no side effects.

{SCOPE_READ_TEXT}"""

PROPOSE_ADD_CONCEPTS_DESCRIPTION = f"""\
Add one or more new concepts directly to a semantic model — no lake scan required.  \
Each term in `terms` is a `TermDraft`:
- `name` (required): OM term name pattern (letter, then letters/digits/spaces/underscores, ≤128).
- `definition`: skos:definition text. Strongly recommended: a concept without a definition \
is a label, not shared meaning, and the proposal carries a warning for each one.
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


async def _om_tags(deps: AppDeps) -> tuple[list[str], dict[str, int]]:
    """Every live classification tag of OM (up to MAX_TAGS) and its usage count."""
    names: list[str] = []
    counts: dict[str, int] = {}
    after: str | None = None
    while len(names) < MAX_TAGS:
        page = await deps.om.list_tags(after=after, limit=500)
        for t in page.data:
            if t.deprecated or t.disabled:
                continue
            names.append(t.fully_qualified_name)
            counts[t.fully_qualified_name] = t.usage_count or 0
        after = page.after
        if not after:
            break
    return names[:MAX_TAGS], counts


def register_vocabulary_tools(mcp: MCPServer, deps: AppDeps) -> None:
    """Register the vocabulary tools on ``mcp``."""

    # ------------------------------------------------------------------
    # READ: harvest_vocabulary_candidates
    # ------------------------------------------------------------------

    async def harvest_vocabulary_candidates(
        ctx: Context,
        tags: Annotated[
            list[str] | None,
            Field(
                max_length=MAX_TAGS,
                description=(
                    "Raw strings to normalise (classification FQNs such as `PII.Email`, "
                    "column names, free labels; at most 1000). Omit to harvest "
                    "OpenMetadata's classification tags, ranked by usage."
                ),
            ),
        ] = None,
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
            raw = list(tags or [])
            freq = dict(frequency_map or {})
            source = "the given strings"
            if not raw:
                raw, freq = await _om_tags(deps)
                source = "OpenMetadata classification tags"
            candidates = harvest_folksonomy(raw, frequency_map=freq)
            valid = len(candidates)
            text_lines = [
                f"**{valid} valid candidate(s)** from {len(raw)} of {source}.",
                "",
            ]
            for c in candidates[:20]:
                used = f" (used {c.frequency}x)" if c.frequency else ""
                text_lines.append(f"- `{c.name}`{used} ← `{c.source_tag}`")
            if valid > 20:
                text_lines.append(f"… and {valid - 20} more (see structured content).")
            text_lines.append(
                "\nNext: pass the names you want to `propose_add_concepts` as `TermDraft` items."
            )
            data = CVCandidatesResult(
                total_input=len(raw),
                total_valid=valid,
                candidates=candidates,
            )
            return tool_result("\n".join(text_lines), data)

        return await guarded("harvest_vocabulary_candidates", ctx, READ_SCOPE, body)

    mcp.add_tool(
        harvest_vocabulary_candidates,
        name="harvest_vocabulary_candidates",
        title="Harvest vocabulary candidates",
        description=HARVEST_VOCABULARY_CANDIDATES_DESCRIPTION,
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
            undefined = [t.name for t in terms if not (t.definition or "").strip()]
            if undefined:
                shown = ", ".join(undefined[:10]) + (" …" if len(undefined) > 10 else "")
                warnings.append(
                    f"{len(undefined)} concept(s) have no definition ({shown}): add one before "
                    "review, or reviewers and agents will guess the meaning"
                )
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
        title="Propose add concepts",
        description=PROPOSE_ADD_CONCEPTS_DESCRIPTION,
        annotations=WRITE,
    )
