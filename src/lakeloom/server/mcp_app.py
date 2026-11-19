"""MCP server over Streamable HTTP at ``/mcp`` (FR-AGT-01, FR-AGT-02, FR-AGT-05, FR-AGT-06).

Built on ``mcp`` 2.x, where ``FastMCP`` became ``mcp.server.mcpserver.MCPServer``.
Authentication is not the SDK's: :class:`~lakeloom.server.auth.BearerAuthMiddleware`
guards ``/mcp`` and stores the :class:`Principal` in the ASGI scope state, which tools
read through ``ctx.request_context.request``. Tools check their own scope and answer a
missing one with a typed ``forbidden`` error result (FR-AGT-05).

Tools: ``get_server_info`` (FR-OM-06, §7: scope ``lakeloom:read``, no side effects), the
source/scan tools of :mod:`lakeloom.server.tools_sources` (M1a) and the semantic read
tools of :mod:`lakeloom.server.tools_semantic` (M1b), the model and proposal tools of
:mod:`lakeloom.server.tools_proposals` and the prompts of :mod:`lakeloom.server.prompts`
(M2a), the review tools of :mod:`lakeloom.server.tools_review` (M2b), the release tools of
:mod:`lakeloom.server.tools_release` (M3b). Shared plumbing
(scope checks, typed errors, metrics) lives in :mod:`lakeloom.server.tools_common`.
"""

from contextlib import AbstractAsyncContextManager
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import FastAPI
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations

import lakeloom
from lakeloom.config import Settings
from lakeloom.server.auth import Principal
from lakeloom.server.http_app import AppDeps, create_app
from lakeloom.server.mcp_sessions import SessionBindingMiddleware
from lakeloom.server.prompts import register_prompts
from lakeloom.server.resources import LakeloomMCP
from lakeloom.server.server_info import ServerInfo, build_server_info, summarize
from lakeloom.server.tools_analytics import register_analytics_tools
from lakeloom.server.tools_common import READ_SCOPE, guarded, tool_error
from lakeloom.server.tools_deliverables import register_deliverable_tools
from lakeloom.server.tools_guide import register_guide_tools
from lakeloom.server.tools_import import register_import_tools
from lakeloom.server.tools_linking import register_linking_tools
from lakeloom.server.tools_map import register_map_tools
from lakeloom.server.tools_proposals import register_proposal_tools
from lakeloom.server.tools_release import register_release_tools
from lakeloom.server.tools_review import register_review_tools
from lakeloom.server.tools_semantic import register_semantic_tools
from lakeloom.server.tools_sources import register_source_tools
from lakeloom.server.tools_taxonomy import register_taxonomy_tools
from lakeloom.server.tools_thesaurus import register_thesaurus_tools
from lakeloom.server.tools_vocabulary import register_vocabulary_tools

__all__ = ["MCP_PATH", "create_mcp", "create_server_app", "tool_error", "transport_security"]

MCP_PATH = "/mcp"

GET_SERVER_INFO_DESCRIPTION = """\
Describe this Lakeloom server: its version and environment, the connected OpenMetadata \
instance (version, whether it is reachable, whether its RDF/knowledge-graph support is \
enabled), which optional features are switched on (triplestore, stdio, token_exchange, \
releases; \
these are configuration flags, not a guarantee the capability is live in this release), the \
configured lake sources and semantic models, and who you are authenticated as (subject, \
principal type, granted scopes).

Call this first to learn what the server can do before using other tools. Takes no \
arguments. Read-only with no side effects; safe to call repeatedly. If OpenMetadata is \
down the call still succeeds and reports `openmetadata.reachable: false` with an error.

Requires scope `lakeloom:read`; without it the call fails with error code `forbidden`."""


