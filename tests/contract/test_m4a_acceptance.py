"""M4a acceptance against the live stack: reconciliation finds a direct edit in OpenMetadata and
commits it to Git, background-job leases on the stack's Postgres, the per-principal rate limit
and MCP sessions bound to their principal (FR-GIT-08, NFR-DEP-07, NFR-SEC-16).

Git mode is ``direct`` and the state store is Postgres. The compose stack runs no scheduled
reconciliation (``LAKELOOM_RECONCILE_INTERVAL_SECONDS: "0"``): the test drives it with
``docker compose exec lakeloom lakeloom reconcile``, and a ``--commit`` run would be refused
while the server's job held the ``reconcile`` lease. For the same reason the server never runs
a reconciliation itself here, so its ``get_server_info`` has no ``jobs.reconcile`` summary;
the drift is checked in the audit row the CLI's run writes to the shared state store instead.

The module builds the throwaway model ``retail_m4a`` as in M3c: the agent scans
``fixture_delta``, creates the model, proposes from the scan and submits; alice approves (the
bot never does); the publication to the dev Gitea repository is ``done``. The direct edit is
the bot's ``PATCH`` of the ``Customer`` term description outside any change set (ADR 0017
§ Research R1, ``test_om_direct_edit_probe.py``).

Teardown (runs on failure too): open proposals discarded, change sets and the glossary and
ontology of ``retail_m4a`` hard-deleted (checked gone), the seeded ``retail`` model checked
untouched. Leftovers of a crashed earlier run are removed in the setup. No OpenMetadata release
is sealed and no tag or branch is created. The commits of ``ontologies/retail_m4a/`` stay in the
dev repository's history (dev only). Tokens and the database URL are never printed or put in
assertion messages (NFR-SEC-11).
"""

import asyncio
import json
import os
import subprocess
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from mcp.types import CallToolResult
from pydantic import SecretStr
from rdflib import SKOS, Graph, URIRef
from sqlalchemy import select
from stack import COMPOSE, KEYCLOAK_URL, OM_URL, STATE_DB_URL, mcp_session, server_info

from lakeloom.config import Settings
from lakeloom.server.http_app import build_deps
from lakeloom.server.mcp_app import create_server_app
from lakeloom.state.db import create_engine
from lakeloom.state.leases import LeaseRepository, instance_id
from lakeloom.state.models import AuditLogRow, JobLeaseRow
from lakeloom.state.publications import PublicationRepository

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="module")]

MODEL = "retail_m4a"
CONCEPT = "Customer"
EDITED = "A customer, edited directly in OpenMetadata (M4a acceptance)"
GITEA = "http://localhost:3000"
GITEA_REPO = "lakeloom-admin/ontologies"
TABLES = ("customers", "products", "orders", "invoices")
OM_TABLE = "lakeloom_fixture.retail.sales.{}"
CACHE_WAIT_SECONDS = 90  # > the dev stack's model_cache_ttl_seconds (60)
PUBLICATION_WAIT_SECONDS = 180  # > the server's outbox interval (60 s) plus a publish
EXIT_DRIFT = 3
SESSION_HEADER = "mcp-session-id"
# A raw JSON-RPC client: the protocol version before per-request ``_meta`` envelopes.
PROTOCOL_VERSION = "2025-06-18"
UNKNOWN_SESSION = {
    "error": {
        "code": "not_found",
        "message": "unknown MCP session",
        "hint": "initialize a new MCP session",
    }
}
# The dev compose map (deploy/compose/docker-compose.yml) for the in-process app.
GROUP_MAP = {
    "ontology-reviewers": ["lakeloom:read", "lakeloom:review"],
    "data-engineers": ["lakeloom:read", "lakeloom:propose"],
    "lakeloom-admins": ["lakeloom:read", "lakeloom:admin"],
}


class Token(str):
    """A bearer token whose repr is redacted: pytest prints frame arguments on failure."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<redacted token>"


class DbUrl(str):
    """The state-store URL (it holds a password): redacted in repr, never printed."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<redacted database URL>"


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
    clone: Path
    proposals: list[str] = field(default_factory=list[str])
    change_sets: set[str] = field(default_factory=set[str])
    base_iri: str = ""


# Only wraps, takes no other step: nothing in this frame (raw token arguments) can fail.
@pytest.fixture(scope="module")
def tokens(agent_token: str, admin_agent_token: str, human_token: str, om_bot_token: str) -> Tokens:
    return Tokens(
        Token(agent_token), Token(admin_agent_token), Token(human_token), Token(om_bot_token)
    )


