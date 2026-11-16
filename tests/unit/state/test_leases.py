"""Job leases in the state store (NFR-DEP-07): one holder per job across replicas."""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from lakeloom.state.db import create_engine, upgrade
from lakeloom.state.leases import LeaseRepository, instance_id

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
TTL = timedelta(seconds=60)


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    await upgrade(eng)
    yield eng
    await eng.dispose()


def test_instance_id_is_stable_and_shaped() -> None:
    first = instance_id()
    assert first == instance_id()
    assert f"-{os.getpid()}-" in first
    assert len(first.rsplit("-", 1)[1]) == 8
    assert len(first) <= 128


async def test_acquire_free_lease(engine: AsyncEngine) -> None:
    leases = LeaseRepository(engine)
    assert await leases.holder_of("job") is None
    assert await leases.acquire("job", "a", TTL, now=NOW)
    assert await leases.holder_of("job") == "a"


async def _contend(leases: list[LeaseRepository], name: str) -> list[bool]:
    return list(
        await asyncio.gather(
            *(lease.acquire(name, f"holder-{i}", TTL, now=NOW) for i, lease in enumerate(leases))
        )
    )


async def test_only_one_holder_under_contention(engine: AsyncEngine) -> None:
    leases = [LeaseRepository(engine) for _ in range(20)]
    results = await _contend(leases, "job")
    assert results.count(True) == 1
    winner = f"holder-{results.index(True)}"
    assert await leases[0].holder_of("job") == winner


async def test_renew_by_holder(engine: AsyncEngine) -> None:
    leases = LeaseRepository(engine)
    assert await leases.acquire("job", "a", TTL, now=NOW)
    assert await leases.acquire("job", "a", TTL, now=NOW + timedelta(seconds=50))
    # Renewed: still held at +100 s, past the first expiry, so b is refused.
    assert not await leases.acquire("job", "b", TTL, now=NOW + timedelta(seconds=100))
    assert await leases.holder_of("job") == "a"
    assert await _times(engine) == (NOW, NOW + timedelta(seconds=110))  # acquired_at kept


async def _times(engine: AsyncEngine) -> tuple[datetime, datetime]:
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT acquired_at, expires_at FROM job_leases"))).one()
    return _as_utc(row[0]), _as_utc(row[1])


def _as_utc(value: object) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


async def test_takeover_after_expiry(engine: AsyncEngine) -> None:
    a, b = LeaseRepository(engine), LeaseRepository(engine)
    assert await a.acquire("job", "a", TTL, now=NOW)
    assert not await b.acquire("job", "b", TTL, now=NOW + timedelta(seconds=59))
    assert await b.acquire("job", "b", TTL, now=NOW + timedelta(seconds=61))
    assert await b.holder_of("job") == "b"
    later = NOW + timedelta(seconds=61)
    assert await _times(engine) == (later, later + TTL)  # a takeover resets acquired_at
    # The old holder's next renew fails: it must not run another tick.
    assert not await a.acquire("job", "a", TTL, now=NOW + timedelta(seconds=62))


async def test_release_keeps_other_holder(engine: AsyncEngine) -> None:
    a, b = LeaseRepository(engine), LeaseRepository(engine)
    assert await a.acquire("job", "a", TTL, now=NOW)
    assert await b.acquire("job", "b", TTL, now=NOW + timedelta(seconds=61))
    await a.release("job", "a")  # a lost the lease: its release must not delete b's row
    assert await b.holder_of("job") == "b"
    await b.release("job", "b")
    assert await b.holder_of("job") is None
    assert await a.acquire("job", "a", TTL, now=NOW + timedelta(seconds=62))


async def test_leases_are_per_job(engine: AsyncEngine) -> None:
    leases = LeaseRepository(engine)
    assert await leases.acquire("one", "a", TTL, now=NOW)
    assert await leases.acquire("two", "b", TTL, now=NOW)
    assert (await leases.holder_of("one"), await leases.holder_of("two")) == ("a", "b")


@pytest.mark.contract
async def test_leases_postgres() -> None:
    url = os.environ.get("LAKELOOM_TEST_DB_URL")
    if not url:
        pytest.skip("LAKELOOM_TEST_DB_URL not set")
    engines = [create_engine(url) for _ in range(4)]
    name = f"test-{uuid.uuid4().hex}"
    try:
        await upgrade(engines[0])
        leases = [LeaseRepository(engines[i % 4]) for i in range(20)]
        results = await _contend(leases, name)
        assert results.count(True) == 1
        winner = f"holder-{results.index(True)}"
        assert await leases[0].holder_of(name) == winner
        # Expired: exactly one of the others takes it over.
        later = NOW + timedelta(seconds=61)
        again = await asyncio.gather(
            *(lease.acquire(name, f"late-{i}", TTL, now=later) for i, lease in enumerate(leases))
        )
        assert again.count(True) == 1
        await leases[0].release(name, winner)  # lost: a no-op
        assert await leases[0].holder_of(name) == f"late-{again.index(True)}"
        await leases[0].release(name, f"late-{again.index(True)}")
        assert await leases[0].holder_of(name) is None
    finally:
        for e in engines:
            await e.dispose()
