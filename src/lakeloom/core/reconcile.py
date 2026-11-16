"""Reconciliation: OpenMetadata against the last Git commit of each model (FR-GIT-08).

A *direct edit* is a change made in OpenMetadata outside Lakeloom (ADR 0017 § Research: a
term ``PATCH`` outside any change set). :meth:`ReconcileService.check` compares one model's
current OM export with ``ontology.ttl`` at the commit of its latest ``done`` publication:

- an approval not in Git yet: ``skipped`` ("publication in flight"), since OM is ahead of
  Git by design until it is published. That is a pending or blocked publication, an
  ``applied`` proposal without a publication row (``GitHook`` inserts it after the apply), or
  a pending proposal with a change set applied in OM (mid-apply, or stopped part-way). It is
  checked after the OM read and again right before a commit is enqueued (``_in_flight``);
- no done publication: ``skipped`` ("never published");
- the baseline commit missing even after fetching the target branch and the publication's
  branch (a squash-merged pull request): ``skipped`` ("baseline commit not found");
- OM unreachable: ``error``.

Both graphs go through :func:`~lakeloom.rdf.semantic.semantic_view` (bookkeeping, glossary
id, prefixes and triple order never count) and :func:`~lakeloom.core.release_diff.diff_graphs`:
no bump is ``in_sync``, anything else ``drifted`` with the added, changed and removed terms.

:meth:`ReconcileService.run` checks every model with a done publication; with ``commit`` it
enqueues one publication per drifted model (``proposal_id`` ``rec_<ulid>``), built through the
same export step as an applied proposal (:func:`~lakeloom.gitstore.snapshot.exports_or_base`,
FR-GIT-03), in the model's publication mode. Every Git write goes through the outbox
(FR-GIT-09). OpenMetadata is only read (FR-OM-02): nothing is ever reverted.

A run writes one audit row per drifted model (principal ``system:reconcile``), the
``lakeloom_reconcile_*`` metrics and one log line per drifted model with counts only (term
IRIs come from OM and are never logged, NFR-SEC-11). Term IRIs in :class:`ReconcileResult` are
untrusted: a tool or CLI that shows them must quote them.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol

from pydantic import BaseModel, Field
from rdflib import Graph

from lakeloom import metrics
from lakeloom.config import Settings
from lakeloom.core.audit import AuditRecord, record
from lakeloom.core.callers import Actor
from lakeloom.core.model_create import validate_slug
from lakeloom.core.model_policy import ModelPolicyEntry, git_mode_for
from lakeloom.core.models import ModelInfo, ModelService
from lakeloom.core.proposals import Proposal
from lakeloom.core.release_diff import ReleaseDiff, diff_graphs
from lakeloom.errors import LakeloomError
from lakeloom.gitstore.git import GitError
from lakeloom.gitstore.layout import RECONCILE_ORIGIN, Snapshot, build_reconcile_message
from lakeloom.gitstore.publisher import repo_lock
from lakeloom.gitstore.snapshot import exports_or_base
from lakeloom.om.client import OMError
from lakeloom.rdf.canonical import parse_rdf
from lakeloom.rdf.semantic import semantic_view
from lakeloom.state.audit import AuditRepository
from lakeloom.state.ids import new_ulid_id
from lakeloom.state.proposals import PENDING_STATUSES, ProposalRepository
from lakeloom.state.publications import Publication, PublicationRepository, new_publication_id

__all__ = [
    "RECONCILE_ACTOR",
    "ReconcileResult",
    "ReconcileRun",
    "ReconcileService",
    "ReconcileStatus",
]

_log = logging.getLogger("lakeloom.reconcile")

ReconcileStatus = Literal["in_sync", "drifted", "skipped", "error"]
STATUSES: Final[tuple[ReconcileStatus, ...]] = ("in_sync", "drifted", "skipped", "error")
RECONCILE_ACTOR: Final = Actor(subject="system:reconcile", type="system")
TOOL: Final = "reconcile"
IN_FLIGHT: Final = "publication in flight"
NEVER_PUBLISHED: Final = "never published"
NO_BASELINE: Final = "baseline commit not found"
NOT_EXPORTED: Final = "not committed (export failed)"
NOT_QUEUED: Final = "not committed (internal error)"
_COMMIT_ERRORS: Final = {NOT_EXPORTED: "export_failed", NOT_QUEUED: "internal"}
_NOT_CONFIGURED = LakeloomError(
    "validation_error",
    "Git publishing is not configured",
    "set LAKELOOM_GIT_REMOTE to reconcile OpenMetadata with Git",
)


class ReconcileResult(BaseModel):
    """One model's state. ``publication_id`` is the publication a commit enqueued."""

    model: str
    status: ReconcileStatus
    reason: str | None = None
    added: list[str] = Field(default_factory=list[str])
    changed: list[str] = Field(default_factory=list[str])
    removed: list[str] = Field(default_factory=list[str])
    publication_id: str | None = None


