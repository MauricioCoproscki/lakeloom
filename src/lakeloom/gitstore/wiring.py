"""Build the :class:`GitPublisher` from settings, for the server and the CLI alike.

With ``LAKELOOM_GIT_REMOTE`` unset the publisher is disabled (no working copy, no forge) and
nothing is validated. Otherwise ``pull_request`` as the default mode or in any model's
``git: {mode}`` needs a forge: ``LAKELOOM_FORGE=none`` is a ``ConfigError``, and so is a
remote whose owner/repository cannot be parsed. The forge is built only when a mode needs it.
"""

from collections.abc import Sequence

import httpx

from lakeloom.config import ConfigError, Settings
from lakeloom.core.model_policy import ModelPolicyEntry
from lakeloom.gitstore.forge import forge_for
from lakeloom.gitstore.git import GitRunner
from lakeloom.gitstore.publisher import Exporter, GitPublisher
from lakeloom.gitstore.repo import WorkingCopy
from lakeloom.state.publications import PublicationRepository

__all__ = ["FORGE_TIMEOUT", "build_publisher", "git_modes"]

FORGE_TIMEOUT = 15.0


def git_modes(settings: Settings, policies: Sequence[ModelPolicyEntry]) -> set[str]:
    """Every mode in effect: the default and each model override."""
    return {settings.git_mode} | {e.git.mode for e in policies if e.git is not None}


def build_publisher(
    settings: Settings,
    repo: PublicationRepository,
    http: httpx.AsyncClient | None,
    policies: Sequence[ModelPolicyEntry] = (),
    *,
    exporter: Exporter | None = None,
) -> GitPublisher:
    """The publisher for ``settings``; ``http`` is the forge client (needed for a forge)."""
    if not settings.git_remote:
        return GitPublisher(settings, repo, None, None, exporter=exporter)
    forge = None
    if "pull_request" in git_modes(settings, policies):
        if settings.forge == "none":
            raise ConfigError(
                "pull_request Git mode (LAKELOOM_GIT_MODE or a model's git.mode) needs a "
                "forge: set LAKELOOM_FORGE to github, gitlab or gitea, or use direct mode"
            )
        if http is None:
            raise ConfigError("pull_request Git mode needs an HTTP client for the forge")
        forge = forge_for(settings, http)
    root = settings.data_dir / "git"
    wc = WorkingCopy(GitRunner(settings, root), root / "repo", settings.git_remote)
    return GitPublisher(settings, repo, wc, forge, exporter=exporter)
