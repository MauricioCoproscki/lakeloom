"""HTTP app: health, readiness, metrics, request ids, auth wiring (NFR-OPS-01..03, NFR-SEC-11)."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from lakeloom.config import ConfigError, Settings
from lakeloom.om.client import OMClient
from lakeloom.server.auth import TokenValidator
from lakeloom.server.health import Check, CheckResult
from lakeloom.server.http_app import AppDeps, build_deps, create_app
from lakeloom.state.db import create_engine, upgrade
from lakeloom.state.leases import LeaseRepository

BOT_TOKEN = "super-secret-bot-token-xyz"  # noqa: S105


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        om_url="http://om.test:8585",  # pyright: ignore[reportArgumentType]
        om_bot_token=SecretStr(BOT_TOKEN),
        oidc_issuer="https://idp.test/realms/lakeloom",
        public_url="https://lakeloom.test",  # pyright: ignore[reportArgumentType]
        db_url=f"sqlite:///{tmp_path}/l.db",
        data_dir=tmp_path,
    )


@pytest.fixture
async def deps(tmp_path: Path) -> AsyncIterator[AppDeps]:
    settings = _settings(tmp_path)
    engine: AsyncEngine = create_engine(settings.db_url)
    validator = TokenValidator(
        issuer="https://idp.test/realms/lakeloom",
        audience="lakeloom",
        http=httpx.AsyncClient(),
        jwks_url="https://idp.test/jwks",
    )
    om = OMClient("http://om.test:8585", settings.om_bot_token)
    yield AppDeps(settings=settings, om=om, engine=engine, validator=validator)
    await om.aclose()
    await engine.dispose()


def _check(name: str, ok: bool, detail: str | None = None) -> Check:
    async def check() -> CheckResult:
        return CheckResult(name, ok, detail)

    return check


def _client(deps: AppDeps, checks: Sequence[Check] | None = None) -> httpx.AsyncClient:
    app = create_app(deps, checks=checks)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_nfr_ops_01_healthz_no_auth(deps: AppDeps) -> None:
    async with _client(deps) as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_nfr_ops_01_readyz_all_green(deps: AppDeps) -> None:
    checks = [_check("openmetadata", True), _check("state_store", True)]
    async with _client(deps, checks) as c:
        r = await c.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert [x["name"] for x in body["checks"]] == ["openmetadata", "state_store"]


async def test_nfr_ops_01_readyz_reports_failing_check(deps: AppDeps) -> None:
    checks = [_check("openmetadata", False, "down"), _check("state_store", True)]
    async with _client(deps, checks) as c:
        r = await c.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "fail"
    failing = [x for x in body["checks"] if not x["ok"]]
    assert failing[0]["name"] == "openmetadata"
    assert failing[0]["ok"] is False


async def test_nfr_ops_01_default_readyz_check_names(deps: AppDeps) -> None:
    async with _client(deps) as c:
        r = await c.get("/readyz")
    assert [x["name"] for x in r.json()["checks"]] == [
        "openmetadata",
        "state_store",
        "git_workspace",
    ]


async def test_nfr_ops_02_metrics_exposes_request_counter(deps: AppDeps) -> None:
    async with _client(deps) as c:
        await c.get("/healthz")
        r = await c.get("/metrics")
    assert r.status_code == 200
    assert 'lakeloom_http_requests_total{method="GET",route="/healthz",status="200"}' in r.text
    assert "lakeloom_om_request_seconds" in r.text
    assert "lakeloom_tool_calls_total" in r.text


async def test_nfr_ops_02_unmatched_route_label(deps: AppDeps) -> None:
    async with _client(deps) as c:
        await c.get("/healthz/../nope/12345", headers={"Authorization": "Bearer x"})
        r = await c.get("/metrics")
    assert "12345" not in r.text


async def test_nfr_ops_03_request_id_echoed(deps: AppDeps) -> None:
    async with _client(deps) as c:
        r = await c.get("/healthz", headers={"X-Request-ID": "abc-1"})
        r2 = await c.get("/healthz")
    assert r.headers["x-request-id"] == "abc-1"
    assert len(r2.headers["x-request-id"]) == 36


async def test_protected_route_requires_token(deps: AppDeps) -> None:
    async with _client(deps) as c:
        r = await c.get("/something")
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]
    assert (
        "https://lakeloom.test/.well-known/oauth-protected-resource"
        in (r.headers["www-authenticate"])
    )


async def test_prm_document_is_public(deps: AppDeps) -> None:
    async with _client(deps) as c:
        r = await c.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    assert r.json()["resource"] == "https://lakeloom.test"


async def test_openapi_json_is_protected_no_cors(deps: AppDeps) -> None:
    async with _client(deps) as c:
        r = await c.get("/healthz", headers={"Origin": "https://evil.test"})
    assert "access-control-allow-origin" not in r.headers


async def test_extra_mounts_are_served_behind_auth(deps: AppDeps) -> None:
    async def sub(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"hi"})

    app = create_app(deps, extra_mounts=[("/sub", sub)])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/sub/x")
    assert r.status_code == 401


async def test_nfr_sec_11_bot_token_not_in_logs(
    deps: AppDeps, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    async with _client(deps) as c:
        await c.get("/readyz")
        await c.get("/healthz", headers={"Authorization": "Bearer sekrit-bearer"})
        await c.get("/x", headers={"Authorization": "Bearer sekrit-bearer"})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert BOT_TOKEN not in text
    assert "sekrit-bearer" not in text


def test_build_deps_requires_config() -> None:
    with pytest.raises(ConfigError) as exc:
        build_deps(Settings())
    msg = str(exc.value)
    for var in ("LAKELOOM_OM_URL", "LAKELOOM_OIDC_ISSUER", "LAKELOOM_PUBLIC_URL"):
        assert var in msg


async def test_build_deps_and_lifespan_close(tmp_path: Path) -> None:
    deps = build_deps(_settings(tmp_path))
    app = create_app(deps, checks=[_check("openmetadata", True)])
    async with app.router.lifespan_context(app):
        pass
    assert deps.validator is not None


def test_fr_src_01_build_deps_loads_sources(tmp_path: Path) -> None:
    lake = Path(__file__).parents[2] / "fixtures" / "lake"
    config = tmp_path / "sources.yaml"
    config.write_text(
        "sources:\n  - type: delta\n    name: lake\n    tables:\n"
        f"      - {{uri: {(lake / 'orders').as_uri()}, schema: sales}}\n"
    )
    settings = _settings(tmp_path).model_copy(update={"config_file": config})

    deps = build_deps(settings)

    assert list(deps.sources) == ["lake"]
    assert deps.sources["lake"].kind == "delta"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "does not exist"),
        ("sources:\n  - type: delta\n    name: x\n", "invalid sources config"),
    ],
)
def test_fr_src_01_build_deps_rejects_bad_sources_config(
    tmp_path: Path, content: str | None, message: str
) -> None:
    config = tmp_path / "sources.yaml"
    if content is not None:
        config.write_text(content)
    settings = _settings(tmp_path).model_copy(update={"config_file": config})

    with pytest.raises(ConfigError) as exc:
        build_deps(settings)

    assert message in str(exc.value)


async def test_lifespans_run_inside_app_lifespan(deps: AppDeps) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def hook() -> AsyncIterator[None]:
        events.append("enter")
        yield
        events.append("exit")

    app = create_app(deps, lifespans=[hook])
    assert events == []
    async with app.router.lifespan_context(app):
        assert events == ["enter"]
    assert events == ["enter", "exit"]


def test_no_web_ui_docs_routes(deps: AppDeps) -> None:
    # Spec §0.7: no web UI. /openapi.json stays (FR-PUB-08 groundwork), behind auth.
    paths = {getattr(r, "path", None) for r in create_app(deps).routes}
    assert "/openapi.json" in paths
    assert not paths & {"/docs", "/docs/oauth2-redirect", "/redoc"}


def _state_store(body: dict[str, object]) -> dict[str, object]:
    checks = body["checks"]
    assert isinstance(checks, list)
    return next(c for c in checks if c["name"] == "state_store")


async def test_upgrade_retries_when_state_store_unreachable(deps: AppDeps) -> None:
    from sqlalchemy.exc import OperationalError

    calls = 0

    async def flaky(_: AsyncEngine) -> None:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise OperationalError("SELECT 1", {}, OSError("postgres://u:secret@h/db down"))

    app = create_app(deps, upgrade_fn=flaky, upgrade_backoff=(0.01, 0.02))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            r = await c.get("/readyz")
            assert r.status_code == 503
            assert _state_store(r.json())["detail"] == "schema upgrade pending"
            for _ in range(200):
                await asyncio.sleep(0.01)
                if calls >= 3:
                    break
            await asyncio.sleep(0.02)
            r = await c.get("/readyz")
            assert _state_store(r.json())["ok"] is True
    assert calls == 3


async def test_nfr_sec_11_upgrade_non_connection_error_stops_startup_sanitized(
    deps: AppDeps, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(_: AsyncEngine) -> None:
        raise RuntimeError("postgresql://u:secret@h/db")

    caplog.set_level(logging.DEBUG)
    app = create_app(deps, upgrade_fn=broken)
    with pytest.raises(RuntimeError, match="state store migration failed") as info:
        async with app.router.lifespan_context(app):
            pass
    assert "secret" not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__
    assert "secret" not in "\n".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in str(info.value)


async def test_unreachable_db_app_starts_and_cancels_retry(tmp_path: Path) -> None:
    settings = _settings(tmp_path).model_copy(
        update={"db_url": "postgresql://u:p@127.0.0.1:1/none"}
    )
    deps = build_deps(settings)
    app = create_app(deps, upgrade_backoff=(0.01, 0.02))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            r = await c.get("/readyz")
    assert r.status_code == 503
    assert _state_store(r.json())["detail"] == "schema upgrade pending"


# --- Git publishing wiring (FR-GIT-05, NFR-OPS-04) -------------------------------------------


def _git_settings(tmp_path: Path, **kw: object) -> Settings:
    values: dict[str, object] = {
        "git_remote": f"file://{tmp_path}/remote.git",
        "reconcile_interval_seconds": 0,  # the reconcile job has tests of its own
        **kw,
    }
    return _settings(tmp_path).model_copy(update=values)


@pytest.mark.parametrize(
    ("kw", "policy", "message"),
    [
        ({"git_mode": "pull_request", "forge": "none"}, None, "LAKELOOM_FORGE"),
        ({"git_mode": "direct", "forge": "none"}, "pull_request", "LAKELOOM_FORGE"),
        ({"git_mode": "pull_request", "forge": "gitea"}, None, "owner/repo"),
    ],
)
def test_pull_request_with_forge_none_is_config_error(
    tmp_path: Path, kw: dict[str, object], policy: str | None, message: str
) -> None:
    settings = _git_settings(tmp_path, **kw)
    if policy is not None:
        config = tmp_path / "lakeloom.yaml"
        config.write_text(f"models:\n  - name: shop\n    git: {{mode: {policy}}}\n")
        settings = settings.model_copy(update={"config_file": config})
    with pytest.raises(ConfigError) as exc:
        build_deps(settings)
    assert message in str(exc.value)
    assert "remote.git" not in str(exc.value) or message == "owner/repo"


def test_git_settings_that_need_no_forge_are_accepted(tmp_path: Path) -> None:
    direct = build_deps(_git_settings(tmp_path, git_mode="direct", forge="none"))
    assert direct.publisher.enabled
    off = build_deps(_settings(tmp_path).model_copy(update={"forge": "none"}))
    assert not off.publisher.enabled  # no remote: pull_request + none is not checked
    gitea = build_deps(
        _settings(tmp_path).model_copy(
            update={"git_remote": "https://git.example.test/acme/onto.git", "forge": "gitea"}
        )
    )
    assert gitea.publisher.enabled and gitea.git_http is not None


class _CountingPublisher:
    enabled = True

    def __init__(self, check: Callable[[], Awaitable[None]] | None = None) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.check = check

    async def process(self) -> list[object]:
        if self.check is not None:
            await self.check()
        self.calls += 1
        self.started.set()
        return []


def _job_tasks() -> list["asyncio.Task[object]"]:
    return [t for t in asyncio.all_tasks() if t.get_name().startswith("lakeloom-job:")]


async def test_lifespan_worker_cancelled_cleanly(tmp_path: Path) -> None:
    deps = build_deps(_git_settings(tmp_path, git_mode="direct", forge="none"))
    fake = _CountingPublisher()
    key = deps.publisher.repo_key
    deps.publisher = fake  # pyright: ignore[reportAttributeAccessIssue]
    app = create_app(deps, checks=[_check("openmetadata", True)])
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(fake.started.wait(), 2)
        workers = _job_tasks()
        assert [t.get_name() for t in workers] == [f"lakeloom-job:git-publisher:{key}"]
        assert deps.jobs.status()[f"git-publisher:{key}"].held
    assert workers[0].done()
    assert fake.calls == 1  # sleeping between runs when shut down
    assert deps.git_http is not None and deps.git_http.is_closed


async def test_git_worker_runs_under_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _git_settings(tmp_path, git_mode="direct", forge="none")
    deps = build_deps(settings)
    name = f"git-publisher:{deps.publisher.repo_key}"
    engine = create_engine(settings.db_url)
    await upgrade(engine)
    other = LeaseRepository(engine)
    assert await other.acquire(name, "other-replica", timedelta(hours=1))

    attempted = asyncio.Event()
    real_acquire = LeaseRepository.acquire

    async def spy(self: LeaseRepository, *args: Any, **kw: Any) -> bool:
        try:
            return await real_acquire(self, *args, **kw)
        finally:
            attempted.set()

    monkeypatch.setattr(LeaseRepository, "acquire", spy)

    # Held by another replica: this one never runs the outbox.
    blocked = _CountingPublisher()
    deps.publisher = blocked  # pyright: ignore[reportAttributeAccessIssue]
    app = create_app(deps, checks=[_check("openmetadata", True)])
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(attempted.wait(), 2)
        await asyncio.sleep(0.05)
        assert blocked.calls == 0
        assert not deps.jobs.status()[name].held
    assert await other.holder_of(name) == "other-replica"  # stop never releases a foreign lease

    # Free: process runs, and only while this replica holds the lease.
    await other.release(name, "other-replica")
    deps = build_deps(settings)

    async def holds() -> None:
        assert await other.holder_of(name) == deps.jobs.holder

    running = _CountingPublisher(holds)
    deps.publisher = running  # pyright: ignore[reportAttributeAccessIssue]
    app = create_app(deps, checks=[_check("openmetadata", True)])
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(running.started.wait(), 2)
    assert running.calls == 1
    assert await other.holder_of(name) is None  # released on shutdown
    await engine.dispose()


def test_triplestore_sink_is_its_own_job(tmp_path: Path) -> None:
    deps = build_deps(
        _git_settings(
            tmp_path,
            git_mode="direct",
            forge="none",
            triplestore_enabled=True,
            triplestore_url="http://fuseki.test:3030/l/data",
        )
    )
    assert deps.sink_worker is not None
    assert set(deps.jobs.status()) == {
        f"git-publisher:{deps.publisher.repo_key}",
        "triplestore-sink",
    }


async def test_lifespan_without_git_starts_no_worker(deps: AppDeps) -> None:
    app = create_app(deps, checks=[_check("openmetadata", True)])
    async with app.router.lifespan_context(app):
        assert not _job_tasks()
        assert deps.jobs.status() == {}


# --- scheduled reconciliation and drift (FR-GIT-08, FR-PRO-07) --------------------------------


def test_reconcile_job_off_without_git(tmp_path: Path) -> None:
    off = build_deps(_settings(tmp_path))  # the default interval (3600 s), no Git
    assert off.settings.reconcile_interval_seconds == 3600
    assert off.reconcile_service is None and "reconcile" not in off.jobs.status()

    on = build_deps(
        _git_settings(tmp_path, git_mode="direct", forge="none", reconcile_interval_seconds=3600)
    )
    assert on.reconcile_service is not None and "reconcile" in on.jobs.status()

    manual = build_deps(_git_settings(tmp_path, git_mode="direct", forge="none"))  # interval 0
    assert manual.reconcile_service is not None and "reconcile" not in manual.jobs.status()


async def test_reconcile_job_commit_flag(tmp_path: Path) -> None:
    calls: list[bool] = []

    class FakeReconcile:
        async def run(self, *, commit: bool) -> None:
            calls.append(commit)

    for flag in (False, True):
        deps = build_deps(
            _git_settings(
                tmp_path,
                git_mode="direct",
                forge="none",
                reconcile_interval_seconds=60,
                reconcile_commit=flag,
            )
        )
        deps.reconcile_service = FakeReconcile()  # pyright: ignore[reportAttributeAccessIssue]
        await deps._reconcile_tick()  # pyright: ignore[reportPrivateUsage]
    assert calls == [False, True]
    assert deps._reconcile_interval() == 60  # pyright: ignore[reportPrivateUsage]


def test_drift_job_off_by_default(tmp_path: Path) -> None:
    off = build_deps(_settings(tmp_path))
    assert off.settings.drift_interval_seconds == 0
    assert "drift-schedule" not in off.jobs.status()

    on = build_deps(_settings(tmp_path).model_copy(update={"drift_interval_seconds": 300}))
    assert "drift-schedule" in on.jobs.status()  # no Git needed
    assert on._drift_interval() == 300  # pyright: ignore[reportPrivateUsage]


async def test_drift_tick_runs_the_schedule(tmp_path: Path) -> None:
    deps = build_deps(_settings(tmp_path).model_copy(update={"drift_interval_seconds": 300}))
    runs: list[str] = []

    class FakeSchedule:
        async def run(self) -> None:
            runs.append("run")

    deps.drift_schedule = FakeSchedule()  # pyright: ignore[reportAttributeAccessIssue]
    await deps._drift_tick()  # pyright: ignore[reportPrivateUsage]
    assert runs == ["run"]
