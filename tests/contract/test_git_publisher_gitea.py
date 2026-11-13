"""GitPublisher against the dev Gitea, in process (FR-GIT-01, FR-GIT-04, FR-GIT-05, FR-GIT-09).

Each test builds the publisher with :func:`~lakeloom.gitstore.wiring.build_publisher` (real
``git`` over HTTPS-style basic auth through ``GIT_ASKPASS``, the real Gitea forge client, a
SQLite outbox in ``tmp_path``) against a throwaway private repository that is created through
the Gitea API and deleted in a ``finally``. The snapshots are synthetic: no OpenMetadata.
Credentials are the fixed DEV ONLY Gitea admin of the compose stack; none is printed.
"""

import base64
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from lakeloom.config import Settings
from lakeloom.gitstore.layout import Snapshot
from lakeloom.gitstore.publisher import INITIAL_MESSAGE, GitPublisher
from lakeloom.gitstore.wiring import build_publisher
from lakeloom.state.db import create_engine, upgrade
from lakeloom.state.publications import Publication, PublicationRepository, new_publication_id

pytestmark = pytest.mark.contract

GITEA = "http://localhost:3000"
API = f"{GITEA}/api/v1"
USER = "lakeloom-admin"
PASSWORD = "lakeloom-admin"  # noqa: S105 - DEV ONLY fixed public compose credential
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
TTL = (
    "@prefix ex: <http://example.invalid/shop#> .\n\n"
    "ex:Order a <http://www.w3.org/2002/07/owl#Class> .\n"
)
EXPORTS = {"ontology.ttl": TTL, "ontology.jsonld": "{}\n", "ontology.owl": "<rdf:RDF/>\n"}


@contextmanager
def throwaway_repo() -> Iterator[str]:
    """A private, empty repository of the Gitea admin; deleted afterwards."""
    name = f"contract-pub-{uuid.uuid4().hex[:10]}"
    try:
        r = httpx.post(
            f"{API}/user/repos",
            auth=(USER, PASSWORD),
            json={"name": name, "auto_init": False, "default_branch": "main", "private": True},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        pytest.skip(f"Gitea not reachable at {GITEA}: {exc!r}")
    assert r.status_code == 201, r.text
    try:
        yield name
    finally:
        httpx.delete(f"{API}/repos/{USER}/{name}", auth=(USER, PASSWORD), timeout=10)


@pytest_asyncio.fixture
async def outbox(tmp_path: Path) -> AsyncIterator[PublicationRepository]:
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    await upgrade(engine)
    try:
        yield PublicationRepository(engine)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


def settings_for(tmp_path: Path, repo: str, *, mode: str, token: str = PASSWORD) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        git_remote=f"{GITEA}/{USER}/{repo}.git",
        git_branch="main",
        git_mode=mode,
        git_username=USER,
        git_token=token,
        forge="gitea",
        forge_api_url=API,
        data_dir=tmp_path / "data",
    )


def publication(proposal_id: str, *, model: str = "shop", mode: str = "direct") -> Publication:
    pub_id = new_publication_id()
    message = (
        f"ontology({model}): add_concept x1\n\n- add_concept:Order\n\n"
        f"Lakeloom-Proposal: {proposal_id}\n"
        "Lakeloom-ChangeSet: cs-1\n"
        "Approved-By: alice\n"
        "Lake-Source-Version: lakeloom_fixture.retail.sales.orders@3\n"
        f"Lakeloom-Publication: {pub_id}\n"
    )
    snap = Snapshot(
        model=model,
        glossary_id="g-1",
        glossary_fqn=model,
        base_iri="http://example.invalid/shop#",
        model_version="0.2",
        om_version="1.12.0",
        exports=EXPORTS,  # type: ignore[arg-type]
        last_change_set="cs-1",
        proposal_id=proposal_id,
        provenance={"publication_id": pub_id, "proposal_id": proposal_id},
        message=message,
    )
    return Publication(
        id=pub_id,
        model=model,
        proposal_id=proposal_id,
        mode=mode,  # type: ignore[arg-type]
        created_at=NOW,
        updated_at=NOW,
        payload=snap,
    )


def _api(path: str, **params: Any) -> Any:
    r = httpx.get(f"{API}/repos/{USER}/{path}", params=params, auth=(USER, PASSWORD), timeout=10)
    r.raise_for_status()
    return r.json()


def commits(repo: str, ref: str) -> list[dict[str, Any]]:
    """Commits of ``ref``, newest first.

    Right after the first push into an empty repository Gitea may still answer 409 (its
    "repository is empty" flag is updated asynchronously); retry briefly.
    """
    params = {"sha": ref, "limit": 50, "stat": "false"}
    url = f"{API}/repos/{USER}/{repo}/commits"
    r = httpx.get(url, params=params, auth=(USER, PASSWORD), timeout=10)
    for _ in range(40):
        if r.status_code != 409:
            break
        time.sleep(0.25)
        r = httpx.get(url, params=params, auth=(USER, PASSWORD), timeout=10)
    r.raise_for_status()
    return list(r.json())


def content(repo: str, path: str, ref: str) -> bytes:
    body = _api(f"{repo}/contents/{path}", ref=ref)
    return base64.b64decode(body["content"])


