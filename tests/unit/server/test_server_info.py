"""``build_server_info`` (FR-OM-06, NFR-SEC-11)."""

from kit import OM_BOT_TOKEN, DepsFactory, om_down
from pydantic import SecretStr

import lakeloom
from lakeloom.server.auth import Principal, PrincipalType
from lakeloom.server.server_info import ServerInfo, build_server_info, summarize

CALLER_TOKEN = "caller-access-token-abc123"  # noqa: S105


def principal(scopes: frozenset[str] = frozenset({"lakeloom:read"})) -> Principal:
    return Principal(
        subject="svc-123",
        type=PrincipalType.AGENT,
        client_id="lakeloom-agent",
        username=None,
        scopes=scopes,
        groups=frozenset(),
        token=SecretStr(CALLER_TOKEN),
    )


async def test_fr_om_06_server_info_reports_om_version_and_rdf(make_deps: DepsFactory) -> None:
    deps = make_deps(triplestore_enabled=True, oidc_token_exchange=False)

    info = await build_server_info(deps, principal())

    assert isinstance(info, ServerInfo)
    assert info.name == "lakeloom"
    assert info.version == lakeloom.__version__
    assert info.environment == "development"
    assert info.openmetadata.reachable is True
    assert info.openmetadata.version == "2.1.0"
    assert info.openmetadata.rdf_enabled is True
    assert info.features == {
        "triplestore": True,
        "stdio": True,
        "token_exchange": False,
        "releases": False,
    }
    assert info.sources == []
    assert info.models == []
    assert info.caller is not None
    assert info.caller.subject == "svc-123"
    assert info.caller.type == "agent"
    assert info.caller.scopes == ["lakeloom:read"]


async def test_fr_om_06_server_info_when_om_down(make_deps: DepsFactory) -> None:
    deps = make_deps(om_down)

    info = await build_server_info(deps, None)

    assert info.openmetadata.reachable is False
    assert info.openmetadata.error
    assert info.caller is None


async def test_nfr_sec_11_server_info_never_contains_token(make_deps: DepsFactory) -> None:
    deps = make_deps()

    info = await build_server_info(deps, principal())
    dumped = info.model_dump_json()

    assert CALLER_TOKEN not in dumped
    assert OM_BOT_TOKEN not in dumped
    assert info.caller is not None
    assert "token" not in info.caller.model_dump()


async def test_server_info_reports_review_identity_mode(make_deps: DepsFactory) -> None:
    unavailable = await build_server_info(make_deps(), principal())
    assert unavailable.review_identity == "unavailable"

    forward = await build_server_info(
        make_deps(oidc_token_exchange=False, om_forward_user_token=True), principal()
    )
    assert forward.review_identity == "forward"

    exchange = await build_server_info(
        make_deps(oidc_client_id="lakeloom", oidc_client_secret=SecretStr("x")), principal()
    )
    assert exchange.review_identity == "exchange"


async def test_server_info_git_status_has_no_remote_url(make_deps: DepsFactory) -> None:
    deps = make_deps(
        git_remote="https://bot-remote-tok-981@git.example.test/acme/onto.git",
        git_mode="direct",
        forge="none",
    )
    from lakeloom.state.db import upgrade

    await upgrade(deps.engine)

    info = await build_server_info(deps, principal())

    assert info.git is not None
    assert info.git["enabled"] is True and info.git["mode"] == "direct"
    assert (info.git["pending"], info.git["blocked"], info.git["last_error"]) == (0, 0, None)
    dumped = info.model_dump_json()
    assert "git.example.test" not in dumped and "remote-tok-981" not in dumped
    assert "Git" in summarize(info)


async def test_server_info_git_off(make_deps: DepsFactory) -> None:
    info = await build_server_info(make_deps(), None)
    assert info.git is not None and info.git["enabled"] is False


async def test_server_info_reports_releases_feature(make_deps: DepsFactory) -> None:
    off = await build_server_info(make_deps(), principal())
    on = await build_server_info(
        make_deps(git_remote="https://git.example.test/acme/onto.git", git_mode="direct"),
        principal(),
    )

    assert off.features["releases"] is False
    assert on.features["releases"] is True
    assert "releases" in summarize(on) and "releases" not in summarize(off)
