"""Publish on apply: snapshot, enqueue and the inline attempt after approve (FR-GIT-04/05).

Reuses the review kit of ``test_review``. A :class:`FakePublisher` records what the review
hook enqueues; one test runs the real :class:`GitPublisher` against a local bare remote.
"""

import asyncio
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine
from test_proposals import (
    customers,
    engine_fixture,  # noqa: F401  (the engine fixture)
    widened,
)
from test_review import ALICE, ReviewKit, _error  # pyright: ignore[reportPrivateUsage]

from lakeloom.config import Settings
from lakeloom.core import review_git
from lakeloom.core.confirm import ConfirmTokens
from lakeloom.core.model_policy import ModelPolicyEntry, git_mode_for
from lakeloom.core.review import ReviewService
from lakeloom.errors import LakeloomError
from lakeloom.gitstore import GitRunner, WorkingCopy
from lakeloom.gitstore.layout import Snapshot
from lakeloom.gitstore.publisher import GitPublisher
from lakeloom.om.client import OMUnavailable
from lakeloom.om.models import OMCapabilities
from lakeloom.state.publications import Publication, PublicationRepository

SECRET = b"s" * 32
INTERNAL = "Git: queued (internal error); it is retried automatically"
NOT_QUEUED = "Git: not queued (internal error); an operator must re-publish it"


class FakePublisher:
    """Records enqueued publications; ``outcome`` is what ``process`` leaves on the row."""

    enabled = True
    repo_key = "k" * 64

    def __init__(self) -> None:
        self.rows: dict[str, Publication] = {}
        self.events: list[str] = []
        self.outcome: dict[str, Any] = {"status": "done", "commit_sha": "a" * 40}
        self.gate: asyncio.Event | None = None
        self.processed = 0
        self.processing = asyncio.Event()
        self.fail_enqueue: Exception | None = None
        self.fail_fill: Exception | None = None

    async def enqueue(self, pub: Publication) -> Publication:
        if self.fail_enqueue is not None:
            raise self.fail_enqueue
        self.events.append(f"enqueue:{pub.proposal_id}")
        self.rows[pub.proposal_id] = pub
        return pub

    async def fill_snapshot(self, pub_id: str, snap: Snapshot) -> bool:
        if self.fail_fill is not None:
            raise self.fail_fill
        for pid, pub in self.rows.items():
            if pub.id == pub_id and pub.status == "pending" and pub.attempts == 0:
                self.events.append(f"fill:{pid}")
                self.rows[pid] = pub.model_copy(update={"payload": snap})
                return True
        return False

    async def process(self, *, now: datetime | None = None) -> list[Publication]:
        self.processing.set()
        if self.gate is not None:
            await self.gate.wait()
        self.processed += 1
        out: list[Publication] = []
        for pid, pub in self.rows.items():
            if pub.status == "pending":
                self.rows[pid] = pub.model_copy(update=self.outcome)
                out.append(self.rows[pid])
        return out

    async def for_proposal(self, proposal_id: str) -> Publication | None:
        return self.rows.get(proposal_id)


