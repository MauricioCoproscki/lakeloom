"""Read-only HTTP API under /v1 (FR-PUB-02, FR-PUB-07, FR-PUB-08), with a fake OM."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from kit import AuthKit, DepsFactory
from rdflib import Graph
from releases_kit import D1, WEB, Remote, git_deps, seeded
from starlette.testclient import TestClient
from test_tools_semantic import COLUMNS, ORDERS, fake_deps

from lakeloom.server.mcp_app import create_server_app

IRI = "http://x/ontology/retail/Order"
ETAG_RE = '"sha256:'
OPENAPI = Path(__file__).parents[3] / "docs" / "api" / "openapi.json"


@pytest.fixture
def deps(make_deps: DepsFactory) -> Any:
    return fake_deps(make_deps)


@pytest.fixture
def client(deps: Any) -> Iterator[TestClient]:
    with TestClient(create_server_app(deps), base_url="https://lakeloom.test") as c:
        yield c


@pytest.fixture
def auth(kit: AuthKit) -> dict[str, str]:
    return {"Authorization": f"Bearer {kit.token()}"}


def get(client: TestClient, auth: dict[str, str], path: str, accept: str | None = None) -> Any:
    headers = dict(auth)
    if accept:
        headers["Accept"] = accept
    return client.get(path, headers=headers)


def assert_validators(r: Any, media: str) -> None:
    assert r.status_code == 200, r.text
    assert r.headers["etag"].startswith(ETAG_RE)
    assert r.headers["last-modified"].endswith("GMT")
    assert 'rel="via"' in r.headers["link"]
    assert r.headers["vary"] == "Accept"
    assert r.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert r.headers["content-type"].split(";")[0] == media


def test_models_list(client: TestClient, auth: dict[str, str]) -> None:
    r = get(client, auth, "/v1/models")

    assert_validators(r, "application/json")
    assert [m["slug"] for m in r.json()["items"]] == ["retail"]


def test_models_list_paging_params(client: TestClient, auth: dict[str, str]) -> None:
    ok = get(client, auth, "/v1/models?limit=1")
    bad_limit = get(client, auth, "/v1/models?limit=0")
    too_big = get(client, auth, "/v1/models?limit=201")
    bad_cursor = get(client, auth, "/v1/models?cursor=!!nope")

    assert ok.status_code == 200 and "next_cursor" in ok.json()
    assert bad_limit.status_code == 400
    assert bad_limit.json()["error"]["code"] == "validation_error"
    assert too_big.status_code == 400
    assert bad_cursor.status_code == 400
    assert bad_cursor.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize(
    ("accept", "media", "charset"),
    [
        (None, "text/turtle", True),
        ("*/*", "text/turtle", True),
        ("text/turtle", "text/turtle", True),
        ("application/ld+json", "application/ld+json", True),
        ("application/rdf+xml", "application/rdf+xml", False),
        ("application/n-triples", "application/n-triples", False),
        ("application/ld+json;q=0.5, text/turtle;q=0.9", "text/turtle", True),
    ],
)
def test_model_negotiation(
    client: TestClient, auth: dict[str, str], accept: str | None, media: str, charset: bool
) -> None:
    r = get(client, auth, "/v1/models/retail", accept)

    assert_validators(r, media)
    if charset:
        assert r.headers["content-type"].endswith("charset=utf-8")


def test_model_turtle_parses(client: TestClient, auth: dict[str, str]) -> None:
    r = get(client, auth, "/v1/models/retail", "text/turtle")

    assert len(Graph().parse(data=r.text, format="turtle")) > 0


def test_unacceptable_accept_406(client: TestClient, auth: dict[str, str]) -> None:
    r = get(client, auth, "/v1/models/retail", "text/html")

    assert r.status_code == 406
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    assert err["message"] == "not acceptable"
    assert "text/turtle" in err["hint"] and "supported:" in err["hint"]


def test_if_none_match_304(client: TestClient, auth: dict[str, str]) -> None:
    first = get(client, auth, "/v1/models/retail")
    etag = first.headers["etag"]

    same = client.get("/v1/models/retail", headers={**auth, "If-None-Match": etag})
    weak = client.get("/v1/models/retail", headers={**auth, "If-None-Match": f"W/{etag}"})
    star = client.get("/v1/models/retail", headers={**auth, "If-None-Match": "*"})
    listed = client.get("/v1/models/retail", headers={**auth, "If-None-Match": f'"x", {etag}'})
    stale = client.get("/v1/models/retail", headers={**auth, "If-None-Match": '"sha256:0"'})

    for r in (same, weak, star, listed):
        assert r.status_code == 304
        assert r.content == b""
        assert r.headers["etag"] == etag
        assert r.headers["last-modified"] == first.headers["last-modified"]
        assert r.headers["link"] == first.headers["link"]
    assert stale.status_code == 200
    assert stale.headers["etag"] == etag


def test_etag_differs_per_media_type(client: TestClient, auth: dict[str, str]) -> None:
    a = get(client, auth, "/v1/models/retail", "text/turtle")
    b = get(client, auth, "/v1/models/retail", "application/n-triples")

    assert a.headers["etag"] != b.headers["etag"]


def test_versions(client: TestClient, auth: dict[str, str]) -> None:
    listing = get(client, auth, "/v1/models/retail/versions")
    latest = get(client, auth, "/v1/models/retail/versions/latest")
    plain = get(client, auth, "/v1/models/retail")
    other = get(client, auth, "/v1/models/retail/versions/1.0.0")

    assert listing.status_code == 200
    (entry,) = listing.json()
    assert entry["version"] == "latest"
    assert "text/turtle" in entry["media_types"]
    assert latest.content == plain.content
    assert latest.headers["etag"] == plain.headers["etag"]
    assert other.status_code == 404
    assert other.json()["error"]["hint"] == "Git publishing is off"


def test_model_context_jsonld(client: TestClient, auth: dict[str, str]) -> None:
    r = get(client, auth, "/v1/models/retail/context.jsonld")
    as_json = get(client, auth, "/v1/models/retail/context.jsonld", "application/json")
    html = get(client, auth, "/v1/models/retail/context.jsonld", "text/html")

    assert_validators(r, "application/ld+json")
    assert "@context" in r.json()
    assert as_json.status_code == 200 and as_json.content == r.content
    assert html.status_code == 406


def test_concept(client: TestClient, auth: dict[str, str]) -> None:
    md = get(client, auth, "/v1/concepts/retail.Order")
    js = get(client, auth, "/v1/concepts/retail.Order", "application/json")
    turtle = get(client, auth, "/v1/concepts/retail.Order", "text/turtle")

    assert_validators(md, "text/markdown")
    assert md.headers["content-type"].endswith("charset=utf-8")
    assert "Order" in md.text
    assert_validators(js, "application/json")
    assert js.json()["concept"]["fqn"] == "retail.Order"
    assert turtle.status_code == 406


def test_asset_context(client: TestClient, auth: dict[str, str]) -> None:
    md = get(client, auth, f"/v1/assets/{ORDERS}/context")
    js = get(client, auth, f"/v1/assets/{ORDERS}/context", "application/json")
    ld = get(client, auth, f"/v1/assets/{ORDERS}/context", "application/ld+json")
    cut = get(client, auth, f"/v1/assets/{ORDERS}/context?max_tokens=120")
    bad = get(client, auth, f"/v1/assets/{ORDERS}/context?max_tokens=0")
    nan = get(client, auth, f"/v1/assets/{ORDERS}/context?max_tokens=abc")

    assert_validators(md, "text/markdown")
    for name, concept in COLUMNS.items():
        assert f"`{name}`" in md.text and f"`{concept}`" in md.text
    assert js.json()["asset"]["table_fqn"] == ORDERS
    assert_validators(ld, "application/ld+json")
    assert "@graph" in ld.json()
    assert len(cut.text) < len(md.text)
    assert cut.headers["etag"] != md.headers["etag"]
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "validation_error"
    assert nan.status_code == 400 and nan.json()["error"]["code"] == "validation_error"


def test_percent_encoded_iri_and_fqn_paths(client: TestClient, auth: dict[str, str]) -> None:
    encoded = "http%3A%2F%2Fx%2Fontology%2Fretail%2FOrder"

    by_iri = get(client, auth, f"/v1/concepts/{encoded}", "application/json")
    by_raw_iri = get(client, auth, f"/v1/concepts/{IRI}", "application/json")
    asset = get(client, auth, f"/v1/assets/{ORDERS.replace('.', '%2E')}/context")

    assert by_iri.status_code == 200 and by_iri.json()["concept"]["fqn"] == "retail.Order"
    assert by_raw_iri.content == by_iri.content
    assert asset.status_code == 200


def test_requires_read_scope_403(client: TestClient, kit: AuthKit) -> None:
    headers = {"Authorization": f"Bearer {kit.token(scope='openid')}"}
    for path in (
        "/v1/models",
        "/v1/models/retail",
        "/v1/models/retail/versions",
        "/v1/models/retail/context.jsonld",
        "/v1/concepts/retail.Order",
        f"/v1/assets/{ORDERS}/context",
    ):
        r = client.get(path, headers=headers)
        assert r.status_code == 403, path
        assert r.json()["error"]["code"] == "forbidden"


def test_no_token_401(client: TestClient) -> None:
    r = client.get("/v1/models")

    assert r.status_code == 401
    assert "www-authenticate" in r.headers


def test_errors_are_json_not_html(deps: Any, client: TestClient, auth: dict[str, str]) -> None:
    unknown = get(client, auth, "/v1/models/nope")
    concept = get(client, auth, "/v1/concepts/does.not.Exist")
    asset = get(client, auth, "/v1/assets/no.such.table/context")
    deps.om.down = True
    down = get(client, auth, "/v1/models/retail")

    for r, status, code in (
        (unknown, 404, "not_found"),
        (concept, 404, "not_found"),
        (asset, 404, "not_found"),
        (down, 503, "upstream_unavailable"),
    ):
        assert r.status_code == status, r.text
        assert r.headers["content-type"].startswith("application/json")
        err = r.json()["error"]
        assert err["code"] == code and err["message"] and err["hint"]
        assert "Traceback" not in r.text and "<html" not in r.text.lower()


def test_unexpected_exception_is_generic_500(deps: Any, kit: AuthKit, auth: dict[str, str]) -> None:
    async def boom(*_: object, **__: object) -> Any:
        raise RuntimeError("secret connection string")

    deps.models.get_model = boom
    with TestClient(create_server_app(deps), base_url="https://lakeloom.test") as c:
        r = c.get("/v1/models/retail", headers=auth)

    assert r.status_code == 500
    assert r.json() == {
        "error": {
            "code": "upstream_unavailable",
            "message": "internal error",
            "hint": "check the server logs",
        }
    }
    assert "secret" not in r.text


SHAPES_TYPES = ("text/turtle", "application/ld+json", "application/n-triples")


def test_shapes_endpoint_negotiates_three_types(client: TestClient, auth: dict[str, str]) -> None:
    default = get(client, auth, "/v1/models/retail/shapes")
    for media in SHAPES_TYPES:
        assert_validators(get(client, auth, "/v1/models/retail/shapes", media), media)
    assert default.headers["content-type"].startswith("text/turtle")
    shapes = Graph().parse(data=default.text, format="turtle")
    assert len(shapes) > 0
    assert get(client, auth, "/v1/models/retail/shapes", "application/rdf+xml").status_code == 406
    nt = get(client, auth, "/v1/models/retail/shapes", "application/n-triples")
    assert len(Graph().parse(data=nt.text, format="nt")) == len(shapes)


def test_shapes_etag_and_link(client: TestClient, auth: dict[str, str]) -> None:
    import hashlib

    r = get(client, auth, "/v1/models/retail/shapes")
    onto = get(client, auth, "/v1/models/retail")

    assert r.headers["etag"] == '"sha256:' + hashlib.sha256(r.content).hexdigest() + '"'
    assert r.headers["link"] == onto.headers["link"] and 'rel="via"' in r.headers["link"]
    again = client.get(
        "/v1/models/retail/shapes", headers={**auth, "If-None-Match": r.headers["etag"]}
    )
    assert again.status_code == 304 and again.content == b""
    assert get(client, auth, "/v1/models/nope/shapes").status_code == 404


async def test_shapes_release_with_git_off_is_not_found(deps: Any) -> None:
    from lakeloom.errors import LakeloomError
    from lakeloom.publish.artifacts import shapes_artifact

    ok = await shapes_artifact(deps.models, "retail", "text/turtle", version="latest")
    assert ok.body
    with pytest.raises(LakeloomError) as exc:
        await shapes_artifact(deps.models, "retail", "text/turtle", version="1.0.0")
    assert exc.value.code == "not_found"
    assert exc.value.hint == "Git publishing is off"


def test_context_endpoint_full_context(client: TestClient, auth: dict[str, str]) -> None:
    ctx = get(client, auth, "/v1/models/retail/context.jsonld").json()["@context"]

    assert ctx["@vocab"] == "https://lakeloom.test/ontology/retail/"
    assert ctx["@version"] == 1.1 and "xsd" in ctx and "skos" in ctx
    assert ctx["Order"] == IRI  # a class term, beyond the minimal


def test_openapi_lists_shapes_route(client: TestClient, auth: dict[str, str]) -> None:
    op = client.get("/openapi.json", headers=auth).json()["paths"]["/v1/models/{slug}/shapes"][
        "get"
    ]

    assert op["summary"]
    assert set(op["responses"]["200"]["content"]) == set(SHAPES_TYPES)


def test_asset_context_jsonld_context_is_pruned(client: TestClient, auth: dict[str, str]) -> None:
    r = get(client, auth, f"/v1/assets/{ORDERS}/context", "application/ld+json")

    assert r.status_code == 200, r.text
    ctx = r.json()["@context"]
    assert "Order" not in ctx  # the payload uses no model term: pruned from the full context
    assert ctx["@version"] == 1.1 and ctx["dcterms"] == "http://purl.org/dc/terms/"
    assert ctx["@vocab"] == "https://lakeloom.test/ontology/retail/"
    assert r.json()["@graph"]


def test_openapi_requires_auth(client: TestClient, auth: dict[str, str]) -> None:
    assert client.get("/openapi.json").status_code == 401
    paths = client.get("/openapi.json", headers=auth).json()["paths"]
    assert "/v1/models/{slug}/versions/{version}" in paths


def test_openapi_up_to_date(deps: Any) -> None:
    """FR-PUB-08: the committed docs/api/openapi.json is the app's."""
    app = create_server_app(deps)

    expected = json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"

    assert OPENAPI.read_text() == expected, "run: uv run python scripts/gen_openapi.py"


