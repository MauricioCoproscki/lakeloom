import asyncio
import logging
import os
import stat
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import SecretStr

from lakeloom.config import Settings
from lakeloom.gitstore import GitError, GitRunner, redact
from lakeloom.gitstore.git import classify_stderr

TOKEN = "tok-SECRET-12345"  # noqa: S105


def _fake_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "git"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return bindir


async def test_env_is_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: GitRunner
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[user]\n\tname = WRONG\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    repo = tmp_path / "r"
    repo.mkdir()
    await runner.run("init", "-q", cwd=repo)
    assert (await runner.run("config", "user.name", cwd=repo)).strip() == "Lakeloom"
    assert "WRONG" not in await runner.run("config", "--list", "--show-origin", cwd=repo)
    env = runner.build_env()
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_ASKPASS" not in env


async def test_token_never_in_argv_or_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_settings: Callable[..., Settings],
) -> None:
    log = tmp_path / "calls.log"
    _fake_git(
        tmp_path,
        monkeypatch,
        f'echo "$@" >> {log}\n'
        'echo "fatal: unable to access https://u:$LAKELOOM_GIT_ASKPASS_PASS@h/r.git/: '
        'pw=$LAKELOOM_GIT_ASKPASS_PASS" >&2\nexit 128\n',
    )
    runner = GitRunner(make_settings(git_token=SecretStr(TOKEN)), tmp_path / "w")
    with pytest.raises(GitError) as exc:
        await runner.run("push", "https://h/r.git")
    assert TOKEN not in str(exc.value)
    assert TOKEN not in exc.value.message
    assert "u:" not in exc.value.message
    assert exc.value.kind == "network"
    assert TOKEN not in log.read_text()


def test_askpass_answers_username_and_password(
    tmp_path: Path, make_settings: Callable[..., Settings]
) -> None:
    runner = GitRunner(
        make_settings(git_token=SecretStr(TOKEN), git_username="alice"), tmp_path / "w"
    )
    env = runner.build_env()
    script = Path(env["GIT_ASKPASS"])
    assert stat.S_IMODE(script.stat().st_mode) == 0o700
    assert script.parent == tmp_path / "w"
    assert TOKEN not in script.read_text()

    def ask(prompt: str) -> str:
        res = subprocess.run(  # noqa: S603
            [str(script), prompt], env=env, capture_output=True, text=True, check=True
        )
        return res.stdout.strip()

    assert ask("Username for 'https://h': ") == "alice"
    assert ask("Password for 'https://alice@h': ") == TOKEN


def test_ssh_command_with_known_hosts(
    tmp_path: Path, make_settings: Callable[..., Settings]
) -> None:
    key, kh = tmp_path / "id key", tmp_path / "kh"
    runner = GitRunner(
        make_settings(git_ssh_key_file=key, git_ssh_known_hosts_file=kh), tmp_path / "w"
    )
    cmd = runner.build_env()["GIT_SSH_COMMAND"]
    assert f"-i '{key}'" in cmd
    assert "IdentitiesOnly=yes" in cmd
    assert "BatchMode=yes" in cmd
    assert f"UserKnownHostsFile={kh}" in cmd
    assert "StrictHostKeyChecking=yes" in cmd
    assert "accept-new" not in cmd


def test_ssh_command_accept_new_without_known_hosts(
    tmp_path: Path,
    make_settings: Callable[..., Settings],
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = GitRunner(make_settings(git_ssh_key_file=tmp_path / "k"), tmp_path / "w")
    with caplog.at_level(logging.WARNING, logger="lakeloom.gitstore.git"):
        cmd = runner.build_env()["GIT_SSH_COMMAND"]
        runner.build_env()
    assert "StrictHostKeyChecking=accept-new" in cmd
    assert "UserKnownHostsFile" not in cmd
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


async def test_timeout_kills_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_settings: Callable[..., Settings],
) -> None:
    pidfile = tmp_path / "pid"
    _fake_git(tmp_path, monkeypatch, f"echo $$ > {pidfile}\nexec sleep 30\n")
    runner = GitRunner(make_settings(), tmp_path / "w", timeout=0.5)
    with pytest.raises(GitError) as exc:
        await runner.run("fetch")
    assert exc.value.kind == "timeout"
    pid = int(pidfile.read_text())
    await asyncio.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize(
    ("stderr", "kind"),
    [
        (
            "remote: Failed to authenticate user\n"
            "fatal: Authentication failed for 'http://localhost:3000/o/r.git/'",
            "auth",
        ),
        ("fatal: could not read Username for 'http://h': terminal prompts disabled", "auth"),
        ("git@h: Permission denied (publickey).", "auth"),
        (
            "To http://localhost:3000/o/r.git\n ! [rejected]        main -> main (fetch first)\n"
            "error: failed to push some refs to 'http://localhost:3000/o/r.git'",
            "rejected",
        ),
        (" ! [rejected]        main -> main (non-fast-forward)", "rejected"),
        (
            "remote: Repository not found\nfatal: repository 'http://h/o/r.git/' not found",
            "not_found",
        ),
        ("fatal: couldn't find remote ref main", "not_found"),
        (
            "fatal: path 'ontologies/m/deliverables/glossary.md' exists on disk, but not in "
            "'1a2b3c'",
            "not_found",
        ),
        ("fatal: unable to access 'http://h/': Could not resolve host: h", "network"),
        ("fatal: unable to connect: Connection refused", "network"),
        ("CONFLICT (content): Merge conflict in a.ttl", "conflict"),
        ("fatal: something odd", "failed"),
        ("", "failed"),
    ],
)
def test_classify_stderr(stderr: str, kind: str) -> None:
    assert classify_stderr(stderr) == kind


def test_redact_and_error_message_cap() -> None:
    assert redact("clone https://user:pw@host/x.git failed") == "clone https://host/x.git failed"
    assert redact(f"a {TOKEN} b", [TOKEN]) == "a *** b"
    err = GitError("failed", "x" * 900 + " https://u:p@h/")
    assert len(err.message) == 500
    assert "u:p" not in GitError("failed", "see https://u:p@h/r").message


async def test_timeout_kills_process_group_and_returns_promptly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_settings: Callable[..., Settings],
) -> None:
    pidfile = tmp_path / "child.pid"
    _fake_git(tmp_path, monkeypatch, f"sleep 30 &\necho $! > {pidfile}\nwait\n")
    runner = GitRunner(make_settings(), tmp_path / "w", timeout=0.5)
    started = time.monotonic()
    with pytest.raises(GitError) as exc:
        await runner.run("fetch")
    assert exc.value.kind == "timeout"
    assert time.monotonic() - started < 3.5
    pid = int(pidfile.read_text())
    await asyncio.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_redact_removes_configured_token_without_explicit_secrets(
    tmp_path: Path, make_settings: Callable[..., Settings]
) -> None:
    secret = "registered-token-98765"  # noqa: S105
    GitRunner(make_settings(git_token=SecretStr(secret)), tmp_path / "w")
    assert secret not in redact(f"x {secret} y")
    assert secret not in GitError("failed", f"x {secret}").message
    assert secret not in str(GitError("failed", f"x {secret}"))
