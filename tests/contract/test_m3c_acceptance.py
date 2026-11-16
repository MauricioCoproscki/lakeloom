"""M3c acceptance against the live stack: a release restored from Git into a fresh model
round-trips without a semantic diff, a re-import of the same file is refused as already
matching, drift after a column was added to a lake table is proposed as one attribute, and the
model's glossary answers read-only SPARQL while updates are refused (FR-IO-02, FR-IO-05,
FR-GIT-07, FR-PRO-07, FR-PUB-03).

Everything goes through real /mcp and /v1 with Keycloak tokens; Git mode is ``direct``. The
module builds the throwaway model ``retail_m3c`` as in M3b: the agent scans ``fixture_delta``,
creates the model, proposes from the scan and submits; alice approves (the bot never does);
the publication to the dev Gitea repository is ``done``; ``create_release`` ``1.0.0`` runs as
the agent client with its optional scope ``lakeloom:publish``. The tests run in file order:

1. the OpenMetadata glossary and ontology ``retail_m3c`` are hard-deleted and created again
   (a fresh OpenMetadata for the model: new glossary id, no terms); ``import_from_git`` of
   release ``1.0.0`` plans exactly the terms of the tagged ``ontology.ttl``; alice approves
   it, and the export has no semantic diff from the tagged file;
2. ``import_model`` of the same Turtle is "already matches"; a JSON-LD with a remote
   ``@context`` is refused;
3. ``retail_m3c_drift`` is built from ``fixture_scratch`` (a copy of the fixture ``orders``
   table, minus its foreign key to ``customers``, in ``.scratch-lake/orders_drift``, mounted
   read-only into Lakeloom); a column
   ``channel`` is appended on the host with delta-rs, and ``detect_drift`` proposes it;
4. SPARQL over ``retail_m3c`` through HTTP and MCP; ``INSERT DATA`` → 403 from Lakeloom.

Teardown (runs on failure too): the test tag deleted through the Gitea API (checked gone),
open proposals discarded, change sets and the glossaries and ontologies of both models
hard-deleted (checked gone), the contents of ``.scratch-lake/`` removed, the seeded
``retail`` model checked untouched. Leftovers of a crashed earlier run are removed in the
setup. No OpenMetadata release is sealed. The commits of ``ontologies/retail_m3c*/`` stay in
the dev repository's history (dev only). Tokens are never printed or put in assertion
messages (NFR-SEC-11).
"""

import asyncio
import json
import os
import shutil
import subprocess
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pyarrow as pa
import pytest
import pytest_asyncio
import yaml
from deltalake import DeltaTable, write_deltalake
from mcp.types import CallToolResult, TextContent
from rdflib import RDF, SKOS, Graph, URIRef
from stack import REPO, TOKEN_URL, mcp_session, token_scopes

from lakeloom.core.import_terms import extract_terms
from lakeloom.core.release_diff import diff_graphs, diff_markdown
from lakeloom.rdf.semantic import semantic_view

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="module")]

MODEL = "retail_m3c"
DRIFT = "retail_m3c_drift"
MODELS = (MODEL, DRIFT)
V1 = "1.0.0"
TAG = f"{MODEL}/v{V1}"
TAGS = (TAG,)
GITEA = "http://localhost:3000"
GITEA_API = f"{GITEA}/api/v1"
GITEA_REPO = "lakeloom-admin/ontologies"
GITEA_AUTH = ("lakeloom-admin", "lakeloom-admin")  # DEV ONLY fixed public compose credential
TABLES = ("customers", "products", "orders", "invoices")
OM_TABLE = "lakeloom_fixture.retail.sales.{}"
SCRATCH = REPO / ".scratch-lake"  # mounted read-only at /scratch-lake (fixture_scratch)
DRIFT_TABLE = SCRATCH / "orders_drift"
DRIFT_FQN = "retail.drift.orders_drift"
NEW_COLUMN = "channel"
CACHE_WAIT_SECONDS = 90  # > the dev stack's model_cache_ttl_seconds (60)
PUBLICATION_WAIT_SECONDS = 120
TAG_WAIT_SECONDS = 30
SELECT_CONCEPTS = (
    "PREFIX skos: <http://www.w3.org/2004/02/skos/core#>\n"
    "SELECT ?c WHERE { ?c a skos:Concept } ORDER BY ?c"
)
INSERT = 'INSERT DATA { <urn:x:a> <urn:x:b> "c" }'