# --- released versions (FR-PUB-02, FR-PUB-07) ------------------------------------------------


@pytest.fixture
def remote(tmp_path: Path) -> Remote:
    return seeded(tmp_path)


@pytest.fixture
def git_client(make_deps: DepsFactory, remote: Remote) -> Iterator[TestClient]:
    deps = git_deps(make_deps, remote)
    with TestClient(create_server_app(deps), base_url="https://lakeloom.test") as c:
        yield c


def test_versions_lists_latest_then_releases(
    git_client: TestClient, auth: dict[str, str], remote: Remote
) -> None:
    r = get(git_client, auth, "/v1/models/retail/versions")

    assert r.status_code == 200, r.text
    latest, newer, older = r.json()
    assert latest["version"] == "latest"
    assert (latest["tag"], latest["commit"], latest["released_at"]) == (None, None, None)
    assert "text/turtle" in latest["media_types"]
    assert [e["version"] for e in (newer, older)] == ["1.1.0", "1.0.0"]
    assert newer["tag"] == "retail/v1.1.0"
    assert newer["commit"] == remote.commit_of("retail/v1.1.0")
    assert newer["released_at"] == "2026-11-10T12:30:00Z"
    assert newer["media_types"] == latest["media_types"]
    assert newer["links"] == [
        {"href": f"{WEB}/src/tag/retail/v1.1.0", "rel": "via"},
        {"href": f"{WEB}/commit/{newer['commit']}", "rel": "via"},
    ]
    assert r.headers["etag"].startswith(ETAG_RE)


