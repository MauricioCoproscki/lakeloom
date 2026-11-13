"""Publish an applied proposal to Git (FR-GIT-04, FR-GIT-05; ADR 0013 § Inline attempt).

:class:`GitHook` runs after a successful approve has stored ``applied``:

1. under a per-model lock, :func:`~lakeloom.gitstore.snapshot.take_snapshot` and
   ``enqueue`` (so queue order equals OM state order);
2. one ``process()`` in a background task, awaited for at most
   ``LAKELOOM_GIT_PUBLISH_TIMEOUT_SECONDS`` (the task keeps running after a timeout);
3. when the publication is not ``done``, a warning on the approving review record, then a
   best-effort, shielded store of the proposal.

It never raises (except ``CancelledError``): OM has applied the proposal, and Git must never
fail or undo that. Failures are logged by type only (no remote URL, no credential).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from lakeloom.config import Settings
from lakeloom.core.model_policy import ModelPolicyEntry, git_mode_for
from lakeloom.core.models import ModelInfo, ModelService
from lakeloom.core.proposals import Proposal
from lakeloom.gitstore.git import redact
from lakeloom.gitstore.snapshot import take_snapshot
from lakeloom.state.publications import Publication, new_publication_id

__all__ = ["INTERNAL_WARNING", "GitHook", "Publisher"]

INTERNAL_WARNING: Final = "Git: queued (internal error); it is retried automatically"
_ERROR_MAX: Final = 300
_log = logging.getLogger(__name__)


class Publisher(Protocol):
    """What the hook needs of :class:`~lakeloom.gitstore.publisher.GitPublisher`."""

    enabled: bool
    repo_key: str

    async def enqueue(self, pub: Publication) -> Publication: ...

    async def process(self, *, now: datetime | None = None) -> list[Publication]: ...

    async def for_proposal(self, proposal_id: str) -> Publication | None: ...


def _queued(reason: str) -> str:
    return f"Git: queued ({reason}); it is retried automatically"


def _blocked(error: str) -> str:
    return f"Git: blocked ({error}); an operator must run lakeloom git retry"


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    return " ".join(redact(text).split())[:_ERROR_MAX] or None


class GitHook:
    def __init__(
        self,
        publisher: Publisher,
        models: ModelService,
        settings: Settings,
        policies: Sequence[ModelPolicyEntry],
    ) -> None:
        self._publisher = publisher
        self._models = models
        self._settings = settings
        self._policies = policies
        self._locks: dict[str, asyncio.Lock] = {}
        # Strong references: an inline attempt that timed out keeps running in the background.
        self.tasks: set[asyncio.Task[Any]] = set()

    async def after_apply(
        self,
        p: Proposal,
        info: ModelInfo | None,
        save: Callable[[Proposal], Awaitable[None]],
    ) -> None:
        """Snapshot, enqueue and try to publish applied ``p``; warn on its review record."""
        if not self._publisher.enabled:
            return
        try:
            warnings = await self._publish(p, info)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.error("proposal %s: Git publication failed (%s)", p.id, type(exc).__name__)
            warnings = [INTERNAL_WARNING]
        if warnings:
            p.reviews[-1].warnings.extend(warnings)
            await asyncio.shield(save(p))

    async def _publish(self, p: Proposal, info: ModelInfo | None) -> list[str]:
        warnings: list[str] = []
        approval = p.reviews[-1]
        pub_id = new_publication_id()
        lock = self._locks.setdefault(p.model, asyncio.Lock())
        async with lock:
            snap = await take_snapshot(
                self._models, self._models.om, p, approval, pub_id, info=info
            )
            now = datetime.now(UTC)
            await self._publisher.enqueue(
                Publication(
                    id=pub_id,
                    repo=self._publisher.repo_key,
                    model=p.model,
                    proposal_id=p.id,
                    mode=git_mode_for(self._policies, p.model, self._settings.git_mode),
                    created_at=now,
                    updated_at=now,
                    payload=snap,
                )
            )
        if snap.exports is None:
            warnings.append(
                "Git: the export at apply failed; it is exported again when it is published"
            )
        task = asyncio.create_task(self._publisher.process())
        self.tasks.add(task)
        task.add_done_callback(self._done)
        timed_out = False
        try:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=self._settings.git_publish_timeout_seconds
            )
        except TimeoutError:
            timed_out = True
        pub = await self._publisher.for_proposal(p.id)
        if pub is None or pub.status == "done":
            return warnings
        error = _clean(pub.last_error)
        if pub.status == "blocked":
            warnings.append(_blocked(error or "unknown error"))
        else:
            warnings.append(_queued(error or ("timed out" if timed_out else "pending")))
        return warnings

    def _done(self, task: asyncio.Task[Any]) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            _log.error("background Git publication failed (%s)", type(exc).__name__)
