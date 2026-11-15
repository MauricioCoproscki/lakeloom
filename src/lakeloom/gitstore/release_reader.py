"""Reads of released versions from the Git tags (FR-PUB-02, FR-PUB-01, FR-IO-01).

Tags are immutable, so file bytes are cached by (tag object sha, file). The remote is only
fetched (pruned: a tag deleted on the remote disappears) under the repository lock and at most
once per ``refresh_seconds`` per model, and the tag list is captured there, under the lock:
a tag the release service has created locally but not pushed yet is never listed. Every other
read hits the local object store without the lock. Git failures other than the throttled
fetch surface as ``upstream_unavailable``.
"""

import asyncio
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from lakeloom.core.model_create import SLUG_PATTERN
from lakeloom.core.release_diff import parse_version
from lakeloom.errors import LakeloomError
from lakeloom.gitstore.git import GitError
from lakeloom.gitstore.publisher import repo_lock
from lakeloom.gitstore.release_layout import tag_name
from lakeloom.gitstore.repo import TagInfo, WorkingCopy
from lakeloom.publish.shacl_gen import ShapesMedia
from lakeloom.rdf.canonical import ExportFormat, parse_rdf, serialize

__all__ = ["ReleaseFile", "ReleaseInfo", "ReleaseReader"]

_log = logging.getLogger(__name__)
_SLUG = re.compile(SLUG_PATTERN)
_CACHE_SIZE = 128
_MAX_SHOWN = 40
_UNAVAILABLE_HINT = "the Git working copy or remote failed; retry shortly"

ReleaseFile = Literal[
    "ontology.ttl",
    "ontology.jsonld",
    "ontology.owl",
    "shapes.ttl",
    "context.jsonld",
    "manifest.yaml",
    "CHANGELOG.md",
]
_FILES: frozenset[str] = frozenset(
    {
        "ontology.ttl",
        "ontology.jsonld",
        "ontology.owl",
        "shapes.ttl",
        "context.jsonld",
        "manifest.yaml",
        "CHANGELOG.md",
    }
)
_STORED: dict[ExportFormat, ReleaseFile] = {
    "turtle": "ontology.ttl",
    "jsonld": "ontology.jsonld",
    "rdfxml": "ontology.owl",
}
_SHAPES_FORMAT: dict[ShapesMedia, ExportFormat] = {
    "application/ld+json": "jsonld",
    "application/n-triples": "ntriples",
    "text/turtle": "turtle",
}


class ReleaseInfo(BaseModel):
    version: str
    tag: str
    object_sha: str
    commit_sha: str
    released_at: datetime


def _shown(text: str) -> str:
    return text if len(text) <= _MAX_SHOWN else text[:_MAX_SHOWN] + "..."


def _versions_hint(slug: str) -> str:
    return f"GET /v1/models/{slug if _SLUG.fullmatch(slug) else '{slug}'}/versions"


def _unknown(slug: str, version: str) -> LakeloomError:
    return LakeloomError(
        "not_found",
        f"no release {_shown(version)!r} of {_shown(slug)!r}",
        hint=_versions_hint(slug),
    )


def _unavailable(exc: GitError) -> LakeloomError:
    _log.warning("release tags unavailable: %s", exc.kind)
    return LakeloomError("upstream_unavailable", "release tags unavailable", hint=_UNAVAILABLE_HINT)