@pytest.fixture(scope="module")
def db_url() -> DbUrl:
    return DbUrl(STATE_DB_URL)


async def call(url: str, token: str, tool: str, args: dict[str, Any]) -> CallToolResult:
    async with mcp_session(url, token) as session:
        return await session.call_tool(tool, args)


def data(result: CallToolResult) -> dict[str, Any]:
    assert result.structured_content is not None, result.content
    return dict(result.structured_content)


def ok(result: CallToolResult) -> dict[str, Any]:
    assert result.is_error is not True, json.dumps(result.structured_content)[:3000]
    return data(result)


def preview_token(preview: dict[str, Any]) -> dict[str, str]:
    return {"confirm_token": str(preview["confirm_token"])}


def _change_sets(proposal: dict[str, Any]) -> set[str]:
    return {
        cs for part in proposal["parts"] for cs in part["change_set_ids"].values() if cs is not None
    }


def _bearer(token: str) -> httpx.Headers:
    return httpx.Headers({"Authorization": f"Bearer {token}"})


def _git_env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }


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
    """A clone of the dev repository, up to date (public: no credentials)."""
    if not dest.exists():
        _git("clone", "-q", f"{GITEA}/{GITEA_REPO}.git", str(dest), cwd=dest.parent)
    _git("fetch", "-q", "--prune", "--force", "origin", cwd=dest)
    return dest


