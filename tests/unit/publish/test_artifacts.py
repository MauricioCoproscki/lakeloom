"""Publication artefacts: ETag, Last-Modified, Link (FR-PUB-02, FR-PUB-07)."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from lakeloom.config import Settings
from lakeloom.core.models import ModelService
from lakeloom.errors import LakeloomError
from lakeloom.gitstore.links import web_base
from lakeloom.gitstore.release_reader import ReleaseInfo
from lakeloom.om.client import OMUnavailable
from lakeloom.om.models import OMEntityRef, OMGlossary, OMGlossaryTerm, OMLineage, OMPage, OMTable
from lakeloom.publish.artifacts import (
    GitLinks,
    asset_context_artifact,
    concept_artifact,
    etag_for,
    matches_if_none_match,
    model_artifact,
    model_context_artifact,
    models_list_artifact,
    require_latest,
    resolve_version,
    shapes_artifact,
)
from lakeloom.publish.negotiation import FORMAT_BY_MEDIA

B = "http://x/ontology/retail/"
TURTLE = f"""\
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix om: <https://open-metadata.org/ontology/> .
@prefix r: <{B}> .
r:Order a skos:Concept, om:GlossaryTerm ;
    skos:prefLabel "Order" ; skos:definition "An order." ;
    om:fullyQualifiedName "retail.Order" .
