"""Cache-validated publication artefacts (FR-PUB-02, FR-PUB-07).

An :class:`Artifact` is a body plus the validators a cache needs: a strong ``ETag`` (SHA-256 of
the body), ``Last-Modified`` and ``Link`` values.

- ``latest`` (OpenMetadata's current state): ``Last-Modified`` is OpenMetadata ``updatedAt`` of
  the source entity, falling back to the time the model graph was loaded; ``Link`` points at
  the OpenMetadata entity (``rel="via"``, ADR 0010) plus, with Git links, the commit of the
  model's last Git publication (``rel="related"``).
- A release (``version=MAJOR.MINOR.PATCH``, read from its Git tag): ``Last-Modified`` is the
  tagger date and ``Link`` points at the tag and its commit on the forge (both ``rel="via"``).
  Git links need a web base (ADR 0015); web URLs never carry user info.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict
from rdflib import Graph

from lakeloom.core.concepts import concept_markdown, explain_concept, fetch_tables
from lakeloom.core.context import (
    get_asset_context,
    render_json,
    render_jsonld,
    render_markdown,
)
from lakeloom.core.models import ModelInfo, ModelService
from lakeloom.errors import LakeloomError
from lakeloom.gitstore.links import commit_url, tag_url
from lakeloom.gitstore.release_reader import ReleaseFile, ReleaseInfo, ReleaseReader
from lakeloom.om.client import OMClient
from lakeloom.publish.negotiation import FORMAT_BY_MEDIA, MediaType
from lakeloom.publish.shacl_gen import ShapesMedia
from lakeloom.rdf.canonical import serialize
from lakeloom.sources.base import LakeSource

__all__ = [
    "Artifact",
    "GitLinks",
    "asset_context_artifact",
    "concept_artifact",
    "context_bytes",
    "etag_for",
    "glossary_link",
    "json_bytes",
    "matches_if_none_match",
    "model_artifact",
    "model_context_artifact",
    "models_list_artifact",
    "require_latest",
    "resolve_version",
    "shapes_artifact",
    "table_link",
    "versions_artifact",
]

type Clock = Callable[[], datetime]

_log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


_LIST_LIMIT = 200
_MODELS_LIST_PAGES = 50  # safety bound on pagination while building the models list


class Artifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    body: bytes
    media_type: str
    etag: str
    last_modified: datetime
    links: list[str]


def etag_for(body: bytes) -> str:
    return '"sha256:' + hashlib.sha256(body).hexdigest() + '"'


def _opaque(tag: str) -> str:
    tag = tag.strip()
    return tag[2:] if tag.startswith(("W/", "w/")) else tag


def matches_if_none_match(header: str | None, etag: str) -> bool:
    """``If-None-Match`` evaluation with the weak comparison of RFC 9110 section 13.1.2."""
    if not header or not header.strip():
        return False
    if header.strip() == "*":
        return True
    wanted = _opaque(etag)
    return any(_opaque(candidate) == wanted for candidate in header.split(",") if candidate.strip())


def glossary_link(om_url: str, slug: str) -> str:
    return f'<{om_url}/glossary/{quote(slug, safe="")}>; rel="via"'


def table_link(om_url: str, fqn: str) -> str:
    return f'<{om_url}/table/{quote(fqn, safe="")}>; rel="via"'


def _from_ms(ms: int | None) -> datetime | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, UTC)


@dataclass(frozen=True)
class GitLinks:
    """Forge web links for artefacts (FR-PUB-07): ``base`` has no user info; ``last_commit``
    gives the commit of a model's last done Git publication (``None`` when there is none)."""

    base: str
    forge: str
    last_commit: Callable[[str], Awaitable[str | None]]

    def tag(self, tag: str) -> str:
        return tag_url(self.base, self.forge, tag)

    def commit(self, sha: str) -> str:
        return commit_url(self.base, self.forge, sha)


def _shown(version: str) -> str:
    return version if len(version) <= 40 else version[:40] + "..."


def require_latest(version: str | None, what: str) -> None:
    """For artefacts that exist only at ``latest`` (the models list, asset context)."""
    if version not in (None, "latest"):
        raise LakeloomError(
            "not_found",
            f"Unknown version {_shown(version)!r}",
            hint=f"{what} is only served at version=latest",
        )