def _compose_exec(*argv: str, timeout: float = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no untrusted input
        [*COMPOSE, "exec", "-T", "lakeloom", *argv],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _reconcile(*extra: str) -> tuple[int, dict[str, Any]]:
    """``lakeloom reconcile --model retail_m4a --json`` in the Lakeloom container: (exit, run).
    The CLI logs to stderr (never tokens); stdout is the run as JSON."""
    proc = _compose_exec("lakeloom", "reconcile", "--model", MODEL, "--json", *extra)
    assert proc.returncode in (0, EXIT_DRIFT), (proc.returncode, proc.stderr[-3000:])
    run = json.loads(proc.stdout)
    assert [r["model"] for r in run["results"]] == [MODEL], run
    return proc.returncode, dict(run["results"][0])


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


async def _teardown(run: Run) -> None:
    """Each phase runs even when an earlier one fails: the model always goes."""
    try:
        await _discard_open(run)
    finally:
        try:
            await _collect_change_sets(run)
        finally:
            try:
                for cs_id in run.change_sets:
                    await run.om.delete(
                        f"/api/v1/ontologyChangeSets/{cs_id}", params={"hardDelete": "true"}
                    )
            finally:
                await _delete_model(run.om, MODEL)
    assert not await _exists(run.om, MODEL)
    for cs_id in run.change_sets:
        r = await run.om.get(f"/api/v1/ontologyChangeSets/{cs_id}", params={"include": "all"})
        assert r.status_code == 404, cs_id
    for table in TABLES:
        left = {t for t in await _table_tags(run.om, table) if t.startswith(f"{MODEL}.")}
        assert not left, (table, left)


async def _wait_for_lakeloom_caches(run: Run) -> None:
    """Wait until Lakeloom's caches (TTL 60 s in dev) no longer name the deleted model."""
    deadline = asyncio.get_running_loop().time() + CACHE_WAIT_SECONDS
    async with httpx.AsyncClient(
        base_url=run.url, headers=_bearer(run.tokens.agent), timeout=30
    ) as api:
        while True:
            r = await api.get("/v1/models")
            if r.status_code == 200 and f'"{MODEL}"' not in r.text:
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


async def _create_model(run: Run) -> None:
    args = {"name": MODEL, "description": "M4a acceptance throwaway model"}
    preview = ok(await call(run.url, run.tokens.admin, "create_model", args))
    created = ok(
        await call(run.url, run.tokens.admin, "create_model", {**args, **preview_token(preview)})
    )
    assert created["created"]


async def _model_from_scan(run: Run) -> None:
    """agent scan → create_model → propose_from_scan → submit → alice approves → published."""
    t = run.tokens
    scanned = ok(await call(run.url, t.agent, "scan_lake_source", {"source": "fixture_delta"}))
    assert scanned["status"] == "completed", scanned
    await _create_model(run)
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
    out = ok(await call(run.url, t.alice, "review_proposal", {**review, **preview_token(preview)}))
    assert out["decided"] is True and out["proposal"]["status"] == "applied", out["proposal"]
    run.change_sets |= _change_sets(out["proposal"])
    await _wait_published(run, pid, out["publication"])


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def run(
    lakeloom_url: str, om_url: str, tokens: Tokens, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[Run]:
    try:
        async with httpx.AsyncClient(timeout=5) as gitea:
            (await gitea.get(f"{GITEA}/api/v1/version")).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"Gitea not reachable at {GITEA}: {exc!r}")
    # the bot: read-only checks, the direct edit and the teardown only
    async with httpx.AsyncClient(base_url=om_url, headers=_bearer(tokens.bot), timeout=60) as om:
        retail = await _retail(om)
        clone = tmp_path_factory.mktemp("m4a") / "ontologies"
        state = Run(url=lakeloom_url, om=om, tokens=tokens, clone=clone)
        if await _exists(om, MODEL):  # a crashed earlier run
            await _delete_model(om, MODEL)
            await _wait_for_lakeloom_caches(state)
        try:
            await _model_from_scan(state)
            model = ok(await call(lakeloom_url, tokens.agent, "get_model", {"model": MODEL}))
            state.base_iri = str(model["base_iri"])
            yield state
        finally:
            try:
                await _teardown(state)
            finally:
                try:
                    await _wait_for_lakeloom_caches(state)
                finally:
                    assert await _retail(om) == retail, "the seeded retail model changed"


async def _direct_edit(run: Run) -> None:
    """The bot's PATCH of the concept's description outside any change set (ADR 0017 R1)."""
    term = (
        (await run.om.get(f"/api/v1/glossaryTerms/name/{MODEL}.{CONCEPT}"))
        .raise_for_status()
        .json()
    )
    patch = [{"op": "replace", "path": "/description", "value": EDITED}]
    r = await run.om.patch(
        f"/api/v1/glossaryTerms/{term['id']}",
        content=json.dumps(patch).encode(),
        headers={"Content-Type": "application/json-patch+json"},
    )
    assert r.status_code == 200, r.status_code
    assert r.json()["description"] == EDITED


async def _wait_reconcile_published(db_url: str, rec_id: str) -> tuple[str, str]:
    """Until the reconcile publication is ``done`` (the server's outbox drains it): its id and
    commit. It must be ``pending`` first: the CLI only queues it."""
    engine = create_engine(db_url)
    try:
        pubs = PublicationRepository(engine)
        pub = await pubs.for_proposal(rec_id)
        assert pub is not None, rec_id
        assert pub.status in ("pending", "done"), (pub.status, pub.last_error)
        deadline = time.monotonic() + PUBLICATION_WAIT_SECONDS
        while pub.status != "done":
            assert pub.status == "pending", (pub.status, pub.last_error)
            assert time.monotonic() < deadline, (pub.status, pub.attempts)
            await asyncio.sleep(3)
            pub = await pubs.for_proposal(rec_id)
            assert pub is not None, rec_id
        assert pub.commit_sha and pub.branch == "main", (pub.commit_sha, pub.branch)
        return pub.id, pub.commit_sha
    finally:
        await engine.dispose()


@dataclass(frozen=True)
class Audit:
    principal_subject: str
    principal_type: str
    result: str
    proposal_id: str | None
    change_set_id: str | None


async def _reconcile_audit(db_url: str, since: datetime) -> list[Audit]:
    """The ``reconcile`` audit rows of the model since ``since`` (subject, type, result, the
    reconcile id and the publication id)."""
    engine = create_engine(db_url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                select(
                    AuditLogRow.principal_subject,
                    AuditLogRow.principal_type,
                    AuditLogRow.result,
                    AuditLogRow.proposal_id,
                    AuditLogRow.change_set_id,
                )
                .where(
                    AuditLogRow.tool == "reconcile",
                    AuditLogRow.model == MODEL,
                    AuditLogRow.at >= since,
                )
                .order_by(AuditLogRow.at)
            )
            return [Audit(*row) for row in rows]
    finally:
        await engine.dispose()


def _definitions(turtle: str, base_iri: str) -> list[str]:
    g = Graph().parse(data=turtle, format="turtle")
    return [str(o) for o in g.objects(URIRef(f"{base_iri}{CONCEPT}"), SKOS.definition)]


async def test_m4a_reconcile_reports_and_commits_direct_edit(run: Run, db_url: DbUrl) -> None:
    concept = f"{run.base_iri}{CONCEPT}"

    # Published from an applied proposal: OpenMetadata and Git agree.
    code, result = _reconcile()
    assert (code, result["status"]) == (0, "in_sync"), result

    # A direct edit in OpenMetadata is drift; nothing is committed without --commit.
    await _direct_edit(run)
    since = datetime.now(UTC).replace(microsecond=0)
    code, result = _reconcile()
    assert (code, result["status"]) == (EXIT_DRIFT, "drifted"), result
    assert result["changed"] == [concept], result
    assert (result["added"], result["removed"]) == ([], []), result
    assert result["publication_id"] is None, result
    # The server runs no reconcile job in this stack (see the module docstring): the CLI's
    # run is recorded in the shared state store, as the job's would be.
    audit = await _reconcile_audit(db_url, since)
    assert [(a.principal_subject, a.principal_type, a.result) for a in audit] == [
        ("system:reconcile", "system", "ok")
    ], audit
    info = await server_info(run.url, run.tokens.agent)
    jobs = info["jobs"]
    assert isinstance(jobs, dict) and "git-publisher" in jobs, jobs
    assert "reconcile" not in jobs or MODEL not in jobs["reconcile"].get("drifted", []), jobs

    # --commit queues a publication; the server's outbox publishes it.
    code, result = _reconcile("--commit")
    assert (code, result["status"]) == (EXIT_DRIFT, "drifted"), result
    assert result["changed"] == [concept], result
    pub_id = result["publication_id"]
    assert isinstance(pub_id, str) and pub_id, result
    audit = await _reconcile_audit(db_url, since)
    [rec_id] = [a.proposal_id for a in audit if a.change_set_id == pub_id]
    assert rec_id is not None and rec_id.startswith("rec_"), rec_id
    published_id, commit = await _wait_reconcile_published(db_url, rec_id)
    assert published_id == pub_id

    clone = _fetch(run.clone)
    message = _git("log", "-1", "--format=%B", commit, cwd=clone)
    assert message.splitlines()[0] == f"ontology({MODEL}): direct edits in OpenMetadata", message
    trailers = message.splitlines()
    assert "Lakeloom-Origin: om-direct-edit" in trailers, message
    assert f"Lakeloom-Reconcile: {rec_id}" in trailers, message
    assert f"Lakeloom-Publication: {pub_id}" in trailers, message
    ttl = _git("show", f"{commit}:ontologies/{MODEL}/ontology.ttl", cwd=clone)
    assert _definitions(ttl, run.base_iri) == [EDITED], _definitions(ttl, run.base_iri)

    # Git holds the direct edit now: in sync again.
    code, result = _reconcile()
    assert (code, result["status"]) == (0, "in_sync"), result


async def test_m4a_leases_on_postgres(lakeloom_url: str, agent_token: str, db_url: DbUrl) -> None:
    engine = create_engine(db_url)
    name = f"m4a-acceptance-{instance_id()}"
    holders = ("contract-replica-a", "contract-replica-b")
    try:
        assert engine.dialect.name == "postgresql", engine.dialect.name
        a, b = LeaseRepository(engine), LeaseRepository(engine)
        ttl = timedelta(seconds=60)
        won = await asyncio.gather(
            a.acquire(name, holders[0], ttl), b.acquire(name, holders[1], ttl)
        )
        assert sorted(won) == [False, True], won
        winner = holders[won.index(True)]
        assert await a.holder_of(name) == winner
        # the loser can neither take nor drop the winner's lease
        loser_repo = b if winner == holders[0] else a
        loser = holders[1] if winner == holders[0] else holders[0]
        assert not await loser_repo.acquire(name, loser, ttl)
        await loser_repo.release(name, loser)
        assert await a.holder_of(name) == winner

        # The running server holds its outbox job's lease under its instance id.
        host = _compose_exec("python", "-c", "import socket; print(socket.gethostname())")
        assert host.returncode == 0, host.stderr[-2000:]
        hostname = host.stdout.strip()
        assert hostname
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(JobLeaseRow.holder, JobLeaseRow.expires_at).where(
                        JobLeaseRow.name.like("git-publisher:%")
                    )
                )
            ).all()
        assert len(rows) == 1, [r.holder for r in rows]
        [(holder, expires_at)] = rows
        assert holder.startswith(f"{hostname}-"), holder
        assert expires_at > datetime.now(UTC), expires_at
        jobs = (await server_info(lakeloom_url, Token(agent_token)))["jobs"]
        assert isinstance(jobs, dict) and jobs["git-publisher"]["held"] is True, jobs
    finally:
        try:
            for holder in holders:
                await LeaseRepository(engine).release(name, holder)
        finally:
            await engine.dispose()


