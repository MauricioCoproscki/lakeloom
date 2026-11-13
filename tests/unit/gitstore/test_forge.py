import base64
from collections.abc import Callable

import httpx
import pytest
import respx

from lakeloom.config import ConfigError, Settings
from lakeloom.gitstore import GitError
from lakeloom.gitstore.forge import (
    GiteaForge,
    GitHubForge,
    GitLabForge,
    PullRequest,
    forge_for,
    parse_owner_repo,
)

TOKEN = "s3cr3t-token-value"  # noqa: S105
ARGS = {"head": "lk/x", "base": "main", "title": "T", "body": "B"}


async def _nosleep(_: float) -> None:
    return None


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://github.com/acme/onto.git", ("github.com", "acme", "onto")),
        ("https://gitea:3000/acme/onto", ("gitea:3000", "acme", "onto")),
        ("git@github.com:acme/onto.git", ("github.com", "acme", "onto")),
        ("ssh://git@host:2222/acme/onto.git", ("host", "acme", "onto")),
        ("https://gitlab.com/a/b/c/onto.git", ("gitlab.com", "a/b/c", "onto")),
        ("https://user:pw@gitlab.com/a/onto.git/", ("gitlab.com", "a", "onto")),
    ],
)
def test_parse_owner_repo(remote: str, expected: tuple[str, str, str]) -> None:
    assert parse_owner_repo(remote) == expected


@pytest.mark.parametrize("remote", ["https://github.com/onlyone", "", "nonsense"])
def test_parse_owner_repo_rejects(remote: str) -> None:
    with pytest.raises(ConfigError):
        parse_owner_repo(remote)


async def test_api_base_derivation(make_settings: Callable[..., Settings]) -> None:
    async with httpx.AsyncClient() as http:
        gh = forge_for(make_settings(forge="github", git_remote="git@github.com:a/b.git"), http)
        gl = forge_for(
            make_settings(forge="gitlab", git_remote="https://gl.example:8443/a/b"), http
        )
        gt = forge_for(make_settings(forge="gitea", git_remote="http://gitea:3000/a/b.git"), http)
        ov = forge_for(
            make_settings(
                forge="gitea", git_remote="http://x/a/b", forge_api_url="http://gitea:3000/api/v1"
            ),
            http,
        )
        assert isinstance(gh, GitHubForge)
        assert gh.api_base == "https://api.github.com"
        assert isinstance(gl, GitLabForge)
        assert gl.api_base == "https://gl.example:8443/api/v4"
        assert isinstance(gt, GiteaForge)
        assert gt.api_base == "http://gitea:3000/api/v1"
        assert isinstance(ov, GiteaForge)
        assert ov.api_base == "http://gitea:3000/api/v1"
        assert forge_for(make_settings(forge="none"), http) is None
        with pytest.raises(ConfigError):
            forge_for(make_settings(forge="github", git_remote=None), http)
        with pytest.raises(ConfigError):
            forge_for(make_settings(forge="github", git_remote="https://github.com/x"), http)


def _gitea(
    http: httpx.AsyncClient, token: str | None = TOKEN, base: str = "http://gitea:3000/api/v1/"
) -> GiteaForge:
    return GiteaForge(http, base, "o", "r", token=token, username="lakeloom-admin", sleep=_nosleep)


@respx.mock
async def test_gitea_create_pr_basic_auth() -> None:
    route = respx.post("http://gitea:3000/api/v1/repos/o/r/pulls").respond(
        201, json={"number": 7, "html_url": "http://gitea:3000/o/r/pulls/7"}
    )
    async with httpx.AsyncClient() as http:
        pr = await _gitea(http).open_pull_request(**ARGS)
    assert pr == PullRequest(number=7, url="http://gitea:3000/o/r/pulls/7")
    req = route.calls.last.request
    want = base64.b64encode(f"lakeloom-admin:{TOKEN}".encode()).decode()
    assert req.headers["authorization"] == f"Basic {want}"
    import json

    assert json.loads(req.content) == {"title": "T", "body": "B", "head": "lk/x", "base": "main"}


