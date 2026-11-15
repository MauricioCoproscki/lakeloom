from collections.abc import Callable

import pytest

from lakeloom.config import Settings
from lakeloom.gitstore.links import commit_url, tag_url, web_base


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        (
            {"git_remote": "https://user@git.example.com/org/repo.git"},
            "https://git.example.com/org/repo",
        ),
        ({"git_remote": "http://gitea:3000/org/repo"}, "http://gitea:3000/org/repo"),
        (
            {"git_remote": "ssh://git@host.example:2222/org/sub/repo.git"},
            "https://host.example/org/sub/repo",
        ),
        ({"git_remote": "git@github.com:org/repo.git"}, "https://github.com/org/repo"),
        (
            {"git_remote": "git@github.com:org/repo.git", "git_web_url": "https://web.example/x/"},
            "https://web.example/x",
        ),
        ({"git_remote": "git@github.com:org/repo.git", "forge": "none"}, None),
        (
            {
                "git_remote": "git@github.com:org/repo.git",
                "forge": "none",
                "git_web_url": "https://w.example/r",
            },
            "https://w.example/r",
        ),
        (
            {"git_web_url": "https://u:s@h.example/o/r?access_token=x#f"},
            "https://h.example/o/r",
        ),
        ({"git_remote": "file:///tmp/remote.git"}, None),
        ({}, None),
        ({"git_web_url": "  "}, None),
    ],
)
def test_web_base_derivation(
    make_settings: Callable[..., Settings], kw: dict[str, str], expected: str | None
) -> None:
    assert web_base(make_settings(**kw)) == expected


def test_tag_and_commit_urls_per_forge() -> None:
    b = "https://h.example/o/r"
    assert tag_url(b, "github", "a/v1.0.0") == f"{b}/tree/a/v1.0.0"
    assert tag_url(b, "gitea", "a/v1.0.0") == f"{b}/src/tag/a/v1.0.0"
    assert tag_url(b, "gitlab", "a/v1.0.0") == f"{b}/-/tree/a/v1.0.0"
    assert tag_url(b, "github", "a b/v1.0.0") == f"{b}/tree/a%20b/v1.0.0"
    assert commit_url(b, "github", "abc") == f"{b}/commit/abc"
    assert commit_url(b, "gitea", "abc") == f"{b}/commit/abc"
    assert commit_url(b, "gitlab", "abc") == f"{b}/-/commit/abc"
