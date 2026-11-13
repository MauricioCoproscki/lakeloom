"""M2c acceptance against the live stack: an approved proposal becomes one Git commit
(FR-GIT-04, FR-GIT-05, FR-PRO-03).

Everything goes through real /mcp with Keycloak tokens; alice approves (the bot never does).
The Lakeloom container publishes to the dev Gitea repository ``lakeloom-admin/ontologies``
in ``direct`` mode (compose), and the throwaway model ``retail_m2c_pr`` in ``pull_request``
mode (``models[].git.mode`` in deploy/compose/lakeloom-sources.yaml). The tests run in file
order:

1. the agent scans fixture_delta, creates ``retail_m2c``, proposes from the scan and submits;
   alice approves: the review text says ``Git: committed``; a clone of the repository has
   the commit whose ``Lakeloom-Proposal`` is the proposal, with ``Approved-By: alice``, the
   APPLIED change sets and the scanned table versions as trailers, and the model's files;
2. two exports of the unchanged model equal the committed ``ontology.ttl`` byte for byte;
3. the same flow for ``retail_m2c_pr``: the approval opens a pull request in Gitea.

Teardown (runs on failure too): open proposals discarded, change sets, ontologies and
glossaries of both models hard-deleted (checked gone), the pull request closed and its branch
deleted, the seeded ``retail`` model checked untouched. No release is sealed. The commits of
``ontologies/retail_m2c/`` stay in the dev repository's history (dev only, see the README).
Tokens are never printed or put in assertion messages (NFR-SEC-11).
"""

import asyncio
import json
import os
import re
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import rdflib
from mcp.types import CallToolResult, TextContent
from stack import mcp_session

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="module")]

MODEL = "retail_m2c"
PR_MODEL = "retail_m2c_pr"
GITEA = "http://localhost:3000"
GITEA_API = f"{GITEA}/api/v1"
GITEA_REPO = "lakeloom-admin/ontologies"
GITEA_AUTH = ("lakeloom-admin", "lakeloom-admin")  # DEV ONLY fixed public compose credential
TABLES = ("customers", "products", "orders", "invoices")
OM_TABLE = "lakeloom_fixture.retail.sales.{}"
REVIEWER = "alice"
CACHE_WAIT_SECONDS = 90  # > the dev stack's model_cache_ttl_seconds (60)
_SHA7 = re.compile(r"^Git: committed ([0-9a-f]{7}) to main$", re.MULTILINE)


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
class Approved:
    """An applied proposal and what the review returned."""

    proposal_id: str
    proposal: dict[str, Any]
    text: str
    publication: dict[str, Any]
    scan: dict[str, Any]


@dataclass
class Run:
    url: str
    om: httpx.AsyncClient
    tokens: Tokens = field(repr=False)
    proposals: list[str] = field(default_factory=list[str])
    change_sets: set[str] = field(default_factory=set[str])
    pr_branches: list[str] = field(default_factory=list[str])
    direct: Approved | None = None
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


def _trailers(clone: Path, sha: str) -> list[tuple[str, str]]:
    out = _git("show", "-s", "--format=%(trailers:only,unfold)", sha, cwd=clone)
    pairs = []
    for line in out.splitlines():
        key, sep, value = line.partition(": ")
        if sep:
            pairs.append((key, value.strip()))
    return pairs


def _commit_of(clone: Path, proposal_id: str) -> str:
    out = _git(
        "log", "origin/main", "--format=%H%x00%(trailers:key=Lakeloom-Proposal,valueonly)",
        cwd=clone,
    )  # fmt: skip
    shas = [
        sha
        for sha, _, value in (line.partition("\x00") for line in out.splitlines() if line)
        if value.strip() == proposal_id
    ]
    assert len(shas) == 1, (proposal_id, shas)
    return shas[0]


def _gitea(method: str, path: str, **kw: Any) -> httpx.Response:
    return httpx.request(
        method, f"{GITEA_API}/repos/{GITEA_REPO}{path}", auth=GITEA_AUTH, timeout=10, **kw
    )


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
    for slug in (MODEL, PR_MODEL):
        await _delete_model(om, slug)
    for slug in (MODEL, PR_MODEL):
        assert not await _exists(om, slug), slug
    for cs_id in run.change_sets:
        r = await om.get(f"/api/v1/ontologyChangeSets/{cs_id}", params={"include": "all"})
        assert r.status_code == 404, cs_id
    for table in TABLES:
        left = {
            t for t in await _table_tags(om, table) if t.startswith((f"{MODEL}.", f"{PR_MODEL}."))
        }
        assert not left, (table, left)


