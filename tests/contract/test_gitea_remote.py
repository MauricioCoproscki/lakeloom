"""Gitea remote contract for M2c Git publishing (FR-GIT-01, FR-GIT-05, NFR-SEC-11).

Raw ``git`` and httpx against the dev Gitea (ADR 0013 § Research). Credentials reach git only
through the subprocess environment (``GIT_ASKPASS`` script), never argv or the URL. A
throwaway repository is created per test and deleted in a ``finally``.
"""

import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.contract

GITEA = "http://localhost:3000"
API = f"{GITEA}/api/v1"
USER = "lakeloom-admin"
PASSWORD = "lakeloom-admin"  # noqa: S105 - DEV ONLY fixed public compose credential


@contextmanager
def throwaway_repo() -> Iterator[str]:
    name = f"contract-{uuid.uuid4().hex[:10]}"
    try:
        r = httpx.post(
            f"{API}/user/repos",
            auth=(USER, PASSWORD),
            json={"name": name, "auto_init": False, "default_branch": "main"},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        pytest.skip(f"Gitea not reachable at {GITEA}: {exc!r}")
    assert r.status_code == 201, r.text
    try:
        yield name
    finally:
        httpx.delete(f"{API}/repos/{USER}/{name}", auth=(USER, PASSWORD), timeout=10)


def _env(tmp_path: Path, password: str = PASSWORD) -> dict[str, str]:
    askpass = tmp_path / "askpass.sh"
    askpass.write_text(
        '#!/bin/sh\ncase "$1" in\n  Username*) printf \'%s\\n\' "$LK_GIT_USER";;\n'
        "  *) printf '%s\\n' \"$LK_GIT_PASS\";;\nesac\n"
    )
    askpass.chmod(0o700)
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "GIT_ASKPASS": str(askpass),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "LK_GIT_USER": USER,
        "LK_GIT_PASS": password,
    }


def _git(
    cwd: Path, env: dict[str, str], *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(  # noqa: S603 - fixed git argv
        [  # noqa: S607
            "git",
            "-c",
            "credential.helper=",
            "-c",
            "user.name=Lakeloom",
            "-c",
            "user.email=lakeloom@localhost.invalid",
            *args,
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if check:
        assert proc.returncode == 0, proc.stderr
    return proc


def _local_repo(tmp_path: Path, env: dict[str, str]) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    _git(work, env, "init", "-q", "-b", "main")
    (work / "README.md").write_text("hello\n")
    _git(work, env, "add", "README.md")
    _git(work, env, "commit", "-q", "-m", "init")
    return work


def test_push_with_askpass_credentials(tmp_path: Path) -> None:
    with throwaway_repo() as name:
        url = f"{GITEA}/{USER}/{name}.git"
        env = _env(tmp_path)
        work = _local_repo(tmp_path, env)

        # An empty repository: ls-remote is empty and fetching the branch fails.
        assert _git(work, env, "ls-remote", url).stdout == ""
        missing = _git(work, env, "fetch", url, "main", check=False)
        assert missing.returncode == 128
        assert "couldn't find remote ref main" in missing.stderr

        # A push creates the missing branch.
        _git(work, env, "push", url, "main")
        assert "refs/heads/main" in _git(work, env, "ls-remote", url).stdout

        # A wrong password is "Authentication failed", with no secret in the output.
        bad = _git(work, _env(tmp_path, "wrong-pass"), "push", url, "main", check=False)
        assert bad.returncode == 128
        assert "Authentication failed" in bad.stderr
        assert "wrong-pass" not in bad.stderr

        # A non-fast-forward push is rejected with "(fetch first)".
        other = tmp_path / "other"
        _git(tmp_path, env, "clone", "-q", url, str(other))
        (other / "b.txt").write_text("b\n")
        _git(other, env, "add", "b.txt")
        _git(other, env, "commit", "-q", "-m", "two")
        _git(other, env, "push", "-q", "origin", "main")
        (work / "c.txt").write_text("c\n")
        _git(work, env, "add", "c.txt")
        _git(work, env, "commit", "-q", "-m", "three")
        rejected = _git(work, env, "push", url, "main", check=False)
        assert rejected.returncode == 1
        assert "[rejected]" in rejected.stderr
        assert "(fetch first)" in rejected.stderr

        # A missing repository.
        gone = _git(work, env, "ls-remote", f"{GITEA}/{USER}/{name}-nope.git", check=False)
        assert gone.returncode == 128
        assert "not found" in gone.stderr


def _create_pr_after_push(api: str, payload: dict[str, str]) -> httpx.Response:
    """POST the PR, retrying a transient 404 that Gitea answers for ~0.1-1 s after a push.

    Observed live (ADR 0013 § Research): the branch is not yet visible to the pulls API right
    after ``git push`` returns, so the first POST may be 404 ``not found``.
    """
    r = httpx.post(api, auth=(USER, PASSWORD), json=payload, timeout=10)
    for _ in range(19):
        if r.status_code != 404:
            break
        time.sleep(0.25)
        r = httpx.post(api, auth=(USER, PASSWORD), json=payload, timeout=10)
    return r


def test_open_pull_request(tmp_path: Path) -> None:
    with throwaway_repo() as name:
        url = f"{GITEA}/{USER}/{name}.git"
        env = _env(tmp_path)
        work = _local_repo(tmp_path, env)
        _git(work, env, "push", "-q", url, "main")
        _git(work, env, "checkout", "-q", "-b", "lakeloom/m/p1")
        (work / "x.txt").write_text("x\n")
        _git(work, env, "add", "x.txt")
        _git(work, env, "commit", "-q", "-m", "pr")
        _git(work, env, "push", "-q", "--force", url, "lakeloom/m/p1")

        api = f"{API}/repos/{USER}/{name}/pulls"
        payload = {"title": "t", "body": "b", "head": "lakeloom/m/p1", "base": "main"}
        created = _create_pr_after_push(api, payload)
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["number"] == 1
        assert body["html_url"] == f"{GITEA}/{USER}/{name}/pulls/1"

        # A duplicate for the same head is 409 "pull request already exists".
        dup = httpx.post(api, auth=(USER, PASSWORD), json=payload, timeout=10)
        assert dup.status_code == 409
        assert "pull request already exists" in dup.json()["message"]

        # The list endpoint ignores ``head``: list open PRs and filter on head.ref.
        listed = httpx.get(
            api,
            params={"state": "open", "head": "no-such-branch"},
            auth=(USER, PASSWORD),
            timeout=10,
        )
        assert listed.status_code == 200
        assert [p["number"] for p in listed.json() if p["head"]["ref"] == "lakeloom/m/p1"] == [1]
        assert [p["head"]["ref"] for p in listed.json()] == ["lakeloom/m/p1"]

        # Auth failure and a missing head branch.
        assert httpx.get(api, auth=(USER, "bad"), timeout=10).status_code == 401
        no_head = httpx.post(
            api, auth=(USER, PASSWORD), json={**payload, "head": "no-branch"}, timeout=10
        )
        assert no_head.status_code == 404