"""
ORDERS = "svc.lake.retail.orders"
GLOSSARY_MS = 4_102_444_800_000  # year 2100: later than any graph load time
TABLE_MS = 4_000_000_000_000


class FakeOM:
    def __init__(self, glossary_ms: int | None = GLOSSARY_MS) -> None:
        self.glossary_ms = glossary_ms
        self.down = False

    def _glossary(self) -> OMGlossary:
        data: dict[str, Any] = {"id": "id-retail", "name": "retail", "fullyQualifiedName": "retail"}
        if self.glossary_ms is not None:
            data["updatedAt"] = self.glossary_ms
        return OMGlossary.model_validate(data)

    async def list_glossaries(self, *, after: str | None = None, limit: int = 50) -> Any:
        return OMPage[OMGlossary](data=[self._glossary()], after=None)

    async def get_glossary_by_name(self, name: str, **_: object) -> OMGlossary | None:
        if self.down:
            raise OMUnavailable("down")
        return self._glossary() if name == "retail" else None

    async def export_native_ontology(self, name: str):
        return None  # no paired native ontology

    async def export_ontology(self, glossary_id: str, fmt: str, **_: object) -> str:
        return TURTLE

    async def get_term(self, id_or_fqn: str, *_: object, **__: object) -> OMGlossaryTerm | None:
        if id_or_fqn != "retail.Order":
            return None
        return OMGlossaryTerm.model_validate(
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "name": "Order",
                "fullyQualifiedName": "retail.Order",
                "glossary": {"id": "id-retail", "name": "retail", "type": "glossary"},
            }
        )

    async def get_term_assets(self, term_id: str, *, offset: int = 0, limit: int = 50) -> Any:
        ref = OMEntityRef.model_validate(
            {"id": ORDERS, "type": "table", "name": "orders", "fullyQualifiedName": ORDERS}
        )
        return OMPage[OMEntityRef](data=[ref], after=None, total=1)

    async def get_table_by_fqn(self, fqn: str, **_: object) -> OMTable | None:
        if fqn != ORDERS:
            return None
        return OMTable.model_validate(
            {
                "id": ORDERS,
                "fullyQualifiedName": ORDERS,
                "updatedAt": TABLE_MS,
                "tags": [{"tagFQN": "retail.Order", "source": "Glossary", "labelType": "Manual"}],
                "columns": [],
            }
        )

    async def get_lineage(self, entity_type: str, fqn: str, **_: object) -> OMLineage:
        return OMLineage()


def make(om_url: str | None = "http://om:8585/", **kw: Any) -> tuple[ModelService, FakeOM]:
    om = FakeOM(**kw)
    settings = Settings(public_url="http://x", om_url=om_url)  # pyright: ignore[reportArgumentType]
    return ModelService(om, settings), om  # pyright: ignore[reportArgumentType]


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms // 1000, UTC)  # HTTP dates: whole seconds


def test_fr_pub_07_etag_is_sha256_of_body() -> None:
    body = b"hello"
    assert etag_for(body) == f'"sha256:{hashlib.sha256(body).hexdigest()}"'


def test_if_none_match_variants() -> None:
    e = etag_for(b"x")
    assert matches_if_none_match("*", e)
    assert matches_if_none_match(e, e)
    assert matches_if_none_match(f'"other", {e}', e)
    assert matches_if_none_match(f"W/{e}", e)
    assert matches_if_none_match(f'"a" ,W/{e} , "b"', e)
    assert not matches_if_none_match('"other"', e)
    assert not matches_if_none_match(None, e)
    assert not matches_if_none_match("", e)
    assert not matches_if_none_match("garbage", e)


@pytest.mark.parametrize(
    ("media", "needle"),
    [
        ("text/turtle", b"@prefix"),
        ("application/ld+json", b"@id"),
        ("application/rdf+xml", b"rdf:RDF"),
        ("application/n-triples", b"<http://x/ontology/retail/Order>"),
    ],
)
async def test_model_artifact_formats_and_links(media: Any, needle: bytes) -> None:
    models, _ = make()
    art = await model_artifact(models, "retail", media, version=None)
    assert needle in art.body
    assert art.media_type == media
    assert art.etag == etag_for(art.body)
    assert art.last_modified == _dt(GLOSSARY_MS)
    assert art.links == ['<http://om:8585/glossary/retail>; rel="via"']
    assert art.body == await models.export("retail", FORMAT_BY_MEDIA[media])


async def test_model_artifact_last_modified_falls_back_to_load_time() -> None:
    models, _ = make(glossary_ms=None)
    before = datetime.now(UTC).replace(microsecond=0)
    art = await model_artifact(models, "retail", "text/turtle", version="latest")
    assert before <= art.last_modified <= datetime.now(UTC)
    again = await model_artifact(models, "retail", "text/turtle", version="latest")
    assert again.last_modified == art.last_modified  # stable while the graph is cached


async def test_no_om_url_means_no_links() -> None:
    models, _ = make(om_url=None)
    art = await model_artifact(models, "retail", "text/turtle", version=None)
    assert art.links == []


@pytest.mark.parametrize("version", ["1.0.0", "v2", ""])
async def test_version_other_than_latest_not_found_with_git_off(version: str) -> None:
    models, _ = make()
    with pytest.raises(LakeloomError) as e:
        await model_artifact(models, "retail", "text/turtle", version=version)
    assert e.value.code == "not_found"
    assert e.value.hint == "Git publishing is off"
    with pytest.raises(LakeloomError):
        await model_context_artifact(models, "retail", version=version)


async def test_unknown_model_not_found() -> None:
    models, _ = make()
    with pytest.raises(LakeloomError) as e:
        await model_artifact(models, "nope", "text/turtle", version=None)
    assert e.value.code == "not_found"


async def test_model_context_artifact() -> None:
    models, _ = make()
    art = await model_context_artifact(models, "retail", version="latest")
    assert art.media_type == "application/ld+json"
    ctx = json.loads(art.body)["@context"]
    assert ctx["@vocab"] == B
    assert ctx["retail"] == B
    assert ctx["dcterms"] == "http://purl.org/dc/terms/"
    assert ctx["skos"] == "http://www.w3.org/2004/02/skos/core#"
    assert art.etag == etag_for(art.body)
    assert art.last_modified == _dt(GLOSSARY_MS)
    assert art.links == ['<http://om:8585/glossary/retail>; rel="via"']


async def test_models_list_artifact() -> None:
    models, _ = make()
    art = await models_list_artifact(models)
    assert art.media_type == "application/json"
    items = json.loads(art.body)["items"]
    assert [i["slug"] for i in items] == ["retail"]
    assert art.etag == etag_for(art.body)


async def test_concept_artifact_markdown_and_json() -> None:
    models, om = make()
    md = await concept_artifact(models, om, "retail.Order", "text/markdown")  # pyright: ignore[reportArgumentType]
    assert md.media_type == "text/markdown"
    assert b"Order" in md.body
    assert md.last_modified == _dt(GLOSSARY_MS)
    assert md.links == ['<http://om:8585/glossary/retail>; rel="via"']
    js = await concept_artifact(models, om, "retail.Order", "application/json")  # pyright: ignore[reportArgumentType]
    assert json.loads(js.body)["concept"]["fqn"] == "retail.Order"
    assert js.etag != md.etag


async def test_concept_unknown_not_found() -> None:
    models, om = make()
    with pytest.raises(LakeloomError) as e:
        await concept_artifact(models, om, "retail.Nope", "text/markdown")  # pyright: ignore[reportArgumentType]
    assert e.value.code == "not_found"


@pytest.mark.parametrize("media", ["text/markdown", "application/json", "application/ld+json"])
async def test_asset_context_artifact(media: Any) -> None:
    models, om = make()
    art = await asset_context_artifact(models, om, {}, ORDERS, media)  # pyright: ignore[reportArgumentType]
    assert art.media_type == media
    assert art.last_modified == _dt(GLOSSARY_MS)  # max(table, realized model)
    assert art.links == [f'<http://om:8585/table/{ORDERS}>; rel="via"']
    assert art.etag == etag_for(art.body)


async def test_asset_link_percent_encodes_fqn_segment() -> None:
    models, om = make()
    fqn = 'svc."my db".s/x'
    # table not found, but the link builder is exercised separately
    from lakeloom.publish.artifacts import table_link

    assert (
        table_link("http://om:8585", fqn)
        == '<http://om:8585/table/svc.%22my%20db%22.s%2Fx>; rel="via"'
    )
    del models, om


async def test_last_modified_is_max_of_updated_at_and_load_time() -> None:
    models, _ = make(glossary_ms=1_000_000_000_000)  # 2001: older than the graph load
    art = await model_artifact(models, "retail", "text/turtle", version=None)
    assert art.last_modified == (await models.loaded_at("retail")).replace(microsecond=0)


async def test_models_list_without_updated_at_uses_clock_and_links_glossaries() -> None:
    models, _ = make(glossary_ms=None)
    now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    art = await models_list_artifact(models, clock=lambda: now)
    assert art.last_modified == now
    assert art.links == ['<http://om:8585/glossary>; rel="via"']


async def test_asset_without_any_timestamp_uses_clock() -> None:
    models, om = make()

    async def get_table(fqn: str, **_: object) -> OMTable | None:
        return OMTable.model_validate({"id": ORDERS, "fullyQualifiedName": ORDERS})

    om.get_table_by_fqn = get_table  # type: ignore[method-assign]
    now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    art = await asset_context_artifact(
        models,
        om,  # pyright: ignore[reportArgumentType]
        {},
        ORDERS,
        "text/markdown",
        clock=lambda: now,  # pyright: ignore[reportArgumentType]
    )
    assert art.last_modified == now


async def test_om_url_userinfo_is_stripped_from_links() -> None:
    models, _ = make(om_url="http://user:secret@om:8585/base/")
    art = await model_artifact(models, "retail", "text/turtle", version=None)
    assert art.links == ['<http://om:8585/base/glossary/retail>; rel="via"']
    assert "secret" not in "".join(art.links)


# --- releases and Git links (FR-PUB-02, FR-PUB-07) --------------------------------------------

TAGGED = datetime(2026, 11, 10, 12, 30, 15, 999, tzinfo=UTC)
REL = ReleaseInfo(
    version="1.0.0",
    tag="retail/v1.0.0",
    object_sha="a" * 40,
    commit_sha="b" * 40,
    released_at=TAGGED,
)
WEB = "https://git.example/org/onto"


class FakeReader:
    """The parts of ReleaseReader the artefacts use; records what was read."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, ...]] = []

    async def get(self, slug: str, version: str) -> ReleaseInfo:
        if (slug, version) != ("retail", "1.0.0"):
            raise LakeloomError("not_found", f"no release {version!r} of {slug!r}", "GET ...")
        return REL

    async def ontology(self, slug: str, version: str, fmt: str) -> bytes:
        self.reads.append(("ontology", slug, version, fmt))
        return f"ontology {fmt}\n".encode()

    async def shapes(self, slug: str, version: str, media: str) -> bytes:
        self.reads.append(("shapes", slug, version, media))
        return b"shapes\n"

    async def file(self, slug: str, version: str, name: str) -> bytes:
        self.reads.append(("file", slug, version, name))
        return b'{"@context": {}}\n'


