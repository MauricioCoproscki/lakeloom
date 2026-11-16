"""Read-only publication API under ``/v1`` (FR-PUB-02, FR-PUB-03, FR-PUB-07).

Every route needs the ``lakeloom:read`` scope. The artefact routes are ``GET``s that
negotiate the representation from ``Accept`` (a type the route does not offer gives ``406``),
serve the artefact bytes
unmodified with ``ETag``, ``Last-Modified``, ``Link``, ``Vary: Accept`` and ``Cache-Control``,
and answer ``304`` to a matching ``If-None-Match``. The model, its shapes and its context take
``?version=`` (default ``latest``): a release is served from its Git tag (FR-PUB-02).
Failures are typed JSON (``{"error": {"code", "message", "hint"}}``), never HTML or a stack
trace.

``POST /v1/models/{slug}/sparql`` (FR-PUB-03) runs a guarded read-only SPARQL query over the
model's glossary (:mod:`lakeloom.core.sparql`). The query comes as ``application/sparql-query``,
a form ``query=`` or JSON ``{"query": ...}``; ``Accept`` picks the result format among those
the query's form allows (406 otherwise) and the answer is OpenMetadata's bytes with
``Cache-Control: no-store`` and no validators.
"""

import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from email.utils import format_datetime
from typing import TYPE_CHECKING, Annotated, Any, Final, cast
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from lakeloom.core.sparql import FORMATS, MEDIA, ResultFormat
from lakeloom.errors import ErrorCode, LakeloomError
from lakeloom.publish.artifacts import (
    Artifact,
    asset_context_artifact,
    concept_artifact,
    matches_if_none_match,
    model_artifact,
    model_context_artifact,
    models_list_artifact,
    shapes_artifact,
    versions_artifact,
)
from lakeloom.publish.negotiation import MediaType, choose
from lakeloom.server.auth import Principal

if TYPE_CHECKING:
    from lakeloom.server.http_app import AppDeps

__all__ = [
    "STATUS_BY_CODE",
    "create_router",
    "lakeloom_error_handler",
    "request_validation_handler",
]

READ_SCOPE: Final = "lakeloom:read"
CACHE_CONTROL: Final = "private, max-age=0, must-revalidate"
STATUS_BY_CODE: Final[dict[ErrorCode, int]] = {
    "not_found": 404,
    "forbidden": 403,
    "validation_error": 400,
    "conflict": 409,
    "rate_limited": 429,
    "upstream_unavailable": 503,
}
MODEL_TYPES: Final[tuple[MediaType, ...]] = (
    "text/turtle",
    "application/ld+json",
    "application/rdf+xml",
    "application/n-triples",
)
SHAPES_TYPES: Final[tuple[MediaType, ...]] = (
    "text/turtle",
    "application/ld+json",
    "application/n-triples",
)
CONCEPT_TYPES: Final[tuple[MediaType, ...]] = ("text/markdown", "application/json")
ASSET_TYPES: Final[tuple[MediaType, ...]] = (
    "text/markdown",
    "application/json",
    "application/ld+json",
)
JSON_TYPES: Final[tuple[MediaType, ...]] = ("application/json",)
SPARQL_BODIES: Final = (
    "application/sparql-query",
    "application/x-www-form-urlencoded",
    "application/json",
)
SPARQL_TYPES: Final[tuple[str, ...]] = tuple(MEDIA.values())
_FORMAT_BY_SPARQL_MEDIA: Final[dict[str, ResultFormat]] = {m: f for f, m in MEDIA.items()}
CONTEXT_TYPES: Final[tuple[MediaType, ...]] = ("application/ld+json", "application/json")

_log = logging.getLogger("lakeloom.http")

VersionQuery = Annotated[
    str | None,
    Query(description="latest (default) or a release MAJOR.MINOR.PATCH (see /versions)"),
]


def error_response(code: ErrorCode, message: str, hint: str, status: int | None = None) -> Response:
    body = LakeloomError(code, message, hint).to_dict()
    return JSONResponse(body, status_code=status or STATUS_BY_CODE[code])


async def lakeloom_error_handler(_: Request, exc: Exception) -> Response:
    err = cast(LakeloomError, exc)
    return error_response(err.code, err.message, err.hint)


