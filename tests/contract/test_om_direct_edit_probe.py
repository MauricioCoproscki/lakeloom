"""OM 2.1.0-rc1 behaviour that M4a reconciliation relies on (ADR 0017 § Research; FR-GIT-08).

One throwaway glossary ``llm4a_probe_<hex>`` (two terms, created by a change set the bot
applies: test setup only, ADR 0011) is created for the module and hard-deleted in teardown even
on failure. Nothing is ever sealed. The facts checked:

- the glossary export is stable: two exports of an unchanged glossary have the same semantic
  view (bookkeeping, prefixes and triple order aside), so an untouched model is ``in_sync``;
- a bot ``PATCH`` of a term description outside any change set (a *direct edit*) succeeds;
- the next export shows the new text as ``skos:definition`` and nothing else of substance, so
  ``diff_graphs`` reports exactly that term as changed (an annotation-only change).
"""

import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from rdflib import Graph, Literal, Namespace
from rdflib.compare import isomorphic
from rdflib.namespace import SKOS

from lakeloom.core.operations import AddConcept, OMContext, draft_term_id, to_om_all
from lakeloom.core.release_diff import diff_graphs
from lakeloom.om.client import OMClient
from lakeloom.rdf.semantic import semantic_view
from lakeloom.untrusted import UntrustedText

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="module")]


@dataclass
class Probe:
    http: httpx.AsyncClient
    client: OMClient
    slug: str
    base: str
    glossary_id: str
    change_sets: list[str] = field(default_factory=list[str])


def _bearer(token: str) -> httpx.Headers:
    """Headers kept in no named local: ``pytest -l`` would print a dict holding the token."""
    return httpx.Headers({"Authorization": f"Bearer {token}"})


async def _apply(http: httpx.AsyncClient, change_set_id: str) -> dict[str, Any]:
    """Test-only: submit and apply a draft as the bot (ADR 0011); returns the change set."""
    session = f"contract-{uuid.uuid4()}"
    lock = {
        "resourceType": "ontologyChangeSet",
        "resourceId": change_set_id,
        "sessionId": session,
        "leaseSeconds": 60,
    }
    lease = (await http.post("/api/v1/ontologyEditLocks/acquire", json=lock)).raise_for_status()
    token = {"lease": {"sessionId": session, "version": lease.json()["version"]}}
    base = f"/api/v1/ontologyChangeSets/{change_set_id}"
    (await http.post(f"{base}/submit", json=token)).raise_for_status()
    body: dict[str, Any] = (await http.post(f"{base}/apply", json=token)).raise_for_status().json()
    return body


async def _teardown(http: httpx.AsyncClient, slug: str, change_sets: list[str]) -> None:
    for cs_id in change_sets:
        await http.delete(f"/api/v1/ontologyChangeSets/{cs_id}", params={"hardDelete": "true"})
    await http.delete(
        f"/api/v1/glossaries/name/{slug}", params={"hardDelete": "true", "recursive": "true"}
    )
    for path in (
        f"/api/v1/glossaries/name/{slug}",
        *(f"/api/v1/ontologyChangeSets/{c}" for c in change_sets),
    ):
        assert (await http.get(path, params={"include": "all"})).status_code == 404, path


def _text(value: str) -> UntrustedText:
    return UntrustedText(text=value, source="contract probe")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def probe(om_url: str, om_bot_token: str) -> AsyncIterator[Probe]:
    slug = f"llm4a_probe_{secrets.token_hex(4)}"
    base = f"http://localhost:8000/ontology/{slug}/"
    change_sets: list[str] = []
    async with httpx.AsyncClient(
        base_url=om_url, headers=_bearer(om_bot_token), timeout=60
    ) as http:
        client = OMClient(om_url, SecretStr(om_bot_token), timeout=60)
        retail = (await http.get("/api/v1/glossaries/name/retail")).raise_for_status().json()
        try:
            glossary = await client.create_glossary(name=slug, display_name=None, description="p")
            ops = [
                AddConcept(op="add_concept", name="Party", description=_text("A party")),
                AddConcept(op="add_concept", name="Order", description=_text("An order")),
            ]
            ids = {o.name: draft_term_id(o, slug) for o in ops}
            ctx = OMContext(
                glossary_name=slug,
                glossary_id=glossary.id,
                base_iri=base,
                ontology_id="",  # unpaired: the glossary side only
                paired=False,
                term_ids=ids,
                term_versions=dict.fromkeys(ids, 0.1),
            )
            cs = await client.create_change_set(
                name=f"{slug}-g",
                glossary_id=glossary.id,
                description="p",
                operations=to_om_all(ops, ctx).glossary,
            )
            change_sets.append(cs.id)
            assert (await _apply(http, cs.id))["state"] == "APPLIED"
            yield Probe(http, client, slug, base, glossary.id, change_sets)
        finally:
            try:
                await _teardown(http, slug, change_sets)
            finally:
                await client.aclose()
                after = (await http.get("/api/v1/glossaries/name/retail")).raise_for_status().json()
                assert (after["version"], after["updatedAt"]) == (
                    retail["version"],
                    retail["updatedAt"],
                ), "the seeded retail glossary changed"


async def _export(probe: Probe) -> Graph:
    turtle = await probe.client.export_ontology(probe.glossary_id, "turtle")
    return Graph().parse(data=turtle, format="turtle")


async def test_export_is_stable_and_a_direct_edit_shows_as_definition(probe: Probe) -> None:
    ns = Namespace(probe.base)
    first, second = await _export(probe), await _export(probe)
    assert isomorphic(semantic_view(first, probe.base), semantic_view(second, probe.base))
    unchanged = diff_graphs(
        semantic_view(first, probe.base),
        semantic_view(second, probe.base),
        from_version=None,
        to_version="",
    )
    assert unchanged.bump is None
    assert list(first.objects(ns.Party, SKOS.definition)) == [Literal("A party")]

    glossary_before = (
        (await probe.http.get(f"/api/v1/glossaries/{probe.glossary_id}")).raise_for_status().json()
    )
    term = (
        (await probe.http.get(f"/api/v1/glossaryTerms/name/{probe.slug}.Party"))
        .raise_for_status()
        .json()
    )
    # the direct edit: a bot PATCH outside any change set
    r = await probe.http.patch(
        f"/api/v1/glossaryTerms/{term['id']}",
        content=b'[{"op": "replace", "path": "/description", "value": "A party, edited in OM"}]',
        headers={"Content-Type": "application/json-patch+json"},
    )
    assert r.status_code == 200, r.status_code
    assert r.json()["description"] == "A party, edited in OM"

    after = await _export(probe)
    assert list(after.objects(ns.Party, SKOS.definition)) == [Literal("A party, edited in OM")]
    drift = diff_graphs(
        semantic_view(first, probe.base),
        semantic_view(after, probe.base),
        from_version=None,
        to_version="",
    )
    assert [t.iri for t in drift.changed] == [str(ns.Party)]
    assert (drift.added, drift.removed, drift.bump) == ([], [], "patch")
    # a term PATCH does not move the glossary's own updatedAt (the model version in rc1)
    glossary_after = (
        (await probe.http.get(f"/api/v1/glossaries/{probe.glossary_id}")).raise_for_status().json()
    )
    assert glossary_after["updatedAt"] == glossary_before["updatedAt"]
