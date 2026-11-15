"""MCP tools for releases (FR-GIT-06, FR-PUB-02, FR-AGT-03, NFR-SEC-12).

``create_release`` (scope ``lakeloom:publish``) tags OpenMetadata's current state of a model in
the ontology repository behind a confirm token; ``list_releases`` and ``diff_releases``
(``lakeloom:read``) read the released versions from their Git tags. With Git publishing off
(``AppDeps.release_service`` and ``reader`` are ``None``) every tool here is a
``validation_error``.

Audit (NFR-SEC-12): ``create_release`` runs through ``guarded(audited=True, audit=deps.audit)``,
which writes the only audit row (the service is built with ``audit=None`` and fills the
running call's audit context: model, and the tag as change set). Labels and IRIs from the
model are quoted as code spans by the core renderers.
"""

import asyncio
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import BaseModel, Field

from lakeloom.core.audit import current_audit
from lakeloom.core.release_diff import ReleaseDiff, diff_graphs, diff_markdown, parse_version
from lakeloom.core.releases import ReleasePlan, ReleaseResult, ReleaseService
from lakeloom.errors import LakeloomError
from lakeloom.gitstore.release_reader import ReleaseInfo, ReleaseReader
from lakeloom.rdf.canonical import parse_rdf
from lakeloom.server.auth import Principal
from lakeloom.server.http_app import AppDeps
from lakeloom.server.tools_common import PUBLISH_SCOPE, READ_SCOPE, client_name, guarded
from lakeloom.server.tools_proposals import (
    DESTRUCTIVE,
    READ_ONLY,
    _confirm_line,  # pyright: ignore[reportPrivateUsage]
    _result,  # pyright: ignore[reportPrivateUsage]
    _scope,  # pyright: ignore[reportPrivateUsage]
)
from lakeloom.untrusted import code_span as _code

__all__ = [
    "CreateReleaseResult",
    "ReleaseList",
    "plan_text",
    "register_release_tools",
    "released_text",
]

CREATE_RELEASE_DESCRIPTION = f"""\
Release a model: commit OpenMetadata's current state of the model (its five exports, manifest, \
CHANGELOG and README) directly on the ontology repository's target branch, tag it \
`<model>/v<version>` (annotated) and push branch and tag together. The version is computed \
from a semantic diff against the previous release (first release `1.0.0`; a removed term or \
structural triple → major, an added one → minor, only labels or definitions → patch); pass \
`bump` (major, minor or patch) or an explicit `version` (MAJOR.MINOR.PATCH) to override, \
never both. The result must be higher than every existing release.

Two steps (FR-AGT-03). Without `confirm_token` this is a dry run: nothing is written, and the \
result is the plan (version, tag, previous release, bump and its reason, warnings, the \
CHANGELOG section) plus a `confirm_token` (valid 10 minutes by default, bound to you, these \
arguments, the model's state and the previous release). Show the plan to the user, then call \
again with the same arguments and `confirm_token`.

Side effect on confirmation: writes a commit and a tag to the Git remote; a tag cannot be \
taken back from here. Retrying the same confirmed call is idempotent (one tag, \
`already_done: true`). A model change or another release since the dry run → `conflict`; \
nothing changed since the previous release, approved changes not yet in Git, an existing \
tag or Git publishing off → `validation_error`; a model whose `publish_groups` exclude you \
→ `forbidden`.

{_scope(PUBLISH_SCOPE)}"""

LIST_RELEASES_DESCRIPTION = f"""\
List the released versions of a model, highest version first: version, tag, tag object and \
commit sha, and the tag date. Read from the Git remote's tags (refreshed at most every 30 \
seconds), so tags made outside Lakeloom count too. Read one with export_model or the \
resources' `?version=`, compare two with diff_releases. Git publishing off → \
`validation_error`. Read-only, no side effects.

{_scope(READ_SCOPE)}"""

DIFF_RELEASES_DESCRIPTION = f"""\
Compare two versions of a model term by term: the terms added, changed and removed, the \
number of triples added and removed, and the bump the difference implies (major, minor, \
patch, or none when identical). `from_version` is a release; `to_version` is a release or \
`latest` (default: OpenMetadata's current state, which is what the next release would \
contain). Returns a Markdown summary and the structured diff. A malformed version → \
`validation_error`; an unknown release → `not_found`; Git publishing off → \
`validation_error`. Labels and IRIs come from the model and are quoted. Read-only, no side \
effects.

{_scope(READ_SCOPE)}"""


class CreateReleaseResult(BaseModel):
    released: bool = Field(description="False for the dry run (preview), true once released.")
    preview: ReleasePlan | None = Field(
        default=None, description="Dry run only: the plan and its confirm_token."
    )
    result: ReleaseResult | None = Field(
        default=None, description="After confirmation: the release record and its plan."
    )


class ReleaseList(BaseModel):
    model: str
    releases: list[ReleaseInfo] = Field(description="Highest version first.")


def _git_off() -> LakeloomError:
    return LakeloomError(
        "validation_error",
        "Git publishing is off",
        "set LAKELOOM_GIT_REMOTE (and the Git credentials) to enable releases",
    )


def _service(deps: AppDeps) -> ReleaseService:
    if deps.release_service is None:
        raise _git_off()
    return deps.release_service


def _reader(deps: AppDeps) -> ReleaseReader:
    if deps.reader is None:
        raise _git_off()
    return deps.reader