async def request_validation_handler(_: Request, exc: Exception) -> Response:
    """A bad query parameter is a typed ``validation_error`` rather than FastAPI's 422."""
    problems = cast(list[dict[str, Any]], cast(RequestValidationError, exc).errors())
    loc = cast(tuple[object, ...], problems[0].get("loc", ())) if problems else ()
    where = ".".join(str(p) for p in loc if p != "query")
    return error_response(
        "validation_error",
        f"invalid parameter {where}" if where else "invalid request",
        "max_tokens must be an integer >= 1; limit an integer from 1 to 200",
    )


def _not_acceptable(offered: Sequence[str]) -> Response:
    return error_response(
        "validation_error", "not acceptable", "supported: " + ", ".join(offered), status=406
    )


def _internal_error() -> Response:
    return JSONResponse(
        {
            "error": {
                "code": "upstream_unavailable",
                "message": "internal error",
                "hint": "check the server logs",
            }
        },
        status_code=500,
    )


def _require_read(request: Request) -> None:
    state: dict[str, Any] = request.scope.get("state") or {}
    principal = state.get("principal")
    if not isinstance(principal, Principal) or READ_SCOPE not in principal.scopes:
        raise LakeloomError(
            "forbidden", f"this route requires the {READ_SCOPE} scope", "token lacks scope"
        )


def _content_type(media: str) -> str:
    textual = media.startswith("text/") or media == "application/json" or media.endswith("+json")
    return f"{media}; charset=utf-8" if textual else media


def _validators(artifact: Artifact) -> dict[str, str]:
    headers = {
        "ETag": artifact.etag,
        "Last-Modified": format_datetime(artifact.last_modified, usegmt=True),
        "Vary": "Accept",
        "Cache-Control": CACHE_CONTROL,
    }
    if artifact.links:
        headers["Link"] = ", ".join(artifact.links)
    return headers


def _respond(request: Request, artifact: Artifact) -> Response:
    headers = _validators(artifact)
    if matches_if_none_match(request.headers.get("if-none-match"), artifact.etag):
        return Response(status_code=304, headers=headers)
    headers["Content-Type"] = _content_type(artifact.media_type)
    return Response(content=artifact.body, headers=headers)  # bytes as hashed for the ETag


def _responses(
    offered: Sequence[MediaType], extra: dict[int | str, Any] | None = None
) -> dict[int | str, Any]:
    headers = {
        name: {"schema": {"type": "string"}, "description": text}
        for name, text in (
            ("ETag", "Strong validator: sha256 of the body."),
            (
                "Last-Modified",
                "latest: when the source changed in OpenMetadata; a release: its tag date.",
            ),
            (
                "Link",
                'latest: the OpenMetadata entity (rel="via") and the last Git publication '
                '(rel="related"); a release: its Git tag and commit (rel="via").',
            ),
        )
    }
    error = {"description": "Typed error", "content": {"application/json": {"schema": _ERROR}}}
    out: dict[int | str, Any] = {
        200: {
            "description": "The artefact in the negotiated media type",
            "content": {m: {} for m in offered},
            "headers": headers,
        },
        304: {"description": "Not modified (If-None-Match matched)"},
        400: error,
        401: {"description": "Missing or invalid bearer token"},
        403: error,
        404: error,
        406: error,
        500: error,
        503: error,
    }
    out.update(extra or {})
    return out


_ERROR: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "error": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "enum": sorted(STATUS_BY_CODE)},
                "message": {"type": "string"},
                "hint": {"type": "string"},
            },
            "required": ["code", "message", "hint"],
        }
    },
    "required": ["error"],
}


def _sparql_query_text(content_type: str, body: bytes, max_chars: int) -> str:
    """The query from one of the three request body forms (FR-PUB-03)."""
    if len(body) > max_chars * 4 + 1024:  # UTF-8 is at most 4 bytes a character
        raise LakeloomError(
            "validation_error",
            f"query is over the {max_chars} character limit",
            "shorten the query (LAKELOOM_SPARQL_MAX_QUERY_CHARS)",
        )
    try:
        raw = body.decode("utf-8")
    except UnicodeDecodeError:
        raise LakeloomError("validation_error", "body is not UTF-8", "send UTF-8") from None
    media = content_type.split(";", 1)[0].strip().lower()
    query: object = None
    if media == "application/sparql-query":
        query = raw
    elif media == "application/x-www-form-urlencoded":
        values = parse_qs(raw, keep_blank_values=True).get("query")
        query = values[0] if values and len(values) == 1 else None
    elif media == "application/json":
        try:
            payload: object = json.loads(raw)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            query = cast(dict[str, object], payload).get("query")
    else:
        raise _UnsupportedBody
    if not isinstance(query, str) or not query.strip():
        raise LakeloomError(
            "validation_error",
            "no query in the request body",
            "send the query as the body (application/sparql-query), as query= in a form, "
            'or as JSON {"query": "..."}',
        )
    return query