class Token(str):
    """A bearer token whose repr is redacted: pytest prints frame arguments on failure."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<redacted token>"


@dataclass(frozen=True)
class Tokens:
    agent: str = field(repr=False)
    publisher: str = field(repr=False)
    admin: str = field(repr=False)
    alice: str = field(repr=False)
    bot: str = field(repr=False)


@dataclass
class Run:
    url: str
    om: httpx.AsyncClient
    tokens: Tokens = field(repr=False)
    clone: Path
    proposals: list[str] = field(default_factory=list[str])
    change_sets: set[str] = field(default_factory=set[str])
    release: dict[str, Any] | None = None
    imported: bool = False


def _publish_token() -> str:
    """``lakeloom-agent`` with its optional scope ``lakeloom:publish`` (client credentials)."""
    r = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": "lakeloom-agent",
            "client_secret": "dev-agent-secret",  # DEV ONLY fixed public dev credential
            "scope": "lakeloom:publish",
        },
        timeout=10,
    )
    assert r.status_code == 200, r.status_code
    token = Token(str(r.json()["access_token"]))
    assert "lakeloom:publish" in token_scopes(token)
    return token


# Only wraps, takes no other step: nothing in this frame (raw token arguments) can fail.
@pytest.fixture(scope="module")
def tokens(agent_token: str, admin_agent_token: str, human_token: str, om_bot_token: str) -> Tokens:
    return Tokens(
        Token(agent_token),
        Token(_publish_token()),
        Token(admin_agent_token),
        Token(human_token),
        Token(om_bot_token),
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


def error_of(result: CallToolResult) -> dict[str, Any]:
    assert result.is_error is True, json.dumps(result.structured_content)[:3000]
    return dict(data(result)["error"])


def text_of(result: CallToolResult) -> str:
    return "".join(c.text for c in result.content if isinstance(c, TextContent))


def preview_token(preview: dict[str, Any]) -> dict[str, str]:
    return {"confirm_token": str(preview["confirm_token"])}


def _change_sets(proposal: dict[str, Any]) -> set[str]:
    return {
        cs for part in proposal["parts"] for cs in part["change_set_ids"].values() if cs is not None
    }


def _operations(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    return [op for part in proposal["parts"] for op in part["operations"]]


def _bearer(token: str) -> httpx.Headers:
    return httpx.Headers({"Authorization": f"Bearer {token}"})


def _git_env(home: Path | None = None) -> dict[str, str]:
    env = {
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if home is not None:
        env["HOME"] = str(home)
    return env


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(  # noqa: S603 - fixed git argv
        ["git", "-c", "credential.helper=", *args],  # noqa: S607
        cwd=cwd,
        env=_git_env(cwd),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _fetch(dest: Path) -> Path:
    """A clone of the dev repository with the current tags (public: no credentials)."""
    if not dest.exists():
        _git("clone", "-q", f"{GITEA}/{GITEA_REPO}.git", str(dest), cwd=dest.parent)
    _git("fetch", "-q", "--prune", "--prune-tags", "--force", "origin", cwd=dest)
    return dest


def _gitea(method: str, path: str) -> httpx.Response:
    return httpx.request(
        method, f"{GITEA_API}/repos/{GITEA_REPO}{path}", auth=GITEA_AUTH, timeout=30
    )


def _tag_path(tag: str) -> str:
    return f"/tags/{quote(tag, safe='')}"


def _remote_tags() -> dict[str, str]:
    """The test tags on the remote, by name: their commit (peeled). Git is the ground truth:
    Gitea's single-tag API can show the old commit of a deleted and re-created tag."""
    url = f"{GITEA}/{GITEA_REPO}.git"
    proc = subprocess.run(  # noqa: S603 - fixed git argv
        ["git", "-c", "credential.helper=", "ls-remote", "--tags", url, f"refs/tags/{MODEL}/*"],  # noqa: S607
        env=_git_env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    refs: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        sha, ref = line.split("\t", 1)
        refs[ref] = sha
    return {
        ref.removeprefix("refs/tags/"): refs.get(f"{ref}^{{}}", sha)
        for ref, sha in refs.items()
        if not ref.endswith("^{}")
    }


def _delete_tags() -> None:
    """Delete the test tags through the Gitea API until Git no longer has them."""
    deadline = time.monotonic() + TAG_WAIT_SECONDS
    while left := [t for t in TAGS if t in _remote_tags()]:
        for tag in left:
            r = _gitea("DELETE", _tag_path(tag))
            assert r.status_code in (204, 404), (tag, r.status_code)
        assert time.monotonic() < deadline, left
        time.sleep(1)


def _empty_scratch() -> None:
    """Remove what ``.scratch-lake/`` holds (the bind-mounted directory itself stays)."""
    SCRATCH.mkdir(exist_ok=True)
    for child in SCRATCH.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


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


async def _glossary(om: httpx.AsyncClient, slug: str) -> dict[str, Any]:
    return dict((await om.get(f"/api/v1/glossaries/name/{slug}")).raise_for_status().json())


async def _glossary_terms(om: httpx.AsyncClient, glossary_id: str) -> list[Any]:
    r = await om.get("/api/v1/glossaryTerms", params={"glossary": glossary_id, "limit": 100})
    return list(r.raise_for_status().json()["data"])


async def _table_tags(om: httpx.AsyncClient, table: str) -> set[str]:
    r = await om.get(
        f"/api/v1/tables/name/{OM_TABLE.format(table)}", params={"fields": "tags,columns"}
    )
    body = r.raise_for_status().json()
    tags = {t["tagFQN"] for t in body.get("tags") or []}
    for column in body.get("columns") or []:
        tags |= {t["tagFQN"] for t in column.get("tags") or []}
    return tags


async def _discard_open(run: Run) -> None:
    agent = run.tokens.agent
    for pid in run.proposals:
        preview = await call(run.url, agent, "discard_proposal", {"proposal_id": pid})
        if preview.is_error:
            continue  # applied or already discarded
        args = {"proposal_id": pid, "confirm_token": data(preview)["confirm_token"]}
        await call(run.url, agent, "discard_proposal", args)


async def _collect_change_sets(run: Run) -> None:
    for pid in run.proposals:
        shown = await call(run.url, run.tokens.agent, "get_proposal", {"proposal_id": pid})
        if not shown.is_error:
            proposal = data(shown)["proposal"]
            run.change_sets |= _change_sets(proposal)
            for part in proposal["parts"]:
                run.change_sets |= {
                    s for s in part.get("superseded_change_set_ids", []) if isinstance(s, str)
                }


async def _delete_change_sets(run: Run) -> None:
    for cs_id in run.change_sets:
        await run.om.delete(f"/api/v1/ontologyChangeSets/{cs_id}", params={"hardDelete": "true"})


async def _check_gone(run: Run) -> None:
    om = run.om
    for slug in MODELS:
        assert not await _exists(om, slug), slug
    for cs_id in run.change_sets:
        r = await om.get(f"/api/v1/ontologyChangeSets/{cs_id}", params={"include": "all"})
        assert r.status_code == 404, cs_id
    for table in TABLES:
        left = {t for t in await _table_tags(om, table) if t.startswith(f"{MODEL}.")}
        assert not left, (table, left)


async def _teardown(run: Run) -> None:
    """Each phase runs even when an earlier one fails: tags, models and scratch always go."""
    try:
        _delete_tags()
    finally:
        try:
            await _discard_open(run)
        finally:
            try:
                await _collect_change_sets(run)
            finally:
                try:
                    await _delete_change_sets(run)
                finally:
                    try:
                        for slug in MODELS:
                            await _delete_model(run.om, slug)
                    finally:
                        _empty_scratch()
    await _check_gone(run)
    assert not any(SCRATCH.iterdir()), sorted(p.name for p in SCRATCH.iterdir())


async def _wait_for_lakeloom_caches(run: Run, slugs: tuple[str, ...] = MODELS) -> None:
    """Wait until Lakeloom's caches (TTL 60 s in dev) no longer name the deleted models."""
    deadline = asyncio.get_running_loop().time() + CACHE_WAIT_SECONDS
    async with httpx.AsyncClient(
        base_url=run.url, headers=_bearer(run.tokens.agent), timeout=30
    ) as api:
        while True:
            r = await api.get("/v1/models")
            if r.status_code == 200 and not any(f'"{slug}"' in r.text for slug in slugs):
                break
            assert asyncio.get_running_loop().time() < deadline, r.status_code
            await asyncio.sleep(2)


async def _wait_published(run: Run, pid: str, publication: dict[str, Any] | None) -> str:
    """Until the proposal's Git publication is ``done``; returns its commit."""
    assert publication is not None, "Git publishing is off in the stack"
    deadline = asyncio.get_running_loop().time() + PUBLICATION_WAIT_SECONDS
    while publication is not None and publication["status"] != "done":
        assert publication["status"] == "pending", publication
        assert asyncio.get_running_loop().time() < deadline, publication
        await asyncio.sleep(2)
        shown = ok(await call(run.url, run.tokens.agent, "get_proposal", {"proposal_id": pid}))
        publication = shown.get("publication")
    assert publication is not None and publication["branch"] == "main", publication
    assert publication["commit"], publication
    return str(publication["commit"])


async def _submit_and_approve(run: Run, pid: str) -> tuple[dict[str, Any], str]:
    """The agent submits, alice approves, the Git publication is done: (proposal, commit)."""
    t = run.tokens
    submitted = ok(await call(run.url, t.agent, "submit_proposal", {"proposal_id": pid}))
    assert submitted["status"] == "submitted"
    review = {"proposal_id": pid, "decision": "approve"}
    preview = ok(await call(run.url, t.alice, "review_proposal", review))
    assert preview["decided"] is False and preview["confirm_token"]
    out = ok(await call(run.url, t.alice, "review_proposal", {**review, **preview_token(preview)}))
    assert out["decided"] is True and out["proposal"]["status"] == "applied", out["proposal"]
    run.change_sets |= _change_sets(out["proposal"])
    commit = await _wait_published(run, pid, out["publication"])
    return dict(out["proposal"]), commit


async def _create_model(run: Run, slug: str, description: str) -> None:
    args = {"name": slug, "description": description}
    preview = await call(run.url, run.tokens.admin, "create_model", args)
    assert preview.is_error is not True, (slug, json.dumps(preview.structured_content)[:3000])
    created = await call(
        run.url, run.tokens.admin, "create_model", {**args, **preview_token(data(preview))}
    )
    assert created.is_error is not True, (slug, json.dumps(created.structured_content)[:3000])
    assert data(created)["created"]


async def _model_from_scan(run: Run, source: str, slug: str) -> dict[str, Any]:
    """agent scan → create_model → propose_from_scan → submit → alice approves."""
    t = run.tokens
    scanned = ok(await call(run.url, t.agent, "scan_lake_source", {"source": source}))
    assert scanned["status"] == "completed", scanned
    await _create_model(run, slug, "M3c acceptance throwaway model")
    proposed = ok(
        await call(
            run.url, t.agent, "propose_from_scan", {"scan_id": scanned["scan_id"], "model": slug}
        )
    )
    pid = str(proposed["proposal_id"])
    run.proposals.append(pid)
    run.change_sets |= _change_sets(proposed["proposal"])
    proposal, _ = await _submit_and_approve(run, pid)
    return proposal


async def _release_v1(run: Run) -> None:
    """``create_release`` 1.0.0 of ``retail_m3c``: dry run, then confirm."""
    publisher = run.tokens.publisher
    args = {"model": MODEL}
    plan = ok(await call(run.url, publisher, "create_release", args))["preview"]
    assert plan["version"] == V1 and plan["tag"] == TAG, plan
    confirmed = ok(
        await call(run.url, publisher, "create_release", {**args, **preview_token(plan)})
    )
    assert confirmed["released"] is True
    run.release = dict(confirmed["result"]["release"])
    assert _remote_tags().get(TAG) == run.release["commit_sha"]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def run(
    lakeloom_url: str, om_url: str, tokens: Tokens, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[Run]:
    try:
        async with httpx.AsyncClient(timeout=5) as gitea:
            (await gitea.get(f"{GITEA_API}/version")).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"Gitea not reachable at {GITEA}: {exc!r}")
    if not SCRATCH.is_dir():
        pytest.skip(f"{SCRATCH} missing: run `mkdir -p .scratch-lake` before `compose up`")
    # the bot: read-only checks, the "fresh OpenMetadata" deletion and the teardown only
    async with httpx.AsyncClient(base_url=om_url, headers=_bearer(tokens.bot), timeout=60) as om:
        retail = await _retail(om)
        clone = tmp_path_factory.mktemp("m3c") / "ontologies"
        state = Run(url=lakeloom_url, om=om, tokens=tokens, clone=clone)
        # A crashed earlier run may have left its tag, models or scratch table behind.
        _delete_tags()
        _empty_scratch()
        left = [slug for slug in MODELS if await _exists(om, slug)]
        for slug in left:
            await _delete_model(om, slug)
        if left:
            await _wait_for_lakeloom_caches(state)
        try:
            await _model_from_scan(state, "fixture_delta", MODEL)
            await _release_v1(state)
            yield state
        finally:
            try:
                await _teardown(state)
            finally:
                try:
                    await _wait_for_lakeloom_caches(state)
                finally:
                    assert await _retail(om) == retail, "the seeded retail model changed"


def _tagged(run: Run) -> tuple[str, str]:
    """The released ``ontology.ttl`` and the base IRI of its manifest, read from the tag."""
    clone = _fetch(run.clone)
    base = f"ontologies/{MODEL}"
    ttl = _git("show", f"{TAG}:{base}/ontology.ttl", cwd=clone)
    manifest = yaml.safe_load(_git("show", f"{TAG}:{base}/manifest.yaml", cwd=clone))
    return ttl, str(manifest["base_iri"])


def _graph(turtle: str) -> Graph:
    return Graph().parse(data=turtle, format="turtle")


def _concepts(turtle: str, base_iri: str) -> set[str]:
    g = _graph(turtle)
    return {
        str(s)
        for s in g.subjects(RDF.type, SKOS.Concept)
        if isinstance(s, URIRef) and str(s).startswith(base_iri)
    }


async def _export(run: Run) -> str:
    out = await call(run.url, run.tokens.agent, "export_model", {"model": MODEL})
    assert ok(out)["format"] == "turtle"
    return text_of(out)


async def test_m3c_import_of_release_round_trips(run: Run) -> None:
    assert run.release is not None, "the 1.0.0 release failed"
    t = run.tokens
    om = run.om
    ttl, base_iri = _tagged(run)
    terms = extract_terms(_graph(ttl), base_iri)
    expected = {
        "add_concept": len(terms.concepts),
        "add_attribute": len(terms.attributes),
        "add_relation": len(terms.relations),
        "add_mapping": len(terms.mappings),
        "add_axiom": len(terms.axioms),
    }
    expected = {kind: n for kind, n in expected.items() if n}
    assert expected["add_concept"] and expected["add_attribute"], expected

    # A fresh OpenMetadata for the model: the scan proposal's change sets, then the glossary
    # and the ontology hard-deleted, and the model created again with the same base IRI.
    old_glossary = (await _glossary(om, MODEL))["id"]
    await _collect_change_sets(run)
    await _delete_change_sets(run)
    await _delete_model(om, MODEL)
    assert not await _exists(om, MODEL)
    await _wait_for_lakeloom_caches(run, (MODEL,))
    await _create_model(run, MODEL, "M3c acceptance throwaway model (restored from Git)")
    glossary = await _glossary(om, MODEL)
    assert glossary["id"] != old_glossary
    assert await _glossary_terms(om, str(glossary["id"])) == []
    model = ok(await call(run.url, t.agent, "get_model", {"model": MODEL}))
    assert base_iri in json.dumps(model), (base_iri, model)

    # Restore release 1.0.0: the dry run plans exactly the terms of the tagged file.
    args = {"model": MODEL, "version": V1}
    dry = await call(run.url, t.agent, "import_from_git", args)
    plan = ok(dry)["preview"]
    assert plan["origin"] == "git" and plan["namespace"] == base_iri, plan["namespace"]
    assert plan["counts"] == expected, (plan["counts"], expected, plan["warnings"])
    assert plan["source_document"]["git_ref"] == TAG, plan["source_document"]
    assert plan["source_document"]["git_commit"] == run.release["commit_sha"]
    assert "confirm_token" in text_of(dry)

    confirmed = ok(await call(run.url, t.agent, "import_from_git", {**args, **preview_token(plan)}))
    proposal = confirmed["proposal"]
    pid = str(proposal["id"])
    run.proposals.append(pid)
    run.change_sets |= _change_sets(proposal)
    assert proposal["status"] == "draft"
    assert proposal["provenance"]["origin"] == "git"
    applied, commit = await _submit_and_approve(run, pid)
    run.imported = True
    assert applied["provenance"]["source_document"]["git_ref"] == TAG

    # The publication commit names where the terms came from.
    message = _git("log", "-1", "--format=%B", commit, cwd=_fetch(run.clone))
    trailers = message.splitlines()
    assert "Lakeloom-Origin: git" in trailers, message
    assert f"Lakeloom-Import: {TAG}@{run.release['commit_sha'][:12]}" in trailers, message

    # No semantic diff between the tagged release and the restored model's export.
    export = await _export(run)
    diff = diff_graphs(
        semantic_view(_graph(ttl), base_iri),
        semantic_view(_graph(export), base_iri),
        from_version=V1,
        to_version="restored",
    )
    assert diff.bump is None, diff_markdown(diff)


async def test_m3c_import_model_file_dry_run(run: Run) -> None:
    assert run.imported, "the restore from Git failed"
    agent = run.tokens.agent
    ttl, _ = _tagged(run)
    same = await call(
        run.url, agent, "import_model", {"model": MODEL, "content": ttl, "format": "turtle"}
    )
    err = error_of(same)
    assert err["code"] == "validation_error", err
    assert "already matches" in err["message"], err

    remote = json.dumps(
        {
            "@context": "http://169.254.169.254/context.jsonld",
            "@id": "http://example.org/x/Thing",
            "@type": "http://www.w3.org/2004/02/skos/core#Concept",
        }
    )
    hostile = await call(
        run.url, agent, "import_model", {"model": MODEL, "content": remote, "format": "jsonld"}
    )
    err = error_of(hostile)
    assert err["code"] == "validation_error", err
    assert "remote JSON-LD contexts are not allowed" in err["message"], err


def _copy_orders() -> None:
    """The fixture ``orders`` table as ``.scratch-lake/orders_drift``, without its declared
    foreign key: the FK names ``sales.customers``, which is not in ``fixture_scratch``, and the
    mapper would propose a relation to a ``Customer`` concept this model does not have."""
    shutil.copytree(REPO / "tests/fixtures/lake/orders", DRIFT_TABLE)
    first = DRIFT_TABLE / "_delta_log" / f"{0:020d}.json"
    actions = [json.loads(line) for line in first.read_text().splitlines() if line.strip()]
    for action in actions:
        config = action.get("metaData", {}).get("configuration")
        if isinstance(config, dict):
            for key in [k for k in config if k.startswith("lakeloom.foreignKey.")]:
                del config[key]
    first.write_text("".join(json.dumps(a, separators=(",", ":")) + "\n" for a in actions))


def _append_column() -> None:
    """One row with a new string column, appended with delta-rs (schema merge)."""
    table = DeltaTable(str(DRIFT_TABLE))
    names = [f.name for f in table.schema().fields]
    row = table.to_pyarrow_table().slice(0, 1).select(names)
    row = row.append_column(NEW_COLUMN, pa.array(["web"], pa.string()))
    write_deltalake(str(DRIFT_TABLE), row, mode="append", schema_mode="merge")


def _table_version(proposal: dict[str, Any]) -> int:
    [table] = [t for t in proposal["provenance"]["tables"] if t["fqn"].endswith(DRIFT_FQN)]
    assert isinstance(table["version"], int), table
    return int(table["version"])


async def test_m3c_drift_after_fixture_column_added(run: Run) -> None:
    _copy_orders()
    baseline = await _model_from_scan(run, "fixture_scratch", DRIFT)
    concepts = {op["concept"] for op in _operations(baseline) if op["op"] == "add_attribute"}
    assert len(concepts) == 1, concepts
    [concept] = concepts
    base_version = _table_version(baseline)

    _append_column()
    assert DeltaTable(str(DRIFT_TABLE)).version() == base_version + 1

    out = await call(run.url, run.tokens.agent, "detect_drift", {"model": DRIFT})
    report = ok(out)
    if report["proposal"] is not None:
        run.proposals.append(str(report["proposal"]["id"]))
        run.change_sets |= _change_sets(report["proposal"])
    [table] = [t for t in report["tables"] if t["fqn"].endswith(DRIFT_FQN)]
    assert table["status"] == "changed", (table, report["warnings"])
    assert table["added_columns"] == [NEW_COLUMN], table
    assert table["removed_columns"] == [] and table["renames"] == [], table

    proposal = report["proposal"]
    assert proposal is not None, text_of(out)
    ops = _operations(proposal)
    added = [op for op in ops if op["op"] == "add_attribute"]
    assert len(added) == 1, ops
    assert added[0]["name"] == NEW_COLUMN and added[0]["concept"] == concept, added
    assert proposal["status"] == "draft"
    assert proposal["provenance"]["origin"] == "drift"
    assert _table_version(proposal) == base_version + 1, proposal["provenance"]


async def test_m3c_sparql_select_and_update_refused(run: Run) -> None:
    assert run.imported, "the restore from Git failed"
    agent = run.tokens.agent
    _, base_iri = _tagged(run)
    expected = _concepts(await _export(run), base_iri)
    assert expected, "the export has no SKOS concepts"

    accept = {"Accept": "application/sparql-results+json"}
    async with httpx.AsyncClient(
        base_url=run.url,
        headers={**_bearer(agent), "Content-Type": "application/sparql-query"},
        timeout=60,
    ) as api:
        r = await api.post(f"/v1/models/{MODEL}/sparql", content=SELECT_CONCEPTS, headers=accept)
        assert r.status_code == 200, (r.status_code, r.text[:500])
        assert r.headers["content-type"].startswith("application/sparql-results+json")
        rows = [b["c"]["value"] for b in r.json()["results"]["bindings"]]
        assert set(rows) == expected and len(rows) == len(expected), (rows, expected)

        refused = await api.post(f"/v1/models/{MODEL}/sparql", content=INSERT, headers=accept)
        assert refused.status_code == 403, (refused.status_code, refused.text[:500])
        assert "SPARQL updates are not allowed" in refused.text, refused.text[:500]

    select = {"model": MODEL, "query": SELECT_CONCEPTS}
    tool = ok(await call(run.url, agent, "sparql_query", select))
    assert tool["form"] == "select" and tool["format"] == "json", tool
    assert [b["c"]["value"] for b in tool["result"]["results"]["bindings"]] == rows

    update = error_of(await call(run.url, agent, "sparql_query", {"model": MODEL, "query": INSERT}))
    assert update["code"] == "forbidden", update
