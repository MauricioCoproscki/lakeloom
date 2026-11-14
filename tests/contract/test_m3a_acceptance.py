"""M3a acceptance against the live stack: published SHACL shapes validate the fixture lake's
data, the JSON-LD context maps a record to the model's IRIs, ``validate_model`` reports on a
fresh model and the publication commit carries ``shapes.ttl`` and ``context.jsonld``
(FR-PUB-04, FR-PUB-05, FR-PUB-07, FR-GIT-02).

Everything goes through real /mcp and /v1 with Keycloak tokens. The module builds one
throwaway model ``retail_m3a``: the agent scans ``fixture_delta``, creates the model,
proposes from the scan and submits; alice approves (the bot never does). The approval is
published to the dev Gitea repository in ``direct`` mode.

Instance data is built from the fixture Delta tables (tests/fixtures/lake): one node per
row, typed with the table's concept class (the shape whose ``sh:name`` is the mapper's
concept name); each column maps to the property of that class's shape whose ``sh:name``
(the property's ``rdfs:label``) equals the mapper's attribute name of the column. Literals
are typed from the property's ``rdfs:range`` in the published model.

Teardown (runs on failure too): open proposals discarded, change sets, the ontology and the
glossary of ``retail_m3a`` hard-deleted (checked gone), the seeded ``retail`` model checked
untouched. No release is sealed. The commit of ``ontologies/retail_m3a/`` stays in the dev
repository's history (dev only). Tokens are never printed or put in assertion messages.
"""

import asyncio
import json
import os
import re
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pyshacl
import pytest
import pytest_asyncio
from deltalake import DeltaTable
from mcp.types import CallToolResult, TextContent
from rdflib import RDF, RDFS, XSD, Graph, Literal, Namespace, URIRef
from rdflib.term import Node
from stack import mcp_session

from lakeloom.core.mapper import _attribute_name, _concept_name
from lakeloom.core.model_policy import MappingPolicy

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="module")]

MODEL = "retail_m3a"
GITEA = "http://localhost:3000"
GITEA_API = f"{GITEA}/api/v1"
GITEA_REPO = "lakeloom-admin/ontologies"
TABLES = ("customers", "products", "orders", "invoices")
LAKE = Path(__file__).resolve().parents[1] / "fixtures" / "lake"
OM_TABLE = "lakeloom_fixture.retail.sales.{}"
CACHE_WAIT_SECONDS = 90  # > the dev stack's model_cache_ttl_seconds (60)
SH = Namespace("http://www.w3.org/ns/shacl#")
ROW = "urn:lakeloom:m3a:{}/{}"
POLICY = MappingPolicy(model=MODEL)
NUMERIC = {str(XSD[t]) for t in ("decimal", "double", "float", "integer", "long", "int")}
_COUNTS = re.compile(r"^- Errors: (\d+), warnings: (\d+), info: (\d+)$", re.MULTILINE)


