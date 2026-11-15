"""OM 2.1.0-rc1 behaviour that M3c import, drift and the SPARQL proxy rely on (ADR 0016
§ Research; FR-IO-02, FR-PUB-03).

One throwaway paired model ``llm3c_probe_<hex>`` (glossary + native ontology of the same
name, ADR 0011) is created for the module, filled by change sets that the bot applies (test
setup only), and hard-deleted in teardown even on failure. Nothing is ever sealed. The
assertions are the facts observed live; an OM upgrade that changes them fails here.
"""

import secrets
import time
import typing
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from rdflib import RDF, RDFS, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL, SKOS

from lakeloom.core.operations import (
    AddAttribute,
    AddAxiom,
    AddConcept,
    AddMapping,
    AddRelation,
    OMContext,
    draft_term_id,
    native_member_key,
    to_om_all,
)
from lakeloom.errors import LakeloomError
from lakeloom.om.client import OMClient
from lakeloom.untrusted import UntrustedText

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="module")]

OM = Namespace("https://open-metadata.org/ontology/")
XSD = "http://www.w3.org/2001/XMLSchema#"
SKOS_CONCEPT = "http://www.w3.org/2004/02/skos/core#Concept"


@dataclass
class Probe:
    http: httpx.AsyncClient
    client: OMClient
    slug: str
    base: str
    glossary_id: str
    ontology_id: str
    ctx: OMContext
    change_sets: list[str] = field(default_factory=list[str])


def _text(value: str) -> UntrustedText:
    return UntrustedText(text=value, source="contract probe")


def _bearer(token: str) -> httpx.Headers:
    """Headers kept in no named local: ``pytest -l`` would print a dict holding the token."""
    return httpx.Headers({"Authorization": f"Bearer {token}"})


async def _apply(probe: Probe, change_set_id: str) -> dict[str, Any]:
    """Test-only: submit and apply a draft as the bot (ADR 0011); returns the change set."""
    session = f"contract-{uuid.uuid4()}"
    lock = {
        "resourceType": "ontologyChangeSet",
        "resourceId": change_set_id,
        "sessionId": session,
        "leaseSeconds": 60,
    }
    http = probe.http
    lease = (await http.post("/api/v1/ontologyEditLocks/acquire", json=lock)).raise_for_status()
    token = {"lease": {"sessionId": session, "version": lease.json()["version"]}}
    base = f"/api/v1/ontologyChangeSets/{change_set_id}"
    (await http.post(f"{base}/submit", json=token)).raise_for_status()
    applied = (await http.post(f"{base}/apply", json=token)).raise_for_status()
    body: dict[str, Any] = applied.json()
    return body