class _UnsupportedBody(Exception):
    """The request body's content type is none of :data:`SPARQL_BODIES` (415)."""


def _sparql_responses() -> dict[int | str, Any]:
    error = {"description": "Typed error", "content": {"application/json": {"schema": _ERROR}}}
    return {
        200: {
            "description": "OpenMetadata's result: SELECT/ASK as SPARQL JSON (default), XML, "
            "CSV or TSV; CONSTRUCT/DESCRIBE as Turtle (default), JSON-LD, N-Triples or "
            "RDF/XML. Not cached (Cache-Control: no-store).",
            "content": {m: {} for m in SPARQL_TYPES},
        },
        400: error,
        401: {"description": "Missing or invalid bearer token"},
        403: error,
        404: error,
        406: error,
        415: error,
        500: error,
        503: error,
    }


_SPARQL_REQUEST_BODY: Final[dict[str, Any]] = {
    "required": True,
    "description": "A read-only SPARQL query (SELECT, ASK, CONSTRUCT or DESCRIBE; no "
    "updates, no SERVICE).",
    "content": {
        "application/sparql-query": {"schema": {"type": "string"}},
        "application/x-www-form-urlencoded": {
            "schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            }
        },
        "application/json": {
            "schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            }
        },
    },
}


def create_router(deps: "AppDeps") -> APIRouter:
    """The ``/v1`` router; dependencies are read per request (tests swap them)."""
    router = APIRouter(prefix="/v1", tags=["publication"], dependencies=[Depends(_require_read)])

    async def serve(
        request: Request,
        offered: Sequence[MediaType],
        build: Callable[[MediaType], Awaitable[Artifact]],
    ) -> Response:
        accept = ", ".join(request.headers.getlist("accept")) or None
        media = choose(accept, offered)
        if media is None:
            return _not_acceptable(offered)
        try:
            artifact = await build(media)
        except LakeloomError as exc:
            return error_response(exc.code, exc.message, exc.hint)
        except Exception as exc:
            _log.error("http api request failed unexpectedly (%s)", type(exc).__name__)
            return _internal_error()
        return _respond(request, artifact)

    def model(slug: str, media: MediaType, version: str | None) -> Awaitable[Artifact]:
        return model_artifact(
            deps.models, slug, media, version=version, releases=deps.reader, git=deps.git_links
        )

    @router.get(
        "/models",
        summary="List the models",
        response_class=Response,
        responses=_responses(JSON_TYPES),
    )
    async def list_models(
        request: Request,
        cursor: Annotated[str | None, Query(description="next_cursor of a previous page")] = None,
        limit: Annotated[int | None, Query(ge=1, le=200, description="page size")] = None,
    ) -> Response:
        return await serve(
            request,
            JSON_TYPES,
            lambda _: models_list_artifact(deps.models, cursor=cursor, limit=limit),
        )

    @router.get(
        "/models/{slug}",
        summary="The model as an ontology (Turtle, JSON-LD, RDF/XML or N-Triples)",
        response_class=Response,
        responses=_responses(MODEL_TYPES),
    )
    async def get_model(request: Request, slug: str, version: VersionQuery = None) -> Response:
        return await serve(request, MODEL_TYPES, lambda m: model(slug, m, version))

    @router.get(
        "/models/{slug}/versions",
        summary="The published versions of a model",
        response_class=Response,
        responses=_responses(JSON_TYPES),
    )
    async def list_versions(request: Request, slug: str) -> Response:
        def build(_: MediaType) -> Awaitable[Artifact]:
            return versions_artifact(
                deps.models, slug, MODEL_TYPES, releases=deps.reader, git=deps.git_links
            )

        return await serve(request, JSON_TYPES, build)

    @router.get(
        "/models/{slug}/versions/{version}",
        summary="One version of the model: latest or a release (MAJOR.MINOR.PATCH)",
        response_class=Response,
        responses=_responses(MODEL_TYPES),
    )
    async def get_model_version(request: Request, slug: str, version: str) -> Response:
        return await serve(request, MODEL_TYPES, lambda m: model(slug, m, version))

    @router.get(
        "/models/{slug}/shapes",
        summary="The model's SHACL shapes (Turtle, JSON-LD or N-Triples)",
        response_class=Response,
        responses=_responses(SHAPES_TYPES),
    )
    async def get_model_shapes(
        request: Request, slug: str, version: VersionQuery = None
    ) -> Response:
        def build(media: MediaType) -> Awaitable[Artifact]:
            return shapes_artifact(
                deps.models,
                slug,
                _narrow(media, "text/turtle", "application/ld+json", "application/n-triples"),
                version=version,
                releases=deps.reader,
                git=deps.git_links,
            )

        return await serve(request, SHAPES_TYPES, build)

    @router.get(
        "/models/{slug}/context.jsonld",
        summary="The JSON-LD context of the model",
        response_class=Response,
        responses=_responses(CONTEXT_TYPES),
    )
    async def get_model_context(
        request: Request, slug: str, version: VersionQuery = None
    ) -> Response:
        def build(_: MediaType) -> Awaitable[Artifact]:
            return model_context_artifact(
                deps.models, slug, version=version, releases=deps.reader, git=deps.git_links
            )

        return await serve(request, CONTEXT_TYPES, build)

    @router.get(
        "/concepts/{concept:path}",
        summary="A concept by IRI, FQN, id or name",
        response_class=Response,
        responses=_responses(CONCEPT_TYPES),
    )
    async def get_concept(request: Request, concept: str) -> Response:
        def build(media: MediaType) -> Awaitable[Artifact]:
            return concept_artifact(
                deps.models, deps.om, concept, _narrow(media, "text/markdown", "application/json")
            )

        return await serve(request, CONCEPT_TYPES, build)

    @router.get(
        "/assets/{fqn:path}/context",
        summary="Everything known about an asset, for an agent's context window",
        response_class=Response,
        responses=_responses(ASSET_TYPES),
    )
    async def get_asset_context(
        request: Request,
        fqn: str,
        max_tokens: Annotated[
            int | None, Query(ge=1, description="cut the content to about this many tokens")
        ] = None,
    ) -> Response:
        def build(media: MediaType) -> Awaitable[Artifact]:
            return asset_context_artifact(
                deps.models,
                deps.om,
                deps.sources,
                fqn,
                _narrow(media, "text/markdown", "application/json", "application/ld+json"),
                max_tokens=max_tokens,
            )

        return await serve(request, ASSET_TYPES, build)

    @router.post(
        "/models/{slug}/sparql",
        summary="Read-only SPARQL over the model's glossary (proxied to OpenMetadata)",
        description="Only the glossary is in the dataset (concepts, labels, definitions, "
        "broader/narrower, mappings, links): the model's attributes, object properties and "
        "axioms are not. Updates and SERVICE are refused (403). `Accept` chooses the result "
        "format among those of the query's form; `*/*` or none gives the form's default.",
        response_class=Response,
        responses=_sparql_responses(),
        openapi_extra={"requestBody": _SPARQL_REQUEST_BODY},
    )
    async def sparql(request: Request, slug: str) -> Response:
        settings = deps.settings
        accept = ", ".join(request.headers.getlist("accept")) or None
        if choose(accept, SPARQL_TYPES) is None:
            return _not_acceptable(SPARQL_TYPES)
        try:
            try:
                text = _sparql_query_text(
                    request.headers.get("content-type", ""),
                    await request.body(),
                    settings.sparql_max_query_chars,
                )
            except _UnsupportedBody:
                return error_response(
                    "validation_error",
                    "unsupported request body type",
                    "supported: " + ", ".join(SPARQL_BODIES),
                    status=415,
                )
            form = await deps.sparql_service.guard(text)  # once, off the event loop
            offered = tuple(MEDIA[f] for f in FORMATS[form])
            media = choose(accept, offered)
            if media is None:
                return _not_acceptable(offered)
            fmt = _FORMAT_BY_SPARQL_MEDIA[media]
            result = await deps.sparql_service.run(slug, text, form, fmt)
        except LakeloomError as exc:
            return error_response(exc.code, exc.message, exc.hint)
        except Exception as exc:
            _log.error("sparql request failed unexpectedly (%s)", type(exc).__name__)
            return _internal_error()
        headers = {
            "Content-Type": _content_type(result.media_type),
            "Cache-Control": "no-store",
            "Vary": "Accept",
        }
        return Response(content=result.body, headers=headers)

    return router


def _narrow[T: str](media: MediaType, *allowed: T) -> T:
    """``media`` as one of the ``allowed`` literals (the negotiation offered only those)."""
    for candidate in allowed:
        if candidate == media:
            return candidate
    raise LakeloomError("validation_error", f"{media} is not available here", "use Accept")