class Token(str):
    """A bearer token whose repr is redacted: pytest prints frame arguments on failure."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<redacted token>"


@dataclass(frozen=True)
class Tokens:
    agent: str = field(repr=False)
    admin: str = field(repr=False)
    alice: str = field(repr=False)
    bot: str = field(repr=False)


@dataclass
class Run:
    url: str
    om: httpx.AsyncClient
    tokens: Tokens = field(repr=False)
    proposals: list[str] = field(default_factory=list[str])
    change_sets: set[str] = field(default_factory=set[str])
    publication: dict[str, Any] | None = None
    clone: Path | None = None


# Only wraps, takes no other step: nothing in this frame (raw token arguments) can fail.
@pytest.fixture(scope="module")
def tokens(agent_token: str, admin_agent_token: str, human_token: str, om_bot_token: str) -> Tokens:
    return Tokens(
        Token(agent_token), Token(admin_agent_token), Token(human_token), Token(om_bot_token)
    )


async def call(url: str, token: str, tool: str, args: dict[str, Any]) -> CallToolResult:
    async with mcp_session(url, token) as session:
        return await session.call_tool(tool, args)


def data(result: CallToolResult) -> dict[str, Any]:
    assert result.structured_content is not None, result.content
    return dict(result.structured_content)


def ok(result: CallToolResult) -> dict[str, Any]:
    assert result.is_error is not True, json.dumps(result.structured_content)[:3000]
    return data(result)


def text_of(result: CallToolResult) -> str:
    return "".join(c.text for c in result.content if isinstance(c, TextContent))


def preview_token(preview: dict[str, Any]) -> dict[str, str]:
    return {"confirm_token": str(preview["confirm_token"])}


def _change_sets(proposal: dict[str, Any]) -> set[str]:
    return {
        cs for part in proposal["parts"] for cs in part["change_set_ids"].values() if cs is not None
    }


def _bearer(token: str) -> httpx.Headers:
    return httpx.Headers({"Authorization": f"Bearer {token}"})


def _git(*args: str, cwd: Path) -> str:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(cwd),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    proc = subprocess.run(  # noqa: S603 - fixed git argv
        ["git", "-c", "credential.helper=", *args],  # noqa: S607
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _clone(dest: Path) -> Path:
    """A fresh clone of the dev repository (public: no credentials needed to read)."""
    if dest.exists():
        _git("fetch", "-q", "origin", cwd=dest)
        _git("reset", "-q", "--hard", "origin/main", cwd=dest)
        return dest
    _git("clone", "-q", f"{GITEA}/{GITEA_REPO}.git", str(dest), cwd=dest.parent)
    return dest


async def _retail(om: httpx.AsyncClient) -> list[tuple[Any, Any]]:
    out: list[tuple[Any, Any]] = []
    for kind in ("glossaries", "ontologies"):
        body = (await om.get(f"/api/v1/{kind}/name/retail")).raise_for_status().json()
        out.append((body["version"], body["updatedAt"]))
    return out


async def _exists(om: httpx.AsyncClient, slug: str) -> bool:
    for kind in ("glossaries", "ontologies"):
        r = await om.get(f"/api/v1/{kind}/name/{slug}", params={"include": "all"})
        if r.status_code != 404:
            return True
    return False


async def _delete_model(om: httpx.AsyncClient, slug: str) -> None:
    for kind in ("ontologies", "glossaries"):
        await om.delete(
            f"/api/v1/{kind}/name/{slug}", params={"hardDelete": "true", "recursive": "true"}
        )


async def _table_tags(om: httpx.AsyncClient, table: str) -> set[str]:
    r = await om.get(
        f"/api/v1/tables/name/{OM_TABLE.format(table)}", params={"fields": "tags,columns"}
    )
    body = r.raise_for_status().json()
    tags = {t["tagFQN"] for t in body.get("tags") or []}
    for column in body.get("columns") or []:
        tags |= {t["tagFQN"] for t in column.get("tags") or []}
    return tags


async def _teardown(run: Run) -> None:
    agent = run.tokens.agent
    for pid in run.proposals:
        preview = await call(run.url, agent, "discard_proposal", {"proposal_id": pid})
        if preview.is_error:
            continue  # applied or already discarded
        args = {"proposal_id": pid, "confirm_token": data(preview)["confirm_token"]}
        await call(run.url, agent, "discard_proposal", args)
    for pid in run.proposals:
        shown = await call(run.url, agent, "get_proposal", {"proposal_id": pid})
        if not shown.is_error:
            proposal = data(shown)["proposal"]
            run.change_sets |= _change_sets(proposal)
            for part in proposal["parts"]:
                run.change_sets |= {
                    s for s in part.get("superseded_change_set_ids", []) if isinstance(s, str)
                }
    om = run.om
    for cs_id in run.change_sets:
        await om.delete(f"/api/v1/ontologyChangeSets/{cs_id}", params={"hardDelete": "true"})
    await _delete_model(om, MODEL)
    assert not await _exists(om, MODEL), MODEL
    for cs_id in run.change_sets:
        r = await om.get(f"/api/v1/ontologyChangeSets/{cs_id}", params={"include": "all"})
        assert r.status_code == 404, cs_id
    for table in TABLES:
        left = {t for t in await _table_tags(om, table) if t.startswith(f"{MODEL}.")}
        assert not left, (table, left)


async def _wait_for_lakeloom_caches(run: Run) -> None:
    """Wait until Lakeloom's caches (TTL 60 s in dev) no longer name the deleted model."""
    deadline = asyncio.get_running_loop().time() + CACHE_WAIT_SECONDS
    async with httpx.AsyncClient(
        base_url=run.url, headers=_bearer(run.tokens.agent), timeout=30
    ) as api:
        while True:
            r = await api.get("/v1/models")
            if r.status_code == 200 and f'"{MODEL}' not in r.text:
                break
            assert asyncio.get_running_loop().time() < deadline, r.status_code
            await asyncio.sleep(2)


