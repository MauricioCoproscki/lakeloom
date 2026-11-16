"""Job leases in the state store (NFR-DEP-07): at most one replica runs a background job.

A lease is one ``job_leases`` row per job name. Acquiring (or renewing) it is a single
conditional ``UPDATE``: it succeeds only when the caller already holds the lease or the lease
has expired, so two replicas can never both win (no read-then-write). When there is no row
yet, an ``INSERT`` creates it, and a unique violation means another replica got there first.

Expiry is judged with the caller's clock, not the database's: the clocks of all replicas are
assumed to agree to well within the TTL (ADR 0017). ``holder`` is an instance id
(:func:`instance_id`), never a token or a principal (NFR-SEC-11).
"""

import functools
import os
import secrets
import socket
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, delete, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from lakeloom.state.models import JobLeaseRow

__all__ = ["HOLDER_MAX", "LeaseRepository", "instance_id"]

HOLDER_MAX = 128
_HOST_MAX = 96  # leaves room for "-<pid>-<8 hex>" within HOLDER_MAX


@functools.cache
def instance_id() -> str:
    """``"<hostname>-<pid>-<8 hex>"``, made once per process."""
    host = socket.gethostname()[:_HOST_MAX] or "host"
    return f"{host}-{os.getpid()}-{secrets.token_hex(4)}"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class LeaseRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def acquire(
        self, name: str, holder: str, ttl: timedelta, *, now: datetime | None = None
    ) -> bool:
        """Take the lease ``name`` for ``ttl``, or renew it when ``holder`` has it already.

        ``False`` when another holder has an unexpired lease. A renewal keeps
        ``acquired_at``; a takeover of an expired lease resets it."""
        at = _utc(now) if now is not None else datetime.now(UTC)
        expires = at + ttl
        stmt = (
            update(JobLeaseRow)
            .where(
                JobLeaseRow.name == name,
                or_(JobLeaseRow.holder == holder, JobLeaseRow.expires_at < at),
            )
            .values(
                holder=holder,
                expires_at=expires,
                acquired_at=case((JobLeaseRow.holder == holder, JobLeaseRow.acquired_at), else_=at),
            )
        )
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
        if result.rowcount:
            return True
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    insert(JobLeaseRow).values(
                        name=name, holder=holder, acquired_at=at, expires_at=expires
                    )
                )
        except IntegrityError:
            return False  # a row exists: held by someone else (or just taken)
        return True

    async def release(self, name: str, holder: str) -> None:
        """Drop the lease ``name`` if ``holder`` has it; another holder's row is left alone."""
        async with self._engine.begin() as conn:
            await conn.execute(
                delete(JobLeaseRow).where(JobLeaseRow.name == name, JobLeaseRow.holder == holder)
            )

    async def holder_of(self, name: str) -> str | None:
        """The holder recorded for ``name`` (expired or not), or ``None`` without a row."""
        async with self._engine.connect() as conn:
            stmt = select(JobLeaseRow.holder).where(JobLeaseRow.name == name)
            return (await conn.execute(stmt)).scalar_one_or_none()
