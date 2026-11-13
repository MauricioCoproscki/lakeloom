"""Pull-request clients for Gitea, GitHub and GitLab (FR-GIT-05).

Error mapping: 401/403 -> ``auth``; 404 -> ``not_found``; other 4xx -> ``failed``;
5xx, transport errors and timeouts -> ``network``. Duplicate PRs are recognised by status
code only (Gitea 409, GitLab 409, GitHub 422), never by message text; the open PR whose
head branch equals ``head`` is then looked up and reused.

A freshly pushed Gitea repository answers ``POST .../pulls`` with 404 for ~0.1-1 s (ADR 0013
research). Every forge's *create* call therefore retries a 404 with a small exponential
backoff (0.25 s, doubling) for up to ``create_budget`` seconds in total and then raises a
transient ``network`` error, not ``not_found``. Other calls never retry. The sleep and the
budget are injectable.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel

from lakeloom.config import ConfigError, Settings
from lakeloom.gitstore.git import GitError, GitErrorKind, redact

__all__ = [
    "Forge",
    "GitHubForge",
    "GitLabForge",
    "GiteaForge",
    "PullRequest",
    "forge_for",
    "parse_owner_repo",
]

_SCP = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>(?!//).+)$")
_TIMEOUT = 15.0
_FIRST_DELAY = 0.25
_CREATE_BUDGET = 5.0
_PAGE_SIZE = 50
_MAX_PAGES = 20

Sleep = Callable[[float], Awaitable[None]]


class PullRequest(BaseModel):
    number: int
    url: str


class Forge(Protocol):
    async def open_pull_request(
        self, *, head: str, base: str, title: str, body: str
    ) -> PullRequest: ...


def parse_owner_repo(remote: str) -> tuple[str, str, str]:
    """Return ``(host[:port], owner, repo)``; GitLab subgroups stay in ``owner``."""
    remote = remote.strip()
    if "://" in remote:
        parts = urlsplit(remote)
        host = parts.hostname or ""
        if parts.scheme in ("http", "https") and parts.port:
            host = f"{host}:{parts.port}"
        path = parts.path
    else:
        m = _SCP.match(remote)
        if m is None:
            raise ConfigError(f"cannot parse owner/repo from git remote {_safe(remote)!r}")
        host, path = m["host"], m["path"]
    segments = [s for s in path.strip("/").removesuffix(".git").split("/") if s]
    if not host or len(segments) < 2:
        raise ConfigError(f"cannot parse owner/repo from git remote {_safe(remote)!r}")
    return host, "/".join(segments[:-1]), segments[-1]


def _safe(text: str) -> str:
    return redact(text)


class _BaseForge:
    kind = ""

    def __init__(
        self,
        http: httpx.AsyncClient,
        api_base: str,
        owner: str,
        repo: str,
        *,
        token: str | None = None,
        username: str = "x-access-token",
        sleep: Sleep = asyncio.sleep,
        create_budget: float = _CREATE_BUDGET,
    ) -> None:
        self._http = http
        self.api_base = api_base.rstrip("/")
        self._owner = owner
        self._repo = repo
        self._token = token or None
        self._username = username
        self._sleep = sleep
        self._budget = create_budget

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.api_base!r}, {self._owner}/{self._repo})"

    # -- hooks -------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json"}

    def _repo_path(self) -> str:
        return f"/repos/{self._owner}/{self._repo}"

    def _create_request(self, head: str, base: str, title: str, body: str) -> tuple[str, Any]:
        raise NotImplementedError

    def _list_request(self, head: str) -> tuple[str, dict[str, str]]:
        raise NotImplementedError

    def _duplicate_status(self) -> int:
        return 409

    def _head_of(self, item: dict[str, Any]) -> str | None:
        raise NotImplementedError

    def _to_pr(self, item: dict[str, Any]) -> PullRequest:
        raise NotImplementedError

    # -- machinery ---------------------------------------------------------
    async def _send(
        self, method: str, path: str, *, params: dict[str, str] | None = None, json: Any = None
    ) -> httpx.Response:
        try:
            return await self._http.request(
                method,
                self.api_base + path,
                params=params,
                json=json,
                headers=self._headers(),
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise self._err("network", f"{type(exc).__name__}: {exc}") from None

    def _err(self, kind: GitErrorKind, message: str) -> GitError:
        return GitError(kind, redact(message, [self._token or ""]))

    def _fail(self, resp: httpx.Response) -> GitError:
        code = resp.status_code
        if code in (401, 403):
            kind: GitErrorKind = "auth"
        elif code == 404:
            kind = "not_found"
        elif code >= 500:
            kind = "network"
        else:
            kind = "failed"
        return self._err(kind, f"{self.kind} API answered {code}: {resp.text[:300]}")

    async def _create(self, path: str, payload: Any) -> httpx.Response:
        elapsed = 0.0
        delay = _FIRST_DELAY
        while True:
            resp = await self._send("POST", path, json=payload)
            if resp.status_code != 404:
                return resp
            if elapsed >= self._budget:
                raise self._err(
                    "network",
                    f"{self.kind} API still answers 404 after {elapsed:g}s; "
                    "repository not yet visible, will retry",
                )
            step = min(delay, self._budget - elapsed)
            await self._sleep(step)
            elapsed += step
            delay *= 2

    _size_param = "limit"

    async def _find_open(self, head: str) -> PullRequest:
        path, params = self._list_request(head)
        for page in range(1, _MAX_PAGES + 1):
            resp = await self._send(
                "GET", path, params={**params, self._size_param: str(_PAGE_SIZE), "page": str(page)}
            )
            if resp.status_code != 200:
                raise self._fail(resp)
            try:
                items: Any = resp.json()
            except ValueError:
                items = None
            if not isinstance(items, list):
                break
            entries = cast("list[Any]", items)
            for item in entries:
                if isinstance(item, dict):
                    entry = cast("dict[str, Any]", item)
                    if self._head_of(entry) == head:
                        try:
                            return self._to_pr(entry)
                        except (KeyError, TypeError, ValueError):
                            raise self._err(
                                "failed", f"{self.kind} listed an unexpected PR body"
                            ) from None
            if len(entries) < _PAGE_SIZE:
                break
        raise self._err(
            "failed", f"{self.kind} reported a duplicate but no open PR has head {head!r}"
        )

    async def open_pull_request(
        self, *, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        path, payload = self._create_request(head, base, title, body)
        resp = await self._create(path, payload)
        if resp.status_code in (200, 201):
            try:
                return self._to_pr(resp.json())
            except (ValueError, KeyError, TypeError):
                raise self._err("failed", f"{self.kind} answered an unexpected body") from None
        if resp.status_code == self._duplicate_status():
            return await self._find_open(head)
        raise self._fail(resp)


class GiteaForge(_BaseForge):
    kind = "gitea"

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        if self._token:
            import base64

            raw = f"{self._username}:{self._token}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        return headers

    def _create_request(self, head: str, base: str, title: str, body: str) -> tuple[str, Any]:
        return f"{self._repo_path()}/pulls", {
            "title": title,
            "body": body,
            "head": head,
            "base": base,
        }

    def _list_request(self, head: str) -> tuple[str, dict[str, str]]:
        # Gitea ignores a ``head`` filter, so the caller filters client-side.
        return f"{self._repo_path()}/pulls", {"state": "open"}

    def _head_of(self, item: dict[str, Any]) -> str | None:
        head: Any = item.get("head")
        if not isinstance(head, dict):
            return None
        ref = cast("dict[str, Any]", head).get("ref")
        return ref if isinstance(ref, str) else None

    def _to_pr(self, item: dict[str, Any]) -> PullRequest:
        return PullRequest(number=int(item["number"]), url=str(item["html_url"]))


class GitHubForge(GiteaForge):
    kind = "github"
    _size_param = "per_page"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _duplicate_status(self) -> int:
        return 422

    def _list_request(self, head: str) -> tuple[str, dict[str, str]]:
        owner = self._owner.split("/")[0]
        return f"{self._repo_path()}/pulls", {"state": "open", "head": f"{owner}:{head}"}


class GitLabForge(_BaseForge):
    kind = "gitlab"
    _size_param = "per_page"

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        if self._token:
            headers["PRIVATE-TOKEN"] = self._token
        return headers

    def _repo_path(self) -> str:
        return f"/projects/{quote(f'{self._owner}/{self._repo}', safe='')}"

    def _create_request(self, head: str, base: str, title: str, body: str) -> tuple[str, Any]:
        return f"{self._repo_path()}/merge_requests", {
            "source_branch": head,
            "target_branch": base,
            "title": title,
            "description": body,
        }

    def _list_request(self, head: str) -> tuple[str, dict[str, str]]:
        return f"{self._repo_path()}/merge_requests", {"state": "opened", "source_branch": head}

    def _head_of(self, item: dict[str, Any]) -> str | None:
        return item.get("source_branch")

    def _to_pr(self, item: dict[str, Any]) -> PullRequest:
        return PullRequest(number=int(item["iid"]), url=str(item["web_url"]))


def _derive_api_base(forge: str, remote: str, host: str) -> str:
    if forge == "github":
        return "https://api.github.com"
    scheme = urlsplit(remote).scheme if "://" in remote else "https"
    if scheme not in ("http", "https"):
        scheme = "https"
    suffix = "/api/v4" if forge == "gitlab" else "/api/v1"
    return f"{scheme}://{host}{suffix}"


def forge_for(settings: Settings, http: httpx.AsyncClient) -> Forge | None:
    """Build the configured forge client, or ``None`` when ``forge`` is ``none``."""
    if settings.forge == "none":
        return None
    if not settings.git_remote:
        raise ConfigError("LAKELOOM_GIT_REMOTE is required when LAKELOOM_FORGE is not 'none'")
    host, owner, repo = parse_owner_repo(settings.git_remote)
    api_base = (
        str(settings.forge_api_url)
        if settings.forge_api_url is not None
        else _derive_api_base(settings.forge, settings.git_remote, host)
    )
    token = settings.git_token.get_secret_value() if settings.git_token is not None else None
    cls = {"gitea": GiteaForge, "github": GitHubForge, "gitlab": GitLabForge}[settings.forge]
    return cls(http, api_base, owner, repo, token=token, username=settings.git_username)
