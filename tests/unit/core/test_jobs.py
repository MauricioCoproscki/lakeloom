"""The lease-guarded job runner (NFR-DEP-07, NFR-OPS-04)."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from lakeloom.core.jobs import Job, JobRunner

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


class FakeLeases:
    """Answers acquire from a script; records every call."""

    def __init__(self, answers: Callable[[int], bool] = lambda n: True) -> None:
        self.answers = answers
        self.acquires: list[tuple[str, str, timedelta]] = []
        self.releases: list[tuple[str, str]] = []

    async def acquire(
        self, name: str, holder: str, ttl: timedelta, *, now: datetime | None = None
    ) -> bool:
        self.acquires.append((name, holder, ttl))
        return self.answers(len(self.acquires))

    async def release(self, name: str, holder: str) -> None:
        self.releases.append((name, holder))


class Sleeps:
    """A sleep that returns at once ``rounds`` times, then blocks until cancelled."""

    def __init__(self, rounds: int) -> None:
        self.rounds = rounds
        self.calls: list[float] = []
        self.parked = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if len(self.calls) > self.rounds:
            self.parked.set()
            await asyncio.Event().wait()
        await asyncio.sleep(0)


def _runner(leases: FakeLeases, sleeps: Sleeps) -> JobRunner:
    return JobRunner(
        leases,  # pyright: ignore[reportArgumentType]
        "me",
        clock=lambda: NOW,
        sleep=sleeps,
    )


def _counting_job(name: str = "job", interval: float = 5.0) -> tuple[Job, list[int]]:
    ran: list[int] = []

    async def tick() -> None:
        ran.append(1)

    return Job(name, lambda: interval, tick), ran


async def test_tick_runs_under_lease_with_ttl() -> None:
    leases, sleeps = FakeLeases(), Sleeps(rounds=2)
    runner = _runner(leases, sleeps)
    job, ran = _counting_job(interval=5.0)
    runner.add(job)
    await runner.start()
    await asyncio.wait_for(sleeps.parked.wait(), 2)
    assert len(ran) == 3
    assert leases.acquires[0] == ("job", "me", timedelta(seconds=60))  # max(3 x 5 s, 60 s)
    assert sleeps.calls == [5.0, 5.0, 5.0]
    status = runner.status()["job"]
    assert status.held and status.last_run_at == NOW and status.last_error is None
    await runner.stop()


async def test_ttl_is_three_intervals_when_longer() -> None:
    leases, sleeps = FakeLeases(), Sleeps(rounds=0)
    runner = _runner(leases, sleeps)
    runner.add(_counting_job(interval=600.0)[0])
    await runner.start()
    await asyncio.wait_for(sleeps.parked.wait(), 2)
    assert leases.acquires[0][2] == timedelta(seconds=1800)
    await runner.stop()


async def test_renew_lost_skips_tick() -> None:
    leases, sleeps = FakeLeases(lambda n: n == 1), Sleeps(rounds=2)
    runner = _runner(leases, sleeps)
    job, ran = _counting_job()
    runner.add(job)
    await runner.start()
    await asyncio.wait_for(sleeps.parked.wait(), 2)
    assert len(leases.acquires) == 3
    assert len(ran) == 1  # the lease was lost on the second acquire: no more ticks
    assert not runner.status()["job"].held
    await runner.stop()


async def test_tick_error_does_not_stop_loop(caplog: pytest.LogCaptureFixture) -> None:
    leases, sleeps = FakeLeases(), Sleeps(rounds=2)
    runner = _runner(leases, sleeps)
    calls: list[int] = []

    async def tick() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom postgresql://user:pw@db/x")  # never logged

    runner.add(Job("job", lambda: 5.0, tick))
    with caplog.at_level(logging.ERROR, logger="lakeloom.jobs"):
        await runner.start()
        await asyncio.wait_for(sleeps.parked.wait(), 2)
    assert len(calls) == 3
    assert "RuntimeError" in caplog.text
    assert "pw@db" not in caplog.text and "boom" not in caplog.text
    assert runner.status()["job"].last_error is None  # cleared by the next good tick
    await runner.stop()


async def test_last_error_is_the_class_name() -> None:
    leases, sleeps = FakeLeases(), Sleeps(rounds=0)
    runner = _runner(leases, sleeps)

    async def tick() -> None:
        raise ValueError("secret text")

    runner.add(Job("job", lambda: 5.0, tick))
    await runner.start()
    await asyncio.wait_for(sleeps.parked.wait(), 2)
    assert runner.status()["job"].last_error == "ValueError"
    await runner.stop()


async def test_acquire_error_skips_tick_and_keeps_looping(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Broken(FakeLeases):
        async def acquire(
            self, name: str, holder: str, ttl: timedelta, *, now: datetime | None = None
        ) -> bool:
            await super().acquire(name, holder, ttl, now=now)
            raise OSError("connection to postgresql://u:pw@db refused")

    leases, sleeps = Broken(), Sleeps(rounds=1)
    runner = _runner(leases, sleeps)
    job, ran = _counting_job()
    runner.add(job)
    with caplog.at_level(logging.WARNING, logger="lakeloom.jobs"):
        await runner.start()
        await asyncio.wait_for(sleeps.parked.wait(), 2)
    assert len(leases.acquires) == 2 and ran == []
    assert "OSError" in caplog.text and "pw@db" not in caplog.text
    await runner.stop()


async def test_stop_releases_leases() -> None:
    leases, sleeps = FakeLeases(), Sleeps(rounds=0)
    runner = _runner(leases, sleeps)
    runner.add(_counting_job("one")[0])
    runner.add(_counting_job("two")[0])
    await runner.start()
    await asyncio.wait_for(sleeps.parked.wait(), 2)
    tasks = [t for t in asyncio.all_tasks() if t.get_name().startswith("lakeloom-job:")]
    assert len(tasks) == 2
    await runner.stop()
    assert all(t.done() for t in tasks)
    assert sorted(leases.releases) == [("one", "me"), ("two", "me")]
    assert not any(s.held for s in runner.status().values())


async def test_stop_waits_for_a_running_tick() -> None:
    leases, sleeps = FakeLeases(), Sleeps(rounds=5)
    runner = _runner(leases, sleeps)
    entered, finished = asyncio.Event(), asyncio.Event()
    gate = asyncio.Event()

    async def tick() -> None:
        entered.set()
        await gate.wait()
        finished.set()

    runner.add(Job("job", lambda: 5.0, tick))
    await runner.start()
    await asyncio.wait_for(entered.wait(), 2)
    stopping = asyncio.create_task(runner.stop())
    await asyncio.sleep(0.01)
    assert not stopping.done()  # between ticks only: the running one is not cut
    gate.set()
    await asyncio.wait_for(stopping, 2)
    assert finished.is_set()
    assert len(sleeps.calls) == 0  # no further tick after stop
    assert leases.releases == [("job", "me")]


async def test_stop_cancels_a_tick_past_the_grace() -> None:
    leases, sleeps = FakeLeases(), Sleeps(rounds=5)
    runner = _runner(leases, sleeps)
    entered = asyncio.Event()

    async def tick() -> None:
        entered.set()
        await asyncio.Event().wait()

    runner.add(Job("job", lambda: 5.0, tick))
    await runner.start()
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(runner.stop(grace=0.01), 2)
    assert leases.releases == [("job", "me")]


async def test_stop_without_start_is_a_no_op() -> None:
    leases = FakeLeases()
    runner = _runner(leases, Sleeps(rounds=0))
    runner.add(_counting_job()[0])
    await runner.stop()
    assert leases.releases == []


async def test_duplicate_job_name_is_refused() -> None:
    runner = _runner(FakeLeases(), Sleeps(rounds=0))
    runner.add(_counting_job()[0])
    with pytest.raises(ValueError, match="job"):
        runner.add(_counting_job()[0])


async def test_release_error_is_logged_by_class(caplog: pytest.LogCaptureFixture) -> None:
    class BadRelease(FakeLeases):
        async def release(self, name: str, holder: str) -> None:
            raise OSError("postgresql://u:pw@db gone")

    leases, sleeps = BadRelease(), Sleeps(rounds=0)
    runner = _runner(leases, sleeps)
    runner.add(_counting_job()[0])
    await runner.start()
    await asyncio.wait_for(sleeps.parked.wait(), 2)
    with caplog.at_level(logging.WARNING, logger="lakeloom.jobs"):
        await runner.stop()
    assert "OSError" in caplog.text and "pw@db" not in caplog.text
