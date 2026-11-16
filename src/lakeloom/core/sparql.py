"""Read-only SPARQL over a model, proxied to OpenMetadata (FR-PUB-03, NFR-SEC-13; ADR 0016).

The dataset is the model's **glossary** alone (``POST /v1/glossaries/{id}/sparql``, ADR 0016
R3): concepts, labels, definitions, synonyms, broader/narrower, mappings and links. The
native attributes, object properties and axioms are not in it. The ontology endpoint (the
sealed active release) is never used: Lakeloom never seals.

The guard runs before any OpenMetadata call, in this order:

1. at most ``LAKELOOM_SPARQL_MAX_QUERY_CHARS`` characters;
2. ``rdflib``'s ``parseQuery`` (a parser only: nothing is evaluated, prepared or fetched);
   on failure ``parseUpdate`` tells an update (``forbidden``) from a syntax error
   (``validation_error`` with rdflib's message on one line, at most 300 characters);
3. the form: SELECT, ASK, CONSTRUCT or DESCRIBE;
4. any ``ServiceGraphPattern`` anywhere in the tree is ``forbidden`` (OM would call out).

``FROM`` / ``FROM NAMED`` pass through (OM scopes the dataset). OM's own parser also refuses
updates, as defence in depth only. Each query is logged as its sha256 and length, never its
text.
"""

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Final, Literal, NoReturn, cast

from pydantic import BaseModel, Field
from pyparsing import ParseResults
from rdflib.plugins.sparql.parser import parseQuery, parseUpdate
from rdflib.plugins.sparql.parserutils import CompValue

from lakeloom.core.tokens import token_estimate
from lakeloom.errors import LakeloomError
from lakeloom.om.client import OMError, OMResultTooLarge, OMUnavailable, SparqlFormat
from lakeloom.untrusted import code_span, single_line

if TYPE_CHECKING:
    from lakeloom.config import Settings
    from lakeloom.core.models import ModelService
    from lakeloom.om.client import OMClient

__all__ = [
    "FORMATS",
    "MEDIA",
    "QueryForm",
    "ResultFormat",
    "SparqlResult",
    "SparqlService",
    "check_query",
    "fit_json_result",
    "pick_format",
    "query_digest",
    "sparql_available",
]

QueryForm = Literal["select", "ask", "construct", "describe"]
ResultFormat = SparqlFormat

# The content types OM returns per format (ADR 0016 R3); also the HTTP ``Accept`` list.
MEDIA: Final[dict[ResultFormat, str]] = {
    "json": "application/sparql-results+json",
    "xml": "application/sparql-results+xml",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "turtle": "text/turtle",
    "jsonld": "application/ld+json",
    "ntriples": "application/n-triples",
    "rdfxml": "application/rdf+xml",
}
_RESULT_SETS: Final[tuple[ResultFormat, ...]] = ("json", "xml", "csv", "tsv")
_GRAPHS: Final[tuple[ResultFormat, ...]] = ("turtle", "jsonld", "ntriples", "rdfxml")
# The formats each form allows, its default first.
FORMATS: Final[dict[QueryForm, tuple[ResultFormat, ...]]] = {
    "select": _RESULT_SETS,
    "ask": _RESULT_SETS,
    "construct": _GRAPHS,
    "describe": _GRAPHS,
}
_FORMS: Final[dict[str, QueryForm]] = {
    "SelectQuery": "select",
    "AskQuery": "ask",
    "ConstructQuery": "construct",
    "DescribeQuery": "describe",
}
_MESSAGE_MAX = 300
_GLOSSARY_404 = re.compile(r"glossary instance for .* not found", re.IGNORECASE)
_MIN_OM: Final = (2, 1)
_VERSION = re.compile(r"^\s*v?(\d+)\.(\d+)")
_READ_ONLY_HINT = "only SELECT, ASK, CONSTRUCT and DESCRIBE are allowed (read-only)"

_log = logging.getLogger("lakeloom.sparql")


def query_digest(text: str) -> str:
    """The sha256 of the query text, which is logged instead of the text."""
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _one_line(prefix: str, text: str) -> str:
    return (prefix + single_line(text))[:_MESSAGE_MAX]