def links(last: str | None = "c" * 40, base: str = WEB) -> GitLinks:
    async def last_commit(slug: str) -> str | None:
        return last if slug == "retail" else None

    return GitLinks(base, "gitea", last_commit)


async def test_release_artifact_validators() -> None:
    models, _ = make()
    reader = FakeReader()
    art = await model_artifact(
        models,
        "retail",
        "text/turtle",
        version="1.0.0",
        releases=reader,
        git=links(),  # type: ignore[arg-type]
    )

    assert art.body == b"ontology turtle\n"
    assert art.etag == etag_for(art.body)
    assert art.last_modified == TAGGED.replace(microsecond=0)  # the tagger date, whole seconds
    assert art.links == [
        f'<{WEB}/src/tag/retail/v1.0.0>; rel="via"',
        f'<{WEB}/commit/{"b" * 40}>; rel="via"',
    ]
    shapes = await shapes_artifact(
        models,
        "retail",
        "application/n-triples",
        version="1.0.0",
        releases=reader,
        git=links(),  # type: ignore[arg-type]
    )
    ctx = await model_context_artifact(
        models,
        "retail",
        version="1.0.0",
        releases=reader,
        git=None,  # type: ignore[arg-type]
    )
    assert (shapes.media_type, shapes.links, shapes.last_modified) == (
        "application/n-triples", art.links, art.last_modified,
    )  # fmt: skip
    assert ctx.media_type == "application/ld+json" and ctx.links == []  # no web base, no links
    assert reader.reads == [
        ("ontology", "retail", "1.0.0", "turtle"),
        ("shapes", "retail", "1.0.0", "application/n-triples"),
        ("file", "retail", "1.0.0", "context.jsonld"),
    ]


