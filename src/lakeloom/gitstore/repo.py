"""A working copy of the ontology repository, driven through :class:`GitRunner`."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from lakeloom.gitstore.git import GitError, GitRunner

__all__ = ["WorkingCopy"]


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
