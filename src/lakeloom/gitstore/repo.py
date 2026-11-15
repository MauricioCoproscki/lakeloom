"""A working copy of the ontology repository, driven through :class:`GitRunner`."""

from __future__ import annotations

import re
import shutil
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath

from pydantic import BaseModel

from lakeloom.core.model_create import SLUG_PATTERN
from lakeloom.core.release_diff import parse_version
from lakeloom.errors import LakeloomError
from lakeloom.gitstore.git import GitError, GitRunner

__all__ = ["LogEntry", "TagInfo", "WorkingCopy"]

_SLUG = re.compile(SLUG_PATTERN)
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_TRAILER_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")


class TagInfo(BaseModel):
    name: str
    object_sha: str
    commit_sha: str
    tagged_at: datetime
    subject: str


class LogEntry(BaseModel):
    sha: str
    subject: str
    trailers: dict[str, list[str]]


def _check_ref(ref: str) -> str:
    """A branch, tag or object name that is safe as a git argument (never an option)."""
    if not _REF.fullmatch(ref) or ".." in ref or "//" in ref or ref.endswith((".", "/", ".lock")):
        raise ValueError(f"unsafe git ref: {ref!r}")
    return ref


def _check_tag(name: str) -> str:
    """``<slug>/v<MAJOR.MINOR.PATCH>`` with a slug Lakeloom accepts."""
    slug, sep, version = name.partition("/v")
    if not sep or not _SLUG.fullmatch(slug):
        raise ValueError(f"unsafe tag name: {name!r}")
    try:
        parse_version(version)
    except LakeloomError:
        raise ValueError(f"unsafe tag name: {name!r}") from None
    return name


def _safe_rel(rel: str) -> PurePosixPath:
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts or ".git" in p.parts or not p.parts:
        raise ValueError(f"unsafe repository path: {rel!r}")
    return p