async def test_latest_adds_related_commit_link() -> None:
    models, _ = make()
    art = await model_artifact(models, "retail", "text/turtle", version=None, git=links())
    none_yet = await model_artifact(
        models, "retail", "text/turtle", version="latest", git=links(None)
    )
    shapes = await shapes_artifact(models, "retail", "text/turtle", version=None, git=links())

    assert art.links == [
        '<http://om:8585/glossary/retail>; rel="via"',
        f'<{WEB}/commit/{"c" * 40}>; rel="related"; title="last Git publication"',
    ]
    assert none_yet.links == ['<http://om:8585/glossary/retail>; rel="via"']
    assert shapes.links == art.links
    assert art.etag == none_yet.etag  # links never change the body


async def test_latest_link_lookup_failure_only_drops_the_link() -> None:
    async def broken(slug: str) -> str | None:
        raise RuntimeError("state store down")

    models, _ = make()
    art = await model_artifact(
        models, "retail", "text/turtle", version=None, git=GitLinks(WEB, "github", broken)
    )
    assert art.links == ['<http://om:8585/glossary/retail>; rel="via"']


@pytest.mark.parametrize(
    "settings",
    [
        {"git_remote": "https://user@git.example/org/onto.git", "forge": "gitea"},
        {"git_remote": "ssh://git@git.example:2222/org/onto.git", "forge": "gitea"},
        {
            "git_remote": "git@git.example:org/onto.git",
            "git_web_url": "https://u:s3cret@w.example/o",
        },
    ],
)
async def test_links_never_carry_userinfo(settings: dict[str, Any]) -> None:
    base = web_base(Settings(_env_file=None, **settings))  # type: ignore[call-arg]
    assert base is not None
    models, _ = make()
    rel = await model_artifact(
        models,
        "retail",
        "text/turtle",
        version="1.0.0",
        releases=FakeReader(),
        git=links(base=base),  # type: ignore[arg-type]
    )
    latest = await model_artifact(
        models, "retail", "text/turtle", version=None, git=links(base=base)
    )
    text = "".join(rel.links + latest.links)
    assert "s3cret" not in text and "user" not in text and "git@" not in text
    assert all(link.startswith("<https://") for link in rel.links)


async def test_resolve_version() -> None:
    reader = FakeReader()
    assert await resolve_version(reader, "retail", None) is None  # type: ignore[arg-type]
    assert await resolve_version(reader, "retail", "latest") is None  # type: ignore[arg-type]
    assert await resolve_version(reader, "retail", "1.0.0") == REL  # type: ignore[arg-type]
    with pytest.raises(LakeloomError) as e:
        await resolve_version(None, "retail", "9" * 300)
    assert e.value.code == "not_found" and len(e.value.message) < 100
    with pytest.raises(LakeloomError) as e:
        require_latest("1.0.0", "asset context")
    assert e.value.code == "not_found" and "only served at version=latest" in e.value.hint
    require_latest(None, "x")
    require_latest("latest", "x")
