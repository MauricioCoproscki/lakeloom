"""Publish an applied proposal to Git (FR-GIT-04, FR-GIT-05; ADR 0013 § Inline attempt).

:class:`GitHook` runs after a successful approve has stored ``applied``:

1. under a per-model lock, :func:`~lakeloom.gitstore.snapshot.take_snapshot` and
   ``enqueue`` (so queue order equals OM state order), in a task of its own;
2. one ``process()`` in a background task, awaited for at most
   ``LAKELOOM_GIT_PUBLISH_TIMEOUT_SECONDS`` (the task keeps running after a timeout);
3. when the publication is not ``done``, a warning on the approving review record, then a
   store of the proposal that runs to completion.

It never raises: OM has applied the proposal, and Git must never fail or undo that. Failures
are logged by type only (no remote URL, no credential).

**Cancellation.** The approval is already stored when the hook starts, so a cancel of the
calling request (a client that went away) is absorbed rather than turned into a failed
approve: the snapshot and enqueue task always completes (FR-GIT-09: the publication is never
lost), the inline wait is abandoned (the ``process()`` task keeps running), the record gets
``Git: queued (request cancelled); it is retried automatically``, and the cancel request is
withdrawn (``Task.uncancel``), so the approve returns and is audited as what it is: applied.
A cancel scope that delivers the cancel again at a later await (anyio) still ends the
request, but only after the publication row exists.
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine, Sequence
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from lakeloom.config import Settings
from lakeloom.core.model_policy import ModelPolicyEntry, git_mode_for
from lakeloom.core.models import ModelInfo, ModelService
from lakeloom.core.proposals import Proposal
from lakeloom.gitstore.git import redact
from lakeloom.gitstore.layout import Snapshot
from lakeloom.gitstore.snapshot import take_snapshot
from lakeloom.state.publications import Publication, new_publication_id

__all__ = ["CANCELLED_WARNING", "INTERNAL_WARNING", "GitHook", "Publisher"]

INTERNAL_WARNING: Final = "Git: queued (internal error); it is retried automatically"
CANCELLED_WARNING: Final = "Git: queued (request cancelled); it is retried automatically"
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


def _absorb_cancel() -> None:
    """Withdraw a cancel request of the running task (the approval is already stored)."""
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        task.uncancel()


async def _complete[T](task: asyncio.Task[T]) -> bool:
    """Wait until ``task`` is done even if the caller is cancelled meanwhile.

    Returns whether the caller was cancelled (the cancel is absorbed). The task's own result
    or exception is left for ``task.result()``.
    """
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                break
            cancelled = True
            _absorb_cancel()
        except Exception:
            break
    return cancelled


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
        # Strong references: tasks that outlive the call (a timed-out or abandoned attempt).
        self.tasks: set[asyncio.Task[Any]] = set()

    def _spawn[T](self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self._done)
        return task

    async def after_apply(
        self,
        p: Proposal,
        info: ModelInfo | None,
        save: Callable[[Proposal], Coroutine[Any, Any, None]],
    ) -> None:
        """Snapshot, enqueue and try to publish applied ``p``; warn on its review record."""
        if not self._publisher.enabled:
            return
        try:
            warnings = await self._publish(p, info)
        except Exception as exc:
            _log.error("proposal %s: Git publication failed (%s)", p.id, type(exc).__name__)
            warnings = [INTERNAL_WARNING]
        if warnings:
            p.reviews[-1].warnings.extend(warnings)
            await _complete(self._spawn(save(p)))

    async def _publish(self, p: Proposal, info: ModelInfo | None) -> list[str]:
        enqueued = self._spawn(self._enqueue(p, info))
        cancelled = await _complete(enqueued)
        if enqueued.cancelled():  # only at shutdown: nobody cancels it otherwise
            raise RuntimeError("the Git snapshot was cancelled")
        snap = enqueued.result()  # its exception, if any, is an internal error
        warnings: list[str] = []
        if snap.exports is None:
            warnings.append(
                "Git: the export at apply failed; it is exported again when it is published"
            )
        task = self._spawn(self._publisher.process())
        if cancelled:
            return [*warnings, CANCELLED_WARNING]
        timed_out = False
        try:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=self._settings.git_publish_timeout_seconds
            )
        except TimeoutError:
            timed_out = True
        except asyncio.CancelledError:
            if task.cancelled():  # the attempt itself was cancelled (shutdown)
                raise RuntimeError("the Git publication attempt was cancelled") from None
            _absorb_cancel()  # the caller went away; the attempt keeps running
            return [*warnings, CANCELLED_WARNING]
        pub = await self._publisher.for_proposal(p.id)
        if pub is None or pub.status == "done":
            return warnings
        error = _clean(pub.last_error)
        if pub.status == "blocked":
            warnings.append(_blocked(error or "unknown error"))
        else:
            warnings.append(_queued(error or ("timed out" if timed_out else "pending")))
        return warnings

    async def _enqueue(self, p: Proposal, info: ModelInfo | None) -> Snapshot:
        """Snapshot and enqueue under the model's lock (queue order = OM state order)."""
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
        return snap

    def _done(self, task: asyncio.Task[Any]) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            _log.error("background Git task failed (%s)", type(exc).__name__)
