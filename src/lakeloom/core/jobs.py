"""Background jobs that run on one replica at a time (NFR-DEP-07, NFR-OPS-04).

Each :class:`Job` is a loop: renew (or take) its lease, run one tick if the lease is held,
sleep for the job's interval. The lease TTL is ``max(3 * interval, 60 s)``, so a replica that
stops renewing loses the job after three missed rounds at most; a replica that lost the lease
skips the tick. A tick that raises is logged with the exception class only (its text may carry
a URL with credentials, NFR-SEC-11) and the loop goes on.

:meth:`JobRunner.stop` ends every loop between ticks: a loop that is sleeping or renewing is
cancelled at once, a running tick may finish within ``grace`` seconds (then it is cancelled
too), and the runner releases its leases so another replica can take over without waiting
for the TTL. Work a tick did not reach (queued publications, pending sinks) stays in the
store for the next holder.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from lakeloom.state.leases import LeaseRepository

__all__ = ["MIN_TTL_SECONDS", "Job", "JobRunner", "JobStatus", "lease_ttl"]

_log = logging.getLogger("lakeloom.jobs")
MIN_TTL_SECONDS = 60.0
DEFAULT_GRACE_SECONDS = 30.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


def lease_ttl(interval: float) -> timedelta:
    """``max(3 * interval, 60 s)``."""
    return timedelta(seconds=max(3 * interval, MIN_TTL_SECONDS))


@dataclass
class Job:
    """``tick`` runs every ``interval()`` seconds on the replica holding the lease ``name``."""

    name: str
    interval: Callable[[], float]
    tick: Callable[[], Awaitable[None]]


@dataclass
class JobStatus:
    held: bool = False
    last_run_at: datetime | None = None
    last_error: str | None = None  # the exception class of the last tick, never its text


class JobRunner:
    def __init__(
        self,
        leases: LeaseRepository,
        holder: str,
        *,
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._leases = leases
        self.holder = holder
        self._clock = clock
        self._sleep = sleep
        self._jobs: dict[str, Job] = {}
        self._status: dict[str, JobStatus] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._in_tick: set[str] = set()
        self._stopping = False

    def add(self, job: Job) -> None:
        if job.name in self._jobs:
            raise ValueError(f"job {job.name!r} is already registered")
        if self._tasks:
            raise RuntimeError("jobs cannot be added to a running JobRunner")
        self._jobs[job.name] = job
        self._status[job.name] = JobStatus()

    def status(self) -> dict[str, JobStatus]:
        """A copy of each job's status, by name."""
        return {
            name: JobStatus(s.held, s.last_run_at, s.last_error) for name, s in self._status.items()
        }

    async def start(self) -> None:
        if self._tasks:
            return
        self._stopping = False
        for name, job in self._jobs.items():
            self._tasks[name] = asyncio.create_task(self._loop(job), name=f"lakeloom-job:{name}")

    async def stop(self, *, grace: float = DEFAULT_GRACE_SECONDS) -> None:
        """End every loop between ticks, then release this runner's leases."""
        if not self._tasks:
            return
        self._stopping = True
        tasks = dict(self._tasks)
        for name, task in tasks.items():
            if name not in self._in_tick:
                task.cancel()
        _, late = await asyncio.wait(tasks.values(), timeout=grace)
        for task in late:
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._in_tick.clear()
        for name in tasks:
            self._status[name].held = False
            try:
                await self._leases.release(name, self.holder)
            except Exception as exc:
                # The lease expires by itself; class only (NFR-SEC-11).
                _log.warning("job %s: lease release failed (%s)", name, type(exc).__name__)

    async def _loop(self, job: Job) -> None:
        status = self._status[job.name]
        while not self._stopping:
            interval = job.interval()
            try:
                held = await self._leases.acquire(
                    job.name, self.holder, lease_ttl(interval), now=self._clock()
                )
            except Exception as exc:
                _log.warning("job %s: lease renewal failed (%s)", job.name, type(exc).__name__)
                held = False
            status.held = held
            if held:
                await self._tick(job, status)
                if self._stopping:
                    return
            await self._sleep(interval)

    async def _tick(self, job: Job, status: JobStatus) -> None:
        self._in_tick.add(job.name)
        try:
            await job.tick()
        except Exception as exc:
            _log.error("job %s failed (%s)", job.name, type(exc).__name__)
            status.last_error = type(exc).__name__
        else:
            status.last_error = None
        finally:
            self._in_tick.discard(job.name)
        status.last_run_at = self._clock()