def test_get_version_serves_tagged_bytes(
    git_client: TestClient, auth: dict[str, str], remote: Remote
) -> None:
    tagged = remote.show("retail/v1.0.0", "ontology.ttl")

    by_path = get(git_client, auth, "/v1/models/retail/versions/1.0.0")
    by_query = get(git_client, auth, "/v1/models/retail?version=1.0.0")
    jsonld = get(git_client, auth, "/v1/models/retail/versions/1.0.0", "application/ld+json")
    nt = get(git_client, auth, "/v1/models/retail/versions/1.0.0", "application/n-triples")

    assert by_path.status_code == 200 and by_path.content == tagged
    assert by_query.content == tagged and by_query.headers["etag"] == by_path.headers["etag"]
    assert jsonld.content == remote.show("retail/v1.0.0", "ontology.jsonld")
    assert len(Graph().parse(data=nt.text, format="nt")) == len(
        Graph().parse(data=tagged, format="turtle")
    )


def test_release_artifact_validators_over_http(
    git_client: TestClient, auth: dict[str, str], remote: Remote
) -> None:
    import hashlib

    r = get(git_client, auth, "/v1/models/retail/versions/1.1.0")
    commit = remote.commit_of("retail/v1.1.0")

    assert r.headers["etag"] == '"sha256:' + hashlib.sha256(r.content).hexdigest() + '"'
    assert r.headers["last-modified"] == "Tue, 10 Nov 2026 12:30:00 GMT"
    assert r.headers["link"] == (
        f'<{WEB}/src/tag/retail/v1.1.0>; rel="via", <{WEB}/commit/{commit}>; rel="via"'
    )