async def _approve_new_model(run: Run) -> dict[str, Any]:
    """agent scan → create_model → propose_from_scan → submit → alice approves."""
    t = run.tokens
    scanned = ok(await call(run.url, t.agent, "scan_lake_source", {"source": "fixture_delta"}))
    assert scanned["status"] == "completed"
    args = {"name": MODEL, "description": "M3a acceptance throwaway model"}
    preview = ok(await call(run.url, t.admin, "create_model", args))
    created = ok(await call(run.url, t.admin, "create_model", {**args, **preview_token(preview)}))
    assert created["created"]

    proposed = ok(
        await call(
            run.url, t.agent, "propose_from_scan", {"scan_id": scanned["scan_id"], "model": MODEL}
        )
    )
    pid = str(proposed["proposal_id"])
    run.proposals.append(pid)
    run.change_sets |= _change_sets(proposed["proposal"])
    submitted = ok(await call(run.url, t.agent, "submit_proposal", {"proposal_id": pid}))
    assert submitted["status"] == "submitted"

    review = {"proposal_id": pid, "decision": "approve"}
    preview = ok(await call(run.url, t.alice, "review_proposal", review))
    assert preview["decided"] is False and preview["confirm_token"]
    result = await call(run.url, t.alice, "review_proposal", {**review, **preview_token(preview)})
    out = ok(result)
    assert out["decided"] is True and out["proposal"]["status"] == "applied"
    run.change_sets |= _change_sets(out["proposal"])
    assert out["publication"] is not None, "Git publishing is off in the stack"
    return dict(out["publication"])


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def run(
    lakeloom_url: str, om_url: str, tokens: Tokens, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[Run]:
    try:
        async with httpx.AsyncClient(timeout=5) as gitea:
            (await gitea.get(f"{GITEA_API}/version")).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"Gitea not reachable at {GITEA}: {exc!r}")
    # the bot: read-only checks and the teardown only
    async with httpx.AsyncClient(base_url=om_url, headers=_bearer(tokens.bot), timeout=60) as om:
        retail = await _retail(om)
        state = Run(url=lakeloom_url, om=om, tokens=tokens)
        state.clone = tmp_path_factory.mktemp("m3a") / "ontologies"
        # A crashed earlier run may have left the fixed-name throwaway model behind.
        if await _exists(om, MODEL):
            await _delete_model(om, MODEL)
            await _wait_for_lakeloom_caches(state)
        try:
            state.publication = await _approve_new_model(state)
            yield state
        finally:
            try:
                await _teardown(state)
            finally:
                try:
                    await _wait_for_lakeloom_caches(state)
                finally:
                    assert await _retail(om) == retail, "the seeded retail model changed"


def _get(run: Run, path: str, accept: str) -> httpx.Response:
    r = httpx.get(
        f"{run.url}/v1/models/{MODEL}{path}",
        headers={**_bearer(run.tokens.agent), "Accept": accept},
        timeout=60,
    )
    assert r.status_code == 200, (path, r.status_code, r.text[:500])
    return r


