"""M3b acceptance against the live stack: a release produces an annotated Git tag whose commit
holds the five exports, the manifest and the CHANGELOG, and the released version is served
over HTTP and MCP from that tag (FR-GIT-06, FR-PUB-02, FR-PUB-07, FR-AGT-03).

Everything goes through real /mcp and /v1 with Keycloak tokens. The module builds one
throwaway model ``retail_m3b``: the agent scans ``fixture_delta``, creates the model,
proposes from the scan and submits; alice approves (the bot never does) and the approval is
published to the dev Gitea repository in ``direct`` mode. ``create_release`` runs as the
agent client ``lakeloom-agent`` with its optional scope ``lakeloom:publish`` (agents may
release; only review is human-only). The tests run in file order:

1. release ``1.0.0`` (dry run, then confirm): the tag, its files and the read surfaces;
2. alice approves an added attribute; the next release is computed as minor (``1.1.0``) and
   ``diff_releases`` lists the attribute.

Not covered live: a release refused while a publication is ``pending`` or ``blocked``. The
dev stack's publication queue cannot be held without breaking Gitea for the other suites,
so that precondition stays covered by the unit tests (tests/unit/core/test_releases.py,
``test_pending_publication_blocks_release``). The Graph Store sink is off in the stack
(the ``rdf`` profile is not started); see test_triplestore_fuseki.py.

Teardown (runs on failure too): the test tags deleted through the Gitea API (checked gone),
open proposals discarded, change sets, the ontology and the glossary of ``retail_m3b``
hard-deleted (checked gone), the seeded ``retail`` model checked untouched. Tags left by a
crashed earlier run are deleted in the setup. No OpenMetadata release is sealed. The commits
of ``ontologies/retail_m3b/`` stay in the dev repository's history (dev only). Tokens are
never printed or put in assertion messages (NFR-SEC-11).
"""

import asyncio
import json
import os
import subprocess
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
import pytest_asyncio
import yaml
from mcp.types import CallToolResult, TextContent
from stack import TOKEN_URL, mcp_session, token_scopes

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="module")]

MODEL = "retail_m3b"
V1, V2 = "1.0.0", "1.1.0"
TAGS = (f"{MODEL}/v{V1}", f"{MODEL}/v{V2}")
GITEA = "http://localhost:3000"
GITEA_API = f"{GITEA}/api/v1"
GITEA_REPO = "lakeloom-admin/ontologies"
GITEA_AUTH = ("lakeloom-admin", "lakeloom-admin")  # DEV ONLY fixed public compose credential
TABLES = ("customers", "products", "orders", "invoices")
OM_TABLE = "lakeloom_fixture.retail.sales.{}"
EXPORTS = ("ontology.ttl", "ontology.jsonld", "ontology.owl", "shapes.ttl", "context.jsonld")
ATTRIBUTE = "loyalty_tier"
CACHE_WAIT_SECONDS = 90  # > the dev stack's model_cache_ttl_seconds (60)
PUBLICATION_WAIT_SECONDS = 120
TAG_WAIT_SECONDS = 30


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
    releases: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])


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
        env={
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
        },
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


async def _check_gone(run: Run) -> None:
    om = run.om
    assert not await _exists(om, MODEL), MODEL
    for cs_id in run.change_sets:
        r = await om.get(f"/api/v1/ontologyChangeSets/{cs_id}", params={"include": "all"})
        assert r.status_code == 404, cs_id
    for table in TABLES:
        left = {t for t in await _table_tags(om, table) if t.startswith(f"{MODEL}.")}
        assert not left, (table, left)


async def _teardown(run: Run) -> None:
    """Each phase runs even when an earlier one fails: tags and model are always deleted."""
    om = run.om
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
                    for cs_id in run.change_sets:
                        await om.delete(
                            f"/api/v1/ontologyChangeSets/{cs_id}", params={"hardDelete": "true"}
                        )
                finally:
                    await _delete_model(om, MODEL)
    await _check_gone(run)


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