def test_shapes_and_context_by_version(
    git_client: TestClient, auth: dict[str, str], remote: Remote
) -> None:
    shapes = get(git_client, auth, "/v1/models/retail/shapes?version=1.0.0")
    shapes_nt = get(
        git_client, auth, "/v1/models/retail/shapes?version=1.0.0", "application/n-triples"
    )
    context = get(git_client, auth, "/v1/models/retail/context.jsonld?version=1.1.0")

    assert shapes.status_code == 200 and shapes.content == remote.show(
        "retail/v1.0.0", "shapes.ttl"
    )
    assert len(Graph().parse(data=shapes_nt.text, format="nt")) == 2
    assert context.status_code == 200
    assert context.content == remote.show("retail/v1.1.0", "context.jsonld")
    assert context.headers["last-modified"] == "Tue, 10 Nov 2026 12:30:00 GMT"


def test_release_without_shapes_is_not_found(
    make_deps: DepsFactory, tmp_path: Path, auth: dict[str, str]
) -> None:
    remote = Remote(tmp_path)
    remote.release("1.0.0", D1, shapes=False)
    deps = git_deps(make_deps, remote)
    with TestClient(create_server_app(deps), base_url="https://lakeloom.test") as c:
        r = get(c, auth, "/v1/models/retail/shapes?version=1.0.0")
        ok = get(c, auth, "/v1/models/retail/versions/1.0.0")

    assert r.status_code == 404 and "has no shapes" in r.json()["error"]["message"]
    assert ok.status_code == 200