class WorkingCopy:
    def __init__(self, runner: GitRunner, path: Path, remote: str) -> None:
        self._runner = runner
        self.path = path
        self._remote = remote

    async def _git(self, *args: str, input: bytes | None = None) -> str:
        return await self._runner.run(
            "-c", "core.symlinks=false", *args, cwd=self.path, input=input
        )

    def _has_git_dir(self) -> bool:
        return (self.path / ".git").exists()

    def _clear_path(self) -> None:
        """Remove whatever sits at ``path`` so a fresh clone can take its place."""
        if self.path.is_dir() and not self.path.is_symlink():
            shutil.rmtree(self.path)
        elif self.path.exists() or self.path.is_symlink():
            self.path.unlink()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def _is_valid(self) -> bool:
        if not self._has_git_dir():
            return False
        try:
            out = await self._git("rev-parse", "--absolute-git-dir")
        except GitError:
            return False
        return self._same_path(Path(out.strip()), self.path / ".git")

    @staticmethod
    def _same_path(a: Path, b: Path) -> bool:
        return a.resolve() == b.resolve()

    async def ensure(self) -> None:
        """Clone when missing or corrupt; repoint ``origin`` when the remote changed."""
        if await self._is_valid():
            try:
                current = (await self._git("remote", "get-url", "origin")).strip()
            except GitError:
                current = ""
            if current != self._remote:
                if current:
                    await self._git("remote", "set-url", "origin", self._remote)
                else:
                    await self._git("remote", "add", "origin", self._remote)
            return
        self._clear_path()
        await self._runner.run(
            "clone", "-c", "core.symlinks=false", "--origin", "origin", self._remote, str(self.path)
        )

    async def remote_head(self, branch: str) -> str | None:
        out = await self._git("ls-remote", "--heads", "origin", f"refs/heads/{branch}")
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.strip() == f"refs/heads/{branch}" and sha:
                return sha
        return None

    async def checkout_tip(self, branch: str) -> None:
        await self._git("fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
        await self._git("checkout", "-f", "-B", branch, f"origin/{branch}")
        await self._git("reset", "--hard", f"origin/{branch}")
        await self._git("clean", "-fdx")

    async def start_branch(self, name: str, from_branch: str) -> None:
        await self.checkout_tip(from_branch)
        await self._git("checkout", "-f", "-B", name, f"origin/{from_branch}")

    async def init_branch(self, branch: str, files: Mapping[str, bytes], message: str) -> str:
        """Create ``branch`` as an orphan commit holding only ``files``; returns its sha."""
        await self._git("symbolic-ref", "HEAD", f"refs/heads/{branch}")
        await self._git("update-ref", "-d", f"refs/heads/{branch}")
        await self._git("read-tree", "--empty")
        await self._git("clean", "-fdx")
        self.write(files)
        return await self.commit(message)

    def write(self, files: Mapping[str, bytes]) -> None:
        root = self.path.resolve()
        targets: list[tuple[Path, bytes]] = []
        for rel, data in files.items():
            target = (root / _safe_rel(rel)).resolve()
            if not target.is_relative_to(root):
                raise ValueError(f"unsafe repository path: {rel!r}")
            targets.append((target, data))
        for target, data in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def read(self, path: str) -> bytes | None:
        target = self.path / _safe_rel(path)
        if target.is_symlink() or not target.resolve().is_relative_to(self.path.resolve()):
            raise ValueError(f"unsafe repository path: {path!r}")
        try:
            return target.read_bytes()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            return None

    def list_files(self, prefix: str) -> list[str]:
        """Sorted repo-relative POSIX paths of files under ``prefix`` (excluding ``.git``)."""
        base = self.path / _safe_rel(prefix) if prefix.strip("/") else self.path
        if not base.is_dir() or not base.resolve().is_relative_to(self.path.resolve()):
            return []
        root = self.path
        return sorted(
            p.relative_to(root).as_posix()
            for p in base.rglob("*")
            if p.is_file() and not p.is_symlink() and ".git" not in p.relative_to(root).parts
        )

    async def commit(self, message: str) -> str:
        await self._git("add", "-A")
        if not (await self._git("status", "--porcelain")).strip():
            raise GitError("failed", "nothing to commit")
        await self._git("commit", "-F", "-", input=message.encode("utf-8"))
        return (await self._git("rev-parse", "HEAD")).strip()

    async def push(self, local: str, remote_branch: str, *, force: bool = False) -> None:
        args = ["push", "origin"]
        if force:
            args.append("--force")
        args.append(f"refs/heads/{local}:refs/heads/{remote_branch}")
        await self._git(*args)

    async def find_trailer(self, ref: str, key: str, value: str) -> str | None:
        try:
            await self._git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        except GitError:
            return None
        out = await self._git(
            "log", ref, f"--format=%x01%H%x00%(trailers:key={key},valueonly)", "--"
        )
        for record in out.split("\x01"):
            sha, _, values = record.partition("\x00")
            if any(line.strip() == value for line in values.splitlines()):
                return sha.strip()
        return None

    # --- tags and history (M3b releases) -------------------------------------------------

    async def fetch_tags(self, pattern: str) -> None:
        """Fetch the remote tags matching ``<slug>/v*``: forced (a moved tag is overwritten)
        and pruned (a local tag of the pattern that the remote lacks is deleted)."""
        slug, sep, rest = pattern.partition("/")
        if not _SLUG.fullmatch(slug) or not sep or rest != "v*":
            raise ValueError(f"unsafe tag pattern: {pattern!r}")
        await self._git(
            "fetch", "origin", "--prune", "--force", f"+refs/tags/{pattern}:refs/tags/{pattern}"
        )

    async def tags(self, prefix: str) -> list[TagInfo]:
        """Local tags under ``prefix`` (for example ``retail/``), unsorted."""
        if not prefix.endswith("/") or not _SLUG.fullmatch(prefix[:-1]):
            raise ValueError(f"unsafe tag prefix: {prefix!r}")
        fmt = (
            "%01%(refname)%00%(objectname)%00%(*objectname)%00"
            "%(creatordate:iso-strict)%00%(contents:subject)"
        )
        out = await self._git("for-each-ref", f"--format={fmt}", f"refs/tags/{prefix}")
        found: list[TagInfo] = []
        for record in out.split("\x01"):
            if not record.strip():
                continue
            ref, obj, peeled, when, subject = record.rstrip("\n").split("\x00", 4)
            found.append(
                TagInfo(
                    name=ref.removeprefix("refs/tags/"),
                    object_sha=obj,
                    commit_sha=peeled or obj,
                    tagged_at=datetime.fromisoformat(when),
                    subject=subject,
                )
            )
        return found

    async def remote_tag(self, name: str) -> str | None:
        """The commit a remote tag points at (peeled), or ``None`` when it does not exist."""
        _check_tag(name)
        ref = f"refs/tags/{name}"
        out = await self._git("ls-remote", "--tags", "origin", ref, f"{ref}^{{}}")
        plain: str | None = None
        for line in out.splitlines():
            sha, _, found = line.partition("\t")
            if found.strip() == f"{ref}^{{}}":
                return sha
            if found.strip() == ref:
                plain = sha
        return plain

    async def show(self, ref: str, path: str) -> bytes | None:
        """The bytes of ``path`` at ``ref`` (a commit or tag), ``None`` when it is absent."""
        _check_ref(ref)
        rel = _safe_rel(path).as_posix()
        try:
            out = await self._git("show", f"{ref}:{rel}")
        except GitError as exc:
            if exc.kind == "not_found":
                return None
            raise
        return out.encode("utf-8")

    async def tag_annotated(self, name: str, message: str) -> str:
        """Tag ``HEAD`` with an annotated tag; the message is kept verbatim. Returns its sha."""
        _check_tag(name)
        await self._git(
            "tag", "-a", "--cleanup=verbatim", "-F", "-", name, input=message.encode("utf-8")
        )
        return (await self._git("rev-parse", f"refs/tags/{name}")).strip()

    async def delete_tag(self, name: str) -> None:
        _check_tag(name)
        await self._git("tag", "-d", name)

    async def push_atomic(self, branch: str, tag: str) -> None:
        """Push ``branch`` and ``tag`` together: both land or neither does."""
        _check_ref(branch)
        _check_tag(tag)
        await self._git(
            "push",
            "--atomic",
            "origin",
            f"refs/heads/{branch}:refs/heads/{branch}",
            f"refs/tags/{tag}:refs/tags/{tag}",
        )

    async def log_since(
        self, since: str | None, path: str, key: str | Sequence[str]
    ) -> list[LogEntry]:
        """Commits touching ``path`` after ``since`` (all when ``None``), oldest first.

        ``trailers`` holds the values of the ``key`` trailer(s) of each commit only.
        """
        keys = (key,) if isinstance(key, str) else tuple(key)
        if not keys or any(_TRAILER_KEY.fullmatch(k) is None for k in keys):
            raise ValueError(f"unsafe trailer key: {key!r}")
        rel = _safe_rel(path).as_posix()
        rng = f"{_check_ref(since)}..HEAD" if since is not None else "HEAD"
        fields = "".join(f"%x00%(trailers:key={k},valueonly,unfold)" for k in keys)
        out = await self._git("log", "--reverse", rng, f"--format=%x01%H%x00%s{fields}", "--", rel)
        entries: list[LogEntry] = []
        for record in out.split("\x01"):
            if not record.strip():
                continue
            sha, subject, *values = record.split("\x00", 1 + len(keys))
            trailers: dict[str, list[str]] = {}
            for k, text in zip(keys, values, strict=True):
                lines = [v.strip() for v in text.splitlines() if v.strip()]
                if lines:
                    trailers[k] = lines
            entries.append(LogEntry(sha=sha.strip(), subject=subject, trailers=trailers))
        return entries

    async def trailer_of(self, ref: str, key: str) -> list[str]:
        """The values of the ``key`` trailer of commit ``ref`` (empty when none)."""
        if _TRAILER_KEY.fullmatch(key) is None:
            raise ValueError(f"unsafe trailer key: {key!r}")
        out = await self._git(
            "log", "-1", _check_ref(ref), f"--format=%(trailers:key={key},valueonly,unfold)", "--"
        )
        return [v.strip() for v in out.splitlines() if v.strip()]
