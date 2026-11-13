"""Publication outbox (FR-GIT-09, NFR-OPS-04).

One row per applied proposal that must reach Git. The :class:`~lakeloom.gitstore.layout.Snapshot`
taken at apply is the JSON ``payload``; the columns hold the queue state. ``seq`` orders one
repository's queue (``repo`` is the sha256 of the remote URL, so the URL itself, which may
carry user info, is never stored).
"""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import JSON, DateTime, Index, Integer, String, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from lakeloom.gitstore.layout import Snapshot
from lakeloom.state.ids import new_ulid_id
from lakeloom.state.models import Base

__all__ = [
    "Publication",
    "PublicationMode",
    "PublicationRepository",
    "PublicationRow",
    "PublicationStatus",
    "new_publication_id",
]

PublicationStatus = Literal["pending", "done", "blocked"]
PublicationMode = Literal["direct", "pull_request"]
STATUSES: tuple[PublicationStatus, ...] = ("pending", "done", "blocked")
_WIDTHS = {"last_error": 500, "branch": 255, "pr_url": 1024, "commit_sha": 64}


def new_publication_id() -> str:
    return new_ulid_id("pub_")


class PublicationRow(Base):
    __tablename__ = "publications"
    __table_args__ = (Index("ix_publications_repo_status_seq", "repo", "status", "seq"),)

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(64), unique=True)
    repo: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(255))
    proposal_id: Mapped[str] = mapped_column(String(64), unique=True)
    mode: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    branch: Mapped[str | None] = mapped_column(String(255))
    pr_url: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class Publication(BaseModel):
    """A queued Git publication; mirrors :class:`PublicationRow`."""

    seq: int | None = None
    id: str
    repo: str = ""
    model: str
    proposal_id: str
    mode: PublicationMode
    status: PublicationStatus = "pending"
    attempts: int = 0
    next_attempt_at: datetime | None = None
    last_error: str | None = None
    commit_sha: str | None = None
    branch: str | None = None
    pr_url: str | None = None
    created_at: datetime
    updated_at: datetime
    payload: Snapshot


def _utc(value: datetime) -> datetime:
    """SQLite drops tzinfo; values are stored as UTC, so reattach it."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _fit(value: str | None, column: str) -> str | None:
    return None if value is None else value.replace("\x00", "")[: _WIDTHS[column]]


def _fill_state(row: PublicationRow, pub: Publication) -> None:
    row.mode = pub.mode
    row.status = pub.status
    row.attempts = pub.attempts
    row.next_attempt_at = None if pub.next_attempt_at is None else _utc(pub.next_attempt_at)
    row.last_error = _fit(pub.last_error, "last_error")
    row.commit_sha = _fit(pub.commit_sha, "commit_sha")
    row.branch = _fit(pub.branch, "branch")
    row.pr_url = _fit(pub.pr_url, "pr_url")
    row.updated_at = _utc(pub.updated_at)
    row.payload = pub.payload.model_dump(mode="json")


def _from_row(row: PublicationRow) -> Publication:
    return Publication(
        seq=row.seq,
        id=row.id,
        repo=row.repo,
        model=row.model,
        proposal_id=row.proposal_id,
        mode=row.mode,  # type: ignore[arg-type]
        status=row.status,  # type: ignore[arg-type]
        attempts=row.attempts,
        next_attempt_at=None if row.next_attempt_at is None else _utc(row.next_attempt_at),
        last_error=row.last_error,
        commit_sha=row.commit_sha,
        branch=row.branch,
        pr_url=row.pr_url,
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
        payload=Snapshot.model_validate(row.payload),
    )


class PublicationRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def add(self, pub: Publication) -> Publication:
        """Insert ``pub``; a publication for the same proposal is returned unchanged instead."""
        existing = await self.for_proposal(pub.proposal_id)
        if existing is not None:
            return existing
        row = PublicationRow(
            id=pub.id,
            repo=pub.repo,
            model=pub.model,
            proposal_id=pub.proposal_id,
            created_at=_utc(pub.created_at),
        )
        _fill_state(row, pub)
        try:
            async with self._sessions() as session, session.begin():
                session.add(row)
        except IntegrityError:
            existing = await self.for_proposal(pub.proposal_id)
            if existing is None:
                raise
            return existing
        return _from_row(row)

    async def head(self, repo: str) -> Publication | None:
        """The oldest publication of ``repo`` that is not done (pending or blocked)."""
        stmt = (
            select(PublicationRow)
            .where(PublicationRow.repo == repo, PublicationRow.status != "done")
            .order_by(PublicationRow.seq)
            .limit(1)
        )
        async with self._sessions() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return None if row is None else _from_row(row)

    async def update(self, pub: Publication) -> None:
        """Store the queue state, result and payload of ``pub`` (``LookupError`` if unknown)."""
        async with self._sessions() as session, session.begin():
            stmt = select(PublicationRow).where(PublicationRow.id == pub.id).with_for_update()
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                raise LookupError(f"publication {pub.id!r} does not exist")
            _fill_state(row, pub)

    async def for_proposal(self, proposal_id: str) -> Publication | None:
        stmt = select(PublicationRow).where(PublicationRow.proposal_id == proposal_id)
        async with self._sessions() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return None if row is None else _from_row(row)

    async def list(
        self, status: PublicationStatus | None = None, limit: int = 50
    ) -> list[Publication]:
        """Publications in queue order (oldest first), optionally of one status."""
        stmt = select(PublicationRow).order_by(PublicationRow.seq).limit(limit)
        if status is not None:
            stmt = stmt.where(PublicationRow.status == status)
        async with self._sessions() as session:
            rows = list((await session.execute(stmt)).scalars())
        return [_from_row(r) for r in rows]

    async def requeue_blocked(self, repo: str) -> int:
        """Move ``repo``'s blocked publications back to pending, due now; returns the count."""
        stmt = (
            update(PublicationRow)
            .where(PublicationRow.repo == repo, PublicationRow.status == "blocked")
            .values(status="pending", attempts=0, next_attempt_at=None)
        )
        async with self._sessions() as session, session.begin():
            result = await session.execute(stmt)
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def counts(self, repo: str) -> dict[str, int]:
        stmt = (
            select(PublicationRow.status, func.count())
            .where(PublicationRow.repo == repo)
            .group_by(PublicationRow.status)
        )
        async with self._sessions() as session:
            rows = (await session.execute(stmt)).all()
        out: dict[str, int] = dict.fromkeys(STATUSES, 0)
        for status, n in rows:
            out[status] = int(n)
        return out