def _close_pull_requests(run: Run) -> None:
    """Close every open PR of this run's branches and delete the branches (Gitea API)."""
    pulls = _gitea("GET", "/pulls", params={"state": "open", "limit": 50})
    pulls.raise_for_status()
    for pr in pulls.json():
        if pr["head"]["ref"] in run.pr_branches:
            closed = _gitea("PATCH", f"/pulls/{pr['number']}", json={"state": "closed"})
            assert closed.status_code == 201, closed.text
    for branch in run.pr_branches:
        r = _gitea("DELETE", f"/branches/{branch}")
        assert r.status_code in (204, 404), (branch, r.status_code)
        assert _gitea("GET", f"/branches/{branch}").status_code == 404, branch


async def _wait_for_lakeloom_caches(run: Run) -> None:
    """Wait until Lakeloom's caches (TTL 60 s in dev) no longer name the deleted models."""
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
        state.clone = tmp_path_factory.mktemp("m2c") / "ontologies"
        # A crashed earlier run may have left the fixed-name throwaway models behind.
        if any([await _exists(om, MODEL), await _exists(om, PR_MODEL)]):
            for slug in (MODEL, PR_MODEL):
                await _delete_model(om, slug)
            await _wait_for_lakeloom_caches(state)
        try:
            yield state
        finally:
            try:
                await _teardown(state)
            finally:
                try:
                    _close_pull_requests(state)
                finally:
                    try:
                        await _wait_for_lakeloom_caches(state)
                    finally:
                        assert await _retail(om) == retail, "the seeded retail model changed"


async def _approve_new_model(run: Run, slug: str) -> Approved:
    """agent scan → create_model → propose_from_scan → submit → alice approves."""
    t = run.tokens
    scanned = ok(await call(run.url, t.agent, "scan_lake_source", {"source": "fixture_delta"}))
    assert scanned["status"] == "completed"
    args = {"name": slug, "description": "M2c acceptance throwaway model"}
    preview = ok(await call(run.url, t.admin, "create_model", args))
    created = ok(await call(run.url, t.admin, "create_model", {**args, **preview_token(preview)}))
    assert created["created"]

    proposed = ok(
        await call(
            run.url, t.agent, "propose_from_scan", {"scan_id": scanned["scan_id"], "model": slug}
        )
    )
    pid = str(proposed["proposal_id"])
    run.proposals.append(pid)
    if slug == PR_MODEL:  # registered before the approve: teardown closes a leaked PR too
        run.pr_branches.append(f"lakeloom/{slug}/{pid}")
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
    return Approved(pid, out["proposal"], text_of(result), dict(out["publication"]), scanned)


def preview_token(preview: dict[str, Any]) -> dict[str, str]:
    return {"confirm_token": str(preview["confirm_token"])}


def _direct(run: Run) -> Approved:
    if run.direct is None or run.clone is None:
        pytest.skip(
            "the approval of test_m2c_alice_approval_is_committed_with_all_trailers is missing"
        )
    return run.direct


