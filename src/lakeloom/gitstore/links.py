"""Web links to a release tag or commit on the forge (FR-PUB-07)."""

from urllib.parse import quote, urlsplit, urlunsplit

from lakeloom.config import ConfigError, Settings
from lakeloom.gitstore.forge import parse_owner_repo

__all__ = ["commit_url", "tag_url", "web_base"]


def web_base(settings: Settings) -> str | None:
    """``LAKELOOM_GIT_WEB_URL``, else derived from the Git remote; ``None`` when unknown."""
    if settings.git_web_url is not None:
        parts = urlsplit(str(settings.git_web_url))
        host = parts.netloc.rpartition("@")[2]  # web links never carry user info (NFR-SEC-11)
        return urlunsplit(parts._replace(netloc=host)).rstrip("/")
    remote = (settings.git_remote or "").strip()
    if not remote or settings.forge == "none":
        return None
    try:
        host, owner, repo = parse_owner_repo(remote)
    except ConfigError:
        return None  # a local path or file:// remote has no web page
    scheme = "http" if remote.lower().startswith("http://") else "https"
    return f"{scheme}://{host}/{owner}/{repo}"


def tag_url(base: str, forge: str, tag: str) -> str:
    """The page of ``tag`` (an unknown forge such as ``none`` uses GitHub's layout)."""
    enc = quote(tag, safe="/")
    if forge == "gitea":
        return f"{base}/src/tag/{enc}"
    if forge == "gitlab":
        return f"{base}/-/tree/{enc}"
    return f"{base}/tree/{enc}"


def commit_url(base: str, forge: str, sha: str) -> str:
    return f"{base}/-/commit/{sha}" if forge == "gitlab" else f"{base}/commit/{sha}"