def plan_text(plan: ReleasePlan) -> str:
    """The dry run as Markdown (shared with ``lakeloom release create``)."""
    previous = _code(plan.previous) if plan.previous else "none (first release)"
    lines = [
        f"# Release {_code(plan.tag)} of model {_code(plan.model)}",
        "",
        f"- Version: {_code(plan.version)} (previous: {previous})",
        f"- Bump: {plan.diff.bump or 'none'} ({plan.diff.reason})",
    ]
    lines += [f"- Warning: {w}" for w in plan.warnings]
    return "\n".join(lines) + "\n\n" + plan.changelog_section.rstrip("\n") + "\n"


def released_text(out: ReleaseResult) -> str:
    """A done release as Markdown (shared with ``lakeloom release create --yes``)."""
    rel = out.release
    head = (
        f"Release {_code(rel.tag)} already exists (this confirmed call was completed earlier)."
        if out.already_done
        else f"Released {_code(rel.tag)}."
    )
    return (
        f"{head}\n\n- Commit: {_code(rel.commit_sha[:7])}\n- Released by: {_code(rel.released_by)}"
        f"\n- Triple store: {out.triplestore}\n"
    )


def _check_version(version: str) -> None:
    if version != "latest":
        parse_version(version)


def register_release_tools(mcp: MCPServer, deps: AppDeps) -> None:
    """Register create_release, list_releases and diff_releases on ``mcp``."""

    async def create_release(
        model: Annotated[str, Field(description="Model slug, as returned by list_models.")],
        ctx: Context,
        version: Annotated[
            str | None,
            Field(description="Explicit MAJOR.MINOR.PATCH; omit to use the computed version."),
        ] = None,
        bump: Annotated[
            Literal["major", "minor", "patch"] | None,
            Field(description="Override the computed bump; not together with version."),
        ] = None,
        confirm_token: Annotated[
            str | None,
            Field(description="Token from the dry run; omit it to preview without changes."),
        ] = None,
    ) -> Annotated[CallToolResult, CreateReleaseResult]:
        async def body(principal: Principal) -> CallToolResult:
            current_audit().model = model
            svc = _service(deps)
            if confirm_token is None:
                plan = await svc.plan(principal, model, version=version, bump=bump)
                text = f"{plan_text(plan)}\n{_confirm_line('create_release', plan.confirm_token)}"
                return _result(text, CreateReleaseResult(released=False, preview=plan))
            out = await svc.create(
                principal,
                model,
                version=version,
                bump=bump,
                confirm_token=confirm_token,
                client=client_name(ctx),
            )
            return _result(released_text(out), CreateReleaseResult(released=True, result=out))

        return await guarded(
            "create_release", ctx, PUBLISH_SCOPE, body, audited=True, audit=deps.audit
        )

    async def list_releases(
        model: Annotated[str, Field(description="Model slug, as returned by list_models.")],
        ctx: Context,
    ) -> Annotated[CallToolResult, ReleaseList]:
        async def body(_: Principal) -> CallToolResult:
            releases = await _reader(deps).list(model)
            if not releases:
                text = f"Model {_code(model)} has no releases yet.\n"
            else:
                lines = [f"**{len(releases)} release(s)** of model {_code(model)}"]
                lines += [
                    f"- {_code(r.version)}: tag {_code(r.tag)}, commit "
                    f"{_code(r.commit_sha[:7])}, {r.released_at.isoformat()}"
                    for r in releases
                ]
                text = "\n".join(lines) + "\n"
            return _result(text, ReleaseList(model=model, releases=releases))

        return await guarded("list_releases", ctx, READ_SCOPE, body)

    async def diff_releases(
        model: Annotated[str, Field(description="Model slug, as returned by list_models.")],
        from_version: Annotated[str, Field(description="The older release, MAJOR.MINOR.PATCH.")],
        ctx: Context,
        to_version: Annotated[
            str,
            Field(description="The newer release, or latest (default: the current state)."),
        ] = "latest",
    ) -> Annotated[CallToolResult, ReleaseDiff]:
        async def body(_: Principal) -> CallToolResult:
            _check_version(from_version)
            _check_version(to_version)
            reader = _reader(deps)

            async def turtle(version: str) -> bytes:
                if version == "latest":
                    return await deps.models.export(model, "turtle")
                return await reader.ontology(model, version, "turtle")

            old_ttl, new_ttl = await turtle(from_version), await turtle(to_version)
            old = await asyncio.to_thread(parse_rdf, old_ttl, "turtle")
            new = await asyncio.to_thread(parse_rdf, new_ttl, "turtle")
            diff = await asyncio.to_thread(
                diff_graphs, old, new, from_version=from_version, to_version=to_version
            )
            return _result(diff_markdown(diff), diff)

        return await guarded("diff_releases", ctx, READ_SCOPE, body)

    tools: list[tuple[Any, str, str, str, ToolAnnotations]] = [
        (
            create_release,
            "create_release",
            "Create release",
            CREATE_RELEASE_DESCRIPTION,
            DESTRUCTIVE,
        ),
        (list_releases, "list_releases", "List releases", LIST_RELEASES_DESCRIPTION, READ_ONLY),
        (diff_releases, "diff_releases", "Diff releases", DIFF_RELEASES_DESCRIPTION, READ_ONLY),
    ]
    for fn, name, title, description, annotations in tools:
        mcp.add_tool(fn, name=name, title=title, description=description, annotations=annotations)