def git_settings(**kw: Any) -> Settings:
    values: dict[str, Any] = {
        "public_url": "http://x",
        "git_remote": "file:///unused/remote.git",
        "git_mode": "direct",
        "forge": "none",
        "git_publish_timeout_seconds": 5,
        **kw,
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def with_git(
    kit: ReviewKit,
    publisher: Any,
    settings: Settings | None = None,
    policies: list[ModelPolicyEntry] | None = None,
) -> ReviewKit:
    """Rebuild the kit's review service with ``publisher`` (the OM fake reports 1.12.0)."""

    async def caps() -> OMCapabilities:
        return OMCapabilities(reachable=True, version="1.12.0")

    kit.om.get_capabilities = caps  # type: ignore[attr-defined]
    kit.review = ReviewService(
        kit.service,
        kit.repo,
        kit.obo,  # pyright: ignore[reportArgumentType]
        kit.models,
        ConfirmTokens(SECRET, 600),
        settings or git_settings(),
        kit.audit,
        policies or [],
        publisher=publisher,
    )
    return kit


def git_warnings(p: Any) -> list[str]:
    return [w for w in p.reviews[-1].warnings if w.startswith("Git:")]


# --- policies ---------------------------------------------------------------------------------


def test_git_mode_for_and_strict_policy() -> None:
    entries = [ModelPolicyEntry.model_validate({"name": "shop", "git": {"mode": "direct"}})]
    assert git_mode_for(entries, "shop", "pull_request") == "direct"
    assert git_mode_for(entries, "retail", "pull_request") == "pull_request"
    assert git_mode_for([ModelPolicyEntry(name="shop")], "shop", "direct") == "direct"
    with pytest.raises(ValidationError):
        ModelPolicyEntry.model_validate({"name": "shop", "git": {"mode": "squash"}})
    with pytest.raises(ValidationError):
        ModelPolicyEntry.model_validate({"name": "shop", "git": {"mode": "direct", "x": 1}})


# --- approve ---------------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(cwd)}
    env |= {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


async def test_approve_enqueues_snapshot_and_publishes_inline(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    bare = tmp_path / "remote.git"
    _git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    settings = git_settings(git_remote=f"file://{bare}", data_dir=tmp_path)
    runner = GitRunner(settings, tmp_path / "git")
    wc = WorkingCopy(runner, tmp_path / "git" / "repo", f"file://{bare}")
    repo = PublicationRepository(engine)
    kit = ReviewKit(engine)
    publisher = GitPublisher(settings, repo, wc, None)
    with_git(kit, publisher, settings)
    p = await kit.submitted()

    out = await kit.approve(p)

    assert out.status == "applied"
    assert git_warnings(out) == []
    pub = await repo.for_proposal(p.id)
    assert pub is not None and pub.status == "done" and pub.mode == "direct"
    assert pub.branch == "main" and pub.commit_sha
    snap = pub.payload
    assert snap.exports is None  # cleared once done; the commit holds them
    assert _git("ls-tree", "--name-only", "main", "ontologies/shop/", cwd=bare).split() == [
        f"ontologies/shop/{n}"
        for n in (
            "context.jsonld",
            "manifest.yaml",
            "ontology.jsonld",
            "ontology.owl",
            "ontology.ttl",
            "shapes.ttl",
        )
    ]
    assert snap.om_version == "1.12.0" and snap.model_version
    assert snap.glossary_id == "id-shop" and snap.proposal_id == p.id
    assert [t.fqn for t in snap.source_tables] == [t.fqn for t in p.provenance.tables]
    assert snap.last_change_set == out.reviews[-1].applied[-1]
    log = _git("log", "-1", "--format=%B", "main", cwd=bare)
    assert f"Lakeloom-Proposal: {p.id}" in log
    assert f"Lakeloom-Publication: {pub.id}" in log and "Approved-By: alice" in log
    assert pub.id.startswith("pub_")


async def test_git_failure_never_fails_approval(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakePublisher()
    fake.fail_enqueue = RuntimeError("boom https://u:tok-secret@git.example/r.git")
    kit = with_git(ReviewKit(engine), fake)
    p = await kit.submitted()

    with caplog.at_level("ERROR", logger="lakeloom.core.review_git"):
        out = await kit.approve(p)

    assert out.status == "applied"
    assert git_warnings(out) == [NOT_QUEUED]  # no row exists: never "queued"
    stored = await kit.repo.get(p.id)
    assert stored.status == "applied" and git_warnings(stored) == [NOT_QUEUED]
    assert any(r.levelname == "ERROR" and p.id in r.getMessage() for r in caplog.records)
    assert "tok-secret" not in caplog.text and "git.example" not in caplog.text
    assert fake.processed == 0


async def test_row_is_queued_before_any_openmetadata_read(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePublisher()
    kit = with_git(ReviewKit(engine), fake)
    p = await kit.submitted()
    real_export, real_get = kit.models.export, kit.models.get_model

    async def export(slug: str, fmt: Any) -> bytes:
        fake.events.append(f"om:export:{fmt}")
        return await real_export(slug, fmt)

    async def get_model(slug: str, **kw: Any) -> Any:
        fake.events.append("om:get_model")
        return await real_get(slug, **kw)

    monkeypatch.setattr(kit.models, "export", export)
    monkeypatch.setattr(kit.models, "get_model", get_model)
    reads_before = len(fake.events)

    out = await kit.approve(p)

    assert out.status == "applied" and git_warnings(out) == []
    events = fake.events[reads_before:]
    git_events = [e for e in events if not e.startswith("om:")]
    first_read = next(i for i, e in enumerate(events) if e.startswith("om:export"))
    assert events.index(f"enqueue:{p.id}") < first_read
    assert git_events == [f"enqueue:{p.id}", f"fill:{p.id}"]
    snap = fake.rows[p.id].payload
    assert snap.exports is not None and snap.om_version == "1.12.0" and snap.model_version


async def test_fill_failure_leaves_queued_row_without_exports(engine: AsyncEngine) -> None:
    fake = FakePublisher()
    fake.fail_fill = RuntimeError("db gone")
    fake.outcome = {"status": "pending", "attempts": 1, "last_error": "export failed: down"}
    kit = with_git(ReviewKit(engine), fake)
    p = await kit.submitted()

    out = await kit.approve(p)

    assert out.status == "applied"
    row = fake.rows[p.id]
    assert row.status == "pending" and row.payload.exports is None
    warnings = git_warnings(out)
    assert any("exported again" in w for w in warnings), warnings
    assert NOT_QUEUED not in warnings


async def test_process_raising_is_internal_warning(engine: AsyncEngine) -> None:
    fake = FakePublisher()

    async def boom(**_: Any) -> list[Publication]:
        raise RuntimeError("nope")

    fake.process = boom  # type: ignore[method-assign]
    kit = with_git(ReviewKit(engine), fake)
    out = await kit.approve(await kit.submitted())
    assert out.status == "applied" and git_warnings(out) == [INTERNAL]


async def test_export_failure_enqueues_snapshot_without_exports(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePublisher()
    fake.outcome = {"status": "pending", "attempts": 1, "last_error": "export failed: down"}
    kit = with_git(ReviewKit(engine), fake)
    p = await kit.submitted()

    async def down(slug: str, fmt: str) -> bytes:
        raise LakeloomError("upstream_unavailable", "OpenMetadata is down", "retry")

    monkeypatch.setattr(kit.models, "export", down)

    out = await kit.approve(p)

    assert out.status == "applied"
    row = fake.rows[p.id]
    assert row.status == "pending"  # queued before the export, which then failed
    snap = row.payload
    assert snap.exports is None and snap.om_version is None and snap.model_version == ""
    assert fake.events[-1] == f"enqueue:{p.id}"  # no fill: there was nothing to store
    assert f"Lakeloom-Proposal: {p.id}" in snap.message
    assert snap.provenance["proposal_id"] == p.id
    warnings = git_warnings(out)
    assert any("export" in w and "exported again" in w for w in warnings), warnings
    assert "Git: queued (export failed: down); it is retried automatically" in warnings


async def test_blocked_outcome_warns_operator(engine: AsyncEngine) -> None:
    fake = FakePublisher()
    fake.outcome = {
        "status": "blocked",
        "attempts": 1,
        "last_error": "git auth: https://u:tok@h/r.git refused",
    }
    kit = with_git(ReviewKit(engine), fake)
    out = await kit.approve(await kit.submitted())
    (warning,) = git_warnings(out)
    assert warning.startswith("Git: blocked (git auth: ")
    assert warning.endswith("); an operator must run lakeloom git retry")
    assert "tok" not in warning


async def test_inline_timeout_leaves_task_running(engine: AsyncEngine) -> None:
    fake = FakePublisher()
    fake.gate = asyncio.Event()
    kit = with_git(ReviewKit(engine), fake, git_settings(git_publish_timeout_seconds=0.05))
    p = await kit.submitted()

    out = await kit.approve(p)

    assert out.status == "applied"
    assert git_warnings(out) == ["Git: queued (timed out); it is retried automatically"]
    hook = kit.review._git  # pyright: ignore[reportPrivateUsage]
    assert hook is not None
    (task,) = hook.tasks
    assert not task.done()  # not cancelled by the timeout
    fake.gate.set()
    await asyncio.wait_for(task, 1)
    assert fake.processed == 1 and fake.rows[p.id].status == "done"
    assert hook.tasks == set()


async def test_snapshots_enqueued_in_apply_order(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePublisher()
    fake.outcome = {"status": "pending"}  # unattempted: the second fill is not pre-empted
    kit = with_git(ReviewKit(engine), fake)
    a = await kit.submitted(widened(1)[0])
    b = await kit.submitted(customers())
    real = review_git.take_snapshot

    async def slow(*args: Any, **kw: Any) -> Any:
        pid = args[2].id
        fake.events.append(f"snapshot:{pid}")
        await asyncio.sleep(0.02)
        return await real(*args, **kw)

    monkeypatch.setattr(review_git, "take_snapshot", slow)

    done = await asyncio.gather(kit.approve(a), kit.approve(b))

    assert [p.status for p in done] == ["applied", "applied"]
    ev = fake.events
    assert len(ev) == 6
    first = ev[0].split(":", 1)[1]
    second = ev[3].split(":", 1)[1]
    assert ev == [
        f"enqueue:{first}",
        f"snapshot:{first}",
        f"fill:{first}",
        f"enqueue:{second}",
        f"snapshot:{second}",
        f"fill:{second}",
    ]
    assert {first, second} == {a.id, b.id}


async def test_reject_and_request_changes_do_not_publish(engine: AsyncEngine) -> None:
    fake = FakePublisher()
    kit = with_git(ReviewKit(engine), fake)
    p1 = await kit.submitted(widened(1)[0])
    p2 = await kit.submitted(customers())
    preview = await kit.review.review(p1.id, "reject", principal=ALICE, client=None, reason="no")
    rejected = await kit.review.review(
        p1.id,
        "reject",
        principal=ALICE,
        client=None,
        reason="no",
        confirm_token=preview.confirm_token,  # type: ignore[union-attr]
    )
    changed = await kit.review.review(
        p2.id, "request_changes", principal=ALICE, client=None, reason="rename"
    )
    assert rejected.status == "rejected" and changed.status == "changes_requested"  # type: ignore[union-attr]
    assert fake.rows == {} and fake.processed == 0


async def test_apply_failed_does_not_publish(engine: AsyncEngine) -> None:
    fake = FakePublisher()
    kit = with_git(ReviewKit(engine), fake)
    p = await kit.submitted()
    cs = p.parts[0].change_set_ids["glossary"]
    kit.om.fail[f"apply:{cs}"] = OMUnavailable("down", 503)
    preview = await kit.review.review(p.id, "approve", principal=ALICE, client=None)
    err = await _error(
        kit.review.review(
            p.id,
            "approve",
            principal=ALICE,
            client=None,
            confirm_token=preview.confirm_token,  # type: ignore[union-attr]
        )
    )
    assert err.code == "upstream_unavailable"
    assert (await kit.repo.get(p.id)).status == "apply_failed"
    assert fake.rows == {} and fake.processed == 0


async def test_per_model_git_mode_override(engine: AsyncEngine) -> None:
    fake = FakePublisher()
    policies = [ModelPolicyEntry.model_validate({"name": "shop", "git": {"mode": "direct"}})]
    kit = with_git(
        ReviewKit(engine, policies=policies),
        fake,
        git_settings(git_mode="pull_request", forge="gitea"),
        policies,
    )
    p = await kit.submitted()
    await kit.approve(p)
    assert fake.rows[p.id].mode == "direct"
    assert fake.rows[p.id].repo == fake.repo_key

    fake2 = FakePublisher()
    kit2 = with_git(ReviewKit(engine), fake2, git_settings(git_mode="pull_request", forge="gitea"))
    p2 = await kit2.submitted(widened(1)[0])
    await kit2.approve(p2)
    assert fake2.rows[p2.id].mode == "pull_request"


async def test_disabled_publisher_is_a_no_op(engine: AsyncEngine) -> None:
    repo = PublicationRepository(engine)
    publisher = GitPublisher(Settings(_env_file=None), repo, None, None)  # type: ignore[call-arg]
    kit = with_git(ReviewKit(engine), publisher)
    p = await kit.submitted()
    out = await kit.approve(p)
    assert out.status == "applied" and git_warnings(out) == []
    assert await repo.for_proposal(p.id) is None


# --- cancellation (FR-GIT-09: a publication is never lost) -----------------------------------


async def _confirming(kit: ReviewKit, p: Any) -> "asyncio.Task[Any]":
    preview = await kit.review.review(p.id, "approve", principal=ALICE, client="pytest")
    return asyncio.create_task(
        kit.review.review(
            p.id,
            "approve",
            principal=ALICE,
            client="pytest",
            confirm_token=preview.confirm_token,  # type: ignore[union-attr]
        )
    )


async def test_cancel_during_snapshot_still_enqueues(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePublisher()
    kit = with_git(ReviewKit(engine), fake)
    p = await kit.submitted()
    started, release = asyncio.Event(), asyncio.Event()
    real = review_git.take_snapshot

    async def blocked(*args: Any, **kw: Any) -> Any:
        started.set()
        await release.wait()
        return await real(*args, **kw)

    monkeypatch.setattr(review_git, "take_snapshot", blocked)
    call = await _confirming(kit, p)
    await asyncio.wait_for(started.wait(), 2)
    call.cancel()
    await asyncio.sleep(0)
    release.set()

    out = await asyncio.wait_for(call, 2)  # the cancel is absorbed: the approve stands

    assert out.status == "applied"
    assert p.id in fake.rows  # the publication was enqueued despite the cancel
    assert git_warnings(out) == [review_git.CANCELLED_WARNING]
    stored = await kit.repo.get(p.id)
    assert stored.status == "applied" and git_warnings(stored) == [review_git.CANCELLED_WARNING]
    assert (await kit.reviews())[-1].result == "ok"


async def test_cancel_during_inline_wait_keeps_task_running(engine: AsyncEngine) -> None:
    fake = FakePublisher()
    fake.gate = asyncio.Event()
    kit = with_git(ReviewKit(engine), fake, git_settings(git_publish_timeout_seconds=30))
    p = await kit.submitted()
    call = await _confirming(kit, p)
    await asyncio.wait_for(fake.processing.wait(), 2)
    call.cancel()

    out = await asyncio.wait_for(call, 2)

    assert out.status == "applied" and p.id in fake.rows
    assert git_warnings(out) == [review_git.CANCELLED_WARNING]
    hook = kit.review._git  # pyright: ignore[reportPrivateUsage]
    assert hook is not None
    (task,) = hook.tasks
    assert not task.done()
    fake.gate.set()
    await asyncio.wait_for(task, 1)
    assert fake.rows[p.id].status == "done"
