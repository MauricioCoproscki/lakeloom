"""Publication outbox repository (FR-GIT-09, NFR-OPS-04)."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from importlib.resources import as_file, files
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, update
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from lakeloom.gitstore.layout import Snapshot
from lakeloom.state.db import create_engine, upgrade
from lakeloom.state.publications import (
    Publication,
    PublicationRepository,
    PublicationRow,
    new_publication_id,
)

NOW = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)
REPO = "a" * 64


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    await upgrade(eng)
    yield eng
    await eng.dispose()


@pytest.fixture
def repo(engine: AsyncEngine) -> PublicationRepository:
    return PublicationRepository(engine)


def _pub(proposal_id: str = "prop_1", *, repo: str = REPO, model: str = "shop") -> Publication:
    pub_id = new_publication_id()
    snap = Snapshot(
        model=model,
        glossary_id="g1",
        glossary_fqn=model,
        model_version="0.1",
        proposal_id=proposal_id,
        message=f"ontology({model}): x\n\nLakeloom-Publication: {pub_id}\n",
    )
    return Publication(
        id=pub_id,
        repo=repo,
        model=model,
        proposal_id=proposal_id,
        mode="direct",
        created_at=NOW,
        updated_at=NOW,
        payload=snap,
    )


def test_publication_id_prefix() -> None:
    pid = new_publication_id()
    assert pid.startswith("pub_") and len(pid) == 30


async def test_add_assigns_seq_and_roundtrips(repo: PublicationRepository) -> None:
    a = await repo.add(_pub("p1"))
    b = await repo.add(_pub("p2"))
    assert a.seq is not None and b.seq is not None and b.seq > a.seq
    got = await repo.for_proposal("p1")
    assert got is not None
    assert got.id == a.id and got.status == "pending" and got.attempts == 0
    assert got.created_at == NOW and got.created_at.tzinfo is not None
    assert got.payload.proposal_id == "p1"
    assert await repo.for_proposal("nope") is None


async def test_duplicate_proposal_returns_existing(repo: PublicationRepository) -> None:
    first = await repo.add(_pub("p1"))
    again = await repo.add(_pub("p1"))
    assert again.id == first.id and again.seq == first.seq
    assert len(await repo.list()) == 1


async def test_head_is_lowest_seq_not_done(repo: PublicationRepository) -> None:
    assert await repo.head(REPO) is None
    a = await repo.add(_pub("p1"))
    b = await repo.add(_pub("p2"))
    await repo.add(_pub("p3", repo="b" * 64))
    head = await repo.head(REPO)
    assert head is not None and head.id == a.id
    a.status = "done"
    a.commit_sha = "f" * 40
    await repo.update(a)
    head = await repo.head(REPO)
    assert head is not None and head.id == b.id
    b.status = "blocked"
    b.last_error = "auth"
    await repo.update(b)
    head = await repo.head(REPO)
    assert head is not None and head.id == b.id and head.status == "blocked"


async def test_update_persists_fields(repo: PublicationRepository) -> None:
    a = await repo.add(_pub("p1"))
    a.attempts = 2
    a.next_attempt_at = NOW + timedelta(minutes=2)
    a.last_error = "x" * 900
    a.branch = "main"
    a.pr_url = "http://forge/pr/1"
    a.updated_at = NOW + timedelta(seconds=1)
    await repo.update(a)
    got = await repo.for_proposal("p1")
    assert got is not None
    assert got.attempts == 2 and got.next_attempt_at == NOW + timedelta(minutes=2)
    assert got.last_error is not None and len(got.last_error) == 500
    assert got.branch == "main" and got.pr_url == "http://forge/pr/1"


async def test_update_unknown_raises(repo: PublicationRepository) -> None:
    with pytest.raises(LookupError):
        await repo.update(_pub("p9"))


async def test_list_counts_and_requeue(repo: PublicationRepository) -> None:
    a = await repo.add(_pub("p1"))
    b = await repo.add(_pub("p2"))
    await repo.add(_pub("p3"))
    a.status = "done"
    await repo.update(a)
    b.status = "blocked"
    b.attempts = 3
    b.next_attempt_at = NOW
    await repo.update(b)
    assert await repo.counts(REPO) == {"pending": 1, "done": 1, "blocked": 1}
    assert await repo.counts("c" * 64) == {"pending": 0, "done": 0, "blocked": 0}
    assert [p.proposal_id for p in await repo.list()] == ["p1", "p2", "p3"]
    assert [p.proposal_id for p in await repo.list(status="blocked")] == ["p2"]
    assert len(await repo.list(limit=2)) == 2
    assert await repo.requeue_blocked("c" * 64) == 0
    assert await repo.requeue_blocked(REPO) == 1
    got = await repo.for_proposal("p2")
    assert got is not None and got.status == "pending"
    assert got.attempts == 0 and got.next_attempt_at is None


async def test_fill_payload_only_before_any_attempt(repo: PublicationRepository) -> None:
    a = await repo.add(_pub("p1"))
    filled = a.payload.model_copy(update={"exports": {"ontology.ttl": "x"}, "om_version": "1"})
    assert await repo.fill_payload(a.id, filled)
    got = await repo.for_proposal("p1")
    assert got is not None and got.payload.exports == {"ontology.ttl": "x"}

    b = await repo.add(_pub("p2"))
    tried = b.model_copy(update={"attempts": 1, "last_error": "down"})
    await repo.update(tried)
    assert not await repo.fill_payload(b.id, filled.model_copy(update={"proposal_id": "p2"}))
    c = await repo.add(_pub("p3"))
    await repo.update(c.model_copy(update={"status": "done"}))
    assert not await repo.fill_payload(c.id, filled.model_copy(update={"proposal_id": "p3"}))
    for pid in ("p2", "p3"):
        row = await repo.for_proposal(pid)
        assert row is not None and row.payload.exports is None
    assert not await repo.fill_payload("pub_missing", filled)


async def test_corrupt_payload_is_surfaced_as_blocked(
    repo: PublicationRepository, engine: AsyncEngine
) -> None:
    bad = await repo.add(_pub("p1"))
    await repo.add(_pub("p2"))
    async with engine.begin() as conn:
        await conn.execute(
            update(PublicationRow).where(PublicationRow.id == bad.id).values(payload={"x": 1})
        )

    head = await repo.head(REPO)  # never raises: the queue owner sees a blocked head

    assert head is not None and head.id == bad.id
    assert head.status == "blocked" and head.last_error == "invalid stored snapshot"
    assert (await repo.counts(REPO))["blocked"] == 1  # persisted on the row
    listed = await repo.list("blocked")
    assert [r.id for r in listed] == [bad.id] and listed[0].last_error == "invalid stored snapshot"
    one = await repo.for_proposal("p1")
    assert one is not None and one.status == "blocked"
    assert await repo.requeue_blocked(REPO) == 1
    again = await repo.head(REPO)  # still invalid: blocked again, not raised
    assert again is not None and again.status == "blocked"


async def test_0003_creates_publications_and_downgrades(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")

    def migrate(target: str, down: bool) -> object:
        def run(conn: Connection) -> None:
            with as_file(files("lakeloom.state") / "migrations") as script_dir:
                cfg = Config()
                cfg.set_main_option("script_location", str(script_dir))
                cfg.attributes["connection"] = conn
                (command.downgrade if down else command.upgrade)(cfg, target)

        return run

    async def tables() -> set[str]:
        async with engine.connect() as conn:
            return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))

    try:
        async with engine.begin() as conn:
            await conn.run_sync(migrate("0002", down=False))  # type: ignore[arg-type]
        assert "publications" not in await tables()
        async with engine.begin() as conn:
            await conn.run_sync(migrate("0003", down=False))  # type: ignore[arg-type]
        assert "publications" in await tables()
        async with engine.connect() as conn:
            idx = await conn.run_sync(
                lambda c: {i["name"] for i in inspect(c).get_indexes("publications")}
            )
            cols = await conn.run_sync(
                lambda c: {col["name"] for col in inspect(c).get_columns("publications")}
            )
        assert "ix_publications_repo_status_seq" in idx
        assert {
            "seq", "id", "repo", "model", "proposal_id", "mode", "status", "attempts",
            "next_attempt_at", "last_error", "commit_sha", "branch", "pr_url",
            "created_at", "updated_at", "payload",
        } <= cols  # fmt: skip
        async with engine.begin() as conn:
            await conn.run_sync(migrate("0002", down=True))  # type: ignore[arg-type]
        remaining = await tables()
        assert "publications" not in remaining and "proposals" in remaining
    finally:
        await engine.dispose()