@dataclass(frozen=True)
class Reply:
    status_code: int
    retry_after: str | None
    text: str

    def json(self) -> Any:
        return json.loads(self.text)


def _rate_limited_calls(settings: Settings, agent: str, alice: str) -> list[Reply]:
    """Six ``GET /v1/models`` as the agent, then one as alice, on an in-process app."""
    app = create_server_app(build_deps(settings))
    out: list[Reply] = []
    with TestClient(app, base_url="http://localhost:8000") as client:
        for token in (agent,) * 6 + (alice,):
            r = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
            out.append(Reply(r.status_code, r.headers.get("retry-after"), r.text))
    return out


async def test_m4a_rate_limit_live(
    lakeloom_url: str, tokens: Tokens, tmp_path: Path, om_url: str
) -> None:
    # The stack's OM, Keycloak and group map; a private SQLite and no Git, so the app runs no
    # job and never competes with the shared server for a lease. A refill of 1/min keeps the
    # sixth call over the burst of 5 however slow OpenMetadata answers.
    settings = Settings(
        om_url=OM_URL,  # pyright: ignore[reportArgumentType]
        om_bot_token=SecretStr(tokens.bot),
        oidc_issuer=f"{KEYCLOAK_URL}/realms/lakeloom",
        oidc_jwks_url=f"{KEYCLOAK_URL}/realms/lakeloom/protocol/openid-connect/certs",
        public_url=lakeloom_url,  # pyright: ignore[reportArgumentType]
        scope_group_map=GROUP_MAP,
        db_url=f"sqlite:///{tmp_path / 'state.db'}",
        data_dir=tmp_path,
        rate_limit_burst=5,
        rate_limit_per_minute=1,
        git_remote=None,
        reconcile_interval_seconds=0,
        drift_interval_seconds=0,
    )
    assert om_url == OM_URL
    responses = await asyncio.to_thread(_rate_limited_calls, settings, tokens.agent, tokens.alice)
    *agent, alice = responses
    assert [r.status_code for r in agent] == [200] * 5 + [429], [r.text[:300] for r in agent]
    limited = agent[-1]
    retry_after = limited.retry_after
    assert retry_after is not None and int(retry_after) >= 1, retry_after
    assert limited.json() == {
        "error": {
            "code": "rate_limited",
            "message": "rate limit exceeded for this principal",
            "hint": f"retry after {retry_after} s",
        }
    }
    assert alice.status_code == 200, alice.text[:300]


