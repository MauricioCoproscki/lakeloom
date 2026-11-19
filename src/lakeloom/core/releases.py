"""Releases: a Git tag over OpenMetadata's current state (FR-GIT-06, FR-GIT-09, FR-AGT-03).

:meth:`ReleaseService.plan` is the dry run: it computes the version (semantic diff against the
previous release's ``ontology.ttl``), the CHANGELOG section and a confirm token bound to
``sha256(model_version + "\\n" + previous tag object sha | "none")``. :meth:`ReleaseService.create`
takes the repository lock (shared with the publication worker and the tag reader), checks
idempotency, plans again, verifies the token against the fresh state, then commits the release
directly on the target branch, tags it and pushes branch and tag atomically. A rejected push
deletes the local tag, resets to the new tip and renders again (at most 3 times); any other
failure deletes the local tag, resets and is reported (nothing is queued). The release id is
derived from the token, so a retry of the same confirmed call never makes a second tag.

Lock discipline: ``asyncio.Lock`` is not re-entrant, so nothing here calls the
:class:`ReleaseReader` reads while holding ``repo_lock``; the working copy is used directly and
only :meth:`ReleaseReader.invalidate` is called. OpenMetadata is only read (bot client).
"""

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from pydantic import BaseModel

from lakeloom.config import Settings
from lakeloom.core.annotations import applied_operations
from lakeloom.core.audit import AuditContext, AuditRecord, current_audit, record
from lakeloom.core.callers import Caller
from lakeloom.core.confirm import ConfirmTokens
from lakeloom.core.model_create import SLUG_PATTERN
from lakeloom.core.model_policy import (
    ModelPolicyEntry,
    access_for,
    require_group,
    triplestore_for,
)
from lakeloom.core.models import ModelService
from lakeloom.core.release_diff import (
    Bump,
    ReleaseDiff,
    Version,
    diff_graphs,
    format_version,
    next_version,
    parse_version,
)
from lakeloom.errors import LakeloomError
from lakeloom.gitstore.git import GitError
from lakeloom.gitstore.layout import Snapshot
from lakeloom.gitstore.publisher import INITIAL_MESSAGE, INITIAL_README, repo_lock, utcnow
from lakeloom.gitstore.release_layout import (
    ProposalEntry,
    changelog_section,
    release_message,
    render_changelog,
    render_release,
    tag_message,
    tag_name,
)
from lakeloom.gitstore.release_reader import ReleaseReader
from lakeloom.gitstore.repo import TagInfo, WorkingCopy
from lakeloom.gitstore.snapshot import fill_exports
from lakeloom.om.client import OMClient
from lakeloom.rdf.canonical import parse_rdf
from lakeloom.state.audit import AuditRepository
from lakeloom.state.proposals import ProposalRepository
from lakeloom.state.publications import PublicationRepository
from lakeloom.state.releases import Release, ReleaseRepository, SinkStatus

__all__ = ["CREATE_RELEASE_TOOL", "ReleasePlan", "ReleaseResult", "ReleaseService"]

_log = logging.getLogger(__name__)

CREATE_RELEASE_TOOL = "create_release"
MAX_RERENDERS = 3
_SLUG = re.compile(SLUG_PATTERN)
_MANIFEST = re.compile(r"^ontologies/([^/]+)/manifest\.yaml$")
_SECTION = re.compile(r"^## \[", re.MULTILINE)
_RELEASE_ID = "Lakeloom-Release-Id"
_RETRY_HINT = "retry shortly; a retry with the same confirm_token is safe"


class ReleasePlan(BaseModel):
    model: str
    version: str
    tag: str
    previous: str | None
    diff: ReleaseDiff
    requested_bump: Bump | None
    warnings: list[str]
    changelog_section: str
    confirm_token: str
    expires_at: int


_TRIPLESTORE: dict[SinkStatus, Literal["off", "queued", "done", "failed"]] = {
    "none": "off",
    "pending": "queued",
    "done": "done",
    "failed": "failed",
}


class ReleaseResult(BaseModel):
    release: Release
    plan: ReleasePlan
    already_done: bool
    triplestore: Literal["off", "queued", "done", "failed"]


@dataclass
class _Previous:
    version: Version
    tag: TagInfo
    ttl: bytes


