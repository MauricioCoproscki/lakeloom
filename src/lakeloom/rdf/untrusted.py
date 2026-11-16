"""Safe parsing of untrusted RDF (FR-IO-04, NFR-SEC-13).

User-supplied files are parsed without network access: remote JSON-LD contexts and RDF/XML
DTDs/entities are refused up front (RDF/XML must be UTF-8, so the DTD search sees the text),
the size is checked before parsing, and the triple count and parse time are capped. Syntax
errors keep ``parse_rdf``'s line/column message.
"""

import asyncio
import json
import re
from typing import Any, cast

from rdflib import Graph, URIRef
from rdflib.namespace import SKOS

from lakeloom.errors import LakeloomError
from lakeloom.rdf.canonical import InputFormat, parse_rdf

__all__ = ["BOOKKEEPING", "parse_untrusted", "parse_untrusted_async"]

# OpenMetadata bookkeeping predicates that carry no semantic content for diffing.
BOOKKEEPING: frozenset[URIRef] = frozenset(
    {
        URIRef("https://open-metadata.org/ontology/fullyQualifiedName"),
        SKOS.inScheme,
        SKOS.topConceptOf,
    }
)


def _refuse(message: str, hint: str) -> LakeloomError:
    return LakeloomError("validation_error", message, hint=hint)


def _remote_context(value: Any) -> bool:
    if isinstance(value, str):
        return True
    if isinstance(value, dict):
        return "@import" in value
    if isinstance(value, list):
        return any(_remote_context(item) for item in cast("list[Any]", value))
    return False


def _has_remote_context(node: Any) -> bool:
    if isinstance(node, dict):
        for key, value in cast("dict[str, Any]", node).items():
            if key == "@context" and _remote_context(value):
                return True
            if _has_remote_context(value):
                return True
        return False
    if isinstance(node, list):
        return any(_has_remote_context(item) for item in cast("list[Any]", node))
    return False


def _check_jsonld(data: bytes) -> None:
    try:
        doc = json.loads(data)
    except ValueError:
        return  # parse_rdf reports the syntax error with line and column
    if _has_remote_context(doc):
        raise _refuse(
            "remote JSON-LD contexts are not allowed; inline the context",
            "Replace the @context URL (or @import) with the inline context object and retry.",
        )


_XML_ENCODING = re.compile(r"""^\ufeff?\s*<\?xml\b[^>]*?\bencoding\s*=\s*["']([^"']*)["']""")


def _check_rdfxml(data: bytes) -> None:
    """RDF/XML must be strict UTF-8 without NULs or a non-UTF-8 encoding declaration (so no
    other encoding can hide a DTD from the search), and must declare no DTD or entity."""
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        text = None
    declared = _XML_ENCODING.match(text) if text is not None else None
    if (
        text is None
        or "\x00" in text
        or (declared is not None and declared.group(1).lower() not in {"utf-8", "utf8"})
    ):
        raise _refuse("RDF/XML must be UTF-8", "Re-encode the file as UTF-8 and retry.")
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise _refuse(
            "RDF/XML with a DTD or entity declaration is not allowed",
            "Remove the <!DOCTYPE>/<!ENTITY> declarations and write the IRIs in full.",
        )


def parse_untrusted(data: bytes, fmt: InputFormat, *, max_bytes: int, max_triples: int) -> Graph:
    """Parse untrusted ``data`` safely; every refusal is a ``validation_error``."""
    if len(data) > max_bytes:
        raise _refuse(
            f"import is {len(data)} bytes; the limit is {max_bytes} (LAKELOOM_IMPORT_MAX_BYTES)",
            "Split the file into smaller imports, or ask the operator to raise the limit.",
        )
    try:
        if fmt == "jsonld":
            _check_jsonld(data)
        elif fmt == "rdfxml":
            _check_rdfxml(data)
        graph = parse_rdf(data, fmt)
    except RecursionError:
        raise _refuse(
            f"{'JSON-LD' if fmt == 'jsonld' else 'RDF'} nesting is too deep",
            "Flatten the document (for JSON-LD, use @graph with @id references instead of "
            "deeply nested objects or lists) and retry.",
        ) from None
    if len(graph) > max_triples:
        raise _refuse(
            f"import has {len(graph)} triples; the limit is {max_triples} "
            "(LAKELOOM_IMPORT_MAX_TRIPLES)",
            "Split the file into smaller imports, or ask the operator to raise the limit.",
        )
    return graph


async def parse_untrusted_async(
    data: bytes,
    fmt: InputFormat,
    *,
    max_bytes: int,
    max_triples: int,
    timeout: float,  # noqa: ASYNC109
) -> Graph:
    """Size check first, then parse in a worker thread under ``timeout`` seconds.

    The worker thread is not killed on timeout; it ends when the parse does.
    """
    if len(data) > max_bytes:  # reject before spawning a thread
        parse_untrusted(data, fmt, max_bytes=max_bytes, max_triples=max_triples)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                parse_untrusted, data, fmt, max_bytes=max_bytes, max_triples=max_triples
            ),
            timeout,
        )
    except TimeoutError as exc:
        raise _refuse(
            f"import parsing exceeded {timeout:g} s (LAKELOOM_IMPORT_TIMEOUT_SECONDS)",
            "Import a smaller file, or ask the operator to raise the timeout.",
        ) from exc