def _rpc(method: str, rid: int | None, params: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if rid is not None:
        body["id"] = rid
    if params is not None:
        body["params"] = params
    return body


def _mcp_headers(token: str, sid: str | None = None) -> httpx.Headers:
    headers = _bearer(token)
    headers["Accept"] = "application/json, text/event-stream"
    headers["Content-Type"] = "application/json"
    if sid is not None:
        headers[SESSION_HEADER] = sid
        headers["mcp-protocol-version"] = PROTOCOL_VERSION
    return headers


async def test_m4a_mcp_session_foreign_principal_refused(lakeloom_url: str, tokens: Tokens) -> None:
    init = _rpc(
        "initialize",
        1,
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "m4a-acceptance", "version": "0"},
        },
    )
    async with httpx.AsyncClient(base_url=lakeloom_url, timeout=30) as http:
        opened = await http.post("/mcp", json=init, headers=_mcp_headers(tokens.agent))
        assert opened.status_code == 200, opened.text[:500]
        sid = opened.headers[SESSION_HEADER]
        try:
            ready = _rpc("notifications/initialized", None)
            r = await http.post("/mcp", json=ready, headers=_mcp_headers(tokens.agent, sid))
            assert r.status_code == 202, r.text[:500]

            # alice replays the agent's session id: refused as an unknown session
            listing = _rpc("tools/list", 2)
            foreign = await http.post("/mcp", json=listing, headers=_mcp_headers(tokens.alice, sid))
            unknown = await http.post(
                "/mcp", json=listing, headers=_mcp_headers(tokens.alice, "never-issued")
            )
            assert foreign.status_code == 404, foreign.text[:500]
            assert foreign.json() == unknown.json() == UNKNOWN_SESSION

            # the agent's own session keeps working
            own = await http.post("/mcp", json=listing, headers=_mcp_headers(tokens.agent, sid))
            assert own.status_code == 200, own.text[:500]
            assert "get_server_info" in own.text
        finally:
            await http.delete("/mcp", headers=_mcp_headers(tokens.agent, sid))
