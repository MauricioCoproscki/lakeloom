import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from lakeloom.config import Settings
from lakeloom.gitstore import GitRunner, WorkingCopy

_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Foreign",
    "GIT_AUTHOR_EMAIL": "f@example.invalid",
    "GIT_COMMITTER_NAME": "Foreign",
    "GIT_COMMITTER_EMAIL": "f@example.invalid",
}


def plain_git(*args: str, cwd: Path) -> str:
    import os

    env = {"PATH": os.environ["PATH"], "HOME": str(cwd), **_ENV}
    res = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    def _make(**kw: object) -> Settings:
        return Settings(_env_file=None, **kw)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def bare(tmp_path: Path) -> Path:
    path = tmp_path / "remote.git"
    plain_git("init", "--bare", "-b", "main", str(path), cwd=tmp_path)
    return path


@pytest.fixture
def remote_url(bare: Path) -> str:
    return f"file://{bare}"


@pytest.fixture
def runner(tmp_path: Path, make_settings: Callable[..., Settings]) -> GitRunner:
    return GitRunner(make_settings(), tmp_path / "work")


@pytest.fixture
def wc(runner: GitRunner, tmp_path: Path, remote_url: str) -> WorkingCopy:
    return WorkingCopy(runner, tmp_path / "work" / "repo", remote_url)


@pytest.fixture
def push_foreign(tmp_path: Path, remote_url: str) -> Callable[[str, dict[str, str]], None]:
    """Push a commit to ``main`` of the remote through a second, independent clone."""
    counter = 0

    def _push(message: str, files: dict[str, str]) -> None:
        nonlocal counter
        counter += 1
        clone = tmp_path / f"foreign{counter}"
        plain_git("clone", remote_url, str(clone), cwd=tmp_path)
        plain_git("checkout", "-B", "main", cwd=clone)
        for rel, text in files.items():
            (clone / rel).parent.mkdir(parents=True, exist_ok=True)
            (clone / rel).write_text(text)
        plain_git("add", "-A", cwd=clone)
        plain_git("commit", "-m", message, cwd=clone)
        plain_git("push", "origin", "main", cwd=clone)

    return _push


@pytest.fixture
def other_bare(tmp_path: Path) -> Path:
    path = tmp_path / "other.git"
    plain_git("init", "--bare", "-b", "main", str(path), cwd=tmp_path)
    return path


@pytest.fixture
def git_cmd() -> Callable[..., str]:
    return plain_git