async def test_m2c_alice_approval_is_committed_with_all_trailers(run: Run) -> None:
    info = data(await call(run.url, run.tokens.alice, "get_server_info", {}))
    assert info["git"]["enabled"] is True and info["git"]["mode"] == "direct", info["git"]

    approved = await _approve_new_model(run, MODEL)
    run.direct = approved
    pub = approved.publication
    assert pub["status"] == "done", pub
    assert pub["branch"] == "main" and pub["commit"] and pub["pr_url"] is None
    line = _SHA7.search(approved.text)
    assert line is not None, approved.text[-500:]
    assert pub["commit"].startswith(line.group(1))

    assert run.clone is not None
    clone = _clone(run.clone)
    sha = _commit_of(clone, approved.proposal_id)
    assert sha == pub["commit"]
    trailers = _trailers(clone, sha)
    values: dict[str, list[str]] = {}
    for key, value in trailers:
        values.setdefault(key, []).append(value)
    assert values["Lakeloom-Proposal"] == [approved.proposal_id]
    assert values["Approved-By"] == [REVIEWER]
    assert values["Lakeloom-Publication"] == [pub["id"]]
    assert values["Lakeloom-Scan"] == [approved.scan["scan_id"]]
    # Proposed-By and AI-Generated, against the proposal's stored provenance.
    shown = ok(
        await call(run.url, run.tokens.agent, "get_proposal", {"proposal_id": approved.proposal_id})
    )
    prov = shown["proposal"]["provenance"]
    assert prov["principal_type"] == "agent", prov["principal_type"]
    assert values["Lakeloom-Proposed-By"] == [
        f"{prov['principal_subject']} ({prov['principal_type']})"
    ]
    assert values["Lakeloom-AI-Generated"] == ["true" if prov["ai_generated"] else "false"]

    # One Lakeloom-ChangeSet per applied set, equal to OpenMetadata's APPLIED change sets.
    change_sets = values["Lakeloom-ChangeSet"]
    assert len(change_sets) == len(set(change_sets))
    assert set(change_sets) == _change_sets(approved.proposal)
    for cs_id in change_sets:
        cs = (await run.om.get(f"/api/v1/ontologyChangeSets/{cs_id}")).raise_for_status().json()
        assert cs["state"] == "APPLIED", (cs_id, cs["state"])
        assert cs["applicationResult"]["appliedBy"] == REVIEWER, cs_id

    # One Lake-Source-Version per scanned table, equal to the scan's versions.
    scanned = {
        ".".join(
            p for p in (t["ref"].get("catalog"), t["ref"]["schema"], t["ref"]["name"]) if p
        ): t["version"]["version"]
        for t in approved.scan["record"]["tables"]
    }
    assert len(scanned) == len(TABLES) and all(v is not None for v in scanned.values()), scanned
    assert sorted(values["Lake-Source-Version"]) == sorted(
        f"{fqn}@{version}" for fqn, version in scanned.items()
    )

    # The commit holds the model's files.
    changed = set(_git("show", "--name-only", "--format=", sha, cwd=clone).split())
    base = f"ontologies/{MODEL}"
    assert {f"{base}/ontology.ttl", f"{base}/manifest.yaml", f"provenance/{MODEL}.jsonl"} <= (
        changed | set(_git("ls-tree", "-r", "--name-only", sha, cwd=clone).split())
    )
    ttl = _git("show", f"{sha}:{base}/ontology.ttl", cwd=clone)
    graph = rdflib.Graph().parse(data=ttl, format="turtle")
    labels = {str(o) for o in graph.objects(None, rdflib.RDFS.label)} | {
        str(o) for o in graph.objects(None, rdflib.SKOS.prefLabel)
    }
    concepts = {
        op["name"]
        for part in approved.proposal["parts"]
        for op in part["operations"]
        if op["op"] == "add_concept"
    }
    assert {"Customer", "Order"} <= concepts
    assert concepts <= labels, sorted(concepts - labels)
    manifest = _git("show", f"{sha}:{base}/manifest.yaml", cwd=clone)
    assert f'model: "{MODEL}"' in manifest, manifest
    provenance = [
        json.loads(line)
        for line in _git("show", f"{sha}:provenance/{MODEL}.jsonl", cwd=clone).splitlines()
    ]
    mine = [r for r in provenance if r["proposal_id"] == approved.proposal_id]
    assert len(mine) == 1 and mine[0]["approved_by"] == REVIEWER
    assert mine[0]["publication_id"] == pub["id"]

    # get_proposal shows the same publication.
    assert shown["publication"]["commit"] == pub["commit"]


async def test_m2c_export_is_deterministic(run: Run) -> None:
    approved = _direct(run)
    assert run.clone is not None
    committed = _git(
        "show", f"{approved.publication['commit']}:ontologies/{MODEL}/ontology.ttl", cwd=run.clone
    ).encode()
    exports = []
    for _ in range(2):
        result = await call(run.url, run.tokens.agent, "export_model", {"model": MODEL})
        out = ok(result)
        assert out["format"] == "turtle"
        exports.append(text_of(result).encode("utf-8"))
    assert exports[0] == exports[1] == committed


async def test_m2c_pull_request_mode(run: Run) -> None:
    approved = await _approve_new_model(run, PR_MODEL)
    pub = approved.publication
    branch = f"lakeloom/{PR_MODEL}/{approved.proposal_id}"
    assert branch in run.pr_branches
    assert pub["status"] == "done", pub
    assert pub["branch"] == branch and pub["pr_url"], pub
    assert f"Git: pull request {pub['pr_url']}" in approved.text, approved.text[-500:]

    pulls = _gitea("GET", "/pulls", params={"state": "open", "limit": 50})
    pulls.raise_for_status()
    [pr] = [p for p in pulls.json() if p["head"]["ref"] == branch]
    assert pr["html_url"] == pub["pr_url"]
    assert pr["base"]["ref"] == "main"
    assert pr["head"]["sha"] == pub["commit"]
    assert f"Lakeloom-Proposal: {approved.proposal_id}" in pr["body"]
    assert "Approved-By: alice" in pr["body"]
    # main is untouched by a pull-request publication.
    head = _gitea("GET", f"/contents/ontologies/{PR_MODEL}/manifest.yaml", params={"ref": "main"})
    assert head.status_code == 404
    on_branch = _gitea(
        "GET", f"/contents/ontologies/{PR_MODEL}/ontology.ttl", params={"ref": branch}
    )
    assert on_branch.status_code == 200
