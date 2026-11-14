"""GitPublisher: the outbox worker against a local bare remote (FR-GIT-04/05/09)."""

import hashlib
import json
import subprocess
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from lakeloom import metrics
from lakeloom.config import Settings
from lakeloom.gitstore import GitError, GitRunner, PullRequest, Snapshot, WorkingCopy
from lakeloom.gitstore.publisher import INITIAL_README, GitPublisher, backoff_seconds
from lakeloom.state.db import create_engine, upgrade
from lakeloom.state.publications import Publication, PublicationRepository, new_publication_id

NOW = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)
EXPORTS = {
    "ontology.ttl": "@prefix ex: <http://e/> .\n",
    "ontology.jsonld": "{}\n",
    "ontology.owl": "<rdf/>\n",
}
TOKEN = "tok-Zx81-secret-value"  # noqa: S105
PushForeign = Callable[[str, dict[str, str]], None]
Git = Callable[..., str]


class FakeForge:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.fail: list[GitError] = []

    async def open_pull_request(
        self, *, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        self.calls.append({"head": head, "base": base, "title": title, "body": body})
        if self.fail:
            raise self.fail.pop(0)
        return PullRequest(number=len(self.calls), url=f"http://forge/pulls/{len(self.calls)}")


class FlakyRunner(GitRunner):
    """Raises ``fail`` for git subcommands named in ``on`` while set."""

    fail: GitError | None = None
    on: tuple[str, ...] = ("push",)

    async def run(self, *args: str, cwd: Path | None = None, input: bytes | None = None) -> str:
        sub = next((a for a in args if not a.startswith("-") and "=" not in a), "")
        if self.fail is not None and sub in self.on:
            raise self.fail
        return await super().run(*args, cwd=cwd, input=input)


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    await upgrade(eng)
    yield eng
    await eng.dispose()


@pytest.fixture
def pubs(engine: AsyncEngine) -> PublicationRepository:
    return PublicationRepository(engine)


@pytest.fixture
def settings(make_settings: Callable[..., Settings], remote_url: str) -> Settings:
    return make_settings(git_remote=remote_url, git_branch="main", git_token=TOKEN)


@pytest.fixture
def flaky(settings: Settings, tmp_path: Path) -> FlakyRunner:
    return FlakyRunner(settings, tmp_path / "work")


@pytest.fixture
def work(flaky: FlakyRunner, tmp_path: Path, remote_url: str) -> WorkingCopy:
    return WorkingCopy(flaky, tmp_path / "work" / "repo", remote_url)


@pytest.fixture
def forge() -> FakeForge:
    return FakeForge()


@pytest.fixture
def publisher(
    settings: Settings, pubs: PublicationRepository, work: WorkingCopy, forge: FakeForge
) -> GitPublisher:
    return GitPublisher(settings, pubs, work, forge, clock=lambda: NOW)


def make_pub(
    proposal_id: str = "prop_1",
    *,
    model: str = "shop",
    mode: str = "direct",
    exports: dict[str, str] | None = EXPORTS,
) -> Publication:
    pub_id = new_publication_id()
    message = (
        f"ontology({model}): add_concept x1\n\n- add_concept:Order\n\n"
        f"Lakeloom-Proposal: {proposal_id}\nApproved-By: alice\n"
        f"Lakeloom-Publication: {pub_id}\n"
    )
    snap = Snapshot(
        model=model,
        glossary_id="g1",
        glossary_fqn=model,
        model_version="0.1",
        om_version="1.0",
        exports=exports,  # type: ignore[arg-type]
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


def clone(git_cmd: Git, tmp_path: Path, remote_url: str, branch: str = "main") -> Path:
    dest = tmp_path / f"check-{len(list(tmp_path.glob('check-*')))}"
    git_cmd("clone", "-b", branch, remote_url, str(dest), cwd=tmp_path)
    return dest


def commits(git_cmd: Git, repo: Path, ref: str = "HEAD") -> list[str]:
    return git_cmd("log", "--format=%s", ref, cwd=repo).splitlines()


def counter(name: str, **labels: str) -> float:
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


async def test_direct_publish_commits_files_and_trailers(
    publisher: GitPublisher,
    pubs: PublicationRepository,
    push_foreign: PushForeign,
    git_cmd: Git,
    tmp_path: Path,
    remote_url: str,
) -> None:
    push_foreign("seed", {"README.md": "old\n"})
    pub = await publisher.enqueue(make_pub())
    done_before = counter("lakeloom_git_publications_total", result="done")
    [result] = await publisher.process()
    assert result.status == "done" and result.branch == "main" and result.pr_url is None
    assert counter("lakeloom_git_publications_total", result="done") == done_before + 1
    stored = await pubs.for_proposal("prop_1")
    assert stored is not None and stored.status == "done" and stored.commit_sha == result.commit_sha
    repo = clone(git_cmd, tmp_path, remote_url)
    assert git_cmd("rev-parse", "HEAD", cwd=repo).strip() == result.commit_sha
    assert (repo / "ontologies/shop/ontology.ttl").read_text() == EXPORTS["ontology.ttl"]
    assert (repo / "ontologies/shop/ontology.jsonld").read_text() == EXPORTS["ontology.jsonld"]
    assert (repo / "ontologies/shop/ontology.owl").read_text() == EXPORTS["ontology.owl"]
    assert 'last_proposal: "prop_1"' in (repo / "ontologies/shop/manifest.yaml").read_text()
    prov = (repo / "provenance/shop.jsonl").read_text().splitlines()
    assert [json.loads(line)["publication_id"] for line in prov] == [pub.id]
    assert "| shop |" in (repo / "README.md").read_text()
    body = git_cmd("log", "-1", "--format=%B", cwd=repo)
    parsed = subprocess.run(  # noqa: ASYNC221
        ["git", "interpret-trailers", "--parse"],  # noqa: S607
        input=body,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "Lakeloom-Proposal: prop_1" in parsed
    assert f"Lakeloom-Publication: {pub.id}" in parsed
    assert "Approved-By: alice" in parsed
    assert git_cmd("log", "-1", "--format=%an <%ae>", cwd=repo).strip() == (
        "Lakeloom <lakeloom@localhost.invalid>"
    )
    assert await publisher.process() == []


async def test_pull_request_mode_pushes_branch_and_opens_pr(
    publisher: GitPublisher,
    forge: FakeForge,
    push_foreign: PushForeign,
    git_cmd: Git,
    tmp_path: Path,
    remote_url: str,
) -> None:
    push_foreign("seed", {"README.md": "old\n"})
    await publisher.enqueue(make_pub(mode="pull_request"))
    [result] = await publisher.process()
    branch = "lakeloom/shop/prop_1"
    assert result.status == "done" and result.branch == branch
    assert result.pr_url == "http://forge/pulls/1"
    assert forge.calls == [
        {
            "head": branch,
            "base": "main",
            "title": "ontology(shop): add_concept x1",
            "body": result.payload.message.split("\n", 1)[1].lstrip("\n"),
        }
    ]
    assert forge.calls[0]["body"].startswith("- add_concept:Order\n")
    assert "Lakeloom-Publication:" in forge.calls[0]["body"]
    main = clone(git_cmd, tmp_path, remote_url)
    assert commits(git_cmd, main) == ["seed"]
    pr = clone(git_cmd, tmp_path, remote_url, branch)
    assert git_cmd("rev-parse", "HEAD", cwd=pr).strip() == result.commit_sha
    assert (pr / "ontologies/shop/ontology.ttl").exists()


async def test_pull_request_reuses_existing_branch_with_trailer(
    publisher: GitPublisher,
    pubs: PublicationRepository,
    forge: FakeForge,
    push_foreign: PushForeign,
    git_cmd: Git,
    tmp_path: Path,
    remote_url: str,
) -> None:
    push_foreign("seed", {"README.md": "old\n"})
    await publisher.enqueue(make_pub(mode="pull_request"))
    forge.fail = [GitError("network", "forge 502")]
    [first] = await publisher.process()
    assert first.status == "pending" and first.attempts == 1
    [second] = await publisher.process(now=NOW + timedelta(hours=1))
    assert second.status == "done" and second.pr_url == "http://forge/pulls/2"
    pr = clone(git_cmd, tmp_path, remote_url, "lakeloom/shop/prop_1")
    assert commits(git_cmd, pr) == ["ontology(shop): add_concept x1", "seed"]
    assert len(forge.calls) == 2


async def test_empty_remote_gets_initial_commit_then_pr(
    publisher: GitPublisher, git_cmd: Git, tmp_path: Path, remote_url: str
) -> None:
    await publisher.enqueue(make_pub(mode="pull_request"))
    [result] = await publisher.process()
    assert result.status == "done"
    main = clone(git_cmd, tmp_path, remote_url)
    assert commits(git_cmd, main) == ["chore: initialise ontology repository"]
    assert (
        (main / "README.md").read_bytes()
        == INITIAL_README
        == b"# Ontologies\n\nGenerated by Lakeloom.\n"
    )
    assert sorted(p.name for p in main.iterdir() if p.name != ".git") == ["README.md"]
    pr = clone(git_cmd, tmp_path, remote_url, "lakeloom/shop/prop_1")
    assert commits(git_cmd, pr) == [
        "ontology(shop): add_concept x1",
        "chore: initialise ontology repository",
    ]


async def test_empty_remote_direct_mode(
    publisher: GitPublisher, git_cmd: Git, tmp_path: Path, remote_url: str
) -> None:
    await publisher.enqueue(make_pub())
    [result] = await publisher.process()
    assert result.status == "done"
    main = clone(git_cmd, tmp_path, remote_url)
    assert commits(git_cmd, main) == [
        "ontology(shop): add_concept x1",
        "chore: initialise ontology repository",
    ]


async def test_non_fast_forward_rerenders_on_new_tip(
    publisher: GitPublisher,
    work: WorkingCopy,
    push_foreign: PushForeign,
    git_cmd: Git,
    tmp_path: Path,
    remote_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    push_foreign("seed", {"README.md": "old\n"})
    pub = await publisher.enqueue(make_pub())
    real_commit = work.commit
    calls = 0

    async def commit_then_race(message: str) -> str:
        nonlocal calls
        sha = await real_commit(message)
        calls += 1
        if calls == 1:  # someone else pushes between our commit and our push
            push_foreign("foreign", {"other.txt": "x\n"})
        return sha

    monkeypatch.setattr(work, "commit", commit_then_race)
    [result] = await publisher.process()
    assert result.status == "done" and calls == 2
    main = clone(git_cmd, tmp_path, remote_url)
    assert commits(git_cmd, main) == ["ontology(shop): add_concept x1", "foreign", "seed"]
    assert (main / "other.txt").read_text() == "x\n"
    lines = (main / "provenance/shop.jsonl").read_text().splitlines()
    assert [json.loads(line)["publication_id"] for line in lines] == [pub.id]


async def test_rejected_three_times_is_transient(
    publisher: GitPublisher, flaky: FlakyRunner, push_foreign: PushForeign
) -> None:
    push_foreign("seed", {"README.md": "old\n"})
    await publisher.enqueue(make_pub())
    flaky.fail = GitError("rejected", "! [rejected] main -> main (fetch first)")
    [result] = await publisher.process()
    assert result.status == "pending" and result.attempts == 1
    assert result.next_attempt_at == NOW + timedelta(seconds=60)


async def test_push_failure_keeps_publication_and_retry_commits_once(
    publisher: GitPublisher,
    pubs: PublicationRepository,
    flaky: FlakyRunner,
    push_foreign: PushForeign,
    git_cmd: Git,
    tmp_path: Path,
    remote_url: str,
) -> None:
    push_foreign("seed", {"README.md": "old\n"})
    await publisher.enqueue(make_pub())
    failures = counter("lakeloom_git_push_failures_total")
    flaky.fail = GitError("network", "fatal: unable to access: Could not resolve host: forge")
    [first] = await publisher.process()
    assert first.status == "pending" and first.attempts == 1
    assert first.last_error is not None and "network" in first.last_error
    assert counter("lakeloom_git_push_failures_total") == failures + 1
    assert commits(git_cmd, clone(git_cmd, tmp_path, remote_url)) == ["seed"]
    assert await publisher.process() == []  # not yet due
    flaky.fail = None  # remote restored
    [second] = await publisher.process(now=NOW + timedelta(seconds=61))
    assert second.status == "done" and second.attempts == 1
    assert commits(git_cmd, clone(git_cmd, tmp_path, remote_url)) == [
        "ontology(shop): add_concept x1",
        "seed",
    ]


async def test_unreachable_remote_dir_then_restored(
    publisher: GitPublisher,
    pubs: PublicationRepository,
    push_foreign: PushForeign,
    bare: Path,
    git_cmd: Git,
    tmp_path: Path,
    remote_url: str,
) -> None:
    push_foreign("seed", {"README.md": "old\n"})
    await publisher.enqueue(make_pub())
    moved = bare.with_name("away.git")
    bare.rename(moved)  # noqa: ASYNC240
    [first] = await publisher.process()
    assert first.status == "blocked"  # file:// path gone reads as "repository not found"
    moved.rename(bare)
    assert await publisher.retry_blocked() == 1
    [second] = await publisher.process()
    assert second.status == "done"
    assert len(commits(git_cmd, clone(git_cmd, tmp_path, remote_url))) == 2


async def test_auth_failure_blocks_and_git_retry_requeues(
    publisher: GitPublisher,
    pubs: PublicationRepository,
    flaky: FlakyRunner,
    push_foreign: PushForeign,
) -> None:
    push_foreign("seed", {"README.md": "old\n"})
    await publisher.enqueue(make_pub())
    blocked_before = counter("lakeloom_git_publications_total", result="blocked")
    flaky.fail = GitError("auth", "fatal: Authentication failed for 'http://forge/x.git/'")
    [result] = await publisher.process()
    assert result.status == "blocked" and result.next_attempt_at is None
    assert result.last_error is not None and "auth" in result.last_error
    assert counter("lakeloom_git_publications_total", result="blocked") == blocked_before + 1
    assert await publisher.process(now=NOW + timedelta(days=1)) == []
    status = await publisher.status()
    assert status["blocked"] == 1 and status["pending"] == 0 and "auth" in status["last_error"]
    flaky.fail = None
    assert await publisher.retry_blocked() == 1
    [again] = await publisher.process()
    assert again.status == "done"
    assert (await publisher.status())["last_error"] is None


async def test_crash_after_push_is_detected_by_trailer(
    publisher: GitPublisher,
    pubs: PublicationRepository,
    work: WorkingCopy,
    push_foreign: PushForeign,
    git_cmd: Git,
    tmp_path: Path,
    remote_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    push_foreign("seed", {"README.md": "old\n"})
    pub = await publisher.enqueue(make_pub())
    real_update = pubs.update
    failed = False

    async def update_fails_once(p: Publication) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("database went away")
        await real_update(p)

    monkeypatch.setattr(pubs, "update", update_fails_once)
    await publisher.process()  # must not raise
    stored = await pubs.for_proposal("prop_1")
    assert stored is not None and stored.status == "pending"
    commit_calls = 0
    real_commit = work.commit

    async def counting_commit(message: str) -> str:
        nonlocal commit_calls
        commit_calls += 1
        return await real_commit(message)

    monkeypatch.setattr(work, "commit", counting_commit)
    [result] = await publisher.process()
    assert result.status == "done" and result.id == pub.id and commit_calls == 0
    main = clone(git_cmd, tmp_path, remote_url)
    assert commits(git_cmd, main) == ["ontology(shop): add_concept x1", "seed"]
    assert result.commit_sha == git_cmd("rev-parse", "HEAD", cwd=main).strip()


async def test_fifo_blocked_head_stops_queue(
    publisher: GitPublisher, pubs: PublicationRepository, push_foreign: PushForeign
) -> None:
    push_foreign("seed", {"README.md": "old\n"})
    await publisher.enqueue(make_pub("p1", model="Bad Slug"))  # cannot be rendered
    await publisher.enqueue(make_pub("p2"))
    [result] = await publisher.process()
    assert result.proposal_id == "p1" and result.status == "blocked"
    assert result.last_error is not None and "slug" in result.last_error
    assert await publisher.process() == []
    later = await pubs.for_proposal("p2")
    assert later is not None and later.status == "pending" and later.attempts == 0


async def test_queue_publishes_in_seq_order(
    publisher: GitPublisher,
    push_foreign: PushForeign,
    git_cmd: Git,
    tmp_path: Path,
    remote_url: str,
) -> None:
    push_foreign("seed", {"README.md": "old\n"})
    await publisher.enqueue(make_pub("p1"))
    await publisher.enqueue(make_pub("p2", model="crm"))
    results = await publisher.process()
    assert [(r.proposal_id, r.status) for r in results] == [("p1", "done"), ("p2", "done")]
    main = clone(git_cmd, tmp_path, remote_url)
    assert commits(git_cmd, main) == [
        "ontology(crm): add_concept x1",
        "ontology(shop): add_concept x1",
        "seed",
    ]
    readme = (main / "README.md").read_text()
    assert "| crm |" in readme and "| shop |" in readme


def test_backoff_schedule_values() -> None:
    assert [backoff_seconds(n) for n in range(1, 9)] == [60, 120, 240, 480, 960, 1920, 3600, 3600]


async def test_backoff_schedule(
    publisher: GitPublisher, flaky: FlakyRunner, push_foreign: PushForeign
) -> None:
    push_foreign("seed", {"README.md": "old\n"})
    await publisher.enqueue(make_pub())
    flaky.fail = GitError("timeout", "git push timed out")
    now = NOW
    seen: list[float] = []
    for _ in range(8):
        [r] = await publisher.process(now=now)
        assert r.status == "pending" and r.next_attempt_at is not None
        seen.append((r.next_attempt_at - now).total_seconds())
        assert await publisher.process(now=r.next_attempt_at - timedelta(seconds=1)) == []
        now = r.next_attempt_at
    assert seen == [60, 120, 240, 480, 960, 1920, 3600, 3600]


async def test_missing_exports_filled_at_publish(
    settings: Settings,
    pubs: PublicationRepository,
    work: WorkingCopy,
    forge: FakeForge,
    push_foreign: PushForeign,
    git_cmd: Git,
    tmp_path: Path,
    remote_url: str,
) -> None:
    push_foreign("seed", {"README.md": "old\n"})
    seen: list[Snapshot] = []

    async def exporter(snap: Snapshot) -> Snapshot:
        seen.append(snap)
        exports = {k: v.replace("{}", '{"late": true}') for k, v in EXPORTS.items()}
        return snap.model_copy(
            update={"exports": exports, "om_version": "2.0", "model_version": "0.2"}
        )

    pub_ = GitPublisher(settings, pubs, work, forge, exporter=exporter, clock=lambda: NOW)
    await pub_.enqueue(make_pub(exports=None))
    [result] = await pub_.process()
    assert result.status == "done" and len(seen) == 1
    assert result.payload.om_version == "2.0"
    assert result.payload.exports is None  # cleared once done (the commit holds them)
    main = clone(git_cmd, tmp_path, remote_url)
    assert (main / "ontologies/shop/ontology.jsonld").read_text() == '{"late": true}\n'
    assert 'om_version: "2.0"' in (main / "ontologies/shop/manifest.yaml").read_text()


async def test_missing_exports_without_exporter_or_failing_is_transient(
    settings: Settings, pubs: PublicationRepository, work: WorkingCopy, push_foreign: PushForeign
) -> None:
    push_foreign("seed", {"README.md": "old\n"})
    no_exporter = GitPublisher(settings, pubs, work, None, clock=lambda: NOW)
    await no_exporter.enqueue(make_pub(exports=None))
    [r] = await no_exporter.process()
    assert r.status == "pending" and r.attempts == 1

    async def broken(snap: Snapshot) -> Snapshot:
        raise ConnectionError("OM unreachable")

    failing = GitPublisher(settings, pubs, work, None, exporter=broken, clock=lambda: NOW)
    [r2] = await failing.process(now=NOW + timedelta(hours=1))
    assert r2.status == "pending" and r2.attempts == 2
    assert r2.last_error is not None and "OM unreachable" in r2.last_error


async def test_last_error_redacted(
    settings: Settings, pubs: PublicationRepository, work: WorkingCopy, push_foreign: PushForeign
) -> None:
    push_foreign("seed", {"README.md": "old\n"})

    async def leaky(snap: Snapshot) -> Snapshot:
        raise RuntimeError(f"GET https://bot:{TOKEN}@om.example/api failed {TOKEN} " + "z" * 900)

    p = GitPublisher(settings, pubs, work, None, exporter=leaky, clock=lambda: NOW)
    await p.enqueue(make_pub(exports=None))
    [r] = await p.process()
    assert r.last_error is not None
    assert TOKEN not in r.last_error and "bot:" not in r.last_error
    assert len(r.last_error) <= 500
    stored = await pubs.for_proposal("prop_1")
    assert stored is not None and stored.last_error == r.last_error
    assert TOKEN not in json.dumps(await p.status())


async def test_duplicate_proposal_enqueue_returns_existing(
    publisher: GitPublisher, pubs: PublicationRepository
) -> None:
    first = await publisher.enqueue(make_pub())
    again = await publisher.enqueue(make_pub())
    assert again.id == first.id and again.seq == first.seq
    assert first.repo == publisher.repo_key
    assert len(await pubs.list()) == 1


async def test_repo_key_is_sha256_of_remote(publisher: GitPublisher, remote_url: str) -> None:
    assert publisher.enabled
    assert publisher.repo_key == hashlib.sha256(remote_url.encode()).hexdigest()


async def test_disabled_when_no_remote(
    make_settings: Callable[..., Settings], pubs: PublicationRepository
) -> None:
    p = GitPublisher(make_settings(), pubs, None, None)
    assert not p.enabled and p.repo_key == ""
    out = await p.enqueue(make_pub())
    assert out.status == "pending"
    assert await pubs.list() == []
    assert await p.process() == []
    assert await p.retry_blocked() == 0
    status = await p.status()
    assert status == {
        "enabled": False,
        "mode": "pull_request",
        "pending": 0,
        "blocked": 0,
        "last_error": None,
    }


async def test_pull_request_without_forge_blocks(
    settings: Settings, pubs: PublicationRepository, work: WorkingCopy, push_foreign: PushForeign
) -> None:
    push_foreign("seed", {"README.md": "old\n"})
    p = GitPublisher(settings, pubs, work, None, clock=lambda: NOW)
    await p.enqueue(make_pub(mode="pull_request"))
    [r] = await p.process()
    assert r.status == "blocked" and r.last_error is not None and "forge" in r.last_error


async def test_process_survives_unexpected_errors(
    publisher: GitPublisher, pubs: PublicationRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    await publisher.enqueue(make_pub())

    async def boom(repo: str) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(pubs, "head", boom)
    assert await publisher.process() == []


# --- final review fixes --------------------------------------------------------------------


async def test_done_row_drops_its_exports(
    publisher: GitPublisher, pubs: PublicationRepository, push_foreign: PushForeign
) -> None:
    push_foreign("seed", {"README.md": "old\n"})
    await publisher.enqueue(make_pub())
    [result] = await publisher.process()
    assert result.status == "done" and result.payload.exports is None
    stored = await pubs.for_proposal("prop_1")
    assert stored is not None and stored.status == "done"
    assert stored.payload.exports is None  # the store does not keep three exports per row
    assert stored.payload.message and stored.payload.provenance  # the rest is kept


async def test_pull_request_merged_and_deleted_before_done_is_detected(
    publisher: GitPublisher,
    pubs: PublicationRepository,
    forge: FakeForge,
    work: WorkingCopy,
    push_foreign: PushForeign,
    git_cmd: Git,
    tmp_path: Path,
    remote_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    push_foreign("seed", {"README.md": "old\n"})
    pub = await publisher.enqueue(make_pub(mode="pull_request"))
    real_update = pubs.update
    failed = False

    async def update_fails_once(p: Publication) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("database went away")
        await real_update(p)

    monkeypatch.setattr(pubs, "update", update_fails_once)
    await publisher.process()  # branch pushed and PR opened, but the row stays pending
    stored = await pubs.for_proposal("prop_1")
    assert stored is not None and stored.status == "pending" and len(forge.calls) == 1
    branch = "lakeloom/shop/prop_1"
    # A human merges the PR and the forge deletes its branch.
    other = clone(git_cmd, tmp_path, remote_url)
    git_cmd("fetch", "origin", branch, cwd=other)
    git_cmd("merge", "--no-ff", "-m", "Merge PR 1", "FETCH_HEAD", cwd=other)
    git_cmd("push", "origin", "main", cwd=other)
    git_cmd("push", "origin", "--delete", branch, cwd=other)
    pr_commit = git_cmd("rev-parse", "HEAD^2", cwd=other).strip()
    commit_calls = 0
    real_commit = work.commit

    async def counting_commit(message: str) -> str:
        nonlocal commit_calls
        commit_calls += 1
        return await real_commit(message)

    monkeypatch.setattr(work, "commit", counting_commit)

    [result] = await publisher.process()

    assert result.status == "done" and result.id == pub.id
    assert result.commit_sha == pr_commit and result.branch == "main"
    assert commit_calls == 0 and len(forge.calls) == 1  # no second commit, no second PR
    assert await work.remote_head(branch) is None  # the branch was not pushed again
    main = clone(git_cmd, tmp_path, remote_url)
    log = commits(git_cmd, main)
    assert log[0] == "Merge PR 1"
    assert sorted(log[1:]) == ["ontology(shop): add_concept x1", "seed"]


async def test_corrupt_payload_blocks_visibly_and_status_never_raises(
    publisher: GitPublisher,
    pubs: PublicationRepository,
    engine: AsyncEngine,
    push_foreign: PushForeign,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import update

    from lakeloom.state.publications import PublicationRow

    push_foreign("seed", {"README.md": "old\n"})
    bad = await publisher.enqueue(make_pub("p1"))
    await publisher.enqueue(make_pub("p2"))
    async with engine.begin() as conn:
        await conn.execute(
            update(PublicationRow).where(PublicationRow.id == bad.id).values(payload={"bad": 1})
        )

    assert await publisher.process() == []  # the queue stops on the blocked head
    status = await publisher.status()
    assert status["blocked"] == 1 and status["pending"] == 1
    assert status["last_error"] == "invalid stored snapshot"
    later = await pubs.for_proposal("p2")
    assert later is not None and later.status == "pending" and later.attempts == 0

    async def down(repo: str) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(pubs, "counts", down)
    broken = await publisher.status()  # never raises
    assert broken["enabled"] is True and "unavailable" in str(broken["last_error"])