def check_query(text: str, *, max_chars: int) -> QueryForm:
    """The query's form, or ``forbidden`` / ``validation_error`` (module docstring)."""
    if len(text) > max_chars:
        raise LakeloomError(
            "validation_error",
            f"query is {len(text)} characters, over the {max_chars} character limit",
            "shorten the query (LAKELOOM_SPARQL_MAX_QUERY_CHARS)",
        )
    try:
        tree = parseQuery(text)
    except Exception as exc:  # pyparsing errors, and RecursionError on deep nesting
        _refuse_unparsed(text, exc)
    form = _form(tree)
    if _has_service(tree):
        raise LakeloomError(
            "forbidden",
            "SERVICE is not allowed",
            "remove the SERVICE clause; only the model's own glossary can be queried",
        )
    return form


def _refuse_unparsed(text: str, exc: Exception) -> NoReturn:
    """Raise for a text ``parseQuery`` refused: an update is ``forbidden``, else invalid."""
    try:
        update = parseUpdate(text)
    except Exception:
        update = None
    if isinstance(update, CompValue) and "request" in update:  # at least one operation
        raise LakeloomError("forbidden", "SPARQL updates are not allowed", _READ_ONLY_HINT)
    if isinstance(exc, RecursionError):
        # rdflib's grammar recurses per triple pattern and per brace: in the guard's worker
        # thread about 84 '.'-separated patterns in one group, or 36 nested groups, pass.
        raise LakeloomError(
            "validation_error",
            "query too long or too deeply nested for the SPARQL guard",
            "split long triple blocks with `;` or into `{ }` groups, and nest less",
        ) from None
    message = _one_line("invalid SPARQL query: ", str(exc) or type(exc).__name__)
    raise LakeloomError("validation_error", message, "fix the syntax; " + _READ_ONLY_HINT) from None


def _form(tree: ParseResults) -> QueryForm:
    for part in cast(Iterable[object], tree):
        if isinstance(part, CompValue):
            form = _FORMS.get(part.name)
            if form is not None:
                return form
    raise LakeloomError("validation_error", "not a SPARQL query", _READ_ONLY_HINT)


def _children(node: object) -> Iterable[object]:
    if isinstance(node, CompValue):
        return cast(Iterable[object], node.values())
    if isinstance(node, ParseResults):
        return [*cast(Iterable[object], node), *cast(Iterable[object], node.values())]
    if isinstance(node, (list, tuple)):
        return cast(Iterable[object], node)
    return ()


def _has_service(tree: ParseResults) -> bool:
    """Whether any ``ServiceGraphPattern`` occurs, at any depth (iterative: no recursion)."""
    stack: list[object] = [tree]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, CompValue) and node.name == "ServiceGraphPattern":
            return True
        stack.extend(_children(node))
    return False


def pick_format(form: QueryForm, requested: ResultFormat | None) -> ResultFormat:
    """``requested``, or the form's default; a format that does not fit is invalid."""
    allowed = FORMATS[form]
    if requested is None:
        return allowed[0]
    if requested not in allowed:
        raise LakeloomError(
            "validation_error",
            f"format {requested!r} does not fit a {form.upper()} query",
            f"{form.upper()}: " + ", ".join(allowed) + f" (default {allowed[0]})",
        )
    return requested


def sparql_available(version: str | None) -> bool:
    """Whether an OpenMetadata ``version`` offers glossary SPARQL (2.1 or later)."""
    match = _VERSION.match(version or "")
    if match is None:
        return False
    return (int(match.group(1)), int(match.group(2))) >= _MIN_OM


def fit_json_result(result: object, max_tokens: int | None) -> tuple[object, bool]:
    """Keep the leading bindings of a SPARQL JSON result that fit ``max_tokens`` (FR-CTX-06).

    The size is the token estimate of ``json.dumps(result)``. ``head`` (and anything but
    ``results.bindings``) is always kept; an ASK result or an unknown shape is returned
    whole. The flag is ``True`` when bindings were dropped.
    """
    if max_tokens is None or token_estimate(json.dumps(result)) <= max_tokens:
        return result, False
    if not isinstance(result, dict):
        return result, False
    whole = cast(dict[str, Any], result)
    results = whole.get("results")
    if not isinstance(results, dict):
        return whole, False
    bindings = cast(dict[str, Any], results).get("bindings")
    if not isinstance(bindings, list):
        return whole, False
    rows = cast(list[object], bindings)

    def keep(n: int) -> dict[str, Any]:
        return {**whole, "results": {**cast(dict[str, Any], results), "bindings": rows[:n]}}

    lo, hi = 0, len(rows)  # the largest n whose result fits (0 when none does)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if token_estimate(json.dumps(keep(mid))) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return keep(lo), True


