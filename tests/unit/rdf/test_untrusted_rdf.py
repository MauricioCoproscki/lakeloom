import asyncio
import json
import socket
import time
from typing import Any

import pytest
from rdflib import Graph

from lakeloom.errors import LakeloomError
from lakeloom.rdf import untrusted
from lakeloom.rdf.untrusted import parse_untrusted, parse_untrusted_async

BIG = 10_000_000
TTL = b'@prefix ex: <https://example.org/m/> .\nex:A a ex:B ; ex:p "x" .\n'


def _refused(data: bytes, fmt: Any, **kw: Any) -> LakeloomError:
    with pytest.raises(LakeloomError) as info:
        parse_untrusted(data, fmt, max_bytes=kw.pop("max_bytes", BIG), max_triples=BIG)
    assert info.value.code == "validation_error"
    assert info.value.hint
    return info.value


def test_size_checked_before_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> Graph:
        raise AssertionError("parse_rdf must not run")

    monkeypatch.setattr(untrusted, "parse_rdf", boom)
    err = _refused(b"x" * 11, "turtle", max_bytes=10)
    assert "11 bytes" in err.message
    assert "LAKELOOM_IMPORT_MAX_BYTES" in err.message


def test_parse_ok() -> None:
    assert len(parse_untrusted(TTL, "turtle", max_bytes=BIG, max_triples=BIG)) == 2


@pytest.mark.parametrize(
    "doc",
    [
        {"@context": "https://evil.example/ctx", "@id": "https://e/x"},
        {"@context": [{"a": "https://e/a"}, "https://evil.example/ctx"], "@id": "https://e/x"},
        {"@context": {"a": "https://e/a"}, "@graph": [{"@context": "https://evil/c", "@id": "x"}]},
        {"@context": {"@import": "https://evil.example/ctx"}, "@id": "https://e/x"},
    ],
)
def test_jsonld_remote_context_refused(doc: dict[str, Any]) -> None:
    err = _refused(json.dumps(doc).encode(), "jsonld")
    assert "remote JSON-LD contexts are not allowed" in err.message


def test_jsonld_inline_context_ok() -> None:
    doc = {"@context": {"p": "https://e/p"}, "@id": "https://e/x", "p": "v"}
    g = parse_untrusted(json.dumps(doc).encode(), "jsonld", max_bytes=BIG, max_triples=BIG)
    assert len(g) == 1


@pytest.mark.parametrize("marker", ["<!DOCTYPE", "<!doctype", "<!ENTITY"])
def test_rdfxml_doctype_refused(marker: str) -> None:
    doc = f'<?xml version="1.0"?>\n{marker} rdf [ <!ENTITY a "b"> ]>\n<rdf:RDF/>'.encode()
    err = _refused(doc, "rdfxml")
    assert "DTD" in err.message


def test_parse_error_keeps_line_and_column() -> None:
    err = _refused(
        b"<https://e/a> <https://e/b> <https://e/c> .\n<https://e/a> <https://e/b> .\n", "ntriples"
    )
    assert "line 2" in err.message


def test_triple_cap() -> None:
    with pytest.raises(LakeloomError) as info:
        parse_untrusted(TTL, "turtle", max_bytes=BIG, max_triples=1)
    assert info.value.code == "validation_error"
    assert "LAKELOOM_IMPORT_MAX_TRIPLES" in info.value.message


async def test_timeout_is_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*a: Any, **k: Any) -> Graph:
        time.sleep(0.5)
        return Graph()

    monkeypatch.setattr(untrusted, "parse_untrusted", slow)
    with pytest.raises(LakeloomError) as info:
        await parse_untrusted_async(TTL, "turtle", max_bytes=BIG, max_triples=BIG, timeout=0.05)
    assert info.value.code == "validation_error"
    assert "exceeded" in info.value.message
    assert info.value.hint


async def test_async_size_first_and_ok() -> None:
    with pytest.raises(LakeloomError):
        await parse_untrusted_async(TTL, "turtle", max_bytes=5, max_triples=BIG, timeout=5)
    g = await parse_untrusted_async(TTL, "turtle", max_bytes=BIG, max_triples=BIG, timeout=5)
    assert len(g) == 2
    await asyncio.sleep(0)


def test_no_network_during_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*a: Any, **k: Any) -> Any:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    docs: dict[Any, bytes] = {
        "turtle": TTL,
        "ntriples": b"<https://e/a> <https://e/p> <https://e/b> .\n",
        "rdfxml": (
            b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            b'<rdf:Description rdf:about="https://e/a"/></rdf:RDF>'
        ),
        "jsonld": b'{"@context": {"p": "https://e/p"}, "@id": "https://e/a", "p": "v"}',
    }
    for fmt, data in docs.items():
        parse_untrusted(data, fmt, max_bytes=BIG, max_triples=BIG)