@dataclass
class _GitState:
    previous: _Previous | None
    proposals: list[ProposalEntry]
    changelog: bytes | None


@dataclass
class _Computed:
    version: str
    diff: ReleaseDiff
    warnings: list[str]
    section: str
    digest: str
    snapshot: Snapshot


def _git_failure(exc: GitError) -> LakeloomError:
    if exc.kind == "auth":
        return LakeloomError(
            "forbidden",
            "the Git remote refused Lakeloom's credentials",
            "check LAKELOOM_GIT_TOKEN (or the SSH key) and its write access to the repository",
        )
    if exc.kind == "not_found":
        return LakeloomError(
            "not_found",
            "the Git remote repository or branch was not found",
            "check LAKELOOM_GIT_REMOTE and LAKELOOM_GIT_BRANCH",
        )
    return LakeloomError("upstream_unavailable", f"Git is unavailable ({exc.kind})", _RETRY_HINT)


def _token_expiry(token: str) -> int:
    """The expiry inside a confirm token (already verified or matched), else 0."""
    try:
        part = token.split(".", 1)[0]
        payload: object = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        expires = cast("dict[str, object]", payload).get("e") if isinstance(payload, dict) else None
    except (ValueError, UnicodeError, binascii.Error):
        return 0
    return expires if isinstance(expires, int) else 0


def _first_section(changelog: bytes | None) -> str:
    text = (changelog or b"").decode("utf-8", "replace")
    starts = [m.start() for m in _SECTION.finditer(text)]
    if not starts:
        return ""
    end = starts[1] if len(starts) > 1 else len(text)
    return text[starts[0] : end].strip() + "\n"