def create_mcp(deps: AppDeps) -> LakeloomMCP:
    """The MCP server with Lakeloom's tools registered."""
    mcp = LakeloomMCP(
        deps,
        name="lakeloom",
        title="Lakeloom",
        version=lakeloom.__version__,
        instructions=(
            "Lakeloom builds semantic models of a data lake in OpenMetadata, following the "
            "ontology pipeline: controlled vocabulary, metadata profile (MAP), taxonomy, "
            "thesaurus, ontology, knowledge graph (the method of Jessica Talisman's book "
            "Ontology Pipeline; The Ontology Pipeline is her registered trademark). Start "
            "with get_server_info (versions, "
            "features, your scopes). When the user "
            "is unsure what to do on a model, call get_next_steps: it reports the model's stage "
            "and ranks the next steps, each with the tool to call; the build_ontology prompt "
            "walks a non-expert through the stages. "
            "Explore: list_sources, list_tables, describe_table, scan_lake_source. "
            "Read: list_models, find_concepts, explain_concept (shows deprecations and notes), "
            "find_assets_for_concept, get_asset_context, export_model (format skos includes "
            "the governance annotations), validate_model. "
            "Vocabulary: harvest_vocabulary_candidates, propose_add_concepts. MAP: build_map, "
            "validate_map, export_map, propose_realization. Taxonomy: "
            "propose_add_hierarchy, assess_taxonomy_coverage. Thesaurus: validate_thesaurus, "
            "suggest_related_terms, propose_add_relation, propose_add_scope_note, "
            "propose_add_history_note, propose_deprecate_concept. External vocabularies: "
            "suggest_external_mappings, propose_external_mappings. Analytics: get_model_stats, "
            "assess_realization_coverage, get_annotation_stats, list_deprecated_concepts. "
            "Every change is a draft proposal (also propose_from_scan, propose_change): "
            "get_proposal and diff_proposal show it, submit_proposal and discard_proposal move "
            "it on, rebase_proposal refreshes it. Show proposal summaries and warnings to the "
            "user and submit only when they agree. Nothing is applied until a human approves: "
            "list_pending_reviews and review_proposal are for human reviewers (an agent asks "
            "its human to decide, never decides itself). detect_drift compares a model with "
            "the lake. create_release tags the model in Git (dry run first, then confirm); "
            "list_releases and diff_releases read released versions. Prompts: build_ontology, "
            "bootstrap_data_product, explain_table, review_with_specialist, drift_check. "
            "The same artefacts are MCP resources: lakeloom://models, "
            "lakeloom://models/{slug}/ontology.{ttl|jsonld|owl|nt}, "
            "lakeloom://models/{slug}/shapes.ttl, lakeloom://models/{slug}/context.jsonld "
            "(each with ?version= for a release) and lakeloom://assets/{fqn}/context.md."
        ),
        log_level="WARNING",
    )

    async def get_server_info(ctx: Context) -> Annotated[CallToolResult, ServerInfo]:
        async def body(principal: Principal) -> CallToolResult:
            info = await build_server_info(deps, principal)
            return CallToolResult(
                content=[TextContent(type="text", text=summarize(info))],
                structured_content=info.model_dump(mode="json"),
            )

        return await guarded("get_server_info", ctx, READ_SCOPE, body)

    mcp.add_tool(
        get_server_info,
        name="get_server_info",
        title="Get server info",
        description=GET_SERVER_INFO_DESCRIPTION,
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    register_source_tools(mcp, deps)
    register_semantic_tools(mcp, deps)
    register_proposal_tools(mcp, deps)
    register_review_tools(mcp, deps)
    register_release_tools(mcp, deps)
    register_vocabulary_tools(mcp, deps)
    register_taxonomy_tools(mcp, deps)
    register_thesaurus_tools(mcp, deps)
    register_map_tools(mcp, deps)
    register_linking_tools(mcp, deps)
    register_analytics_tools(mcp, deps)
    register_deliverable_tools(mcp, deps)
    register_guide_tools(mcp, deps)
    register_import_tools(mcp, deps)
    register_prompts(mcp)
    return mcp


def transport_security(settings: Settings) -> TransportSecuritySettings:
    """DNS-rebinding protection: accept the public host plus loopback names (any port)."""
    hosts = ["localhost", "127.0.0.1", "[::1]"]
    origins: list[str] = []
    for loopback in hosts:
        origins += [f"http://{loopback}:*", f"http://{loopback}", f"https://{loopback}:*"]
    allowed_hosts = [h for name in hosts for h in (name, f"{name}:*")]
    if settings.public_base_url:
        public = urlsplit(settings.public_base_url)
        if public.netloc:
            allowed_hosts += [public.netloc, f"{public.hostname}:*"]
            origins.append(f"{public.scheme}://{public.netloc}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=origins,
    )


def create_server_app(deps: AppDeps) -> FastAPI:
    """The complete Lakeloom HTTP app: health, metrics, auth and the MCP endpoint."""
    mcp = create_mcp(deps)
    stateless = deps.settings.mcp_stateless
    # The Starlette app routes on the full path, so it is served as an exact-path
    # endpoint at /mcp (never /mcp/mcp). Its own lifespan is not used by a Route, so
    # the session manager runs in the FastAPI lifespan instead.
    mcp_http = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        transport_security=transport_security(deps.settings),
        stateless_http=stateless,
        json_response=stateless,
    )

    def session_manager() -> AbstractAsyncContextManager[None]:
        return mcp.session_manager.run()

    return create_app(
        deps,
        endpoints=[(MCP_PATH, SessionBindingMiddleware(mcp_http, enabled=not stateless))],
        lifespans=[session_manager],
    )
