"""``GraphStoreSink`` against the dev stack's Fuseki (``rdf`` profile), FR-TS-01.

Skipped unless Fuseki answers ``/$/ping`` (``FUSEKI_URL``, default http://localhost:3030). The
admin password comes from ``FUSEKI_ADMIN_PASSWORD`` (default ``admin``, dev only). A throwaway
in-memory dataset ``lakeloom_m3b`` is created and always deleted again.
"""

import os
from collections.abc import AsyncGenerator

import httpx
import pytest
from pydantic import SecretStr

from lakeloom.publish.triplestore import GraphStoreSink

FUSEKI = os.environ.get("FUSEKI_URL", "http://localhost:3030").rstrip("/")
ADMIN_PASSWORD = os.environ.get("FUSEKI_ADMIN_PASSWORD", "admin")
DATASET = "lakeloom_m3b"
BASE = "http://example.test/ontology/m3b/"
TTL = b"@prefix skos: <http://www.w3.org/2004/02/skos/core#> .\n" + (
    f'<{BASE}Order> a skos:Concept ; skos:prefLabel "Order"@en .\n'.encode()
)


def _fuseki_up() -> bool:
    try:
        return httpx.get(f"{FUSEKI}/$/ping", timeout=2).is_success
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(not _fuseki_up(), reason="Fuseki (rdf profile) is not running")


@pytest.fixture
async def http() -> AsyncGenerator[httpx.AsyncClient]:
    auth = httpx.BasicAuth("admin", ADMIN_PASSWORD)
    async with httpx.AsyncClient(auth=auth, timeout=15) as client:
        created = await client.post(
            f"{FUSEKI}/$/datasets", data={"dbName": DATASET, "dbType": "mem"}
        )
        assert created.status_code in (200, 409), created.text
        try:
            yield client
        finally:
            await client.delete(f"{FUSEKI}/$/datasets/{DATASET}")


async def _ask(http: httpx.AsyncClient, graph: str) -> bool:
    query = f"ASK {{ GRAPH <{graph}> {{ <{BASE}Order> ?p ?o }} }}"
    res = await http.post(
        f"{FUSEKI}/{DATASET}/sparql",
        data={"query": query},
        headers={"Accept": "application/sparql-results+json"},
    )
    res.raise_for_status()
    return bool(res.json()["boolean"])


async def test_graph_store_sink_against_fuseki(http: httpx.AsyncClient) -> None:
    sink = GraphStoreSink(http, f"{FUSEKI}/{DATASET}/data", "admin", SecretStr(ADMIN_PASSWORD))

    await sink.replace_graph(f"{BASE}release/1.0.0", TTL)
    await sink.replace_graph(f"{BASE}latest", TTL)

    assert await _ask(http, f"{BASE}release/1.0.0")
    assert await _ask(http, f"{BASE}latest")
    assert not await _ask(http, f"{BASE}release/9.9.9")