@pytest.mark.parametrize(
    "version", ["9.9.9", "1.0", "01.0.0", "1.0.0-rc1", "v1.0.0", "x" * 300, "..%2Fx", "-x"]
)
def test_unknown_version_404_with_hint(
    git_client: TestClient, auth: dict[str, str], version: str
) -> None:
    in_path = f"/v1/models/retail/versions/{version}"
    if "%2F" in version:  # a slash never reaches the route: the router's own 404
        assert get(git_client, auth, in_path).status_code == 404
    for path in (
        *([] if "%2F" in version else [in_path]),
        f"/v1/models/retail?version={version}",
        f"/v1/models/retail/shapes?version={version}",
        f"/v1/models/retail/context.jsonld?version={version}",
    ):
        r = get(git_client, auth, path)
        assert r.status_code == 404, (path, r.text)
        err = r.json()["error"]
        assert err["code"] == "not_found" and err["message"].startswith("no release")
        assert err["hint"] == "GET /v1/models/retail/versions"
        assert len(err["message"]) < 120


def test_git_off_version_404(client: TestClient, auth: dict[str, str]) -> None:
    for path in (
        "/v1/models/retail/versions/1.0.0",
        "/v1/models/retail?version=1.0.0",
        "/v1/models/retail/shapes?version=1.0.0",
        "/v1/models/retail/context.jsonld?version=1.0.0",
    ):
        r = get(client, auth, path)
        assert r.status_code == 404, path
        err = r.json()["error"]
        assert err["message"] == "no release '1.0.0' of 'retail'"
        assert err["hint"] == "Git publishing is off"
    listing = get(client, auth, "/v1/models/retail/versions").json()
    assert [e["version"] for e in listing] == ["latest"]
    assert get(client, auth, "/v1/models/retail?version=latest").status_code == 200