def _model_graph(run: Run) -> Graph:
    return Graph().parse(data=_get(run, "", "text/turtle").text, format="turtle")


def _shapes(run: Run) -> tuple[str, Graph]:
    r = _get(run, "/shapes", "text/turtle")
    assert r.headers["content-type"].startswith("text/turtle")
    return r.text, Graph().parse(data=r.text, format="turtle")


def _rows(table: str) -> list[dict[str, Any]]:
    return DeltaTable(str(LAKE / table)).to_pyarrow_table().to_pylist()


def _lexical(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


@dataclass(frozen=True)
class TableMap:
    """A fixture table's concept class and, per column, its property and literal datatype."""

    cls: URIRef
    columns: dict[str, tuple[URIRef, URIRef | None]]


def _table_map(table: str, shapes: Graph, model: Graph) -> TableMap:
    concept = _concept_name(table, POLICY)
    [node] = [
        n
        for n in shapes.subjects(RDF.type, SH.NodeShape)
        if str(shapes.value(n, SH.name)) == concept
    ]
    cls = shapes.value(node, SH.targetClass)
    assert isinstance(cls, URIRef), (table, node)
    by_label: dict[str, URIRef] = {}
    for prop_shape in shapes.objects(node, SH.property):
        path = shapes.value(prop_shape, SH.path)
        if isinstance(path, URIRef):
            for label in model.objects(path, RDFS.label):
                by_label[str(label)] = path
    columns: dict[str, tuple[URIRef, URIRef | None]] = {}
    for column in DeltaTable(str(LAKE / table)).schema().fields:
        prop = by_label.get(_attribute_name(column.name, POLICY))
        if prop is None:
            continue  # a column the model maps to a relation, or not at all
        ranges = list(model.objects(prop, RDFS.range))
        xsd = [r for r in ranges if isinstance(r, URIRef) and str(r).startswith(str(XSD))]
        # rdfs:Literal or several ranges: a plain literal
        columns[column.name] = (prop, xsd[0] if len(xsd) == 1 else None)
    return TableMap(cls, columns)


def _instances(tables: dict[str, TableMap]) -> Graph:
    g = Graph()
    for table, tm in tables.items():
        for i, row in enumerate(_rows(table)):
            node = URIRef(ROW.format(table, i))
            g.add((node, RDF.type, tm.cls))
            for column, (prop, datatype) in tm.columns.items():
                value = row[column]
                if value is not None:
                    g.add((node, prop, Literal(_lexical(value), datatype=datatype)))
    return g


def _violations(report: Graph) -> set[tuple[Node | None, Node | None, Node | None]]:
    return {
        (
            report.value(r, SH.focusNode),
            report.value(r, SH.resultPath),
            report.value(r, SH.sourceConstraintComponent),
        )
        for r in report.subjects(RDF.type, SH.ValidationResult)
    }


def _report_text(report: Graph) -> str:
    return "; ".join(
        f"{report.value(r, SH.focusNode)} {report.value(r, SH.resultPath)} "
        f"{report.value(r, SH.resultMessage)}"
        for r in list(report.subjects(RDF.type, SH.ValidationResult))[:10]
    )


def _validate(data_graph: Graph, shapes: Graph) -> tuple[bool, Graph]:
    conforms, report, _ = pyshacl.validate(data_graph, shacl_graph=shapes, inference="none")
    assert isinstance(report, Graph)
    return bool(conforms), report


async def test_m3a_shapes_validate_fixture_data(run: Run) -> None:
    _, shapes = _shapes(run)
    model = _model_graph(run)
    tables = {t: _table_map(t, shapes, model) for t in TABLES}
    for table, tm in tables.items():
        assert tm.columns, (table, "no column maps to a property of its concept")
    instances = _instances(tables)
    assert len(instances) > sum(len(_rows(t)) for t in TABLES)

    conforms, report = _validate(instances, shapes)
    assert conforms, _report_text(report)

    # Break one orders row: a non-numeric total amount (sh:datatype), or, when the amount is
    # not numerically typed, a missing order identifier (sh:minCount 1).
    orders = tables["orders"]
    broken = URIRef(ROW.format("orders", 7))
    prop, datatype = orders.columns["total_amount"]
    if datatype is not None and str(datatype) in NUMERIC:
        instances.remove((broken, prop, None))
        instances.add((broken, prop, Literal("not-a-number", datatype=datatype)))
        component = SH.DatatypeConstraintComponent
    else:
        prop = orders.columns["order_id"][0]
        min_one = {shapes.value(s, SH.path) for s in shapes.subjects(SH.minCount, Literal(1))}
        assert prop in min_one, "order_id has no sh:minCount 1 and the amount is not numeric"
        instances.remove((broken, prop, None))
        component = SH.MinCountConstraintComponent
    conforms, report = _validate(instances, shapes)
    assert not conforms
    assert _violations(report) == {(broken, prop, component)}, _report_text(report)


async def test_m3a_context_maps_record_to_iris(run: Run) -> None:
    r = _get(run, "/context.jsonld", "application/ld+json")
    assert r.headers["content-type"].startswith("application/ld+json")
    context = r.json()["@context"]
    _, shapes = _shapes(run)
    customers = _table_map("customers", shapes, _model_graph(run))
    terms = {
        term["@id"]: key
        for key, term in context.items()
        if isinstance(term, dict) and "@id" in term
    }
    class_terms = {v: k for k, v in context.items() if isinstance(v, str)}
    row = _rows("customers")[0]
    record: dict[str, Any] = {
        "@context": context,
        "@id": ROW.format("customers", 0),
        "@type": class_terms[str(customers.cls)],
    }
    expected: set[tuple[URIRef, Literal]] = set()
    for column, (prop, datatype) in customers.columns.items():
        if row[column] is None:
            continue
        assert str(prop) in terms, (column, prop)
        record[terms[str(prop)]] = _lexical(row[column])
        expected.add((prop, Literal(_lexical(row[column]), datatype=datatype)))
    assert expected

    g = Graph().parse(data=json.dumps(record), format="json-ld")
    subject = URIRef(ROW.format("customers", 0))
    assert (subject, RDF.type, customers.cls) in g
    got = {(p, o) for p, o in g.predicate_objects(subject) if p != RDF.type}
    assert got == expected


async def test_m3a_validate_model(run: Run) -> None:
    result = await call(run.url, run.tokens.agent, "validate_model", {"model": MODEL})
    report = ok(result)
    assert report["model"] == MODEL
    errors = [i for i in report["issues"] if i["severity"] == "error"]
    assert report["conforms"] is True and not errors, errors
    match = _COUNTS.search(text_of(result))
    assert match is not None, text_of(result)[:500]
    counts = report["counts"]
    assert [int(n) for n in match.groups()] == [
        counts.get("error", 0),
        counts.get("warning", 0),
        counts.get("info", 0),
    ]
    assert "- Conforms: yes" in text_of(result)


async def test_m3a_git_commit_has_shapes_and_context(run: Run) -> None:
    pub = run.publication
    assert pub is not None and pub["status"] == "done", pub
    assert pub["branch"] == "main" and pub["commit"], pub
    assert run.clone is not None
    clone = _clone(run.clone)
    sha = pub["commit"]
    base = f"ontologies/{MODEL}"
    # The tree of the commit, not only its diff: an earlier run's identical files stay in
    # the dev repository's history, so an unchanged file is not in the diff.
    tree = set(_git("ls-tree", "-r", "--name-only", sha, cwd=clone).split())
    assert {f"{base}/shapes.ttl", f"{base}/context.jsonld"} <= tree, sorted(tree)

    shapes_text, _ = _shapes(run)
    assert _git("show", f"{sha}:{base}/shapes.ttl", cwd=clone) == shapes_text
    committed = json.loads(_git("show", f"{sha}:{base}/context.jsonld", cwd=clone))
    assert committed == _get(run, "/context.jsonld", "application/ld+json").json()
