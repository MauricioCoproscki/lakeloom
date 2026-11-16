"""The read-only SPARQL guard, formats and service (FR-PUB-03, NFR-SEC-13), over fakes.

The OM client's streamed read has its own tests in tests/unit/om/test_client_semantic.py;
the tool, HTTP route and server info live in tests/unit/server/.
"""

import hashlib
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from lakeloom.core.sparql import (
    MEDIA,
    SparqlResult,
    SparqlService,
    check_query,
    pick_format,
)
from lakeloom.errors import LakeloomError
from lakeloom.om.client import OMError, OMResultTooLarge, OMUnavailable

MAX = 20000
GID = "263277db-bc8a-4f3f-a153-254bfdb4587f"


def refused(text: str, *, max_chars: int = MAX) -> LakeloomError:
    with pytest.raises(LakeloomError) as info:
        check_query(text, max_chars=max_chars)
    return info.value


@pytest.mark.parametrize(
    ("text", "form"),
    [
        ("SELECT ?s WHERE { ?s ?p ?o } LIMIT 5", "select"),
        ("PREFIX skos: <http://www.w3.org/2004/02/skos/core#>\nASK { ?s a skos:Concept }", "ask"),
        ("BASE <http://x/>\nCONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }", "construct"),
        ("BASE <http://x/> PREFIX ex: <http://y#> DESCRIBE ex:Customer", "describe"),
        ("select * { ?s ?p ?o }", "select"),
    ],
)
def test_forms_detected(text: str, form: str) -> None:
    assert check_query(text, max_chars=MAX) == form


@pytest.mark.parametrize(
    "text",
    [
        "INSERT DATA { <http://a> <http://b> <http://c> }",
        "DELETE WHERE { ?s ?p ?o }",
        "LOAD <http://evil.example/data.ttl>",
        "CLEAR DEFAULT",
        "DROP ALL",
        "CREATE GRAPH <http://g>",
        "WITH <http://g> DELETE { ?s ?p ?o } INSERT { ?s ?p 1 } WHERE { ?s ?p ?o }",
        "PREFIX ex: <http://y#> INSERT DATA { ex:a ex:b ex:c }",
    ],
)
def test_updates_forbidden(text: str) -> None:
    err = refused(text)

    assert err.code == "forbidden"
    assert err.message == "SPARQL updates are not allowed"


@pytest.mark.parametrize(
    "text",
    [
        "SELECT * WHERE { SERVICE <http://e> { ?s ?p ?o } }",
        "SELECT * WHERE { ?s ?p ?o OPTIONAL { SERVICE <http://e> { ?a ?b ?c } } }",
        "SELECT * { { ?s ?p ?o } UNION { SERVICE SILENT ?x { ?a ?b ?c } } }",
        "ASK { ?s ?p ?o MINUS { { SERVICE <http://e> { ?s ?p ?o } } } }",
        "CONSTRUCT { ?s ?p ?o } WHERE { SERVICE <http://e> { ?s ?p ?o } }",
    ],
)
def test_nested_service_forbidden(text: str) -> None:
    err = refused(text)

    assert err.code == "forbidden"
    assert err.message == "SERVICE is not allowed"


@pytest.mark.parametrize(
    "text",
    [
        "SELECT * FROM <http://g> WHERE { ?s ?p ?o }",
        "SELECT * FROM NAMED <http://h> WHERE { GRAPH ?g { ?s ?p ?o } }",
        "SELECT * FROM <http://g> FROM NAMED <http://h> WHERE { ?s ?p ?o }",
        "select * from <http://g> where { ?s ?p ?o }",
        "select * from named <http://h> { ?s ?p ?o }",
        "PREFIX ex: <http://y#> SELECT * FROM ex:g WHERE { ?s ?p ?o }",
        "CONSTRUCT { ?s ?p ?o } FROM <http://g> WHERE { ?s ?p ?o }",
        "ASK FROM <http://g> { ?s ?p ?o }",
        "ask from named <http://h> { ?s ?p ?o }",
        "DESCRIBE <http://x/C> FROM <http://g>",
        "describe ?s from named <http://h> where { ?s ?p ?o }",
    ],
)
def test_dataset_clause_forbidden(text: str) -> None:
    err = refused(text)

    assert err.code == "forbidden"
    assert err.message == "FROM and FROM NAMED are not allowed; the dataset is the model's glossary"
    assert err.hint


def test_service_after_prefix_is_still_refused() -> None:
    # rdflib 7.6 cannot parse SERVICE after a PREFIX: refused before OM all the same.
    err = refused("PREFIX ex: <http://y#> SELECT * WHERE { SERVICE <http://e> { ?s ?p ?o } }")

    assert err.code in ("forbidden", "validation_error")


