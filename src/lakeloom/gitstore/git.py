"""Isolated ``git`` CLI runner (FR-GIT-01, FR-GIT-09, NFR-SEC-11).

Every child runs with a minimal environment: no system or global config, no terminal
prompts, no credential helper. Tokens reach git only through a ``GIT_ASKPASS`` helper that
reads the child environment; they are never in argv, URLs, logs or error text.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from lakeloom.config import Settings

__all__ = ["GitError", "GitErrorKind", "GitRunner", "classify_stderr", "redact"]

logger = logging.getLogger(__name__)

GitErrorKind = Literal["auth", "not_found", "rejected", "conflict", "network", "timeout", "failed"]

_MAX_MESSAGE = 500
_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/\s@]+@")
_ASKPASS = """#!/bin/sh
case "$1" in
  *Username*) printf '%s\\n' "$LAKELOOM_GIT_ASKPASS_USER" ;;
  *) printf '%s\\n' "$LAKELOOM_GIT_ASKPASS_PASS" ;;
esac
"""

# Order matters: the first matching rule wins. Texts recorded in docs/decisions/0013.
_RULES: tuple[tuple[GitErrorKind, tuple[str, ...]], ...] = (
    (
        "auth",
        (
            "authentication failed",
            "failed to authenticate",
            "could not read username",
            "could not read password",
            "terminal prompts disabled",
            "permission denied (publickey",
            "host key verification failed",
            "returned error: 401",
            "returned error: 403",
        ),
    ),
    ("conflict", ("conflict (", "automatic merge failed", "needs merge")),
    (
        "rejected",
        (
            "[rejected]",
            "[remote rejected]",
            "(fetch first)",
            "non-fast-forward",
            "updates were rejected",
        ),
    ),
    (
        "not_found",
        (
            "repository not found",
            "does not appear to be a git repository",
            "couldn't find remote ref",
            "not found",
            "does not exist",
        ),
    ),
    (
        "network",
        (
            "could not resolve host",
            "could not resolve hostname",
            "connection refused",
            "connection timed out",
            "connection reset",
            "network is unreachable",
            "failed to connect",
            "unable to access",
            "operation timed out",
            "the remote end hung up",
        ),
    ),
)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove URL user info and every ``secrets`` value from ``text``."""
    out = _USERINFO.sub(r"\g<scheme>", text)
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out


def classify_stderr(stderr: str) -> GitErrorKind:
    """Map git's stderr to a ``GitError`` kind; unknown text is ``failed``."""
    low = stderr.lower()
    for kind, needles in _RULES:
        if any(n in low for n in needles):
            return kind
    return "failed"


class GitError(Exception):
    """A failed git invocation; ``message`` is redacted and at most 500 characters."""

    def __init__(self, kind: GitErrorKind, message: str = "") -> None:
        text = redact(message).strip()[:_MAX_MESSAGE]
        super().__init__(f"git {kind}: {text}" if text else f"git {kind}")
        self.kind: GitErrorKind = kind
        self.message = text


class GitRunner:
    """Runs ``git`` with an isolated environment and the configured credentials."""

    def __init__(self, settings: Settings, workdir: Path, *, timeout: float = 60.0) -> None:
        self._settings = settings
        self._workdir = workdir
        self._timeout = timeout
        self._askpass: Path | None = None
        self._warned_accept_new = False

    @property
    def workdir(self) -> Path:
        return self._workdir

    def _secrets(self) -> list[str]:
        token = self._settings.git_token
        return [token.get_secret_value()] if token is not None else []

    def _ensure_askpass(self) -> Path:
        if self._askpass is None:
            self._workdir.mkdir(parents=True, exist_ok=True)
            path = self._workdir / "askpass.sh"
            path.write_text(_ASKPASS, encoding="utf-8")
            path.chmod(0o700)
            self._askpass = path
        return self._askpass

    def _ssh_command(self) -> str | None:
        s = self._settings
        if s.git_ssh_key_file is None and s.git_ssh_known_hosts_file is None:
            return None
        parts = ["ssh"]
        if s.git_ssh_key_file is not None:
            parts += ["-i", str(s.git_ssh_key_file), "-o", "IdentitiesOnly=yes"]
        parts += ["-o", "BatchMode=yes"]
        if s.git_ssh_known_hosts_file is not None:
            parts += [
                "-o",
                f"UserKnownHostsFile={s.git_ssh_known_hosts_file}",
                "-o",
                "StrictHostKeyChecking=yes",
            ]
        else:
            parts += ["-o", "StrictHostKeyChecking=accept-new"]
            if not self._warned_accept_new:
                self._warned_accept_new = True
                logger.warning(
                    "git SSH has no known-hosts file (LAKELOOM_GIT_SSH_KNOWN_HOSTS_FILE); "
                    "new host keys are accepted on first use"
                )
        return " ".join(shlex.quote(p) for p in parts)

    def build_env(self) -> dict[str, str]:
        """The complete child environment (credentials only when configured)."""
        s = self._settings
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(self._workdir),
            "LC_ALL": "C",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        }
        if s.git_token is not None:
            env["GIT_ASKPASS"] = str(self._ensure_askpass())
            env["LAKELOOM_GIT_ASKPASS_USER"] = s.git_username
            env["LAKELOOM_GIT_ASKPASS_PASS"] = s.git_token.get_secret_value()
        ssh = self._ssh_command()
        if ssh is not None:
            env["GIT_SSH_COMMAND"] = ssh
        return env

    async def run(self, *args: str, cwd: Path | None = None, input: bytes | None = None) -> str:
        s = self._settings
        argv = [
            "git",
            "-c",
            "credential.helper=",
            "-c",
            "core.autocrlf=false",
            "-c",
            f"user.name={s.git_author_name}",
            "-c",
            f"user.email={s.git_author_email}",
            *args,
        ]
        self._workdir.mkdir(parents=True, exist_ok=True)
        run_cwd = cwd if cwd is not None else self._workdir
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=run_cwd,
                env=self.build_env(),
                stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise GitError("failed", redact(str(exc), self._secrets())) from None
        try:
            out, err = await asyncio.wait_for(proc.communicate(input), timeout=self._timeout)
        except TimeoutError:
            await self._kill(proc)
            raise GitError("timeout", f"git {args[0] if args else ''} timed out") from None
        except BaseException:
            await self._kill(proc)
            raise
        if proc.returncode != 0:
            stderr = redact(err.decode("utf-8", "replace"), self._secrets())
            raise GitError(classify_stderr(stderr), stderr)
        return out.decode("utf-8", "replace")

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()