class SparqlResult(BaseModel):
    """One query's answer: the bytes OM returned, in ``media_type``."""

    form: QueryForm
    format: ResultFormat
    media_type: str
    body: bytes = Field(exclude=True, repr=False)


class SparqlService:
    """Guard a query, then run it over a model's glossary with the bot token."""

    def __init__(self, settings: "Settings", models: "ModelService", om: "OMClient") -> None:
        self._settings = settings
        self._models = models
        self._om = om
        # Set when OM answered 404 for the endpoint itself (not for an unknown glossary):
        # this OpenMetadata has no glossary SPARQL; get_server_info reports it (FR-OM-06).
        self.endpoint_missing = False

    async def query(self, model: str, text: str, fmt: ResultFormat | None) -> SparqlResult:
        """Guard ``text`` (:meth:`guard`), then run it (:meth:`run`)."""
        form = await self.guard(text)
        return await self.run(model, text, form, fmt)

    async def guard(self, text: str) -> QueryForm:
        """Log the query (sha256 and length) and run :func:`check_query` once, in a worker
        thread: the pure-Python parse of a long query takes up to seconds, which must not
        block the event loop, and the thread's fixed shallow stack makes the parser's
        recursion limit the same for every caller (CLI, MCP, HTTP)."""
        _log.info("sparql query sha256=%s chars=%d", query_digest(text), len(text))
        return await asyncio.to_thread(
            check_query, text, max_chars=self._settings.sparql_max_query_chars
        )

    async def run(
        self, model: str, text: str, form: QueryForm, fmt: ResultFormat | None
    ) -> SparqlResult:
        """Run a query :meth:`guard` accepted as ``form`` over ``model``'s glossary."""
        chosen = pick_format(form, fmt)
        info = await self._models.get_model(model)
        try:
            body, content_type = await self._om.glossary_sparql(
                info.glossary_id,
                text,
                chosen,
                timeout=float(self._settings.sparql_timeout_seconds),
                max_bytes=self._settings.sparql_max_result_bytes,
            )
        except OMResultTooLarge as exc:
            raise LakeloomError(
                "validation_error",
                f"result larger than {exc.limit} bytes; add LIMIT",
                "add LIMIT or narrow the pattern (LAKELOOM_SPARQL_MAX_RESULT_BYTES)",
            ) from None
        except OMError as exc:
            raise self._error(exc) from None
        media = content_type.split(";", 1)[0].strip().lower()
        if media != MEDIA[chosen]:
            # Never pass an unexpected type on (an HTTP client would honour it).
            _log.warning("sparql result type %r for format %s; using %s", media[:80], chosen,
                         MEDIA[chosen])  # fmt: skip
            media = MEDIA[chosen]
        return SparqlResult(form=form, format=chosen, media_type=media, body=body)

    def _error(self, exc: OMError) -> LakeloomError:
        if isinstance(exc, OMUnavailable):
            return LakeloomError(
                "upstream_unavailable",
                "OpenMetadata is unavailable or the query timed out",
                "retry; add LIMIT or simplify the query (LAKELOOM_SPARQL_TIMEOUT_SECONDS)",
            )
        detail = exc.detail or ""
        if exc.status == 404:
            if _GLOSSARY_404.search(detail):
                return LakeloomError(
                    "not_found", "Model not found in OpenMetadata", "call list_models"
                )
            self.endpoint_missing = True
            return LakeloomError(
                "not_found",
                "SPARQL is not available on this OpenMetadata",
                "needs OpenMetadata 2.1 or later (see get_server_info features.sparql)",
            )
        if exc.status in (401, 403):
            return LakeloomError(
                "forbidden",
                "OpenMetadata refused the SPARQL request",
                "check the Lakeloom bot token and its OpenMetadata role",
            )
        quoted = code_span(single_line(detail)[:_MESSAGE_MAX]) if detail else "(no detail)"
        return LakeloomError(
            "validation_error",
            f"OpenMetadata refused the query: {quoted}",
            "fix the query; " + _READ_ONLY_HINT,
        )