class ReconcileRun(BaseModel):
    at: datetime
    results: list[ReconcileResult]

    def summary(self) -> dict[str, Any]:
        """The ``jobs.reconcile`` block of ``get_server_info`` (slugs and counts only)."""
        counts = dict.fromkeys(STATUSES, 0)
        for r in self.results:
            counts[r.status] += 1
        return {
            "last_run": self.at.isoformat(),
            "counts": counts,
            "drifted": [r.model for r in self.results if r.status == "drifted"],
        }


class Baseline(Protocol):
    """What reconciliation needs of :class:`~lakeloom.gitstore.repo.WorkingCopy`."""

    async def ensure(self) -> None: ...

    async def has_commit(self, sha: str) -> bool: ...

    async def fetch_branch(self, branch: str) -> str: ...

    async def show(self, ref: str, path: str) -> bytes | None: ...


class Publisher(Protocol):
    """What reconciliation needs of :class:`~lakeloom.gitstore.publisher.GitPublisher`."""

    enabled: bool
    repo_key: str

    @property
    def working_copy(self) -> Baseline | None: ...

    async def enqueue(self, pub: Publication) -> Publication: ...


@dataclass
class _Checked:
    result: ReconcileResult
    last: Publication | None = None
    info: ModelInfo | None = None
    diff: ReleaseDiff | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _compare(baseline: bytes, current: Graph, base_iri: str) -> ReleaseDiff:
    """CPU-bound: parse the baseline, then diff the semantic views (in a worker thread)."""
    old = semantic_view(parse_rdf(baseline, "turtle"), base_iri)
    return diff_graphs(old, semantic_view(current, base_iri), from_version=None, to_version="")


def _om_reason(exc: LakeloomError) -> str:
    if exc.code == "upstream_unavailable":
        return "OpenMetadata unavailable"
    if exc.code == "not_found":
        return "model not found in OpenMetadata"
    return f"OpenMetadata read failed ({exc.code})"