async def _native(probe: Probe, tag: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
    state = await probe.client.get_ontology_state(probe.ontology_id)
    cs = await probe.client.create_change_set(
        name=f"{probe.slug}-{tag}",
        glossary_id=None,
        ontology=probe.slug,
        base_working_revision=state.working_revision,
        description="probe",
        operations=operations,
    )
    probe.change_sets.append(cs.id)
    return await _apply(probe, cs.id)


async def _members(probe: Probe, kind: str) -> dict[str, dict[str, Any]]:
    params = {"ontology": probe.slug, "fields": "*", "limit": "100"}
    body = (await probe.http.get(f"/api/v1/{kind}", params=params)).raise_for_status().json()
    return {str(m["name"]): m for m in body["data"]}


async def _live_ctx(probe: Probe) -> OMContext:
    """``probe.ctx`` plus the native ids, versions and tombstones OM has now, keyed as
    ``ProposalService._context`` keys them."""
    kinds: tuple[typing.Literal["concept", "attr", "axiom"], ...] = ("concept", "attr", "axiom")
    live = await probe.client.list_native_members(probe.slug)
    tombs = await probe.client.list_native_members(probe.slug, include_deleted=True)
    ids: dict[str, str] = {}
    versions: dict[str, float] = {}
    for prefix, members in zip(kinds, live, strict=True):
        for m in members:
            key = native_member_key(prefix, m)
            ids[key], versions[key] = str(m["id"]), float(m["version"])
    deleted = {
        native_member_key(prefix, m)
        for prefix, members in zip(kinds, tombs, strict=True)
        for m in members
    } - ids.keys()
    return replace(probe.ctx, native_ids=ids, native_versions=versions, native_deleted=deleted)


def _untargeted(op: dict[str, Any], body: str) -> dict[str, Any]:
    """Raw payload: ``op`` as a create (no ``targetId``/``baseVersion``, a fresh body id)."""
    raw = {k: v for k, v in op.items() if k not in ("targetId", "baseVersion")}
    raw[body] = {**op[body], "id": str(uuid.uuid4())}
    return raw


async def _draft_raw(probe: Probe, tag: str, operations: list[dict[str, Any]]) -> httpx.Response:
    """POST a native draft as given (bypassing ``OMClient`` checks); records it for teardown."""
    state = await probe.client.get_ontology_state(probe.ontology_id)
    body = {
        "name": f"{probe.slug}-{tag}",
        "displayName": tag,
        "description": "probe",
        "ontology": probe.slug,
        "baseWorkingRevision": state.working_revision,
        "operations": [{**o, "id": str(uuid.uuid4()), "state": "ACTIVE"} for o in operations],
    }
    r = await probe.http.post("/api/v1/ontologyChangeSets", json=body)
    if r.status_code < 300:
        probe.change_sets.append(str(r.json()["id"]))
    return r


def _ops() -> list[Any]:
    return [
        AddConcept(op="add_concept", name="Party", description=_text("A party")),
        AddConcept(
            op="add_concept",
            name="Customer",
            display_name="Client",
            description=_text("A customer"),
            parent="Party",
            synonyms=["Buyer"],
        ),
        AddConcept(op="add_concept", name="Order", description=_text("An order")),
        AddAttribute(
            op="add_attribute",
            concept="Customer",
            name="customerId",
            datatype="xsd:long",
            is_identifier=True,
            description=_text("Customer number"),
        ),
        AddAttribute(
            op="add_attribute",
            concept="Customer",
            name="height",
            datatype="xsd:decimal",
            allowed_values=["1.5", "2.0"],
            unit="unit:M",
            description=_text("Height"),
        ),
        AddRelation(op="add_relation", source="Order", relation_type="placedBy", target="Customer"),
        AddMapping(
            op="add_mapping",
            concept="Customer",
            kind="exactMatch",
            target_iri="https://schema.org/Person",  # pyright: ignore[reportArgumentType]
        ),
        AddAxiom(
            op="add_axiom",
            concept="Customer",
            axiom={
                "name": "CustomerPrimaryKey",
                "displayName": "Customer primary key",
                "description": "Primary key",
                "axiomType": "SUBCLASS_OF",
                "expressions": [
                    {
                        "kind": "RESTRICTION",
                        "restrictionKind": "EXACT",
                        "propertyName": "customerId",
                        "cardinality": 1,
                    }
                ],
            },
        ),
    ]


async def _teardown(http: httpx.AsyncClient, slug: str, change_sets: list[str]) -> None:
    for cs_id in change_sets:
        await http.delete(f"/api/v1/ontologyChangeSets/{cs_id}", params={"hardDelete": "true"})
    for kind in ("ontologies", "glossaries"):
        await http.delete(
            f"/api/v1/{kind}/name/{slug}", params={"hardDelete": "true", "recursive": "true"}
        )
    for path in (
        f"/api/v1/ontologies/name/{slug}",
        f"/api/v1/glossaries/name/{slug}",
        *(f"/api/v1/ontologyChangeSets/{c}" for c in change_sets),
    ):
        assert (await http.get(path, params={"include": "all"})).status_code == 404, path


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def probe(om_url: str, om_bot_token: str) -> AsyncIterator[Probe]:
    slug = f"llm3c_probe_{secrets.token_hex(4)}"
    base = f"http://localhost:8000/ontology/{slug}/"
    change_sets: list[str] = []
    async with httpx.AsyncClient(
        base_url=om_url, headers=_bearer(om_bot_token), timeout=60
    ) as http:
        client = OMClient(om_url, SecretStr(om_bot_token), timeout=60)
        retail = (await http.get("/api/v1/glossaries/name/retail")).raise_for_status().json()
        try:
            glossary = await client.create_glossary(name=slug, display_name=None, description="p")
            onto = await client.create_ontology(name=slug, iri=base, description="probe")
            ops = _ops()
            ids = {o.name: draft_term_id(o, slug) for o in ops if isinstance(o, AddConcept)}
            ctx = OMContext(
                glossary_name=slug,
                glossary_id=glossary.id,
                base_iri=base,
                ontology_id=onto.id,
                term_ids=ids,
                term_versions=dict.fromkeys(ids, 0.1),
                relationship_type_ids=await client.list_relationship_types(),
            )
            p = Probe(http, client, slug, base, glossary.id, onto.id, ctx, change_sets)
            out = to_om_all(ops, ctx)
            native = out.native
            terms = await client.create_change_set(
                name=f"{slug}-g", glossary_id=glossary.id, description="p", operations=out.glossary
            )
            change_sets.append(terms.id)
            assert (await _apply(p, terms.id))["state"] == "APPLIED"
            assert (await _native(p, "n", native))["state"] == "APPLIED"
            yield p
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


async def test_glossary_export_vocabulary(probe: Probe) -> None:
    turtle = await probe.client.export_ontology(probe.glossary_id, "turtle")
    g = Graph().parse(data=turtle, format="turtle")
    ns = Namespace(probe.base)
    party, customer, order = ns.Party, ns.Customer, ns.Order

    # types: glossary terms are skos:Concept + om:GlossaryTerm, never owl:Class
    assert set(g.objects(customer, RDF.type)) == {SKOS.Concept, OM.GlossaryTerm}
    # prefLabel = displayName when set, else the name
    assert list(g.objects(customer, SKOS.prefLabel)) == [Literal("Client")]
    assert list(g.objects(party, SKOS.prefLabel)) == [Literal("Party")]
    assert list(g.objects(customer, SKOS.altLabel)) == [Literal("Buyer")]
    assert list(g.objects(customer, SKOS.definition)) == [Literal("A customer")]
    assert not list(g.objects(customer, RDFS.label))
    assert not list(g.objects(customer, RDFS.comment))
    # parent: skos:broader on the child, skos:narrower on the parent, no rdfs:subClassOf
    assert list(g.objects(customer, SKOS.broader)) == [party]
    assert list(g.objects(party, SKOS.narrower)) == [customer]
    assert not list(g.triples((None, RDFS.subClassOf, None)))
    # a child is not a top concept, and its FQN nests under the parent
    assert not list(g.objects(customer, SKOS.topConceptOf))
    assert list(g.objects(party, SKOS.topConceptOf))
    fqn = list(g.objects(customer, OM.fullyQualifiedName))
    assert fqn == [Literal(f"{probe.slug}.Party.Customer")]
    assert list(g.objects(customer, SKOS.exactMatch)) == [URIRef("https://schema.org/Person")]
    # the placedBy relation: applied through a change set it is stored as relatedTo (rc1
    # ignores relationshipType on ADD_RELATIONSHIP) and exported both ways as
    # om:relatedTo + skos:related; no placedBy predicate appears
    assert set(g.predicate_objects(order)) >= {(OM.relatedTo, customer), (SKOS.related, customer)}
    assert set(g.predicate_objects(customer)) >= {(OM.relatedTo, order), (SKOS.related, order)}
    assert not [p for p in g.predicates() if str(p).endswith("placedBy")]
    term = (
        (
            await probe.http.get(
                f"/api/v1/glossaryTerms/name/{probe.slug}.Order", params={"fields": "relatedTerms"}
            )
        )
        .raise_for_status()
        .json()
    )
    assert [(r["relationType"], r["term"]["name"]) for r in term["relatedTerms"]] == [
        ("relatedTo", "Customer")
    ]
    # the glossary export holds no native members
    assert not [
        s for s in g.subjects(RDF.type, OWL.DatatypeProperty) if str(s).startswith(probe.base)
    ]

    # the native side: placedBy is an OBJECT property; enum values come back as literals
    props = await _members(probe, "ontologyProperties")
    assert props["Order.placedBy"]["kind"] == "OBJECT"
    assert props["Order.placedBy"]["range"] == [str(customer)]
    height = props["Customer.height"]
    assert height["enumValues"] == [{"value": "1.5"}, {"value": "2.0"}]
    assert height["unit"] == "unit:M"
    assert props["Customer.customerId"]["isIdentifier"] is True


async def test_enum_values_must_be_ontology_literals(probe: Probe) -> None:
    """``enumValues`` as plain strings is refused at draft time; ``to_om`` sends objects."""
    attr = AddAttribute(
        op="add_attribute",
        concept="Customer",
        name="size",
        datatype="xsd:string",
        allowed_values=["S", "M"],
    )
    (op,) = to_om_all([attr], probe.ctx).native
    assert op["property"]["enumValues"] == [{"value": "S"}, {"value": "M"}]
    strings = {**op, "property": {**op["property"], "enumValues": ["S", "M"]}}
    r = await _draft_raw(probe, "strenum", [strings])
    assert (r.status_code, r.json()["message"]) == (400, "Invalid request format")


async def test_enum_values_round_trip_live(probe: Probe) -> None:
    """A proposal-style ``AddAttribute`` with allowed values, built by ``to_om_all`` and
    applied, reads back through ``native_rdf`` as an ``owl:oneOf`` list in OM's order."""
    attr = AddAttribute(
        op="add_attribute",
        concept="Customer",
        name="shoeSize",
        datatype="xsd:decimal",
        allowed_values=["41.5", "40"],
    )
    native = to_om_all([attr], await _live_ctx(probe)).native
    assert (await _native(probe, "e1", native))["state"] == "APPLIED"
    export = await probe.client.export_native_ontology(probe.slug)
    assert export is not None and export.graph is not None
    heads = list(export.graph.objects(URIRef(f"{probe.base}Customer.shoeSize"), OWL.oneOf))
    assert len(heads) == 1
    assert [str(v) for v in Collection(export.graph, heads[0])] == ["41.5", "40"]


async def test_targeted_upsert_from_to_om_live(probe: Probe) -> None:
    """``to_om_all`` of a changed ``AddAttribute`` on an existing property, with the native
    ids read from OM, is a targeted upsert that applies and replaces the range."""
    first = AddAttribute(
        op="add_attribute",
        concept="Customer",
        name="weight",
        datatype="xsd:decimal",
        unit="unit:KiloGM",
    )
    assert (await _native(probe, "w1", to_om_all([first], probe.ctx).native))["state"] == "APPLIED"
    before = (await _members(probe, "ontologyProperties"))["Customer.weight"]
    changed = first.model_copy(update={"datatype": "xsd:integer"})
    (op,) = to_om_all([changed], await _live_ctx(probe)).native
    assert (op["targetId"], op["baseVersion"]) == (before["id"], before["version"])
    assert (await _native(probe, "w2", [op]))["state"] == "APPLIED"
    after = (await _members(probe, "ontologyProperties"))["Customer.weight"]
    assert after["id"] == before["id"]
    assert after["range"] == [f"{XSD}integer"]
    assert after["unit"] == "unit:KiloGM"  # carried by the upsert, so not cleared


async def test_upsert_property_replaces_existing(probe: Probe) -> None:
    before = (await _members(probe, "ontologyProperties"))["Customer.height"]
    changed = AddAttribute(
        op="add_attribute",
        concept="Customer",
        name="height",
        datatype="xsd:integer",
        description=_text("Height v2"),
    )
    upsert = to_om_all([changed], await _live_ctx(probe)).native
    assert [o["operationType"] for o in upsert] == ["UPSERT_PROPERTY"]
    # to_om addresses the existing definition (R2) ...
    assert (upsert[0]["targetId"], upsert[0]["baseVersion"]) == (before["id"], before["version"])
    assert upsert[0]["property"]["id"] == before["id"]

    # ... because a create of an existing IRI (raw: no targetId/baseVersion, a fresh id) is
    # HTTP 200 but the set ends APPLY_FAILED
    failed = await _native(probe, "n2", [_untargeted(upsert[0], "property")])
    assert failed["state"] == "APPLY_FAILED"
    result = failed["applicationResult"]
    assert (result["operationsApplied"], result["operationsFailed"]) == (0, 1)
    assert result["results"][0]["message"] == (
        f'Definition already exists: {probe.slug}."Customer.height"'
    )
    same = (await _members(probe, "ontologyProperties"))["Customer.height"]
    assert (same["range"], same["version"]) == (before["range"], before["version"])

    # with the existing property's id as targetId and its version as baseVersion: replaced
    assert (await _native(probe, "n3", upsert))["state"] == "APPLIED"
    after = (await _members(probe, "ontologyProperties"))["Customer.height"]
    assert after["id"] == before["id"]
    assert after["range"] == [f"{XSD}integer"]
    assert after["version"] > before["version"]
    # structural fields are replaced in full: fields the upsert omits are cleared ...
    assert after.get("unit") is None
    assert not after.get("enumValues")
    # ... but rc1 ignores a new description (and displayName) on an existing definition
    assert after["description"] == "Height"

    # a description-only upsert with a target is APPLIED and changes nothing
    relabel = AddAttribute(
        op="add_attribute",
        concept="Customer",
        name="height",
        datatype="xsd:integer",
        description=_text("Height v3"),
    )
    op = to_om_all([relabel], await _live_ctx(probe)).native[0]
    assert (op["targetId"], op["baseVersion"]) == (after["id"], after["version"])
    only_text = [{**op, "property": {**op["property"], "displayName": "Height"}}]
    assert (await _native(probe, "n4", only_text))["state"] == "APPLIED"
    last = (await _members(probe, "ontologyProperties"))["Customer.height"]
    assert (last["description"], last["displayName"], last["version"]) == (
        "Height",
        "height",
        after["version"],
    )


async def test_deleted_definition_name_stays_reserved(probe: Probe) -> None:
    """DELETE_PROPERTY leaves a tombstone (``deleted: true``); the name cannot be upserted
    again, neither as a new definition nor with the tombstone as target."""
    temp = AddAttribute(op="add_attribute", concept="Customer", name="temp", datatype="xsd:string")
    assert (await _native(probe, "t1", to_om_all([temp], probe.ctx).native))["state"] == "APPLIED"
    prop = (await _members(probe, "ontologyProperties"))["Customer.temp"]
    delete = [
        {"operationType": "DELETE_PROPERTY", "targetId": prop["id"], "baseVersion": prop["version"]}
    ]
    # one draft may not delete and upsert the same definition
    (upsert,) = to_om_all([temp], await _live_ctx(probe)).native
    r = await _draft_raw(probe, "t1b", [*delete, upsert])
    assert (r.status_code, r.json()["message"]) == (
        400,
        "A native draft may change each definition only once",
    )
    assert (await _native(probe, "t2", delete))["state"] == "APPLIED"
    assert "Customer.temp" not in await _members(probe, "ontologyProperties")
    params = {"ontology": probe.slug, "fields": "*", "include": "deleted"}
    tombs = (await probe.http.get("/api/v1/ontologyProperties", params=params)).json()["data"]
    tomb = next(t for t in tombs if t["name"] == "Customer.temp")
    assert (tomb["id"], tomb["deleted"]) == (prop["id"], True)

    # to_om refuses to create it again once the tombstone is known (fail closed) ...
    ctx = await _live_ctx(probe)
    assert f"attr:{probe.base}Customer.temp" in ctx.native_deleted
    with pytest.raises(LakeloomError) as err:
        to_om_all([temp], ctx)
    assert err.value.code == "validation_error"
    # ... since OM ends a create of the reserved name (raw, as a new definition) APPLY_FAILED
    again = to_om_all([temp], probe.ctx).native
    assert "targetId" not in again[0]
    failed = await _native(probe, "t3", again)
    assert failed["state"] == "APPLY_FAILED"
    assert failed["applicationResult"]["results"][0]["message"] == (
        f'Definition already exists: {probe.slug}."Customer.temp"'
    )
    revive = [
        {
            **again[0],
            "targetId": tomb["id"],
            "baseVersion": tomb["version"],
            "property": {**again[0]["property"], "id": tomb["id"]},
        }
    ]
    state = await probe.client.get_ontology_state(probe.ontology_id)
    cs = await probe.client.create_change_set(
        name=f"{probe.slug}-t4",
        glossary_id=None,
        ontology=probe.slug,
        base_working_revision=state.working_revision,
        description="probe",
        operations=revive,
    )
    probe.change_sets.append(cs.id)
    session = f"contract-{uuid.uuid4()}"
    lock = {"resourceType": "ontologyChangeSet", "resourceId": cs.id, "sessionId": session}
    lease = (await probe.http.post("/api/v1/ontologyEditLocks/acquire", json=lock)).json()
    token = {"lease": {"sessionId": session, "version": lease["version"]}}
    path = f"/api/v1/ontologyChangeSets/{cs.id}"
    (await probe.http.post(f"{path}/submit", json=token)).raise_for_status()
    r = await probe.http.post(f"{path}/apply", json=token)
    assert (r.status_code, r.json()["message"]) == (
        400,
        "Definition is deleted, outside the draft's ontology, or has changed since drafting",
    )


async def test_deleted_concept_name_stays_reserved(probe: Probe) -> None:
    """DELETE_CONCEPT then UPSERT_CONCEPT of the same name: APPLY_FAILED (R4)."""
    concept = {
        "id": str(uuid.uuid4()),
        "name": "V",
        "fullyQualifiedName": f"{probe.slug}.V",
        "iri": f"{probe.base}V",
        "conceptType": "OWL_CLASS",
        "ontology": {"id": probe.ontology_id, "type": "ontology"},
    }
    create = [{"operationType": "UPSERT_CONCEPT", "concept": concept}]
    assert (await _native(probe, "v1", create))["state"] == "APPLIED"
    v = (await _members(probe, "ontologyConcepts"))["V"]
    delete = [{"operationType": "DELETE_CONCEPT", "targetId": v["id"], "baseVersion": v["version"]}]
    assert (await _native(probe, "v2", delete))["state"] == "APPLIED"
    again = [{"operationType": "UPSERT_CONCEPT", "concept": {**concept, "id": str(uuid.uuid4())}}]
    failed = await _native(probe, "v3", again)
    assert failed["state"] == "APPLY_FAILED"
    assert failed["applicationResult"]["results"][0]["message"] == (
        f"Definition already exists: {probe.slug}.V"
    )


async def test_upsert_axiom_replaces_existing_only_with_target(probe: Probe) -> None:
    before = (await _members(probe, "ontologyAxioms"))["CustomerPrimaryKey"]
    assert [e["restrictionKind"] for e in before["expressions"]] == ["EXACT"]
    changed = AddAxiom(
        op="add_axiom",
        concept="Customer",
        axiom={
            "name": "CustomerPrimaryKey",
            "displayName": "Customer primary key",
            "description": "Primary key v2",
            "axiomType": "SUBCLASS_OF",
            "expressions": [
                {
                    "kind": "RESTRICTION",
                    "restrictionKind": "MIN",
                    "propertyName": "customerId",
                    "cardinality": 1,
                }
            ],
        },
    )
    upsert = to_om_all([changed], await _live_ctx(probe)).native
    assert (upsert[0]["targetId"], upsert[0]["baseVersion"]) == (before["id"], before["version"])
    assert upsert[0]["axiom"]["id"] == before["id"]
    # raw create of the existing name (no target): APPLY_FAILED
    failed = await _native(probe, "a2", [_untargeted(upsert[0], "axiom")])
    assert failed["state"] == "APPLY_FAILED"
    assert failed["applicationResult"]["results"][0]["message"] == (
        f"Definition already exists: {probe.slug}.CustomerPrimaryKey"
    )
    # to_om's targeted upsert: applied, expressions replaced
    assert (await _native(probe, "a3", upsert))["state"] == "APPLIED"
    after = (await _members(probe, "ontologyAxioms"))["CustomerPrimaryKey"]
    assert [e["restrictionKind"] for e in after["expressions"]] == ["MIN"]
    assert after["version"] > before["version"]


async def test_glossary_sparql_select_and_formats(probe: Probe) -> None:
    url = f"/api/v1/glossaries/{probe.glossary_id}/sparql"

    async def sparql(query: str, fmt: str, **extra: Any) -> httpx.Response:
        return await probe.http.post(url, json={"query": query, "format": fmt, **extra})

    select = f"SELECT ?s WHERE {{ ?s a <{SKOS_CONCEPT}> }}"
    r = await sparql(select, "json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/sparql-results+json")
    found = {b["s"]["value"] for b in r.json()["results"]["bindings"]}
    # the dataset is this glossary only (retail's concepts are not visible)
    assert found == {f"{probe.base}{n}" for n in ("Party", "Customer", "Order")}

    # native members are not in the glossary dataset
    r = await sparql(f"SELECT ?s WHERE {{ ?s a <{OWL.DatatypeProperty}> }}", "json")
    subjects = {b["s"]["value"] for b in r.json()["results"]["bindings"]}
    assert subjects == {str(OM.fullyQualifiedName)}
    r = await sparql(
        f'ASK {{ ?s a <{OWL.ObjectProperty}> . FILTER(STRSTARTS(STR(?s), "{probe.base}")) }}',
        "json",
    )
    assert r.json()["boolean"] is False
    r = await sparql("SELECT ?g WHERE { GRAPH ?g { ?s ?p ?o } }", "json")
    assert r.json()["results"]["bindings"] == []  # no named graphs

    # result formats and their content types
    for fmt, mime in (
        ("xml", "application/sparql-results+xml"),
        ("csv", "text/csv"),
        ("tsv", "text/tab-separated-values"),
    ):
        r = await sparql(select, fmt)
        assert (r.status_code, r.headers["content-type"].split(";")[0]) == (200, mime), fmt
    construct = (
        f"CONSTRUCT {{ ?s ?p ?o }} WHERE {{ ?s ?p ?o . FILTER(?s = <{probe.base}Customer>) }}"
    )
    r = await sparql(construct, "turtle")
    assert (r.status_code, r.headers["content-type"].split(";")[0]) == (200, "text/turtle")
    g = Graph().parse(data=r.text, format="turtle")
    assert (URIRef(f"{probe.base}Customer"), SKOS.prefLabel, Literal("Client")) in g
    for fmt, mime in (
        ("jsonld", "application/ld+json"),
        ("ntriples", "application/n-triples"),
        ("rdfxml", "application/rdf+xml"),
    ):
        r = await sparql(construct, fmt)
        assert (r.status_code, r.headers["content-type"].split(";")[0]) == (200, mime), fmt
    r = await sparql(f"DESCRIBE <{probe.base}Party>", "turtle")
    assert (r.status_code, r.headers["content-type"].split(";")[0]) == (200, "text/turtle")

    # a format that does not fit the query form is OM's own 400
    r = await sparql(construct, "json")
    assert (r.status_code, r.json()["message"]) == (
        400,
        "Format 'json' is not valid for a CONSTRUCT query",
    )
    r = await sparql(select, "turtle")
    assert (r.status_code, r.json()["message"]) == (
        400,
        "Format 'turtle' is not valid for a SELECT query",
    )

    # updates are rejected by OM's query parser (Lakeloom still refuses them first)
    for update in (
        f"INSERT DATA {{ <{probe.base}X> a <{SKOS_CONCEPT}> }}",
        "DELETE WHERE { ?s ?p ?o }",
    ):
        r = await sparql(update, "json")
        assert r.status_code == 400, update
        assert r.json()["message"].startswith("Invalid SPARQL query: Encountered"), update
    r = await sparql(select, "json")
    assert len(r.json()["results"]["bindings"]) == 3  # nothing was inserted

    # timeout is in milliseconds, 1..30000; an expired query is a 500 with no message
    for bad in (0, 30001):
        assert (await sparql(select, "json", timeout=bad)).status_code == 400, bad
    heavy = "SELECT (COUNT(*) AS ?n) WHERE { ?a ?b ?c . ?d ?e ?f . ?g ?h ?i . ?j ?k ?l }"
    started = time.monotonic()
    r = await sparql(heavy, "json", timeout=200)
    assert time.monotonic() - started < 5  # 200 ms, not 200 s
    assert (r.status_code, r.json()["errorType"]) == (500, "UNHANDLED_SERVER_EXCEPTION")
    # inference is a lower-case enum
    assert (await sparql(select, "json", inference="NONE")).status_code == 400
    assert (await sparql(select, "json", inference="none")).status_code == 200
    # an unknown glossary id is a 404
    r = await probe.http.post(
        f"/api/v1/glossaries/{uuid.uuid4()}/sparql", json={"query": select, "format": "json"}
    )
    assert r.status_code == 404


async def test_imports_preview_shape(probe: Probe) -> None:
    """Research only: OM's native import preview yields native operations (no terms,
    relations or mappings) and persists nothing; Lakeloom does not use it."""
    before = (await probe.client.get_ontology_state(probe.ontology_id)).working_revision
    turtle = (
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        f"<{probe.base}Alpha> a owl:Class .\n"
        f"<{probe.base}Beta> a owl:Class .\n"
    )
    r = await probe.http.post(
        f"/api/v1/ontologies/{probe.ontology_id}/imports/preview",
        json={"data": turtle, "format": "turtle"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "DRAFT"
    assert body["glossaries"] == []
    assert body["ontology"] == probe.slug
    assert body["baseWorkingRevision"] == before
    ops = body["operations"]
    assert [o["operationType"] for o in ops] == ["UPSERT_CONCEPT", "UPSERT_CONCEPT"]
    concepts = sorted((o["concept"] for o in ops), key=lambda c: str(c["iri"]))
    assert [c["iri"] for c in concepts] == [f"{probe.base}Alpha", f"{probe.base}Beta"]
    assert all(c["conceptType"] == "OWL_CLASS" for c in concepts)
    # names are minted from the local name plus a hash, not the local name itself
    assert concepts[0]["name"].startswith("imported-Alpha-")
    assert concepts[0]["name"] != "Alpha"
    # nothing was written
    persisted = await probe.http.get(f"/api/v1/ontologyChangeSets/name/{body['name']}")
    assert persisted.status_code == 404
    state = await probe.client.get_ontology_state(probe.ontology_id)
    assert state.working_revision == before
    iris = {c["iri"] for c in (await _members(probe, "ontologyConcepts")).values()}
    assert f"{probe.base}Alpha" not in iris
