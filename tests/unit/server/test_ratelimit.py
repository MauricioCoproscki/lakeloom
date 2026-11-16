"""Rate limit per principal on MCP tools and the /v1 API (NFR-SEC-16, FR-AGT-05)."""

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from kit import AuthKit, Clock, DepsFactory, mcp_session
from starlette.testclient import TestClient
from test_tools_semantic import fake_deps

from lakeloom.core.callers import Actor
from lakeloom.metrics import REGISTRY
from lakeloom.server.mcp_app import create_server_app
from lakeloom.server.ratelimit import IDLE_SECONDS, RateLimiter, principal_key

KEY = ("agent", "a")


def test_burst_then_limited() -> None:
    limiter = RateLimiter(60, 5, clock=Clock())

    allowed = [limiter.check(KEY) for _ in range(5)]
    sixth = limiter.check(KEY)

    assert allowed == [None] * 5
    assert sixth is not None
    assert sixth >= 1


def test_refill_over_time() -> None:
    clock = Clock()
    limiter = RateLimiter(60, 2, clock=clock)  # one token per second
    assert [limiter.check(KEY) for _ in range(2)] == [None, None]
    assert limiter.check(KEY) is not None

    clock.advance(1.0)

    assert limiter.check(KEY) is None
    assert limiter.check(KEY) is not None
    clock.advance(100.0)  # refill is capped at the burst
    assert [limiter.check(KEY) for _ in range(3)] == [None, None, 1]


def test_principals_do_not_share_buckets() -> None:
    limiter = RateLimiter(60, 1, clock=Clock())
    agent = principal_key(Actor(subject="same", type="agent"))
    human = principal_key(Actor(subject="same", type="human"))

    assert limiter.check(agent) is None
    assert limiter.check(agent) is not None
    assert limiter.check(human) is None
    assert limiter.check(("agent", "other")) is None


def test_zero_disables() -> None:
    limiter = RateLimiter(0, 1, clock=Clock())

    assert not limiter.enabled
    assert all(limiter.check(KEY) is None for _ in range(1000))
    assert limiter.bucket_count == 0


def test_idle_buckets_dropped() -> None:
    clock = Clock()
    limiter = RateLimiter(60, 5, clock=clock)
    for n in range(10):
        limiter.check(("agent", f"s{n}"))
    assert limiter.bucket_count == 10

    clock.advance(IDLE_SECONDS + 61)
    limiter.check(("agent", "fresh"))

    assert limiter.bucket_count == 1


def test_retry_after_is_whole_seconds_at_least_one() -> None:
    fast = RateLimiter(6000, 1, clock=Clock())  # 100 tokens/s: the wait is a fraction
    slow = RateLimiter(1, 1, clock=Clock())  # one token a minute
    fast.check(KEY)
    slow.check(KEY)

    fast_wait, slow_wait = fast.check(KEY), slow.check(KEY)

    assert fast_wait == 1
    assert slow_wait == 60
    assert isinstance(slow_wait, int)


def limited(surface: str) -> float:
    return REGISTRY.get_sample_value("lakeloom_rate_limited_total", {"surface": surface}) or 0.0


async def test_mcp_tool_rate_limited(make_deps: DepsFactory, kit: AuthKit) -> None:
    app = create_server_app(fake_deps(make_deps, rate_limit_burst=2, rate_limit_per_minute=60))
    before = limited("mcp")
    async with mcp_session(app, kit.token()) as session:
        first = await session.call_tool("list_models", {})
        second = await session.call_tool("list_models", {})
        third = await session.call_tool("list_models", {})

    assert not first.is_error
    assert not second.is_error
    assert third.is_error
    error = (third.structured_content or {})["error"]
    assert error["code"] == "rate_limited"
    assert error["message"] == "rate limit exceeded for this principal"
    assert error["hint"].startswith("retry after ")
    assert limited("mcp") == before + 1


async def test_tools_list_not_limited(make_deps: DepsFactory, kit: AuthKit) -> None:
    app = create_server_app(fake_deps(make_deps, rate_limit_burst=1, rate_limit_per_minute=1))
    async with mcp_session(app, kit.token()) as session:
        for _ in range(5):
            await session.list_tools()
            await session.list_prompts()
        ok = await session.call_tool("list_models", {})
        limited_call = await session.call_tool("list_models", {})

    assert not ok.is_error
    assert limited_call.is_error


@pytest.fixture
def client(make_deps: DepsFactory) -> Iterator[TestClient]:
    deps = fake_deps(make_deps, rate_limit_burst=2, rate_limit_per_minute=60)
    with TestClient(create_server_app(deps), base_url="https://lakeloom.test") as c:
        yield c


def test_http_v1_rate_limited(client: TestClient, kit: AuthKit) -> None:
    auth = {"Authorization": f"Bearer {kit.token()}"}
    before = limited("http")

    statuses = [client.get("/v1/models", headers=auth).status_code for _ in range(2)]
    r = client.get("/v1/models", headers=auth)
    other = client.get(
        "/v1/models", headers={"Authorization": f"Bearer {kit.token(sub='someone-else')}"}
    )

    assert statuses == [200, 200]
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1
    assert r.json() == {
        "error": {
            "code": "rate_limited",
            "message": "rate limit exceeded for this principal",
            "hint": f"retry after {r.headers['retry-after']} s",
        }
    }
    assert other.status_code == 200
    assert limited("http") == before + 1


def test_health_and_metrics_not_limited(client: TestClient, kit: AuthKit) -> None:
    auth = {"Authorization": f"Bearer {kit.token()}"}
    for _ in range(3):
        client.get("/v1/models", headers=auth)

    codes = [client.get(path).status_code for path in ("/healthz", "/metrics") * 5]
    prm = client.get("/.well-known/oauth-protected-resource")

    assert set(codes) == {200}
    assert prm.status_code == 200


def test_idp_traceback_logged_at_most_once_a_minute(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lakeloom.server.auth as auth

    now = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
    mw = auth.BearerAuthMiddleware(
        None,  # pyright: ignore[reportArgumentType]
        validator=None,  # pyright: ignore[reportArgumentType]
        resource_metadata_url="https://x/prm",
    )
    caplog.set_level(logging.WARNING, logger="lakeloom.server.auth")

    def log() -> Any:
        try:
            raise auth.IdPUnavailable("down")
        except auth.IdPUnavailable:
            mw._log_unavailable()  # pyright: ignore[reportPrivateUsage]
        return caplog.records[-1]

    first = log()
    now[0] += 10
    second = log()
    now[0] += 60
    third = log()

    assert first.exc_info is not None
    assert not second.exc_info
    assert second.getMessage() == "identity provider unavailable"
    assert third.exc_info is not None