def test_keyword_in_comment_or_literal_is_fine() -> None:
    text = (
        "# INSERT DATA { <a> <b> <c> } and SERVICE <http://e>\n"
        'SELECT ?s WHERE { ?s ?p "SERVICE" . ?s ?q "INSERT DATA" }'
    )

    assert check_query(text, max_chars=MAX) == "select"


def test_syntax_error_is_validation_error_one_line() -> None:
    err = refused("SELECT ?s WHERE {\n ?s ?p \n")
    empty = refused("   \n# only a comment\n")

    assert err.code == "validation_error"
    assert "\n" not in err.message and len(err.message) <= 300
    assert err.message.startswith("invalid SPARQL query: ")
    assert empty.code == "validation_error"


def test_query_length_cap() -> None:
    text = "SELECT * WHERE { ?s ?p ?o }"
    err = refused(text + " " * 200, max_chars=len(text) + 10)

    assert err.code == "validation_error"
    assert str(len(text) + 10) in err.message
    assert check_query(text, max_chars=len(text)) == "select"


def test_deep_nesting_is_a_validation_error() -> None:
    err = refused("SELECT * WHERE " + "{" * 500 + "?s ?p ?o" + "}" * 500)

    assert err.code == "validation_error"


def test_format_must_fit_form() -> None:
    assert pick_format("select", None) == "json"
    assert pick_format("ask", "csv") == "csv"
    assert pick_format("construct", None) == "turtle"
    assert pick_format("describe", "rdfxml") == "rdfxml"
    for form, fmt in (("select", "turtle"), ("ask", "jsonld"), ("construct", "json"),
                      ("describe", "tsv")):  # fmt: skip
        with pytest.raises(LakeloomError) as info:
            pick_format(form, fmt)  # type: ignore[arg-type]
        assert info.value.code == "validation_error"
        assert fmt in info.value.message


def test_media_types_match_om() -> None:
    assert MEDIA == {
        "json": "application/sparql-results+json",
        "xml": "application/sparql-results+xml",
        "csv": "text/csv",
        "tsv": "text/tab-separated-values",
        "turtle": "text/turtle",
        "jsonld": "application/ld+json",
        "ntriples": "application/n-triples",
        "rdfxml": "application/rdf+xml",
    }


# --- service ----------------------------------------------------------------------------


class FakeModels:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def get_model(self, slug: str) -> Any:
        self.asked.append(slug)
        if slug != "retail":
            raise LakeloomError("not_found", f"Unknown model {slug!r}", "see list_models")
        return SimpleNamespace(slug=slug, glossary_id=GID)


class FakeOM:
    def __init__(self, *, body: bytes = b'{"head": {}, "boolean": true}', ctype: str = "",
                 error: Exception | None = None) -> None:  # fmt: skip
        self.body, self.ctype, self.error = body, ctype, error
        self.calls: list[dict[str, Any]] = []

    async def glossary_sparql(
        self,
        glossary_id: str,
        query: str,
        fmt: str,
        *,
        timeout: float,  # noqa: ASYNC109 - mirrors OMClient.glossary_sparql
        max_bytes: int,
    ) -> tuple[bytes, str]:
        self.calls.append(
            {"id": glossary_id, "query": query, "fmt": fmt, "timeout": timeout, "max": max_bytes}
        )
        if self.error is not None:
            raise self.error
        return self.body, self.ctype or MEDIA[fmt]  # type: ignore[index]


def service(om: FakeOM, **settings: Any) -> SparqlService:
    values = {
        "sparql_timeout_seconds": 7.0,
        "sparql_max_result_bytes": 4096,
        "sparql_max_query_chars": 20000,
        **settings,
    }
    return SparqlService(SimpleNamespace(**values), FakeModels(), om)  # type: ignore[arg-type]


async def test_service_queries_the_model_glossary() -> None:
    om = FakeOM(ctype="application/sparql-results+json; charset=utf-8")

    result = await service(om).query("retail", "ASK { ?s ?p ?o }", None)

    assert isinstance(result, SparqlResult)
    assert (result.form, result.format, result.media_type) == (
        "ask", "json", "application/sparql-results+json",
    )  # fmt: skip
    assert result.body == b'{"head": {}, "boolean": true}'
    assert "body" not in result.model_dump()
    assert om.calls == [
        {"id": GID, "query": "ASK { ?s ?p ?o }", "fmt": "json", "timeout": 7.0, "max": 4096}
    ]