async def resolve_version(
    reader: ReleaseReader | None, slug: str, version: str | None
) -> ReleaseInfo | None:
    """The release ``version`` of ``slug``; ``None`` means latest (absent or ``latest``).

    An unknown or malformed version is ``not_found`` (the reader validates it before any Git
    call); with Git publishing off every version but ``latest`` is unknown.
    """
    if version is None or version == "latest":
        return None
    if reader is None:
        raise LakeloomError(
            "not_found",
            f"no release {_shown(version)!r} of {_shown(slug)!r}",
            "Git publishing is off",
        )
    return await reader.get(slug, version)


def _release_links(git: GitLinks | None, rel: ReleaseInfo) -> list[str]:
    if git is None:
        return []
    return [f'<{git.tag(rel.tag)}>; rel="via"', f'<{git.commit(rel.commit_sha)}>; rel="via"']


async def _related_commit(git: GitLinks | None, slug: str) -> str | None:
    """The web URL of the last Git publication of ``slug``; a lookup failure only drops it."""
    if git is None:
        return None
    try:
        sha = await git.last_commit(slug)
    except Exception as exc:
        _log.warning("last Git publication of %s unavailable (%s)", slug, type(exc).__name__)
        return None
    return None if sha is None else git.commit(sha)


def _release_artifact(
    body: bytes, media_type: str, rel: ReleaseInfo, git: GitLinks | None
) -> Artifact:
    return _make(body, media_type, rel.released_at.astimezone(UTC), _release_links(git, rel))


def json_bytes(data: Any) -> bytes:
    text = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return (text + "\n").encode()


def context_bytes(context: dict[str, Any]) -> bytes:
    """The bytes of a model's JSON-LD context document (HTTP body and Git file alike)."""
    return json_bytes({"@context": context})


def _make(body: bytes, media_type: str, last_modified: datetime, links: list[str]) -> Artifact:
    return Artifact(
        body=body,
        media_type=media_type,
        etag=etag_for(body),
        last_modified=last_modified.replace(microsecond=0),  # HTTP dates have 1 s resolution
        links=links,
    )


async def _model_modified(models: ModelService, info: ModelInfo) -> datetime:
    """Best-effort lower bound: the later of OM's ``updatedAt`` and the graph load time."""
    loaded = await models.loaded_at(info.slug)
    updated = _from_ms(info.updated_at)
    return max(loaded, updated) if updated else loaded


async def _model_stamp(
    models: ModelService, info: ModelInfo, git: GitLinks | None = None
) -> tuple[datetime, list[str]]:
    modified = await _model_modified(models, info)
    om_url = models.om_url
    links = [] if om_url is None else [glossary_link(om_url, info.slug)]
    related = await _related_commit(git, info.slug)
    if related is not None:
        links.append(f'<{related}>; rel="related"; title="last Git publication"')
    return modified, links


async def model_artifact(
    models: ModelService,
    slug: str,
    media: MediaType,
    *,
    version: str | None,
    releases: ReleaseReader | None = None,
    git: GitLinks | None = None,
    effective: Callable[[str], Awaitable[Graph]] | None = None,
) -> Artifact:
    """The model as an ontology: OpenMetadata's current state, or a release's tagged bytes.

    ``effective`` returns the model's effective graph (OM plus the governance annotations of
    the proposal log); the MCP tool, the HTTP API and the resources pass the same one, so
    the three serve identical bytes."""
    fmt = FORMAT_BY_MEDIA.get(media)
    if fmt is None:
        raise LakeloomError(
            "validation_error",
            f"{media} is not an ontology serialisation",
            hint="use text/turtle, application/ld+json, application/rdf+xml or "
            "application/n-triples",
        )
    rel = await resolve_version(releases, slug, version)
    if rel is not None and releases is not None:
        return _release_artifact(await releases.ontology(slug, rel.version, fmt), media, rel, git)
    info = await models.get_model(slug)
    if effective is None:
        body = await models.export(slug, fmt)  # loads (or reuses) the graph for loaded_at
    else:
        graph = await effective(slug)
        body = await asyncio.to_thread(serialize, graph, fmt, prefixes=dict(info.prefixes))
    modified, links = await _model_stamp(models, info, git)
    return _make(body, media, modified, links)