def test_conditional_get_on_release_304(git_client: TestClient, auth: dict[str, str]) -> None:
    first = get(git_client, auth, "/v1/models/retail/versions/1.0.0")
    again = git_client.get(
        "/v1/models/retail/versions/1.0.0",
        headers={**auth, "If-None-Match": first.headers["etag"]},
    )

    assert again.status_code == 304 and again.content == b""
    assert again.headers["etag"] == first.headers["etag"]
    assert again.headers["last-modified"] == first.headers["last-modified"]
    assert again.headers["link"] == first.headers["link"]


def test_latest_links_om_and_last_publication(
    make_deps: DepsFactory, remote: Remote, auth: dict[str, str]
) -> None:
    import asyncio
    from datetime import UTC, datetime

    from lakeloom.gitstore.layout import Snapshot
    from lakeloom.state.db import upgrade
    from lakeloom.state.publications import Publication

    deps = git_deps(make_deps, remote)
    now = datetime.now(UTC)
    snap = Snapshot(
        model="retail", glossary_id="g", glossary_fqn="retail", model_version="v1",
        exports={}, proposal_id="prop_1", message="m\n",
    )  # fmt: skip

    async def seed() -> None:
        await upgrade(deps.engine)
        pub = await deps.publications.add(
            Publication(
                id="pub_1", repo=deps.publisher.repo_key, model="retail", proposal_id="prop_1",
                mode="direct", created_at=now, updated_at=now, payload=snap,
            )
        )  # fmt: skip
        await deps.publications.update(
            pub.model_copy(update={"status": "done", "commit_sha": "c" * 40, "branch": "main"})
        )

    asyncio.run(seed())
    with TestClient(create_server_app(deps), base_url="https://lakeloom.test") as c:
        r = get(c, auth, "/v1/models/retail")
        listing = get(c, auth, "/v1/models/retail/versions").json()

    assert r.headers["link"] == (
        '<http://om.test:8585/glossary/retail>; rel="via", '
        f'<{WEB}/commit/{"c" * 40}>; rel="related"; title="last Git publication"'
    )
    assert listing[0]["links"][1] == {"href": f"{WEB}/commit/{'c' * 40}", "rel": "related"}