async def test_service_guards_before_om() -> None:
    om = FakeOM()
    svc = service(om)

    with pytest.raises(LakeloomError) as update:
        await svc.query("retail", "DROP ALL", None)
    with pytest.raises(LakeloomError) as fmt:
        await svc.query("retail", "SELECT * { ?s ?p ?o }", "turtle")

    assert update.value.code == "forbidden" and fmt.value.code == "validation_error"
    assert om.calls == []


async def test_service_unexpected_media_type_falls_back() -> None:
    om = FakeOM(body=b"<html>", ctype="text/html")

    result = await service(om).query("retail", "SELECT * { ?s ?p ?o }", "csv")

    assert result.media_type == "text/csv"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            OMError(
                "OpenMetadata returned HTTP 400", 400, "Invalid SPARQL query: Lexical error\nx"
            ),
            "validation_error",
        ),
        (
            OMError(
                "OpenMetadata returned HTTP 404", 404, f"glossary instance for {GID} not found"
            ),
            "not_found",
        ),
        (OMError("OpenMetadata returned HTTP 401", 401), "forbidden"),
        (OMError("OpenMetadata returned HTTP 403", 403), "forbidden"),
        (OMUnavailable("OpenMetadata returned HTTP 500", 500), "upstream_unavailable"),
        (OMUnavailable("OpenMetadata timed out (ReadTimeout)"), "upstream_unavailable"),
        (OMResultTooLarge("too large", 200, limit=4096), "validation_error"),
    ],
)
async def test_service_maps_om_errors(error: Exception, code: str) -> None:
    svc = service(FakeOM(error=error))

    with pytest.raises(LakeloomError) as info:
        await svc.query("retail", "SELECT * { ?s ?p ?o }", None)

    assert info.value.code == code
    assert "\n" not in info.value.message
    if code == "validation_error" and isinstance(error, OMResultTooLarge):
        assert info.value.message == "result larger than 4096 bytes; add LIMIT"
    if isinstance(error, OMError) and error.status == 400:
        assert "`Invalid SPARQL query: Lexical error x`" in info.value.message
    assert svc.endpoint_missing is False


async def test_service_404_without_glossary_message_marks_endpoint_missing() -> None:
    svc = service(FakeOM(error=OMError("OpenMetadata returned HTTP 404", 404, None)))

    with pytest.raises(LakeloomError) as info:
        await svc.query("retail", "SELECT * { ?s ?p ?o }", None)

    assert info.value.code == "not_found"
    assert "SPARQL" in info.value.message
    assert svc.endpoint_missing is True


async def test_unknown_model_is_not_found() -> None:
    with pytest.raises(LakeloomError) as info:
        await service(FakeOM()).query("nope", "ASK {}", None)

    assert info.value.code == "not_found"


async def test_query_text_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    query_text = 'SELECT ?s WHERE { ?s ?p "top-query_text-literal" }'
    caplog.set_level(logging.DEBUG)

    await service(FakeOM()).query("retail", query_text, None)
    with pytest.raises(LakeloomError):
        await service(FakeOM()).query("retail", query_text + " DROP", None)

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert hashlib.sha256(query_text.encode()).hexdigest() in logged
    assert f"chars={len(query_text)}" in logged
    assert "top-query_text-literal" not in logged and "?s ?p" not in logged


async def test_guard_runs_once_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from lakeloom.core import sparql

    threads: list[threading.Thread] = []
    real = sparql.check_query

    def spy(text: str, *, max_chars: int) -> Any:
        threads.append(threading.current_thread())
        return real(text, max_chars=max_chars)

    monkeypatch.setattr(sparql, "check_query", spy)
    om = FakeOM()

    await service(om).query("retail", "ASK { ?s ?p ?o }", None)

    assert len(threads) == 1 and threads[0] is not threading.main_thread()
    assert len(om.calls) == 1


def _bgp(n: int) -> str:
    return "SELECT * WHERE { " + " . ".join(f"?s{i} <http://p> ?o{i}" for i in range(n)) + " }"


async def test_guard_recursion_limit_is_pinned() -> None:
    svc = service(FakeOM())

    assert await svc.guard(_bgp(50)) == "select"
    assert await svc.guard("SELECT * WHERE " + "{" * 30 + "?s ?p ?o" + "}" * 30) == "select"
    groups = " ".join(f"{{ ?s{i} <http://p> ?o{i} }}" for i in range(300))
    assert await svc.guard(f"SELECT * WHERE {{ {groups} }}") == "select"
    with pytest.raises(LakeloomError) as info:
        await svc.guard(_bgp(300))

    assert info.value.code == "validation_error"
    assert "`;`" in info.value.hint and "`{ }` groups" in info.value.hint
