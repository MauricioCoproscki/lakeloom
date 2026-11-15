"""Release records (FR-GIT-06) and the triple-store sink queue (FR-TS).

One row per release Lakeloom made. Git stays the source of truth for listing releases (a tag
made outside Lakeloom is still listed and served); the table is the audit trail and the queue
of the optional triple-store sink. ``repo`` is the sha256 of the remote URL, as for
publications, so the URL itself (which may carry user info) is never stored.
"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint, func, or_, select
from sqlalchemy import update as sql_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from lakeloom.state.models import Base

__all__ = ["Release", "ReleaseRepository", "ReleaseRow", "SinkStatus"]

SinkStatus = Literal["none", "pending", "done", "failed"]
SINK_STATUSES: tuple[SinkStatus, ...] = ("none", "pending", "done", "failed")
ERROR_MAX = 500


class ReleaseRow(Base):
    __tablename__ = "releases"
    __table_args__ = (
        UniqueConstraint("repo", "tag", name="uq_releases_repo_tag"),
        Index("ix_releases_repo_sink_status", "repo", "sink_status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repo: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(255))
    version: Mapped[str] = mapped_column(String(64))
    tag: Mapped[str] = mapped_column(String(320))
    commit_sha: Mapped[str] = mapped_column(String(64))
    released_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    sink_status: Mapped[str] = mapped_column(String(16))
    sink_attempts: Mapped[int] = mapped_column(Integer)
    sink_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sink_last_error: Mapped[str | None] = mapped_column(String(ERROR_MAX))


class Release(BaseModel):
    """A release Lakeloom made; mirrors :class:`ReleaseRow`."""

    id: str
    repo: str
    model: str
    version: str
    tag: str
    commit_sha: str
    released_by: str
    created_at: datetime
    status: Literal["done"] = "done"
    sink_status: SinkStatus = "none"
    sink_attempts: int = 0
    sink_next_attempt_at: datetime | None = None
    sink_last_error: str | None = None


def _utc(value: datetime) -> datetime:
    """SQLite drops tzinfo; values are stored as UTC, so reattach it."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _fit(value: str, width: int) -> str:
    return value.replace("\x00", "")[:width]


def _fill_sink(row: ReleaseRow, rel: Release) -> None:
    row.status = rel.status
    row.sink_status = rel.sink_status
    row.sink_attempts = rel.sink_attempts
    row.sink_next_attempt_at = (
        None if rel.sink_next_attempt_at is None else _utc(rel.sink_next_attempt_at)
    )
    row.sink_last_error = (
        None if rel.sink_last_error is None else _fit(rel.sink_last_error, ERROR_MAX)
    )


def _from_row(row: ReleaseRow) -> Release:
    return Release(
        id=row.id,
        repo=row.repo,
        model=row.model,
        version=row.version,
        tag=row.tag,
        commit_sha=row.commit_sha,
        released_by=row.released_by,
        created_at=_utc(row.created_at),
        status="done",
        sink_status=row.sink_status,  # type: ignore[arg-type]
        sink_attempts=row.sink_attempts,
        sink_next_attempt_at=(
            None if row.sink_next_attempt_at is None else _utc(row.sink_next_attempt_at)
        ),
        sink_last_error=row.sink_last_error,
    )


class ReleaseRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def add(self, rel: Release) -> Release:
        """Insert ``rel``; an existing row for the same id or (repo, tag) is returned instead."""
        existing = await self.get(rel.id) or await self.for_tag(rel.repo, rel.tag)
        if existing is not None:
            return existing
        row = ReleaseRow(
            id=rel.id,
            repo=rel.repo,
            model=rel.model,
            version=rel.version,
            tag=_fit(rel.tag, 320),
            commit_sha=rel.commit_sha,
            released_by=_fit(rel.released_by, 255),
            created_at=_utc(rel.created_at),
        )
        _fill_sink(row, rel)
        try:
            async with self._sessions() as session, session.begin():
                session.add(row)
        except IntegrityError:
            existing = await self.get(rel.id) or await self.for_tag(rel.repo, rel.tag)
            if existing is None:
                raise
            return existing
        return _from_row(row)

    async def get(self, release_id: str) -> Release | None:
        async with self._sessions() as session:
            row = await session.get(ReleaseRow, release_id)
        return None if row is None else _from_row(row)

    async def update(self, rel: Release) -> None:
        """Store the status and sink state of ``rel`` (``LookupError`` if unknown)."""
        async with self._sessions() as session, session.begin():
            stmt = select(ReleaseRow).where(ReleaseRow.id == rel.id).with_for_update()
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                raise LookupError(f"release {rel.id!r} does not exist")
            _fill_sink(row, rel)

    async def for_tag(self, repo: str, tag: str) -> Release | None:
        stmt = select(ReleaseRow).where(ReleaseRow.repo == repo, ReleaseRow.tag == tag)
        async with self._sessions() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return None if row is None else _from_row(row)

    async def sink_due(self, repo: str, now: datetime) -> list[Release]:
        """``repo``'s releases waiting for the triple store and due at ``now``, oldest first."""
        stmt = (
            select(ReleaseRow)
            .where(
                ReleaseRow.repo == repo,
                ReleaseRow.sink_status == "pending",
                or_(
                    ReleaseRow.sink_next_attempt_at.is_(None),
                    ReleaseRow.sink_next_attempt_at <= _utc(now),
                ),
            )
            .order_by(ReleaseRow.created_at, ReleaseRow.id)
        )
        async with self._sessions() as session:
            rows = list((await session.execute(stmt)).scalars())
        return [_from_row(r) for r in rows]

    async def sink_counts(self, repo: str) -> dict[str, int]:
        stmt = (
            select(ReleaseRow.sink_status, func.count())
            .where(ReleaseRow.repo == repo)
            .group_by(ReleaseRow.sink_status)
        )
        async with self._sessions() as session:
            rows = (await session.execute(stmt)).all()
        out: dict[str, int] = dict.fromkeys(SINK_STATUSES, 0)
        for status, n in rows:
            out[status] = int(n)
        return out

    async def requeue_failed_sinks(self, repo: str) -> int:
        """``lakeloom release sink-retry``: failed sink rows back to pending, due now."""
        stmt = (
            sql_update(ReleaseRow)
            .where(ReleaseRow.repo == repo, ReleaseRow.sink_status == "failed")
            .values(sink_status="pending", sink_attempts=0, sink_next_attempt_at=None)
        )
        async with self._sessions() as session, session.begin():
            result = await session.execute(stmt)
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def sink_last_error(self, repo: str) -> str | None:
        """The error of the most recently released row still pending or failed, if any."""
        stmt = (
            select(ReleaseRow.sink_last_error)
            .where(
                ReleaseRow.repo == repo,
                ReleaseRow.sink_status.in_(("pending", "failed")),
                ReleaseRow.sink_last_error.is_not(None),
            )
            .order_by(ReleaseRow.created_at.desc(), ReleaseRow.id)
            .limit(1)
        )
        async with self._sessions() as session:
            return (await session.execute(stmt)).scalar_one_or_none()