async def _wait_published(run: Run, pid: str, publication: dict[str, Any] | None) -> None:
    """Until the proposal's Git publication is ``done`` (it usually is on approval)."""
    assert publication is not None, "Git publishing is off in the stack"
    deadline = asyncio.get_running_loop().time() + PUBLICATION_WAIT_SECONDS
    while publication is not None and publication["status"] != "done":
        assert publication["status"] == "pending", publication
        assert asyncio.get_running_loop().time() < deadline, publication
        await asyncio.sleep(2)
        shown = ok(await call(run.url, run.tokens.agent, "get_proposal", {"proposal_id": pid}))
        publication = shown.get("publication")
    assert publication is not None and publication["branch"] == "main", publication


async def _approve(run: Run, pid: str) -> None:
    """Submit (if needed), alice approves, wait for the Git publication."""
    t = run.tokens
    review = {"proposal_id": pid, "decision": "approve"}
    preview = ok(await call(run.url, t.alice, "review_proposal", review))
    assert preview["decided"] is False and preview["confirm_token"]
    out = ok(await call(run.url, t.alice, "review_proposal", {**review, **preview_token(preview)}))
    assert out["decided"] is True and out["proposal"]["status"] == "applied"
    run.change_sets |= _change_sets(out["proposal"])
    await _wait_published(run, pid, out["publication"])


async def _approve_new_model(run: Run) -> None:
    """agent scan → create_model → propose_from_scan → submit → alice approves."""
    t = run.tokens
    scanned = ok(await call(run.url, t.agent, "scan_lake_source", {"source": "fixture_delta"}))
    assert scanned["status"] == "completed"
    args = {"name": MODEL, "description": "M3b acceptance throwaway model"}
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
    await _approve(run, pid)


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
        clone = tmp_path_factory.mktemp("m3b") / "ontologies"
        state = Run(url=lakeloom_url, om=om, tokens=tokens, clone=clone)
        # A crashed earlier run may have left its tags or the throwaway model behind.
        _delete_tags()
        if await _exists(om, MODEL):
            await _delete_model(om, MODEL)
            await _wait_for_lakeloom_caches(state)
        try:
            await _approve_new_model(state)
            yield state
        finally:
            try:
                await _teardown(state)
            finally:
                try:
                    await _wait_for_lakeloom_caches(state)
                finally:
                    assert await _retail(om) == retail, "the seeded retail model changed"


