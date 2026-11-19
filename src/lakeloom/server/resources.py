"""MCP resources for models and asset context (FR-PUB-01).

The URIs are ``lakeloom://models``, ``lakeloom://models/{slug}/ontology.{ext}`` (``ttl``,
``jsonld``, ``owl``, ``nt``), ``lakeloom://models/{slug}/shapes.ttl``,
``lakeloom://models/{slug}/context.jsonld`` and
``lakeloom://assets/{fqn}/context.md`` (FQN percent-encoded), each optionally pinned with
``?version=``. The ontology, shapes and context serve a release from its Git tag with
``?version=MAJOR.MINOR.PATCH``; the models list and asset context exist only at ``latest``.

A read builds the same :class:`~lakeloom.publish.artifacts.Artifact` as the HTTP API and returns
its body unchanged, so the bytes equal the HTTP response for that representation. The server
routes the URIs itself (rather than through the SDK's template matcher) because the model list
is dynamic, ``?version=`` must reach the builders and the FQN is percent-decoded here.
Failures are MCP errors whose ``data`` is ``{"error": {"code", "message", "hint"}}``.
"""

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any, Final, Literal
from urllib.parse import parse_qs, unquote

from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    GetPromptRequestParams,
    GetPromptResult,
    InputRequiredResult,
    ListPromptsResult,
    ListResourcesResult,
    ListResourceTemplatesResult,
    PaginatedRequestParams,
    Resource,
    ResourceTemplate,
)
from opentelemetry.trace import Span
from pydantic import AnyUrl

from lakeloom.errors import LakeloomError, RateLimited
from lakeloom.metrics import RATE_LIMITED, RESOURCE_READS
from lakeloom.publish.artifacts import (
    Artifact,
    asset_context_artifact,
    model_artifact,
    model_context_artifact,
    models_list_artifact,
    require_latest,
    shapes_artifact,
)
from lakeloom.publish.negotiation import MediaType
from lakeloom.server.ratelimit import principal_key
from lakeloom.server.tools_common import READ_SCOPE, current_principal
from lakeloom.telemetry import record_resource_read, start_span

if TYPE_CHECKING:
    from lakeloom.server.http_app import AppDeps
    from lakeloom.server.ratelimit import RateLimiter

__all__ = ["MAX_LISTED_MODELS", "LakeloomMCP"]

SCHEME: Final = "lakeloom://"
MAX_LISTED_MODELS: Final = 100
MEDIA_BY_EXT: Final[dict[str, MediaType]] = {
    "ttl": "text/turtle",
    "jsonld": "application/ld+json",
    "owl": "application/rdf+xml",
    "nt": "application/n-triples",
}
_log = logging.getLogger("lakeloom.mcp")

TEMPLATES: Final[tuple[tuple[str, str, str, str], ...]] = (
    (
        "lakeloom://models/{slug}/ontology.{ext}",
        "model_ontology",
        "The model as an ontology; ext is ttl, jsonld, owl or nt. Add ?version=1.0.0 to read "
        "a release (default latest).",
        "text/turtle",
    ),
    (
        "lakeloom://models/{slug}/context.jsonld",
        "model_context",
        "The full JSON-LD context of the model; ?version= reads a release.",
        "application/ld+json",
    ),
    (
        "lakeloom://models/{slug}/shapes.ttl",
        "model_shapes",
        "The model's SHACL shapes as Turtle; ?version= reads a release.",
        "text/turtle",
    ),
    (
        "lakeloom://models/{slug}/deliverables/{filename}",
        "model_deliverable",
        "A stage deliverable: glossary.md/.csv, data-dictionary.md/.csv, taxonomy.md/.mmd/.csv, "
        "synonyms.txt/.csv or deprecations.csv; ?version= reads a release's copy.",
        "text/markdown",
    ),
    (
        "lakeloom://assets/{fqn}/context.md",
        "asset_context",
        "Everything known about an asset as Markdown; percent-encode the table FQN.",
        "text/markdown",
    ),
)


def _mcp_error(error: LakeloomError) -> MCPError:
    code = INVALID_PARAMS if error.code in ("not_found", "validation_error") else INTERNAL_ERROR
    return MCPError(code=code, message=error.message, data=error.to_dict())


Kind = Literal["models", "ontology", "shapes", "context", "asset", "deliverable"]