class ReleaseReader:
    def __init__(
        self,
        wc: WorkingCopy,
        repo_key: str,
        *,
        refresh_seconds: float = 30,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._wc = wc
        self._key = repo_key
        self._refresh = refresh_seconds
        self._clock = clock
        self._fetched: dict[str, float] = {}
        self._tags: dict[str, list[TagInfo]] = {}
        self._cache: OrderedDict[tuple[str, str], bytes] = OrderedDict()

    def invalidate(self, slug: str) -> None:
        """Make the next read of ``slug`` fetch the remote tags again (after a release)."""
        self._fetched.pop(slug, None)

    def _fresh(self, slug: str) -> bool:
        last = self._fetched.get(slug)
        return last is not None and self._clock() - last < self._refresh

    async def _refresh_tags(self, slug: str) -> list[TagInfo]:
        """The tags of ``slug`` as captured under the lock at the last refresh."""
        cached = self._tags.get(slug)
        if cached is not None and self._fresh(slug):
            return cached
        async with repo_lock(self._key):
            cached = self._tags.get(slug)
            if cached is not None and self._fresh(slug):  # a queued reader just fetched
                return cached
            try:
                await self._wc.ensure()
            except GitError as exc:
                raise _unavailable(exc) from None
            try:
                await self._wc.fetch_tags(f"{slug}/v*")
            except GitError as exc:
                # Serve what is already local; a failure is throttled like a success.
                _log.warning("release tag fetch for %s failed: %s", slug, exc.kind)
            try:
                tags = await self._wc.tags(f"{slug}/")
            except GitError as exc:
                raise _unavailable(exc) from None
            self._tags[slug] = tags
            self._fetched[slug] = self._clock()
            return tags

    def _remember(self, key: tuple[str, str], data: bytes) -> None:
        self._cache[key] = data
        self._cache.move_to_end(key)
        while len(self._cache) > _CACHE_SIZE:
            self._cache.popitem(last=False)

    async def _read(self, info: ReleaseInfo, slug: str, name: str) -> bytes | None:
        key = (info.object_sha, name)
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            return hit
        try:
            data = await self._wc.show(info.commit_sha, f"ontologies/{slug}/{name}")
        except GitError as exc:
            raise _unavailable(exc) from None
        if data is not None:
            self._remember(key, data)
        return data

    async def list(self, slug: str) -> list[ReleaseInfo]:
        """Releases of ``slug``, highest version first."""
        if not _SLUG.fullmatch(slug):
            return []
        releases: list[tuple[tuple[int, int, int], ReleaseInfo]] = []
        for tag in await self._refresh_tags(slug):
            version = tag.name.removeprefix(f"{slug}/v")
            try:
                parsed = parse_version(version)
            except LakeloomError:
                continue
            if tag.name != tag_name(slug, version):
                continue
            info = ReleaseInfo(
                version=version,
                tag=tag.name,
                object_sha=tag.object_sha,
                commit_sha=tag.commit_sha,
                released_at=tag.tagged_at,
            )
            if await self._read(info, slug, "ontology.ttl") is None:
                continue
            releases.append((parsed, info))
        releases.sort(key=lambda r: r[0], reverse=True)
        return [info for _, info in releases]

    async def get(self, slug: str, version: str) -> ReleaseInfo:
        try:
            parse_version(version)
        except LakeloomError:
            raise _unknown(slug, version) from None
        for info in await self.list(slug):
            if info.version == version:
                return info
        raise _unknown(slug, version)

    async def file(self, slug: str, version: str, name: ReleaseFile) -> bytes:
        if name not in _FILES:
            raise ValueError(f"unknown release file: {name!r}")
        info = await self.get(slug, version)
        data = await self._read(info, slug, name)
        if data is None:
            what = "shapes" if name in ("shapes.ttl", "context.jsonld") else name
            raise LakeloomError(
                "not_found",
                f"release {version} has no {what}",
                hint=_versions_hint(slug),
            )
        return data

    async def _converted(
        self, slug: str, version: str, name: ReleaseFile, fmt: ExportFormat
    ) -> bytes:
        stored = await self.file(slug, version, name)
        info = await self.get(slug, version)
        key = (info.object_sha, f"{name}:{fmt}")
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            return hit
        graph = await asyncio.to_thread(parse_rdf, stored, "turtle")
        out = await asyncio.to_thread(serialize, graph, fmt)
        self._remember(key, out)
        return out

    async def ontology(self, slug: str, version: str, fmt: ExportFormat) -> bytes:
        stored = _STORED.get(fmt)
        if stored is not None:
            return await self.file(slug, version, stored)
        return await self._converted(slug, version, "ontology.ttl", fmt)

    async def shapes(self, slug: str, version: str, media: ShapesMedia) -> bytes:
        fmt = _SHAPES_FORMAT[media]
        if fmt == "turtle":
            return await self.file(slug, version, "shapes.ttl")
        return await self._converted(slug, version, "shapes.ttl", fmt)