async def model_context_artifact(
    models: ModelService,
    slug: str,
    *,
    version: str | None,
    releases: ReleaseReader | None = None,
    git: GitLinks | None = None,
) -> Artifact:
    """The model's full JSON-LD ``@context`` (FR-PUB-05)."""
    rel = await resolve_version(releases, slug, version)
    if rel is not None and releases is not None:
        body = await releases.file(slug, rel.version, "context.jsonld")
        return _release_artifact(body, "application/ld+json", rel, git)
    info = await models.get_model(slug)
    body = await models.jsonld_context_bytes(slug)  # also fails like the other artefacts
    modified, links = await _model_stamp(models, info, git)
    return _make(body, "application/ld+json", modified, links)


async def shapes_artifact(
    models: ModelService,
    slug: str,
    media: ShapesMedia,
    *,
    version: str | None,
    releases: ReleaseReader | None = None,
    git: GitLinks | None = None,
) -> Artifact:
    """The model's SHACL shapes (FR-PUB-04); turtle is the bytes the Git file carries.
    The bytes are serialised once per model version and media type."""
    rel = await resolve_version(releases, slug, version)
    if rel is not None and releases is not None:
        return _release_artifact(await releases.shapes(slug, rel.version, media), media, rel, git)
    info = await models.get_model(slug)
    body = await models.shapes_bytes(slug, media)
    modified, links = await _model_stamp(models, info, git)
    return _make(body, media, modified, links)


def _url(link: str) -> str:
    return link[1 : link.index(">")]


async def versions_artifact(
    models: ModelService,
    slug: str,
    media_types: Sequence[str],
    *,
    releases: ReleaseReader | None = None,
    git: GitLinks | None = None,
) -> Artifact:
    """``latest`` first, then the releases semver-descending (FR-PUB-02).

    Each entry is ``{version, tag, commit, released_at, media_types, links}``; ``links`` are
    ``{href, rel}`` objects (the same URLs as the artefact's ``Link`` header). The listing's
    validators: the latest artefact's links, and the later of its Last-Modified and the newest
    tagger date. When the release tags are unavailable the listing holds ``latest`` only.
    """
    latest = await model_artifact(
        models, slug, "text/turtle", version=None, releases=releases, git=git
    )  # resolves the model (404) and supplies Last-Modified and Link
    rels: list[ReleaseInfo] = []
    if releases is not None:
        try:
            rels = await releases.list(slug)
        except LakeloomError as exc:
            if exc.code != "upstream_unavailable":
                raise
            # The tags are unreadable right now: still list ``latest`` (FR-PUB-02).
            _log.warning("release tags of %s unavailable; listing latest only", slug)

    def entry(version: str, rel: ReleaseInfo | None, links: list[str]) -> dict[str, Any]:
        return {
            "version": version,
            "tag": None if rel is None else rel.tag,
            "commit": None if rel is None else rel.commit_sha,
            "released_at": None
            if rel is None
            else rel.released_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "media_types": list(media_types),
            "links": [
                {"href": _url(link), "rel": "related" if 'rel="related"' in link else "via"}
                for link in links
            ],
        }

    data = [entry("latest", None, latest.links)]
    data += [entry(r.version, r, _release_links(git, r)) for r in rels]
    stamps = [latest.last_modified, *(r.released_at.astimezone(UTC) for r in rels)]
    return _make(json_bytes(data), "application/json", max(stamps), latest.links)