@respx.mock
async def test_no_token_sends_no_auth() -> None:
    route = respx.post("http://gitea:3000/api/v1/repos/o/r/pulls").respond(
        201, json={"number": 1, "html_url": "u"}
    )
    async with httpx.AsyncClient() as http:
        await _gitea(http, token=None).open_pull_request(**ARGS)
    assert "authorization" not in route.calls.last.request.headers


@respx.mock
async def test_github_bearer_and_html_url() -> None:
    route = respx.post("https://api.github.com/repos/o/r/pulls").respond(
        201, json={"number": 3, "html_url": "https://github.com/o/r/pull/3"}
    )
    async with httpx.AsyncClient() as http:
        f = GitHubForge(http, "https://api.github.com", "o", "r", token=TOKEN, sleep=_nosleep)
        pr = await f.open_pull_request(**ARGS)
    assert pr == PullRequest(number=3, url="https://github.com/o/r/pull/3")
    assert route.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"
    assert route.calls.last.request.headers["accept"] == "application/vnd.github+json"


@respx.mock
async def test_gitlab_private_token_and_web_url() -> None:
    route = respx.post(url="https://gl.example/api/v4/projects/a%2Fb%2Fr/merge_requests").respond(
        201, json={"iid": 5, "web_url": "https://gl.example/a/b/r/-/merge_requests/5"}
    )
    async with httpx.AsyncClient() as http:
        f = GitLabForge(http, "https://gl.example/api/v4", "a/b", "r", token=TOKEN, sleep=_nosleep)
        pr = await f.open_pull_request(**ARGS)
    assert pr == PullRequest(number=5, url="https://gl.example/a/b/r/-/merge_requests/5")
    req = route.calls.last.request
    assert req.headers["private-token"] == TOKEN
    assert "authorization" not in req.headers
    import json

    assert json.loads(req.content) == {
        "source_branch": "lk/x",
        "target_branch": "main",
        "title": "T",
        "description": "B",
    }


@respx.mock
async def test_existing_pr_is_reused() -> None:
    other = {"number": 1, "html_url": "o", "head": {"ref": "other"}}
    mine = {"number": 4, "html_url": "http://gitea:3000/o/r/pulls/4", "head": {"ref": "lk/x"}}
    respx.post("http://gitea:3000/api/v1/repos/o/r/pulls").respond(
        409, json={"message": "whatever text"}
    )
    lst = respx.get("http://gitea:3000/api/v1/repos/o/r/pulls").respond(200, json=[other, mine])
    async with httpx.AsyncClient() as http:
        pr = await _gitea(http).open_pull_request(**ARGS)
    assert pr.number == 4
    assert lst.calls.last.request.url.params["state"] == "open"

    respx.post("https://api.github.com/repos/o/r/pulls").respond(422, json={"message": "x"})
    gl_list = respx.get("https://api.github.com/repos/o/r/pulls").respond(
        200, json=[{"number": 9, "html_url": "gh9", "head": {"ref": "lk/x"}}]
    )
    async with httpx.AsyncClient() as http:
        f = GitHubForge(http, "https://api.github.com", "o", "r", token=TOKEN, sleep=_nosleep)
        assert (await f.open_pull_request(**ARGS)).number == 9
    assert gl_list.calls.last.request.url.params["head"] == "o:lk/x"

    respx.post("https://gl.example/api/v4/projects/a%2Fr/merge_requests").respond(409, json={})
    respx.get("https://gl.example/api/v4/projects/a%2Fr/merge_requests").respond(
        200, json=[{"iid": 2, "web_url": "gl2", "source_branch": "lk/x"}]
    )
    async with httpx.AsyncClient() as http:
        g = GitLabForge(http, "https://gl.example/api/v4", "a", "r", token=TOKEN, sleep=_nosleep)
        assert (await g.open_pull_request(**ARGS)).number == 2