class ReleaseService:
    def __init__(
        self,
        settings: Settings,
        models: ModelService,
        om: OMClient,
        wc: WorkingCopy | None,
        reader: ReleaseReader | None,
        releases: ReleaseRepository,
        publications: PublicationRepository,
        confirm: ConfirmTokens,
        audit: AuditRepository | None,
        policies: Sequence[ModelPolicyEntry] = (),
        *,
        proposal_repo: ProposalRepository | None = None,
        clock: Callable[[], datetime] = utcnow,
        on_released: Callable[[Release], Awaitable[None]] | None = None,
    ) -> None:
        self._settings = settings
        self._models = models
        self._om = om
        self._wc = wc
        self._reader = reader
        self._releases = releases
        self._pubs = publications
        self._confirm = confirm
        self._audit = audit
        self._policies = list(policies)
        self._proposal_repo = proposal_repo
        self._clock = clock
        self._on_released = on_released
        remote = settings.git_remote or ""
        self.enabled: bool = bool(remote) and wc is not None
        self.repo_key: str = hashlib.sha256(remote.encode()).hexdigest() if self.enabled else ""

    # --- entry points -------------------------------------------------------------------

    async def plan(
        self, principal: Caller, model: str, *, version: str | None, bump: Bump | None
    ) -> ReleasePlan:
        """The dry run: version, diff, CHANGELOG section and a confirm token."""
        wc = self._check(principal, model, version, bump)
        await self._require_published(model)
        try:
            async with repo_lock(self.repo_key):
                state = await self._git_state(wc, model, create=False)
        except GitError as exc:
            raise _git_failure(exc) from None
        computed = await self._compute(principal, model, state, version, bump)
        await self._require_free(wc, tag_name(model, computed.version))
        token, expires = self._confirm.issue(
            CREATE_RELEASE_TOOL, self._args(principal, model, version, bump), computed.digest
        )
        return self._plan_of(model, computed, state, bump, token, expires)

    async def create(
        self,
        principal: Caller,
        model: str,
        *,
        version: str | None,
        bump: Bump | None,
        confirm_token: str,
        client: str | None = None,
    ) -> ReleaseResult:
        """Commit, tag and push the release previewed by ``confirm_token`` (idempotent)."""
        trail = current_audit()
        trail.model = model
        try:
            result, stored = await self._create(
                trail, principal, model, version, bump, confirm_token
            )
        except LakeloomError as exc:
            await self._write_audit(principal, client, model, trail.change_set_id, exc.code)
            raise
        except BaseException as exc:
            code = "cancelled" if isinstance(exc, asyncio.CancelledError) else "internal"
            await asyncio.shield(
                self._write_audit(principal, client, model, trail.change_set_id, code)
            )
            raise
        trail.change_set_id = result.release.tag
        if stored and self._on_released is not None:
            try:
                await self._on_released(result.release)
            except Exception as exc:  # the sink never fails the release (FR-TS-03)
                _log.warning("release %s: on_released failed (%s)", result.release.id, exc)
        await self._write_audit(principal, client, model, result.release.tag, None)
        return result

    # --- create ---------------------------------------------------------------------------

    async def _create(
        self,
        trail: AuditContext,
        principal: Caller,
        model: str,
        version: str | None,
        bump: Bump | None,
        token: str,
    ) -> tuple[ReleaseResult, bool]:
        wc = self._check(principal, model, version, bump)
        if not token:
            raise LakeloomError(
                "validation_error",
                "confirm_token is empty",
                "call create_release without confirm_token to get a preview and a token",
            )
        release_id = "rel_" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:26]
        branch = self._settings.git_branch
        async with repo_lock(self.repo_key):
            try:
                await wc.ensure()
                await self._ensure_branch(wc, branch)
                await wc.checkout_tip(branch)
                await wc.fetch_tags(f"{model}/v*")
                done = await self._already_done(trail, wc, model, branch, release_id, bump, token)
                if done is not None:
                    return done
                await self._require_published(model)
                state = await self._git_state(wc, model, create=True)
                computed = await self._compute(principal, model, state, version, bump)
                tag = tag_name(model, computed.version)
                trail.change_set_id = tag
                self._confirm.verify(
                    token,
                    CREATE_RELEASE_TOOL,
                    self._args(principal, model, version, bump),
                    computed.digest,
                )
                await self._require_free(wc, tag)
                message = release_message(
                    model,
                    computed.version,
                    computed.diff,
                    self._released_by(principal),
                    computed.snapshot.model_version,
                    release_id,
                )
                sha = await self._commit_tag_push(wc, model, branch, tag, computed, message)
            except GitError as exc:
                raise _git_failure(exc) from None
            plan = self._plan_of(model, computed, state, bump, token, _token_expiry(token))
            if self._reader is not None:
                self._reader.invalidate(model)  # the tag is pushed, whatever happens next
            rel, stored = await self._record(
                release_id, model, computed.version, tag, sha, self._released_by(principal)
            )
        return self._result(rel, plan, already_done=False), stored

    async def _ensure_branch(self, wc: WorkingCopy, branch: str) -> None:
        if await wc.remote_head(branch) is None:
            await wc.init_branch(branch, {"README.md": INITIAL_README}, INITIAL_MESSAGE)
            await wc.push(branch, branch)

    async def _already_done(
        self,
        trail: AuditContext,
        wc: WorkingCopy,
        model: str,
        branch: str,
        release_id: str,
        bump: Bump | None,
        token: str,
    ) -> tuple[ReleaseResult, bool] | None:
        """The earlier push of this release succeeded: the result, the row stored if missing."""
        sha = await wc.find_trailer(f"origin/{branch}", _RELEASE_ID, release_id)
        if sha is None:
            return None
        tags = await wc.trailer_of(sha, "Lakeloom-Release")
        if not tags or not tags[0].startswith(f"{model}/v"):
            return None
        tag = tags[0]
        version = tag.removeprefix(f"{model}/v")
        try:
            parsed = parse_version(version)
        except LakeloomError:
            return None
        if tag != tag_name(model, version) or await wc.remote_tag(tag) != sha:
            return None  # the tag was removed or moved since: not ours to report
        ttl = await wc.show(sha, f"ontologies/{model}/ontology.ttl") or b""
        previous = await self._previous(wc, model, below=parsed)
        old = await asyncio.to_thread(parse_rdf, previous.ttl, "turtle") if previous else None
        new = await asyncio.to_thread(parse_rdf, ttl, "turtle")
        diff = diff_graphs(
            old,
            new,
            from_version=format_version(previous.version) if previous else None,
            to_version=version,
        )
        plan = ReleasePlan(
            model=model,
            version=version,
            tag=tag,
            previous=format_version(previous.version) if previous else None,
            diff=diff,
            requested_bump=bump,
            warnings=[],
            changelog_section=_first_section(
                await wc.show(sha, f"ontologies/{model}/CHANGELOG.md")
            ),
            confirm_token=token,
            expires_at=_token_expiry(token),
        )
        trail.change_set_id = tag
        if self._reader is not None:
            self._reader.invalidate(model)
        by = await wc.trailer_of(sha, "Released-By")
        rel, stored = await self._record(
            release_id, model, version, tag, sha, by[0] if by else "unknown"
        )
        return self._result(rel, plan, already_done=True), stored

    async def _commit_tag_push(
        self,
        wc: WorkingCopy,
        model: str,
        branch: str,
        tag: str,
        computed: _Computed,
        message: str,
    ) -> str:
        """Render on the tip, commit, tag, push atomically; re-render after a rejection."""
        annotation = tag_message(model, computed.version, computed.section)
        for attempt in range(MAX_RERENDERS + 1):
            if attempt:
                await wc.checkout_tip(branch)
            try:
                files, _ = self._render(wc, model, computed)
                wc.write(files)
                sha = await wc.commit(message)
                await wc.tag_annotated(tag, annotation)
                await wc.push_atomic(branch, tag)
            except BaseException as exc:
                await asyncio.shield(self._discard(wc, branch, tag))
                if not isinstance(exc, GitError) or exc.kind != "rejected":
                    raise
                if await wc.remote_tag(tag) is not None:
                    raise _exists(tag) from None
                if attempt < MAX_RERENDERS:
                    _log.info("release push of %s rejected; rendering on the new tip", tag)
                    continue
                raise LakeloomError(
                    "upstream_unavailable", "the branch kept moving; retry", _RETRY_HINT
                ) from None
            return sha
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    async def _discard(wc: WorkingCopy, branch: str, tag: str) -> None:
        """Drop the local tag and reset to the remote tip (never raises)."""
        try:
            if any(t.name == tag for t in await wc.tags(tag.split("/", 1)[0] + "/")):
                await wc.delete_tag(tag)
        except GitError as exc:
            _log.warning("could not delete local tag %s: %s", tag, exc.kind)
        try:
            await wc.checkout_tip(branch)
        except GitError as exc:
            _log.warning("could not reset to origin/%s: %s", branch, exc.kind)

    def _render(
        self, wc: WorkingCopy, model: str, computed: _Computed
    ) -> tuple[dict[str, bytes], list[str]]:
        manifests: dict[str, bytes] = {}
        for path in wc.list_files("ontologies"):
            m = _MANIFEST.match(path)
            if m is not None and m.group(1) != model and (data := wc.read(path)) is not None:
                manifests[m.group(1)] = data
        changelog, warnings = render_changelog(
            model, wc.read(f"ontologies/{model}/CHANGELOG.md"), computed.section
        )
        files = render_release(
            computed.snapshot,
            version=computed.version,
            released_at=self._clock(),
            changelog=changelog,
            previous_manifest=wc.read(f"ontologies/{model}/manifest.yaml"),
            manifests=manifests,
        )
        return files, warnings

    async def _record(
        self, release_id: str, model: str, version: str, tag: str, sha: str, by: str
    ) -> tuple[Release, bool]:
        """The stored row (added now when missing) and whether this call stored it."""
        try:
            existing = await self._releases.get(release_id)
            if existing is not None:
                return existing, False
        except Exception as exc:
            _log.warning("release %s: state store unavailable (%s)", release_id, type(exc).__name__)
        queued = (
            self._settings.triplestore_enabled and triplestore_for(self._policies, model).enabled
        )
        rel = Release(
            id=release_id,
            repo=self.repo_key,
            model=model,
            version=version,
            tag=tag,
            commit_sha=sha,
            released_by=by,
            created_at=self._clock(),
            sink_status="pending" if queued else "none",
        )
        try:
            return await self._releases.add(rel), True
        except Exception as exc:
            # The tag is on the remote (kept): without the row there is no sink queue entry,
            # no hook and no audit of success, so fail; the retry's idempotent path stores it.
            _log.error("release %s: could not store its row (%s)", release_id, type(exc).__name__)
            raise LakeloomError(
                "upstream_unavailable",
                f"release {tag} was pushed but could not be recorded",
                "retry with the same confirm_token",
            ) from None

    def _result(self, rel: Release, plan: ReleasePlan, *, already_done: bool) -> ReleaseResult:
        return ReleaseResult(
            release=rel,
            plan=plan,
            already_done=already_done,
            triplestore=_TRIPLESTORE[rel.sink_status],
        )

    # --- shared planning ------------------------------------------------------------------

    def _check(
        self, principal: Caller, model: str, version: str | None, bump: Bump | None
    ) -> WorkingCopy:
        if not self.enabled or self._wc is None:
            raise LakeloomError(
                "validation_error",
                "releases need Git: Git publishing is off",
                "set LAKELOOM_GIT_REMOTE (and the Git credentials) to enable releases",
            )
        if not _SLUG.fullmatch(model):
            raise LakeloomError(
                "not_found", f"Unknown model {model[:64]!r}", "call list_models to see the models"
            )
        require_group(principal, access_for(self._policies, model), "publish", model=model)
        if version is not None and bump is not None:
            raise LakeloomError(
                "validation_error",
                "pass a version or a bump, not both",
                "omit both to release the computed version",
            )
        if version is not None:
            parse_version(version)
        if bump is not None and bump not in ("major", "minor", "patch"):
            raise LakeloomError(
                "validation_error", f"unknown bump {bump!r}", "pass major, minor or patch"
            )
        return self._wc

    async def _require_published(self, model: str) -> None:
        if await self._pubs.open_for_model(self.repo_key, model):
            raise LakeloomError(
                "validation_error",
                f"approved changes of {model} are not in Git yet",
                "wait for the Git publication to finish, or run `lakeloom git retry`",
            )

    async def _require_free(self, wc: WorkingCopy, tag: str) -> None:
        try:
            exists = await wc.remote_tag(tag) is not None
        except GitError as exc:
            raise _git_failure(exc) from None
        if exists:
            raise _exists(tag)

    async def _git_state(self, wc: WorkingCopy, model: str, *, create: bool) -> _GitState:
        """The previous release, the proposals since it and the tip's CHANGELOG (locked)."""
        branch = self._settings.git_branch
        if not create:
            await wc.ensure()
            tip = await wc.remote_head(branch) is not None
            if tip:
                await wc.checkout_tip(branch)
            await wc.fetch_tags(f"{model}/v*")
        else:
            tip = True  # create() made the branch and checked out its tip
        previous = await self._previous(wc, model)
        proposals: list[ProposalEntry] = []
        if tip:
            since = previous.tag.name if previous is not None else None
            for entry in await wc.log_since(
                since, f"ontologies/{model}", ("Lakeloom-Proposal", "Approved-By")
            ):
                ids = entry.trailers.get("Lakeloom-Proposal")
                if not ids:
                    continue
                approved = entry.trailers.get("Approved-By") or ["unknown"]
                proposals.append(
                    ProposalEntry(
                        subject=entry.subject,
                        proposal_id=ids[0],
                        approved_by=approved[0],
                        short_sha=entry.sha[:7],
                    )
                )
        changelog = wc.read(f"ontologies/{model}/CHANGELOG.md") if tip else None
        return _GitState(previous=previous, proposals=proposals, changelog=changelog)

    async def _previous(
        self, wc: WorkingCopy, model: str, *, below: Version | None = None
    ) -> _Previous | None:
        """The highest release (below ``below``): a semver tag on the remote holding the
        model's ``ontology.ttl``, as the reader counts them. A local-only tag (left by a crash
        between tag and push) is deleted on the way."""
        found: list[tuple[Version, TagInfo]] = []
        for tag in await wc.tags(f"{model}/"):
            version = tag.name.removeprefix(f"{model}/v")
            try:
                parsed = parse_version(version)
            except LakeloomError:
                continue
            if tag.name == tag_name(model, version) and (below is None or parsed < below):
                found.append((parsed, tag))
        found.sort(key=lambda item: item[0], reverse=True)
        for parsed, tag in found:
            if await wc.remote_tag(tag.name) is None:
                _log.warning("deleting local-only release tag %s", tag.name)
                await wc.delete_tag(tag.name)
                continue
            ttl = await wc.show(tag.commit_sha, f"ontologies/{model}/ontology.ttl")
            if ttl is not None:
                return _Previous(version=parsed, tag=tag, ttl=ttl)
        return None

    async def _compute(
        self,
        principal: Caller,
        model: str,
        state: _GitState,
        version: str | None,
        bump: Bump | None,
    ) -> _Computed:
        base = Snapshot(
            model=model,
            glossary_id="",
            glossary_fqn=model,
            model_version="",
            proposal_id="",
            message="",
        )
        # Collect applied ops so fill_exports can produce an enriched skos.ttl (M8).
        applied_ops = (
            await applied_operations(self._proposal_repo, model)
            if self._proposal_repo is not None
            else None
        )
        try:
            snap = await fill_exports(self._models, self._om, base, ops=applied_ops)
        except RuntimeError as exc:  # the model moved between the version reads
            raise LakeloomError(
                "conflict", str(exc), "retry: the model changed while it was being exported"
            ) from None
        ttl = (snap.exports or {}).get("ontology.ttl", "").encode("utf-8")
        prev = state.previous
        old = await asyncio.to_thread(parse_rdf, prev.ttl, "turtle") if prev else None
        new = await asyncio.to_thread(parse_rdf, ttl, "turtle")
        prev_text = format_version(prev.version) if prev else None
        diff = diff_graphs(old, new, from_version=prev_text, to_version="")
        computed = next_version(prev.version if prev else None, diff.bump)
        if version is not None:
            chosen = parse_version(version)
        elif bump is not None:
            chosen = next_version(prev.version if prev else None, bump)
        else:
            chosen = computed
        if prev is not None and chosen <= prev.version:
            raise LakeloomError(
                "validation_error",
                f"version {format_version(chosen)} is not greater than the latest release "
                f"{prev_text}",
                "omit version to release the computed one, or pass a higher version",
            )
        warnings: list[str] = []
        if prev is not None and chosen < computed:
            warnings.append(
                f"computed {format_version(computed)}, releasing as {format_version(chosen)}"
            )
        chosen_text = format_version(chosen)
        diff = diff.model_copy(update={"to_version": chosen_text})
        section = changelog_section(
            chosen_text, self._clock().date(), self._released_by(principal), diff, state.proposals
        )
        warnings += render_changelog(model, state.changelog, section)[1]
        digest_src = f"{snap.model_version}\n{prev.tag.object_sha if prev else 'none'}"
        return _Computed(
            version=chosen_text,
            diff=diff,
            warnings=warnings,
            section=section,
            digest=hashlib.sha256(digest_src.encode("utf-8")).hexdigest(),
            snapshot=snap,
        )

    def _plan_of(
        self,
        model: str,
        computed: _Computed,
        state: _GitState,
        bump: Bump | None,
        token: str,
        expires: int,
    ) -> ReleasePlan:
        prev = state.previous
        return ReleasePlan(
            model=model,
            version=computed.version,
            tag=tag_name(model, computed.version),
            previous=format_version(prev.version) if prev else None,
            diff=computed.diff,
            requested_bump=bump,
            warnings=computed.warnings,
            changelog_section=computed.section,
            confirm_token=token,
            expires_at=expires,
        )

    @staticmethod
    def _args(
        principal: Caller, model: str, version: str | None, bump: Bump | None
    ) -> dict[str, str | None]:
        return {"model": model, "version": version, "bump": bump, "principal": principal.subject}

    @staticmethod
    def _released_by(principal: Caller) -> str:
        username: object = getattr(principal, "username", None)
        return username if isinstance(username, str) and username else principal.subject

    async def _write_audit(
        self,
        principal: Caller,
        client: str | None,
        model: str,
        tag: str | None,
        code: str | None,
    ) -> None:
        """NFR-SEC-12: principal, model, tag (in ``change_set_id``), result. MCP tools pass
        ``audit=None``: ``guarded`` writes the record from the running audit context."""
        if self._audit is None:
            return
        rec = AuditRecord(
            at=self._clock(),
            principal_subject=principal.subject,
            principal_type=str(principal.type),
            client=client,
            tool=CREATE_RELEASE_TOOL,
            model=model,
            change_set_id=tag,
            result="ok" if code is None else "forbidden" if code == "forbidden" else "error",
            error_code=code,
        )
        await record(self._audit, rec)


def _exists(tag: str) -> LakeloomError:
    return LakeloomError(
        "validation_error",
        f"release {tag} already exists",
        "pass a higher version, or list the releases with list_releases",
    )