def trailers(message: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in message.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key and " " not in key and not key.startswith("-"):
            out.setdefault(key, []).append(value.strip())
    return out


def open_pulls(repo: str) -> list[dict[str, Any]]:
    return list(_api(f"{repo}/pulls", state="open"))


async def drain(publisher: GitPublisher) -> list[Publication]:
    return await publisher.process(now=NOW)


async def test_direct_mode_on_gitea(
    tmp_path: Path, outbox: PublicationRepository, http: httpx.AsyncClient
) -> None:
    with throwaway_repo() as repo:
        publisher = build_publisher(settings_for(tmp_path, repo, mode="direct"), outbox, http)
        assert publisher.enabled
        pub = await publisher.enqueue(publication("prop_direct"))
        [done] = await drain(publisher)
        assert done.status == "done", done.last_error
        assert done.branch == "main" and done.commit_sha

        history = commits(repo, "main")
        # The empty repository got the initial commit first, then the publication.
        assert [c["commit"]["message"].splitlines()[0] for c in history] == [
            "ontology(shop): add_concept x1",
            INITIAL_MESSAGE,
        ]
        assert history[0]["sha"] == done.commit_sha
        found = trailers(history[0]["commit"]["message"])
        assert found["Lakeloom-Proposal"] == ["prop_direct"]
        assert found["Approved-By"] == ["alice"]
        assert found["Lakeloom-ChangeSet"] == ["cs-1"]
        assert found["Lake-Source-Version"] == ["lakeloom_fixture.retail.sales.orders@3"]
        assert found["Lakeloom-Publication"] == [pub.id]
        assert history[0]["commit"]["author"]["name"] == "Lakeloom"

        assert content(repo, "ontologies/shop/ontology.ttl", "main") == TTL.encode()
        manifest = content(repo, "ontologies/shop/manifest.yaml", "main").decode()
        assert 'model: "shop"' in manifest
        provenance = content(repo, "provenance/shop.jsonl", "main").decode()
        assert pub.id in provenance and len(provenance.splitlines()) == 1

        # A second publication is one more commit on top (no re-initialisation).
        await publisher.enqueue(publication("prop_direct_2"))
        [second] = await drain(publisher)
        assert second.status == "done", second.last_error
        assert len(commits(repo, "main")) == 3
        assert await publisher.status() == {
            "enabled": True,
            "mode": "direct",
            "pending": 0,
            "blocked": 0,
            "last_error": None,
        }


async def test_pull_request_mode_on_gitea(
    tmp_path: Path, outbox: PublicationRepository, http: httpx.AsyncClient
) -> None:
    with throwaway_repo() as repo:
        publisher = build_publisher(settings_for(tmp_path, repo, mode="pull_request"), outbox, http)
        await publisher.enqueue(publication("prop_pr", mode="pull_request"))
        [done] = await drain(publisher)
        assert done.status == "done", done.last_error
        branch = "lakeloom/shop/prop_pr"
        assert done.branch == branch
        assert done.pr_url == f"{GITEA}/{USER}/{repo}/pulls/1"

        [pr] = open_pulls(repo)
        assert pr["html_url"] == done.pr_url
        assert pr["head"]["ref"] == branch and pr["base"]["ref"] == "main"
        assert pr["title"] == "ontology(shop): add_concept x1"
        assert "Lakeloom-Proposal: prop_pr" in pr["body"]
        # main holds only the initial commit; the branch holds the publication on top.
        assert [c["commit"]["message"].splitlines()[0] for c in commits(repo, "main")] == [
            INITIAL_MESSAGE
        ]
        head = commits(repo, branch)
        assert head[0]["sha"] == done.commit_sha and len(head) == 2
        assert content(repo, "ontologies/shop/ontology.ttl", branch) == TTL.encode()


async def test_reused_pr_on_gitea(
    tmp_path: Path, outbox: PublicationRepository, http: httpx.AsyncClient
) -> None:
    """A retry after the PR was opened (e.g. a crash before the row was recorded) finds the
    commit by its trailer and reuses the open PR (Gitea's 409 → list open PRs by head)."""
    with throwaway_repo() as repo:
        publisher = build_publisher(settings_for(tmp_path, repo, mode="pull_request"), outbox, http)
        await publisher.enqueue(publication("prop_again", mode="pull_request"))
        [first] = await drain(publisher)
        assert first.status == "done", first.last_error

        # Back to pending, as if the result had never been stored.
        await outbox.update(first.model_copy(update={"status": "pending", "pr_url": None}))
        [again] = await drain(publisher)
        assert again.status == "done", again.last_error
        assert again.pr_url == first.pr_url
        assert again.commit_sha == first.commit_sha
        assert [p["html_url"] for p in open_pulls(repo)] == [first.pr_url]
        assert len(commits(repo, "lakeloom/shop/prop_again")) == 2  # no second commit


async def test_wrong_token_blocks_on_gitea(
    tmp_path: Path, outbox: PublicationRepository, http: httpx.AsyncClient
) -> None:
    wrong = "wrong-dev-password-Q7"  # deliberately invalid
    with throwaway_repo() as repo:
        settings = settings_for(tmp_path, repo, mode="direct", token=wrong)
        publisher = build_publisher(settings, outbox, http)
        await publisher.enqueue(publication("prop_denied"))
        [blocked] = await drain(publisher)
        assert blocked.status == "blocked"
        assert blocked.attempts == 1 and blocked.next_attempt_at is None
        assert blocked.last_error and "authentication failed" in blocked.last_error.lower()
        assert wrong not in blocked.last_error
        status = await publisher.status()
        assert (status["pending"], status["blocked"]) == (0, 1)
        assert await drain(publisher) == []  # a blocked head stops the queue
        assert commits_or_empty(repo) == []


def commits_or_empty(repo: str) -> list[dict[str, Any]]:
    r = httpx.get(f"{API}/repos/{USER}/{repo}/commits", auth=(USER, PASSWORD), timeout=10)
    return [] if r.status_code in (404, 409) else list(r.json())
