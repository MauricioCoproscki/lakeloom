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
    deps = make_deps(
        triplestore_enabled=True, triplestore_url="http://ts/data", oidc_token_exchange=False
    )

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
        "import": True,
        "drift": True,
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


async def test_server_info_import_feature(make_deps: DepsFactory) -> None:
    off = await build_server_info(make_deps(), principal())
    on = await build_server_info(
        make_deps(git_remote="https://git.example.test/acme/onto.git", git_mode="direct"),
        principal(),
    )

    # import_model needs nothing optional; import_from_git reports Git off by itself
    assert off.features["import"] is True and on.features["import"] is True
    assert "import" in summarize(off)
    assert off.features["drift"] is True and "drift" in summarize(off)


async def test_server_info_triplestore_block(make_deps: DepsFactory) -> None:
    from datetime import UTC, datetime

    from lakeloom.state.db import upgrade
    from lakeloom.state.releases import Release

    password = "ts-password-4711"  # noqa: S105
    url = "http://triples.internal:3030/lakeloom/data"
    deps = make_deps(
        git_remote="https://git.example.test/acme/onto.git",
        git_mode="direct",
        forge="none",
        triplestore_enabled=True,
        triplestore_url=url,
        triplestore_username="admin",
        triplestore_password=SecretStr(password),
    )
    await upgrade(deps.engine)
    assert deps.sink_worker is not None
    now = datetime(2026, 11, 15, tzinfo=UTC)
    for n, (status, error) in enumerate(
        [("failed", "triple store answered HTTP 401"), ("done", None)]
    ):
        await deps.releases_repo.add(
            Release(
                id=f"rel_{n}",
                repo=deps.sink_worker.repo_key,
                model="shop",
                version=f"1.{n}.0",
                tag=f"shop/v1.{n}.0",
                commit_sha="b" * 40,
                released_by="alice",
                created_at=now,
                sink_status=status,  # type: ignore[arg-type]
                sink_attempts=1,
                sink_last_error=error,
            )
        )

    info = await build_server_info(deps, principal())

    assert info.triplestore == {
        "enabled": True,
        "pending": 0,
        "failed": 1,
        "done": 1,
        "last_error": "triple store answered HTTP 401",
    }
    assert info.features["triplestore"] is True
    dumped = info.model_dump_json() + summarize(info)
    assert password not in dumped and "triples.internal" not in dumped
    assert "1 failed" in summarize(info)


async def test_server_info_triplestore_off(make_deps: DepsFactory) -> None:
    info = await build_server_info(make_deps(), None)
    assert info.triplestore is None and "Triple store" not in summarize(info)


async def test_release_hook_runs_the_attempt_in_the_background(make_deps: DepsFactory) -> None:
    import asyncio
    from datetime import UTC, datetime

    from lakeloom.state.releases import Release

    deps = make_deps(
        git_remote="https://git.example.test/acme/onto.git",
        git_mode="direct",
        forge="none",
        triplestore_enabled=True,
        triplestore_url="http://ts/data",
    )
    assert deps.sink_worker is not None and deps.release_service is not None
    release_gate = asyncio.Event()
    seen: list[str] = []

    async def slow_attempt(release_id: str) -> None:
        await release_gate.wait()
        seen.append(release_id)

    deps.sink_worker.attempt = slow_attempt  # type: ignore[method-assign]
    rel = Release(
        id="rel_1", repo="r", model="shop", version="1.0.0", tag="shop/v1.0.0",
        commit_sha="b" * 40, released_by="a", created_at=datetime.now(UTC), sink_status="pending",
    )  # fmt: skip
    hook = deps.release_service._on_released
    assert hook is not None
    await asyncio.wait_for(hook(rel), 1)  # returns although the attempt is still blocked
    assert seen == [] and len(deps.sink_tasks) == 1
    release_gate.set()
    await asyncio.gather(*deps.sink_tasks)
    assert seen == ["rel_1"] and not deps.sink_tasks