async def models_list_artifact(
    models: ModelService,
    *,
    cursor: str | None = None,
    limit: int | None = None,
    clock: Clock = _utcnow,
) -> Artifact:
    """All models, or one page (with ``next_cursor``) when ``cursor`` or ``limit`` is given."""
    items: list[dict[str, Any]] = []
    next_cursor: str | None = None
    paged = cursor is not None or limit is not None
    if paged:
        page = await models.list_models(cursor, limit or _LIST_LIMIT)
        items = [m.model_dump(mode="json") for m in page.items]
        next_cursor = page.next_cursor
    else:
        for _ in range(_MODELS_LIST_PAGES):
            page = await models.list_models(cursor, _LIST_LIMIT)
            items += [m.model_dump(mode="json") for m in page.items]
            cursor = page.next_cursor
            if cursor is None:
                break
    newest = max((m["updated_at"] for m in items if m.get("updated_at")), default=None)
    om_url = models.om_url
    links = [] if om_url is None else [f'<{om_url}/glossary>; rel="via"']
    modified = _from_ms(newest) or clock()  # never a made-up date: caches would extrapolate it
    data: dict[str, Any] = {"items": items}
    if paged:
        data["next_cursor"] = next_cursor
    return _make(json_bytes(data), "application/json", modified, links)


async def concept_artifact(
    models: ModelService,
    om: OMClient,
    concept: str,
    media: Literal["text/markdown", "application/json"],
) -> Artifact:
    detail = await explain_concept(models, om, concept)
    if media == "text/markdown":
        body = concept_markdown(detail)[0].encode()
    else:
        body = json_bytes(detail.model_dump(mode="json"))
    info = await models.get_model(detail.concept.model)
    modified, links = await _model_stamp(models, info)
    return _make(body, media, modified, links)


async def asset_context_artifact(
    models: ModelService,
    om: OMClient,
    sources: Mapping[str, LakeSource],
    fqn: str,
    media: Literal["text/markdown", "application/json", "application/ld+json"],
    *,
    max_tokens: int | None = None,
    clock: Clock = _utcnow,
) -> Artifact:
    ctx = await get_asset_context(fqn, models=models, om=om, sources=sources)
    model = await models.get_model(ctx.model) if ctx.model else None
    if media == "text/markdown":
        body = render_markdown(ctx, max_tokens=max_tokens).payload.encode()
    elif media == "application/json":
        body = json_bytes(render_json(ctx, max_tokens=max_tokens).payload)
    else:
        full = await models.jsonld_context(ctx.model) if ctx.model else None
        body = json_bytes(render_jsonld(ctx, model, max_tokens=max_tokens, context=full).payload)
    table = (await fetch_tables(models, om, [ctx.asset.table_fqn]))[ctx.asset.table_fqn]
    # Best-effort lower bound: the table's updatedAt and every realized model's freshness.
    slugs = {r.model for r in ctx.table_concepts} | {
        r.model for col in ctx.columns for r in col.concepts
    }
    stamps = [_from_ms(table.updated_at if table else None)]
    for slug in sorted(slugs):
        stamps.append(await _model_modified(models, await models.get_model(slug)))
    known = [t for t in stamps if t is not None]
    modified = max(known) if known else clock()
    om_url = models.om_url
    links = [] if om_url is None else [table_link(om_url, ctx.asset.table_fqn)]
    return _make(body, media, modified, links)


async def deliverable_artifact(
    models: ModelService,
    slug: str,
    filename: str,
    media_type: str,
    render_latest: Callable[[], Awaitable[bytes]],
    *,
    version: str | None,
    releases: ReleaseReader | None = None,
    git: GitLinks | None = None,
) -> Artifact:
    """A stage deliverable (glossary, taxonomy, synonyms, data dictionary).

    ``latest`` renders the current effective model; a release serves the file its tag
    carries under ``deliverables/`` (releases made before deliverables existed have none:
    ``not_found``)."""
    rel = await resolve_version(releases, slug, version)
    if rel is not None and releases is not None:
        try:
            name = cast("ReleaseFile", f"deliverables/{filename}")
            body = await releases.file(slug, rel.version, name)
        except LakeloomError as exc:
            if exc.code != "not_found":
                raise
            raise LakeloomError(
                "not_found",
                f"release {rel.version} of {slug!r} has no {filename}",
                "releases made before stage deliverables have none; use version latest",
            ) from None
        return _release_artifact(body, media_type, rel, git)
    info = await models.get_model(slug)
    body = await render_latest()
    modified, links = await _model_stamp(models, info, git)
    return _make(body, media_type, modified, links)