class ReconcileService:
    def __init__(
        self,
        *,
        settings: Settings,
        models: ModelService,
        publisher: Publisher,
        publications: PublicationRepository,
        proposals: ProposalRepository,
        audit: AuditRepository | None,
        policies: Sequence[ModelPolicyEntry],
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._settings = settings
        self._models = models
        self._publisher = publisher
        self._pubs = publications
        self._proposals = proposals
        self._audit = audit
        self._policies = list(policies)
        self._clock = clock
        self.last_run: ReconcileRun | None = None

    # --- entry points ---------------------------------------------------------------------

    async def check(self, slug: str) -> ReconcileResult:
        """Compare ``slug``'s OM state with its last Git commit; writes nothing."""
        self._require_git()
        return (await self._check(slug)).result

    async def run(self, *, commit: bool, models: Sequence[str] | None = None) -> ReconcileRun:
        """Check every published model (or only ``models``, ``lakeloom reconcile --model``);
        with ``commit``, enqueue each drift's publication. Only a full run sets the metrics
        and :attr:`last_run` (the ``jobs.reconcile`` block of ``get_server_info``)."""
        self._require_git()
        if models is not None:
            for slug in models:
                validate_slug(slug)
        at = self._clock()
        results: list[ReconcileResult] = []
        slugs = (
            list(models)
            if models is not None
            else await self._pubs.done_models(self._publisher.repo_key)
        )
        for slug in slugs:
            try:
                checked = await self._check(slug)
            except Exception as exc:  # one model's failure never stops the run
                _log.error("reconcile %s failed (%s)", slug, type(exc).__name__)
                reason = f"internal error ({type(exc).__name__})"
                checked = _Checked(ReconcileResult(model=slug, status="error", reason=reason))
            result = checked.result
            rec_id: str | None = None
            if result.status == "drifted" and commit:
                result, rec_id = await self._commit(checked)
            if result.status == "drifted":
                await self._report(result, rec_id)
            results.append(result)
        run = ReconcileRun(at=at, results=results)
        if models is not None:
            return run
        drifted = sum(r.status == "drifted" for r in results)
        metrics.RECONCILE_DRIFTED.set(drifted)
        outcome = (
            "error"
            if any(r.status == "error" for r in results)
            else ("drifted" if drifted else "in_sync")
        )
        metrics.RECONCILE_RUNS.labels(outcome=outcome).inc()
        self.last_run = run
        return run

    # --- check ----------------------------------------------------------------------------

    def _require_git(self) -> Baseline:
        wc = self._publisher.working_copy
        if not self._publisher.enabled or wc is None:
            raise _NOT_CONFIGURED
        return wc

    async def _check(self, slug: str) -> _Checked:
        repo = self._publisher.repo_key

        def result(status: ReconcileStatus, reason: str | None = None) -> _Checked:
            return _Checked(ReconcileResult(model=slug, status=status, reason=reason))

        if await self._pubs.has_open(repo, slug):
            return result("skipped", IN_FLIGHT)
        last = await self._pubs.last_done(repo, slug)
        if last is None:
            return result("skipped", NEVER_PUBLISHED)
        try:
            baseline = await self._baseline(last)
        except GitError as exc:
            return result("error", f"Git unavailable ({exc.kind})")
        if baseline is None:
            return result("skipped", NO_BASELINE)
        try:
            self._models.invalidate(slug)  # a term PATCH leaves the model version unchanged
            info = await self._models.get_model(slug)
            current = await self._models.graph(slug)
        except LakeloomError as exc:
            return result("error", _om_reason(exc))
        base_iri = info.base_iri or last.payload.base_iri
        if not base_iri:
            return result("error", "the model has no base IRI")
        try:
            diff = await asyncio.to_thread(_compare, baseline, current, base_iri)
        except LakeloomError:
            return result("error", "the Git baseline is not readable Turtle")
        # An approval applying (or applied) meanwhile moves OM ahead of Git by design.
        try:
            if await self._in_flight(last):
                return result("skipped", IN_FLIGHT)
        except OMError:
            return result("error", "OpenMetadata unavailable")
        if diff.bump is None:
            return _Checked(ReconcileResult(model=slug, status="in_sync"), last, info, diff)
        drifted = ReconcileResult(
            model=slug,
            status="drifted",
            added=[t.iri for t in diff.added],
            changed=[t.iri for t in diff.changed],
            removed=[t.iri for t in diff.removed],
        )
        return _Checked(drifted, last, info, diff)

    async def _in_flight(self, last: Publication) -> bool:
        """Whether an approval of ``last.model`` may have changed OM without being in Git.

        Called after the OM read. The checks follow an approval's own order (change sets
        applied in OM while the proposal is still pending -> ``applied`` without a
        publication row -> a pending or blocked row -> a new done row), so a proposal that
        moves on between two checks is caught by a later one. Applied proposals are looked
        at from the baseline publication's creation on (older ones have their row); a
        pending proposal's change sets are read from OM (bot client, reads only).
        """
        slug, repo = last.model, self._publisher.repo_key
        candidates = await self._proposals.unpublished_candidates(
            slug, applied_since=last.created_at
        )
        for p in candidates:
            if p.status in PENDING_STATUSES and await self._applied_in_om(p):
                return True
        for p in candidates:
            if p.status == "applied" and await self._pubs.for_proposal(p.id) is None:
                return True
        if await self._pubs.has_open(repo, slug):
            return True
        latest = await self._pubs.last_done(repo, slug)
        return latest is None or latest.id != last.id

    async def _applied_in_om(self, p: Proposal) -> bool:
        """Any of pending ``p``'s change sets applied (stored, or in OM now: mid-apply)."""
        for part in p.parts:
            for side, cs_id in part.change_set_ids.items():
                if cs_id is None:
                    continue
                if part.om_state.get(side) == "APPLIED":  # an approval that stopped part-way
                    return True
                cs = await self._models.om.get_change_set(cs_id)
                if cs is not None and cs.state == "APPLIED":
                    return True
        return False

    async def _baseline(self, last: Publication) -> bytes | None:
        """``ontology.ttl`` at ``last``'s commit, fetching the target branch, then ``last``'s
        branch, when the commit is not local. ``None`` when the commit cannot be found (or,
        never for a publication commit, it has no ``ontology.ttl``: also "not found").
        Any other Git failure raises."""
        wc = self._publisher.working_copy
        sha = last.commit_sha
        if wc is None or not sha:
            return None
        branches = dict.fromkeys(b for b in (self._settings.git_branch, last.branch) if b)
        try:
            async with repo_lock(self._publisher.repo_key):
                await wc.ensure()
                found = await wc.has_commit(sha)
                for branch in branches:
                    if found:
                        break
                    try:
                        await wc.fetch_branch(branch)
                    except GitError as exc:
                        if exc.kind != "not_found":  # a deleted (merged) PR branch
                            raise
                        continue
                    found = await wc.has_commit(sha)
                if not found:
                    return None
                return await wc.show(sha, f"ontologies/{last.model}/ontology.ttl")
        except ValueError:  # an unsafe stored sha or branch name is never used
            return None

    # --- commit and report ----------------------------------------------------------------

    async def _commit(self, checked: _Checked) -> tuple[ReconcileResult, str | None]:
        """Enqueue the publication of a drift: the result naming it (or why it was not)
        and the reconcile id."""
        result, last, info, diff = checked.result, checked.last, checked.info, checked.diff
        if last is None or info is None or diff is None:
            raise RuntimeError("only a drifted check is committed")
        slug = result.model
        rec_id = new_ulid_id("rec_")
        pub_id = new_publication_id()
        now = self._clock()
        base = Snapshot(
            model=slug,
            glossary_id=info.glossary_id,
            glossary_fqn=info.fqn,
            base_iri=info.base_iri,
            prefixes=dict(info.prefixes),
            model_version="",
            last_change_set=last.payload.last_change_set,  # no change set made this commit
            proposal_id=rec_id,
            provenance={
                "publication_id": pub_id,
                "model": slug,
                "origin": RECONCILE_ORIGIN,
                "reconcile_id": rec_id,
                "at": now.isoformat(),
            },
            message=build_reconcile_message(slug, diff, rec_id, pub_id),
        )
        try:
            snap = await exports_or_base(self._models, self._models.om, base, what=rec_id)
            if snap.exports is None:
                # Exporting at publish time could take in an approval made since: not here.
                return result.model_copy(update={"reason": NOT_EXPORTED}), None
            # The exports were read after the check: an approval since then is in them.
            if await self._in_flight(last):
                return result.model_copy(update={"status": "skipped", "reason": IN_FLIGHT}), None
            stored = await self._publisher.enqueue(
                Publication(
                    id=pub_id,
                    repo=self._publisher.repo_key,
                    model=slug,
                    proposal_id=rec_id,
                    mode=git_mode_for(self._policies, slug, self._settings.git_mode),
                    created_at=now,
                    updated_at=now,
                    payload=snap,
                )
            )
        except Exception as exc:
            _log.error("reconcile %s: publication not queued (%s)", slug, type(exc).__name__)
            return result.model_copy(update={"reason": NOT_QUEUED}), None
        return result.model_copy(update={"publication_id": stored.id}), stored.proposal_id

    async def _report(self, result: ReconcileResult, reconcile_id: str | None) -> None:
        failed = _COMMIT_ERRORS.get(result.reason or "")
        _log.info(
            "reconcile: direct edits in OpenMetadata",
            extra={
                "model": result.model,
                "added": len(result.added),
                "changed": len(result.changed),
                "removed": len(result.removed),
                "publication_id": result.publication_id,
            },
        )
        await record(
            self._audit,
            AuditRecord(
                at=self._clock(),
                principal_subject=RECONCILE_ACTOR.subject,
                principal_type=RECONCILE_ACTOR.type,
                tool=TOOL,
                model=result.model,
                proposal_id=reconcile_id,  # the outbox row's proposal_id
                change_set_id=result.publication_id,
                result="error" if failed else "ok",
                error_code=failed,
            ),
        )