class LakeloomMCP(MCPServer[Any]):
    """:class:`MCPServer` that serves Lakeloom's publication artefacts as resources."""

    def __init__(self, deps: "AppDeps", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._deps = deps

    @property
    def rate_limiter(self) -> "RateLimiter":
        """The per-principal limiter ``guarded()`` checks before each tool body (NFR-SEC-16)."""
        return self._deps.limiter

    def _require_read(self, context: Context[Any, Any] | None, what: str = "MCP resources") -> None:
        principal = None if context is None else current_principal(context)
        if principal is None or READ_SCOPE not in principal.scopes:
            raise _mcp_error(
                LakeloomError(
                    "forbidden",
                    f"{what} require the {READ_SCOPE} scope",
                    f"token lacks {READ_SCOPE}",
                )
            )

    def _check_rate(self, context: Context[Any, Any] | None) -> None:
        """The same bucket as ``guarded()`` (NFR-SEC-16): a resource read is a call, unlike
        ``initialize`` and the ``*/list`` requests."""
        principal = None if context is None else current_principal(context)
        if principal is None:
            return  # _require_read refused it already
        retry_after = self._deps.limiter.check(principal_key(principal))
        if retry_after is not None:
            RATE_LIMITED.labels(surface="mcp").inc()
            limited = RateLimited(retry_after)
            raise MCPError(
                code=INTERNAL_ERROR,
                message=limited.message,
                data={**limited.to_dict(), **limited.details()},
            )

    def _context(self, ctx: ServerRequestContext[Any]) -> Context[Any, Any]:
        return Context(request_context=ctx, mcp_server=self)

    async def _handle_list_resources(
        self, ctx: ServerRequestContext[Any], params: PaginatedRequestParams | None
    ) -> ListResourcesResult:
        # The SDK does not forward the request to list(); the scope is checked here (NFR-SEC-06).
        self._require_read(self._context(ctx))
        return await super()._handle_list_resources(ctx, params)

    async def _handle_list_resource_templates(
        self, ctx: ServerRequestContext[Any], params: PaginatedRequestParams | None
    ) -> ListResourceTemplatesResult:
        self._require_read(self._context(ctx))
        return await super()._handle_list_resource_templates(ctx, params)

    async def _handle_list_prompts(
        self, ctx: ServerRequestContext[Any], params: PaginatedRequestParams | None
    ) -> ListPromptsResult:
        self._require_read(self._context(ctx), "MCP prompts")
        return await super()._handle_list_prompts(ctx, params)

    async def _handle_get_prompt(
        self, ctx: ServerRequestContext[Any], params: GetPromptRequestParams
    ) -> GetPromptResult | InputRequiredResult:
        self._require_read(self._context(ctx), "MCP prompts")
        return await super()._handle_get_prompt(ctx, params)

    async def list_resources(self) -> list[Resource]:
        """``lakeloom://models`` and the concrete URIs of up to 100 models."""
        out = [
            Resource(
                uri="lakeloom://models",
                name="models",
                description="The semantic models (glossaries) Lakeloom publishes.",
                mime_type="application/json",
            )
        ]
        try:
            page = await self._deps.models.list_models(None, MAX_LISTED_MODELS)
        except LakeloomError as exc:  # OM down: still list what needs no lookup
            _log.warning("resources/list: model lookup failed (%s)", exc.code)
            return out
        for info in page.items[:MAX_LISTED_MODELS]:
            for ext, media in MEDIA_BY_EXT.items():
                out.append(
                    Resource(
                        uri=f"lakeloom://models/{info.slug}/ontology.{ext}",
                        name=f"{info.slug} ontology ({ext})",
                        mime_type=media,
                    )
                )
            out.append(
                Resource(
                    uri=f"lakeloom://models/{info.slug}/shapes.ttl",
                    name=f"{info.slug} SHACL shapes",
                    mime_type="text/turtle",
                )
            )
            out.append(
                Resource(
                    uri=f"lakeloom://models/{info.slug}/context.jsonld",
                    name=f"{info.slug} JSON-LD context",
                    mime_type="application/ld+json",
                )
            )
        return out

    async def list_resource_templates(self) -> list[ResourceTemplate]:
        return [
            ResourceTemplate(uri_template=uri, name=name, description=description, mime_type=mime)
            for uri, name, description, mime in TEMPLATES
        ]

    async def read_resource(
        self, uri: AnyUrl | str, context: Context[Any, Any] | None = None
    ) -> Iterable[ReadResourceContents]:
        text = str(uri)
        # The span names the resource kind only, never the URI (it can carry an FQN).
        with start_span("mcp.resource") as span:
            return await self._read(text, context, span)

    def _count(self, span: Span, kind: str, outcome: str) -> None:
        span.set_attribute("lakeloom.resource", kind)
        span.set_attribute("lakeloom.outcome", outcome)
        RESOURCE_READS.labels(resource=kind, outcome=outcome).inc()
        record_resource_read(kind, outcome)

    async def _read(
        self, text: str, context: Context[Any, Any] | None, span: Span
    ) -> Iterable[ReadResourceContents]:
        kind: Kind | None = None
        try:
            self._require_read(context)  # before routing: no not_found/validation oracle
        except MCPError:
            self._count(span, "any", "forbidden")
            raise
        try:
            self._check_rate(context)
        except MCPError:
            self._count(span, "any", "rate_limited")
            raise
        try:
            kind, build = self._route(text)
            artifact = await build()
        except LakeloomError as exc:
            self._count(span, kind or "unknown", "error")
            raise _mcp_error(exc) from exc
        except MCPError:
            raise
        except Exception as exc:
            self._count(span, kind or "unknown", "error")
            # Only the type: the text may carry upstream detail (NFR-SEC-11).
            _log.error(
                "resource %s failed unexpectedly (%s)", kind or "unknown", type(exc).__name__
            )
            raise _mcp_error(
                LakeloomError(
                    "upstream_unavailable",
                    f"reading the resource failed unexpectedly ({type(exc).__name__})",
                    "retry; if it keeps failing, check the Lakeloom server logs",
                )
            ) from exc
        self._count(span, kind, "ok")
        return [ReadResourceContents(content=artifact.body.decode(), mime_type=artifact.media_type)]

    def _route(self, uri: str) -> tuple[Kind, Callable[[], Awaitable[Artifact]]]:
        if not uri.startswith(SCHEME):
            raise _not_found(uri)
        rest, _, query = uri[len(SCHEME) :].partition("?")
        query = query.partition("#")[0]
        rest = rest.partition("#")[0]
        versions = parse_qs(query).get("version")
        version = versions[0] if versions else None
        deps = self._deps
        head, _, tail = rest.partition("/")
        if head == "models" and not tail:
            return "models", lambda: _models(deps, version)
        if head == "models":
            slug_enc, _, name = tail.partition("/")
            slug = unquote(slug_enc)
            if name.startswith("ontology."):
                ext = name[len("ontology.") :]
                media = MEDIA_BY_EXT.get(ext)
                if media is None:
                    raise LakeloomError(
                        "validation_error",
                        f"unknown ontology format {ext!r}",
                        "use one of: " + ", ".join(MEDIA_BY_EXT),
                    )
                from lakeloom.core.annotations import effective_graph

                return "ontology", lambda: model_artifact(
                    deps.models,
                    slug,
                    media,
                    version=version,
                    releases=deps.reader,
                    git=deps.git_links,
                    effective=lambda s: effective_graph(deps.models, deps.proposal_repo, s),
                )
            if name == "shapes.ttl":
                return "shapes", lambda: shapes_artifact(
                    deps.models,
                    slug,
                    "text/turtle",
                    version=version,
                    releases=deps.reader,
                    git=deps.git_links,
                )
            if name == "context.jsonld":
                return "context", lambda: model_context_artifact(
                    deps.models, slug, version=version, releases=deps.reader, git=deps.git_links
                )
            if name.startswith("deliverables/"):
                from lakeloom.core.deliverables import DELIVERABLES
                from lakeloom.server.deliverables import serve_deliverable

                filename = name[len("deliverables/") :]
                d = next((x for x in DELIVERABLES if x.filename == filename), None)
                if d is None:
                    raise LakeloomError(
                        "validation_error",
                        f"unknown deliverable {filename!r}",
                        "use one of: " + ", ".join(x.filename for x in DELIVERABLES),
                    )
                return "deliverable", serve_deliverable(deps, slug, d, version=version)
        if head == "assets" and tail.endswith("/context.md"):
            fqn = unquote(tail[: -len("/context.md")])
            return "asset", lambda: _asset(deps, fqn, version)
        raise _not_found(uri)


def _not_found(uri: str) -> LakeloomError:
    return LakeloomError(
        "not_found",
        f"Unknown resource {uri!r}",
        "list resources and resource templates to see the available URIs",
    )


async def _models(deps: "AppDeps", version: str | None) -> Artifact:
    require_latest(version, "the models list")
    return await models_list_artifact(deps.models)


async def _asset(deps: "AppDeps", fqn: str, version: str | None) -> Artifact:
    require_latest(version, "asset context")
    return await asset_context_artifact(deps.models, deps.om, deps.sources, fqn, "text/markdown")