async def _release(run: Run, expected: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Dry run (checked to compute ``expected``), then confirm: (plan, plan text, result)."""
    publisher = run.tokens.publisher
    args = {"model": MODEL}
    dry = await call(run.url, publisher, "create_release", args)
    preview = ok(dry)
    assert preview["released"] is False
    plan = preview["preview"]
    assert plan["version"] == expected and plan["tag"] == f"{MODEL}/v{expected}", plan
    confirmed = ok(
        await call(run.url, publisher, "create_release", {**args, **preview_token(plan)})
    )
    assert confirmed["released"] is True
    result = confirmed["result"]
    assert result["already_done"] is False and result["release"]["tag"] == plan["tag"], result
    run.releases[expected] = result["release"]
    return plan, text_of(dry), result


def _get(run: Run, path: str, accept: str) -> httpx.Response:
    r = httpx.get(
        f"{run.url}/v1/models/{MODEL}{path}",
        headers={**_bearer(run.tokens.agent), "Accept": accept},
        timeout=60,
    )
    assert r.status_code == 200, (path, r.status_code, r.text[:500])
    return r


async def test_m3b_release_v1_produces_tag_and_artefacts(run: Run) -> None:
    plan, text, result = await _release(run, V1)
    assert plan["previous"] is None
    assert "Initial release" in plan["changelog_section"]
    assert "Initial release" in text and f"`{V1}`" in text
    tag = TAGS[0]

    # The Gitea tag exists, annotated, and points at the release commit.
    # Git is checked for the commit: Gitea's single-tag endpoint can show the commit of an
    # earlier tag of the same name (a rerun after the teardown deleted it).
    assert _remote_tags().get(tag) == result["release"]["commit_sha"]
    assert _gitea("GET", _tag_path(tag)).status_code == 200

    clone = _fetch(run.clone)
    assert _git("cat-file", "-t", tag, cwd=clone).strip() == "tag"
    sha = _git("rev-list", "-n", "1", tag, cwd=clone).strip()
    assert sha == result["release"]["commit_sha"]
    base = f"ontologies/{MODEL}"
    tree = set(_git("ls-tree", "-r", "--name-only", tag, cwd=clone).split())
    expected = {f"{base}/{f}" for f in (*EXPORTS, "manifest.yaml", "CHANGELOG.md")}
    assert expected <= tree, sorted(expected - tree)
    manifest = yaml.safe_load(_git("show", f"{tag}:{base}/manifest.yaml", cwd=clone))
    assert manifest["release"] == V1 and manifest["released_at"], manifest
    changelog = _git("show", f"{tag}:{base}/CHANGELOG.md", cwd=clone)
    assert f"## [{V1}]" in changelog
    subject = _git("log", "-1", "--format=%s", tag, cwd=clone).strip()
    assert subject == f"release({MODEL}): v{V1}"

    # /versions lists latest and the release.
    versions = _get(run, "/versions", "application/json").json()
    assert [v["version"] for v in versions][:1] == ["latest"]
    assert V1 in [v["version"] for v in versions]
    [entry] = [v for v in versions if v["version"] == V1]
    assert entry["tag"] == tag and entry["commit"] == sha

    # The release served from its tag, with Git links.
    ttl = _get(run, f"/versions/{V1}", "text/turtle")
    assert ttl.text == _git("show", f"{tag}:{base}/ontology.ttl", cwd=clone)
    assert f"/src/tag/{tag}" in ttl.headers["link"], ttl.headers["link"]
    assert f"/commit/{sha}" in ttl.headers["link"]
    shapes = _get(run, f"/shapes?version={V1}", "text/turtle")
    assert shapes.text == _git("show", f"{tag}:{base}/shapes.ttl", cwd=clone)

    async with mcp_session(run.url, run.tokens.agent) as session:
        read = await session.read_resource(f"lakeloom://models/{MODEL}/ontology.ttl?version={V1}")
    content = read.contents[0]
    assert content.mime_type == "text/turtle"
    assert getattr(content, "text", "").encode() == ttl.content

    listed = ok(await call(run.url, run.tokens.agent, "list_releases", {"model": MODEL}))
    assert [r["version"] for r in listed["releases"]] == [V1]


async def test_m3b_second_release_is_minor(run: Run) -> None:
    assert V1 in run.releases, "the first release failed"
    t = run.tokens
    customer = f"{MODEL}.Customer"
    # The mapper proposes no realizations: one starts the proposal, then the attribute.
    started = ok(
        await call(
            run.url,
            t.agent,
            "propose_realization",
            {"concept": customer, "asset": OM_TABLE.format("customers"), "model": MODEL},
        )
    )
    pid = str(started["proposal_id"])
    run.proposals.append(pid)
    run.change_sets |= _change_sets(started["proposal"])
    attribute = {
        "op": "add_attribute",
        "concept": customer,
        "name": ATTRIBUTE,
        "datatype": "xsd:string",
    }
    changed = ok(
        await call(
            run.url, t.agent, "propose_change", {"proposal_id": pid, "operations": [attribute]}
        )
    )
    run.change_sets |= _change_sets(changed["proposal"])
    assert changed["status"] == "draft"
    assert ok(await call(run.url, t.agent, "submit_proposal", {"proposal_id": pid}))
    await _approve(run, pid)

    plan, _, _ = await _release(run, V2)
    assert plan["previous"] == V1
    assert plan["diff"]["bump"] == "minor", plan["diff"]
    added = {c["label"] for c in plan["diff"]["added"]}
    assert ATTRIBUTE in added, added
    section = plan["changelog_section"]
    assert section.startswith(f"## [{V2}]")
    added_block = section.split("### Added", 1)[1].split("###", 1)[0]
    assert ATTRIBUTE in added_block, section
    proposals_block = section.split("### Proposals", 1)[1]
    assert pid in proposals_block and "approved by alice" in proposals_block, section

    diff = ok(
        await call(
            run.url,
            t.agent,
            "diff_releases",
            {"model": MODEL, "from_version": V1, "to_version": V2},
        )
    )
    assert diff["bump"] == "minor"
    assert ATTRIBUTE in {c["label"] for c in diff["added"]}, diff["added"]

    listed = ok(await call(run.url, t.agent, "list_releases", {"model": MODEL}))
    assert [r["version"] for r in listed["releases"]] == [V2, V1]
