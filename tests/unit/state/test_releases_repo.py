"""Release records and the triple-store sink queue (FR-GIT-06, FR-TS), migration 0004."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from importlib.resources import as_file, files
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from lakeloom.gitstore.layout import Snapshot
from lakeloom.state.db import create_engine, upgrade
from lakeloom.state.publications import Publication, PublicationRepository, new_publication_id
from lakeloom.state.releases import Release, ReleaseRepository

NOW = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
REPO = "a" * 64


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    await upgrade(eng)
    yield eng
    await eng.dispose()


@pytest.fixture
def repo(engine: AsyncEngine) -> ReleaseRepository:
    return ReleaseRepository(engine)


def _rel(version: str = "1.0.0", *, sink: str = "none", **kw: object) -> Release:
    data: dict[str, object] = {
        "id": f"rel_{version.replace('.', '')}{'0' * 20}",
        "repo": REPO,
        "model": "shop",
        "version": version,
        "tag": f"shop/v{version}",
        "commit_sha": "c" * 40,
        "released_by": "alice",
        "created_at": NOW,
        "sink_status": sink,
        **kw,
    }
    return Release.model_validate(data)


async def test_add_get_and_for_tag(repo: ReleaseRepository) -> None:
    rel = _rel()
    stored = await repo.add(rel)
    assert stored == rel
    assert await repo.get(rel.id) == rel
    assert await repo.get("rel_missing") is None
    assert await repo.for_tag(REPO, "shop/v1.0.0") == rel
    assert await repo.for_tag("b" * 64, "shop/v1.0.0") is None
    assert rel.status == "done" and rel.sink_attempts == 0


async def test_add_same_release_again_returns_existing(repo: ReleaseRepository) -> None:
    first = await repo.add(_rel())
    again = await repo.add(_rel().model_copy(update={"released_by": "bob", "sink_status": "done"}))
    assert again == first


async def test_add_replaces_stale_row_of_a_recreated_tag(repo: ReleaseRepository) -> None:
    """FR-GIT-06: the tag was deleted on the remote and the version released again: the row
    of the old tag (another id, another commit) is stale and replaced, never returned."""
    first = await repo.add(_rel())
    new = _rel().model_copy(update={"id": "rel_other", "commit_sha": "d" * 40})
    assert await repo.add(new) == new
    assert await repo.get(first.id) is None
    assert await repo.for_tag(REPO, "shop/v1.0.0") == new


async def test_add_replaces_row_of_same_id_on_another_commit(repo: ReleaseRepository) -> None:
    """The same confirmed call retried after its tag was deleted pushes a new commit."""
    await repo.add(_rel())
    moved = _rel().model_copy(update={"commit_sha": "d" * 40})
    assert await repo.add(moved) == moved
    assert await repo.get(moved.id) == moved


async def test_update_and_sink_queue(repo: ReleaseRepository) -> None:
    due = await repo.add(_rel("1.0.0", sink="pending"))
    later = await repo.add(
        _rel("1.1.0", sink="pending", sink_next_attempt_at=NOW + timedelta(minutes=5))
    )
    await repo.add(_rel("1.2.0", sink="done"))
    await repo.add(_rel("1.3.0"))
    assert [r.id for r in await repo.sink_due(REPO, NOW)] == [due.id]
    assert {r.id for r in await repo.sink_due(REPO, NOW + timedelta(minutes=5))} == {
        due.id,
        later.id,
    }
    failed = due.model_copy(
        update={"sink_status": "failed", "sink_attempts": 10, "sink_last_error": "x" * 900}
    )
    await repo.update(failed)
    got = await repo.get(due.id)
    assert got is not None and got.sink_status == "failed" and len(got.sink_last_error or "") == 500
    assert await repo.sink_counts(REPO) == {"none": 1, "pending": 1, "done": 1, "failed": 1}
    assert await repo.requeue_failed_sinks(REPO) == 1
    back = await repo.get(due.id)
    assert back is not None and back.sink_status == "pending" and back.sink_attempts == 0
    assert back.sink_next_attempt_at is None
    with pytest.raises(LookupError):
        await repo.update(_rel("9.9.9"))


async def test_datetimes_are_utc(repo: ReleaseRepository) -> None:
    await repo.add(_rel(sink="pending", sink_next_attempt_at=NOW))
    got = await repo.for_tag(REPO, "shop/v1.0.0")
    assert got is not None and got.created_at.tzinfo is not None
    assert got.sink_next_attempt_at == NOW


def _pub(proposal_id: str, model: str = "shop") -> Publication:
    snap = Snapshot(
        model=model, glossary_id="g", glossary_fqn=model, model_version="1",
        proposal_id=proposal_id, message="m",
    )  # fmt: skip
    return Publication(
        id=new_publication_id(), repo=REPO, model=model, proposal_id=proposal_id,
        mode="direct", created_at=NOW, updated_at=NOW, payload=snap,
    )  # fmt: skip


async def test_publications_open_for_model_and_last_done(engine: AsyncEngine) -> None:
    pubs = PublicationRepository(engine)
    assert await pubs.open_for_model(REPO, "shop") == 0
    assert await pubs.last_done(REPO, "shop") is None
    a = await pubs.add(_pub("p1"))
    b = await pubs.add(_pub("p2"))
    c = await pubs.add(_pub("p3"))
    await pubs.add(_pub("p4", model="other"))
    assert await pubs.open_for_model(REPO, "shop") == 3
    await pubs.update(a.model_copy(update={"status": "done", "commit_sha": "1" * 40}))
    await pubs.update(b.model_copy(update={"status": "done", "commit_sha": "2" * 40}))
    await pubs.update(c.model_copy(update={"status": "blocked"}))
    assert await pubs.open_for_model(REPO, "shop") == 1
    assert await pubs.open_for_model("b" * 64, "shop") == 0
    last = await pubs.last_done(REPO, "shop")
    assert last is not None and last.id == b.id and last.commit_sha == "2" * 40
    assert await pubs.last_done(REPO, "other") is None


async def test_publications_has_open_and_done_models(engine: AsyncEngine) -> None:
    pubs = PublicationRepository(engine)
    assert await pubs.done_models(REPO) == []
    a = await pubs.add(_pub("p1", model="zeta"))
    b = await pubs.add(_pub("p2"))
    assert await pubs.has_open(REPO, "shop") and not await pubs.has_open(REPO, "none")
    await pubs.update(a.model_copy(update={"status": "done"}))
    await pubs.update(b.model_copy(update={"status": "done"}))
    await pubs.add(_pub("p3"))  # pending: shop stays published and is open
    assert await pubs.done_models(REPO) == ["shop", "zeta"]
    assert await pubs.done_models("b" * 64) == []
    assert await pubs.has_open(REPO, "shop") and not await pubs.has_open(REPO, "zeta")


async def test_migration_0004_upgrade_downgrade(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    try:
        await upgrade(engine)
        async with engine.connect() as conn:
            found = await conn.run_sync(
                lambda c: (
                    inspect(c).get_table_names(),
                    {col["name"] for col in inspect(c).get_columns("releases")},
                    inspect(c).get_unique_constraints("releases")
                    + [
                        {"column_names": i["column_names"]}
                        for i in inspect(c).get_indexes("releases")
                        if i.get("unique")
                    ],
                )
            )
        tables, columns, uniques = found
        assert "releases" in tables
        assert columns == {
            "id", "repo", "model", "version", "tag", "commit_sha", "released_by",
            "created_at", "status", "sink_status", "sink_attempts", "sink_next_attempt_at",
            "sink_last_error",
        }  # fmt: skip
        assert any(u["column_names"] == ["repo", "tag"] for u in uniques)

        def down(conn: Connection) -> None:
            with as_file(files("lakeloom.state") / "migrations") as script_dir:
                cfg = Config()
                cfg.set_main_option("script_location", str(script_dir))
                cfg.attributes["connection"] = conn
                command.downgrade(cfg, "0003")

        async with engine.begin() as conn:
            await conn.run_sync(down)
        async with engine.connect() as conn:
            after = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
        assert "releases" not in after and "publications" in after
        await upgrade(engine)  # and back up again
        async with engine.connect() as conn:
            assert "releases" in await conn.run_sync(lambda c: inspect(c).get_table_names())
    finally:
        await engine.dispose()
