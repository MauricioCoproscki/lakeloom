"""OM 2.1.0-rc1: is a change set ever half-visible in a glossary export? (ADR 0017 R4, FR-GIT-08)

Reconciliation compares an export with the last Git commit and assumes an export never shows
part of a change set that is being applied (ADR 0017 R4). This probe checks it: one throwaway
glossary ``llm4b_probe_<hex>`` gets one change set adding ``TERMS`` terms (created and applied
by the bot: test setup only, ADR 0011), while a second task exports the glossary in a loop and
counts how many of those terms each export holds. The glossary and the change set are
hard-deleted in teardown even on failure; nothing is ever sealed. The observed counts and
timing are recorded in ADR 0018.
"""

import asyncio
import secrets
import time
import uuid
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from rdflib import Graph, Namespace
from rdflib.namespace import SKOS

from lakeloom.core.operations import AddConcept, OMContext, draft_term_id, to_om_all
from lakeloom.om.client import OMClient
from lakeloom.untrusted import UntrustedText

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="module")]

TERMS = 40


def _bearer(token: str) -> httpx.Headers:
    """Headers kept in no named local: ``pytest -l`` would print a dict holding the token."""
    return httpx.Headers({"Authorization": f"Bearer {token}"})


async def _lease(http: httpx.AsyncClient, change_set_id: str) -> dict[str, Any]:
    session = f"contract-{uuid.uuid4()}"
    lock = {
        "resourceType": "ontologyChangeSet",
        "resourceId": change_set_id,
        "sessionId": session,
        "leaseSeconds": 120,
    }
    lease = (await http.post("/api/v1/ontologyEditLocks/acquire", json=lock)).raise_for_status()
    return {"lease": {"sessionId": session, "version": lease.json()["version"]}}


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


async def test_r4_export_never_shows_a_partial_change_set(om_url: str, om_bot_token: str) -> None:
    slug = f"llm4b_probe_{secrets.token_hex(4)}"
    base = f"http://localhost:8000/ontology/{slug}/"
    ns = Namespace(base)
    names = [f"Term{i:02d}" for i in range(TERMS)]
    change_sets: list[str] = []
    async with httpx.AsyncClient(
        base_url=om_url, headers=_bearer(om_bot_token), timeout=120
    ) as http:
        client = OMClient(om_url, SecretStr(om_bot_token), timeout=120)
        try:
            glossary = await client.create_glossary(name=slug, display_name=None, description="p")
            ops = [
                AddConcept(
                    op="add_concept",
                    name=n,
                    description=UntrustedText(text=f"{n} probe", source="contract probe"),
                )
                for n in names
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
            token = await _lease(http, cs.id)
            path = f"/api/v1/ontologyChangeSets/{cs.id}"
            (await http.post(f"{path}/submit", json=token)).raise_for_status()

            seen: list[tuple[float, float, int]] = []  # (started, finished, terms present)
            stop = asyncio.Event()

            async def export_loop() -> None:
                while not stop.is_set():
                    started = time.monotonic()
                    turtle = await client.export_ontology(glossary.id, "turtle")
                    graph = Graph().parse(data=turtle, format="turtle")
                    present = sum(1 for n in names if (ns[n], SKOS.definition, None) in graph)
                    seen.append((started, time.monotonic(), present))

            exporter = asyncio.create_task(export_loop())
            await asyncio.sleep(0.5)
            apply_started = time.monotonic()
            applied = (await http.post(f"{path}/apply", json=token)).raise_for_status().json()
            apply_finished = time.monotonic()
            await asyncio.sleep(1.0)
            stop.set()
            await exporter

            assert applied["state"] == "APPLIED"
            counts = [present for _, _, present in seen]
            overlapping = [
                present
                for started, finished, present in seen
                if started < apply_finished and finished > apply_started
            ]
            print(  # recorded in ADR 0018
                f"R4 probe: {len(seen)} exports, apply took "
                f"{apply_finished - apply_started:.3f} s, {len(overlapping)} overlapped it, "
                f"counts seen {sorted(set(counts))}"
            )
            assert overlapping, "no export ran while the change set was applied"
            assert counts[-1] == TERMS
            assert set(counts) <= {0, TERMS}, f"a partial change set was exported: {counts}"
        finally:
            try:
                await _teardown(http, slug, change_sets)
            finally:
                await client.aclose()