@respx.mock
async def test_duplicate_without_match_fails() -> None:
    respx.post("http://gitea:3000/api/v1/repos/o/r/pulls").respond(409, json={})
    respx.get("http://gitea:3000/api/v1/repos/o/r/pulls").respond(
        200, json=[{"number": 1, "html_url": "o", "head": {"ref": "other"}}]
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(GitError) as ei:
            await _gitea(http).open_pull_request(**ARGS)
    assert ei.value.kind == "failed"


@respx.mock
async def test_gitea_404_after_push_is_retried() -> None:
    route = respx.post("http://gitea:3000/api/v1/repos/o/r/pulls").mock(
        side_effect=[
            httpx.Response(404, json={"message": "not found"}),
            httpx.Response(404, json={"message": "not found"}),
            httpx.Response(201, json={"number": 2, "html_url": "u"}),
        ]
    )
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    async with httpx.AsyncClient() as http:
        f = GiteaForge(http, "http://gitea:3000/api/v1", "o", "r", token=TOKEN, sleep=sleep)
        assert (await f.open_pull_request(**ARGS)).number == 2
    assert route.call_count == 3
    assert sleeps == [0.25, 0.5]


@respx.mock
async def test_persistent_404_is_network_not_not_found() -> None:
    route = respx.post("http://gitea:3000/api/v1/repos/o/r/pulls").respond(404, json={})
    async with httpx.AsyncClient() as http:
        with pytest.raises(GitError) as ei:
            await _gitea(http).open_pull_request(**ARGS)
    assert ei.value.kind == "network"
    assert route.call_count > 1


@respx.mock
@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, "auth"),
        (403, "auth"),
        (400, "failed"),
        (422, "failed"),
        (500, "network"),
        (503, "network"),
    ],
)
async def test_error_classification(status: int, kind: str) -> None:
    respx.post("http://gitea:3000/api/v1/repos/o/r/pulls").respond(status, json={"message": "m"})
    async with httpx.AsyncClient() as http:
        with pytest.raises(GitError) as ei:
            await _gitea(http).open_pull_request(**ARGS)
    assert ei.value.kind == kind


@respx.mock
async def test_list_404_is_not_found_and_transport_is_network() -> None:
    respx.post("http://gitea:3000/api/v1/repos/o/r/pulls").respond(409, json={})
    respx.get("http://gitea:3000/api/v1/repos/o/r/pulls").respond(404, json={})
    async with httpx.AsyncClient() as http:
        with pytest.raises(GitError) as ei:
            await _gitea(http).open_pull_request(**ARGS)
        assert ei.value.kind == "not_found"
    respx.post("http://gitea:3000/api/v1/repos/o/r/pulls").mock(
        side_effect=httpx.ConnectError("boom")
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(GitError) as ei:
            await _gitea(http).open_pull_request(**ARGS)
        assert ei.value.kind == "network"
    respx.post("http://gitea:3000/api/v1/repos/o/r/pulls").mock(side_effect=httpx.ReadTimeout("t"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(GitError) as ei:
            await _gitea(http).open_pull_request(**ARGS)
        assert ei.value.kind == "network"


@respx.mock
async def test_token_not_in_errors() -> None:
    respx.post("http://gitea:3000/api/v1/repos/o/r/pulls").respond(
        401, json={"message": f"bad token {TOKEN}"}
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(GitError) as ei:
            await _gitea(http).open_pull_request(**ARGS)
    assert TOKEN not in str(ei.value)
    assert TOKEN not in ei.value.message
    assert TOKEN not in repr(_gitea(httpx.AsyncClient()))
    respx.post("http://gitea:3000/api/v1/repos/o/r/pulls").mock(
        side_effect=httpx.ConnectError(f"conn {TOKEN}")
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(GitError) as ei:
            await _gitea(http).open_pull_request(**ARGS)
    assert TOKEN not in str(ei.value)
    assert "Authorization" not in str(ei.value)


async def test_client_not_closed() -> None:
    async with httpx.AsyncClient() as http:
        with respx.mock:
            respx.post("http://gitea:3000/api/v1/repos/o/r/pulls").respond(
                201, json={"number": 1, "html_url": "u"}
            )
            await _gitea(http).open_pull_request(**ARGS)
        assert not http.is_closed
