"""The optional Graph Store sink and its worker (FR-TS-01, FR-TS-02, FR-TS-03, NFR-SEC-11)."""

import base64
import hashlib
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from lakeloom.config import ConfigError, Settings
from lakeloom.core.model_policy import ModelPolicyEntry
from lakeloom.core.models import ModelInfo
from lakeloom.errors import LakeloomError
from lakeloom.gitstore.publisher import backoff_seconds
from lakeloom.gitstore.release_reader import ReleaseInfo
from lakeloom.publish.triplestore import (
    MAX_SINK_ATTEMPTS,
    GraphStoreSink,
    SinkError,
    SinkWorker,
)
from lakeloom.state.db import create_engine, upgrade
from lakeloom.state.releases import Release, ReleaseRepository

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
BASE = "http://x/ontology/shop/"
URL = "http://fuseki:3030/lakeloom/data"
PASSWORD = "s3cret-pw"  # noqa: S105
REMOTE = "file:///tmp/remote.git"
REPO = hashlib.sha256(REMOTE.encode()).hexdigest()


def sink_for(
    handler: Any, *, user: str | None = "admin", password: str | None = PASSWORD
) -> GraphStoreSink:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GraphStoreSink(http, URL, user, SecretStr(password) if password else None)


class Recorder:
    def __init__(self, status: int = 204, text: str = "") -> None:
        self.requests: list[httpx.Request] = []
        self.status = status
        self.text = text

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, text=self.text)

    @property
    def graphs(self) -> list[str]:
        return [r.url.params["graph"] for r in self.requests]


async def test_put_release_latest_and_shapes_graph_iris() -> None:
    rec = Recorder()
    sink = sink_for(rec)

    await sink.replace_graph(f"{BASE}release/1.0.0", b"<a> <b> <c> .")

    (req,) = rec.requests
    assert req.method == "PUT"
    assert str(req.url).split("?")[0] == URL
    assert req.url.params["graph"] == f"{BASE}release/1.0.0"
    assert req.headers["content-type"] == "text/turtle"
    assert req.content == b"<a> <b> <c> ."
    assert (
        req.headers["authorization"]
        == "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()
    )


async def test_no_username_means_no_auth() -> None:
    rec = Recorder()
    await sink_for(rec, user=None, password=None).replace_graph("g", b"x")
    assert "authorization" not in rec.requests[0].headers


# --- the worker ---------------------------------------------------------------------------


class FakeReader:
    def __init__(self, versions: list[str], shapes: bool = True, skos: bool = True) -> None:
        self.versions = versions  # highest first
        self.shapes = shapes
        self.skos = skos

    async def list(self, slug: str) -> list[ReleaseInfo]:
        return [
            ReleaseInfo(
                version=v,
                tag=f"{slug}/v{v}",
                object_sha="a" * 40,
                commit_sha="b" * 40,
                released_at=NOW,
            )
            for v in self.versions
        ]

    async def file(self, slug: str, version: str, name: str) -> bytes:
        if name == "shapes.ttl" and not self.shapes:
            raise LakeloomError("not_found", "release has no shapes", "none")
        if name == "skos.ttl" and not self.skos:
            raise LakeloomError("not_found", "release has no skos", "none")
        if version not in self.versions:
            raise LakeloomError("not_found", "no such release", "none")
        return f"{name}@{version}".encode()


class FakeModels:
    def __init__(self, base_iri: str | None = BASE) -> None:
        self.base_iri = base_iri

    async def get_model(self, slug: str) -> ModelInfo:
        return ModelInfo(slug=slug, glossary_id="g", fqn=slug, base_iri=self.base_iri)


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    eng = create_engine(f"sqlite:///{tmp_path}/l.db")
    await upgrade(eng)
    yield eng
    await eng.dispose()


def release(version: str = "1.0.0", **kw: Any) -> Release:
    return Release(
        id=f"rel_{version}",
        repo=REPO,
        model="shop",
        version=version,
        tag=f"shop/v{version}",
        commit_sha="b" * 40,
        released_by="alice",
        created_at=NOW,
        sink_status="pending",
        **kw,
    )


class Kit:
    def __init__(self, engine: AsyncEngine) -> None:
        self.repo = ReleaseRepository(engine)
        self.rec = Recorder()

    def worker(
        self,
        *,
        sink: Any = None,
        versions: list[str] | None = None,
        shapes_file: bool = True,
        skos_file: bool = True,
        policies: list[ModelPolicyEntry] | None = None,
        base_iri: str | None = BASE,
        **settings: Any,
    ) -> SinkWorker:
        s = Settings(  # type: ignore[call-arg]
            _env_file=None,
            git_remote=REMOTE,
            triplestore_enabled=True,
            triplestore_url=URL,
            **settings,
        )
        return SinkWorker(
            s,
            self.repo,
            FakeReader(versions or ["1.0.0"], shapes_file, skos_file),  # type: ignore[arg-type]
            FakeModels(base_iri),  # type: ignore[arg-type]
            sink or sink_for(self.rec),
            policies or [],
            clock=lambda: NOW,
        )


@pytest.fixture
def kit(engine: AsyncEngine) -> Kit:
    return Kit(engine)


async def test_worker_puts_release_latest_shapes_and_skos(kit: Kit) -> None:
    await kit.repo.add(release())
    (done,) = await kit.worker().process(NOW)
    assert done.sink_status == "done" and done.sink_attempts == 1
    assert kit.rec.graphs == [
        f"{BASE}release/1.0.0",
        f"{BASE}release/1.0.0/skos",
        f"{BASE}latest",
        f"{BASE}shapes",
        f"{BASE}skos",
    ]
    assert [r.content for r in kit.rec.requests] == [
        b"ontology.ttl@1.0.0",
        b"skos.ttl@1.0.0",
        b"ontology.ttl@1.0.0",
        b"shapes.ttl@1.0.0",
        b"skos.ttl@1.0.0",
    ]
    stored = await kit.repo.get("rel_1.0.0")
    assert stored is not None and stored.sink_status == "done" and stored.sink_last_error is None


async def test_latest_only_for_highest_release(kit: Kit) -> None:
    await kit.repo.add(release("1.0.0"))
    await kit.worker(versions=["1.1.0", "1.0.0"]).process(NOW)
    assert kit.rec.graphs == [f"{BASE}release/1.0.0", f"{BASE}release/1.0.0/skos"]


async def test_retry_of_older_release_never_overwrites_shapes(kit: Kit) -> None:
    """``<base>shapes`` follows ``<base>latest``: an older release's retry leaves it alone."""
    await kit.repo.add(release("1.0.0"))
    await kit.repo.add(release("1.1.0"))
    await kit.worker(versions=["1.1.0", "1.0.0"]).process(NOW)
    shapes = [r.content for r in kit.rec.requests if r.url.params["graph"] == f"{BASE}shapes"]
    assert shapes == [b"shapes.ttl@1.1.0"]


async def test_shapes_off_by_policy(kit: Kit) -> None:
    policies = [ModelPolicyEntry.model_validate({"name": "shop", "triplestore": {"shapes": False}})]
    await kit.repo.add(release())
    await kit.worker(policies=policies).process(NOW)
    assert f"{BASE}shapes" not in kit.rec.graphs
    assert f"{BASE}release/1.0.0" in kit.rec.graphs and f"{BASE}latest" in kit.rec.graphs


async def test_shapes_off_by_setting_and_on_by_policy_override(kit: Kit) -> None:
    await kit.repo.add(release())
    await kit.worker(triplestore_shapes=False).process(NOW)
    assert f"{BASE}shapes" not in kit.rec.graphs
    on = [ModelPolicyEntry.model_validate({"name": "shop", "triplestore": {"shapes": True}})]
    await kit.repo.add(release("1.1.0"))
    kit.rec.requests.clear()
    await kit.worker(triplestore_shapes=False, policies=on, versions=["1.1.0", "1.0.0"]).process(
        NOW
    )
    assert f"{BASE}shapes" in kit.rec.graphs


async def test_release_without_shapes_file_is_still_done(kit: Kit) -> None:
    await kit.repo.add(release())
    (done,) = await kit.worker(shapes_file=False).process(NOW)
    assert done.sink_status == "done" and f"{BASE}shapes" not in kit.rec.graphs


async def test_model_without_base_iri_fails(kit: Kit) -> None:
    await kit.repo.add(release())
    (out,) = await kit.worker(base_iri=None).process(NOW)
    assert out.sink_status == "failed" and out.sink_next_attempt_at is None
    assert out.sink_last_error is not None and "base IRI" in out.sink_last_error
    assert kit.rec.requests == []


@pytest.mark.parametrize(
    ("outcome", "transient"),
    [
        (httpx.Response(408), True),
        (httpx.Response(429), True),
        (httpx.Response(500), True),
        (httpx.Response(503), True),
        (httpx.ConnectError("refused"), True),
        (httpx.ReadTimeout("slow"), True),
        (httpx.Response(401), False),
        (httpx.Response(404), False),
        (httpx.Response(400), False),
    ],
)
async def test_transient_and_permanent_classification(
    outcome: httpx.Response | httpx.TransportError, transient: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with pytest.raises(SinkError) as err:
        await sink_for(handler).replace_graph("g", b"x")
    assert err.value.transient is transient


async def test_transient_failure_backs_off_then_recovers(kit: Kit) -> None:
    rec = Recorder(status=503)
    await kit.repo.add(release())
    worker = kit.worker(sink=sink_for(rec))
    (first,) = await worker.process(NOW)
    assert first.sink_status == "pending" and first.sink_attempts == 1
    assert first.sink_next_attempt_at == NOW + timedelta(seconds=backoff_seconds(1))
    assert await worker.process(NOW + timedelta(seconds=30)) == []  # not due yet
    rec.status = 204
    (second,) = await worker.process(NOW + timedelta(seconds=61))
    assert second.sink_status == "done" and second.sink_attempts == 2
    assert second.sink_last_error is None


async def test_permanent_failure_is_failed_at_once(kit: Kit) -> None:
    await kit.repo.add(release())
    (out,) = await kit.worker(sink=sink_for(Recorder(status=401))).process(NOW)
    assert out.sink_status == "failed" and out.sink_attempts == 1
    assert out.sink_last_error is not None and "HTTP 401" in out.sink_last_error


async def test_failed_after_10_attempts(kit: Kit) -> None:
    await kit.repo.add(release())
    worker = kit.worker(sink=sink_for(Recorder(status=500)))
    at = NOW
    last: Release | None = None
    for attempt in range(1, MAX_SINK_ATTEMPTS + 1):
        (last,) = await worker.process(at)
        assert last.sink_attempts == attempt
        if attempt < MAX_SINK_ATTEMPTS:
            assert last.sink_status == "pending"
            assert last.sink_next_attempt_at is not None
            at = last.sink_next_attempt_at
    assert last is not None and last.sink_status == "failed" and last.sink_next_attempt_at is None
    assert await worker.process(at + timedelta(days=1)) == []
    status = await worker.status()
    assert status["failed"] == 1 and status["pending"] == 0 and "HTTP 500" in status["last_error"]


async def test_sink_error_redacts_password(kit: Kit, caplog: pytest.LogCaptureFixture) -> None:
    def echo(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"bad credentials admin:{PASSWORD} for {request.url}")

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach http://admin:{PASSWORD}@fuseki:3030/")

    for handler in (echo, boom):
        engine_repo = kit.repo
        rel = release(f"1.{0 if handler is echo else 1}.0")
        await engine_repo.add(rel)
        with caplog.at_level("DEBUG"):
            (out,) = await kit.worker(sink=sink_for(handler)).process(NOW)
        assert out.sink_last_error is not None and out.sink_last_error
        stored = await kit.repo.get(rel.id)
        assert stored is not None and stored.sink_last_error is not None
        assert PASSWORD not in stored.sink_last_error
        assert PASSWORD not in repr(out) and PASSWORD not in caplog.text
        status = await kit.worker().status()
        assert PASSWORD not in str(status)


async def test_sink_error_redacts_basic_header_and_encoded_password() -> None:
    special = "p@ss:w/rd"
    token = base64.b64encode(f"admin:{special}".encode()).decode()

    def echo(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            text=f"Authorization: {request.headers['authorization']} "
            f"url http://admin:p%40ss%3Aw%2Frd@fuseki/",
        )

    with pytest.raises(SinkError) as exc:
        await sink_for(echo, password=special).replace_graph("g", b"x")
    assert token not in exc.value.message
    assert special not in exc.value.message and "p%40ss%3Aw%2Frd" not in exc.value.message
    assert "HTTP 401" in exc.value.message


async def test_stale_due_list_does_not_burn_an_attempt(kit: Kit, engine: AsyncEngine) -> None:
    """A row the inline attempt has just rescheduled is skipped by ``process``."""
    later = NOW + timedelta(minutes=5)
    await kit.repo.add(release(sink_attempts=1, sink_next_attempt_at=later))
    stale = release()  # as read before the inline attempt rescheduled it

    class Stale(ReleaseRepository):
        async def sink_due(self, repo: str, now: datetime) -> list[Release]:
            return [stale]

    worker = kit.worker()
    worker._releases = Stale(engine)
    assert await worker.process(NOW) == []
    assert kit.rec.requests == []
    stored = await kit.repo.get("rel_1.0.0")
    assert stored is not None and stored.sink_attempts == 1
    assert stored.sink_next_attempt_at == later
    # due at ``later``: attempted then
    assert [r.sink_status for r in await worker.process(later)] == ["done"]


async def test_policy_disabled_row_is_dropped_from_the_queue(kit: Kit) -> None:
    off = [ModelPolicyEntry.model_validate({"name": "shop", "triplestore": {"enabled": False}})]
    await kit.repo.add(release())
    (out,) = await kit.worker(policies=off).process(NOW)
    assert out.sink_status == "none" and kit.rec.requests == []


async def test_process_never_raises_and_inline_attempt_does_not_double_push(
    kit: Kit, engine: AsyncEngine
) -> None:
    await kit.repo.add(release())
    worker = kit.worker()
    first = await worker.attempt("rel_1.0.0")
    again = await worker.attempt("rel_1.0.0")  # already done: nothing to push
    assert first is not None and first.sink_status == "done" and again is None
    assert len(kit.rec.requests) == 5  # ontology, latest, shapes, skos-versioned, skos-latest

    class Broken(ReleaseRepository):
        async def sink_due(self, repo: str, now: datetime) -> list[Release]:
            raise RuntimeError("store down")

    broken = kit.worker()
    broken._releases = Broken(engine)
    assert await broken.process(NOW) == []


async def test_disabled_worker_does_nothing(kit: Kit, engine: AsyncEngine) -> None:
    await kit.repo.add(release())
    worker = kit.worker()
    worker._settings = Settings(_env_file=None, git_remote=REMOTE)  # type: ignore[call-arg]
    assert await worker.process(NOW) == []
    assert (await worker.status())["enabled"] is False


async def test_skos_versioned_graph_pushed_for_non_highest_release(kit: Kit) -> None:
    """``release/{v}/skos`` is always pushed when skos.ttl is present; ``<base>skos`` is not."""
    await kit.repo.add(release("1.0.0"))
    await kit.worker(versions=["1.1.0", "1.0.0"]).process(NOW)
    graphs = kit.rec.graphs
    assert f"{BASE}release/1.0.0/skos" in graphs
    assert f"{BASE}skos" not in graphs


async def test_skos_latest_graph_pushed_only_for_highest_release(kit: Kit) -> None:
    await kit.repo.add(release("1.1.0"))
    await kit.worker(versions=["1.1.0", "1.0.0"]).process(NOW)
    graphs = kit.rec.graphs
    assert f"{BASE}release/1.1.0/skos" in graphs
    assert f"{BASE}skos" in graphs


async def test_skos_off_by_policy(kit: Kit) -> None:
    policies = [ModelPolicyEntry.model_validate({"name": "shop", "triplestore": {"skos": False}})]
    await kit.repo.add(release())
    await kit.worker(policies=policies).process(NOW)
    assert all("skos" not in g for g in kit.rec.graphs)


async def test_skos_off_by_setting_and_on_by_policy_override(kit: Kit) -> None:
    await kit.repo.add(release())
    await kit.worker(triplestore_skos=False).process(NOW)
    assert all("skos" not in g for g in kit.rec.graphs)
    on = [ModelPolicyEntry.model_validate({"name": "shop", "triplestore": {"skos": True}})]
    await kit.repo.add(release("1.1.0"))
    kit.rec.requests.clear()
    await kit.worker(triplestore_skos=False, policies=on, versions=["1.1.0", "1.0.0"]).process(NOW)
    assert f"{BASE}release/1.1.0/skos" in kit.rec.graphs


async def test_release_without_skos_file_is_still_done(kit: Kit) -> None:
    await kit.repo.add(release())
    (done,) = await kit.worker(skos_file=False).process(NOW)
    assert done.sink_status == "done"
    assert all("skos" not in g for g in kit.rec.graphs)


def test_enabled_without_url_is_config_error() -> None:
    with pytest.raises(ConfigError, match="LAKELOOM_TRIPLESTORE_URL"):
        Settings(_env_file=None, triplestore_enabled=True)  # type: ignore[call-arg]
    assert Settings(_env_file=None).triplestore_enabled is False  # type: ignore[call-arg]


def test_password_file_and_repr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pw = tmp_path / "pw"
    pw.write_text(PASSWORD + "\n")
    monkeypatch.setenv("LAKELOOM_TRIPLESTORE_PASSWORD_FILE", str(pw))
    s = Settings(_env_file=None, triplestore_enabled=True, triplestore_url=URL)  # type: ignore[call-arg]
    assert s.triplestore_password is not None
    assert s.triplestore_password.get_secret_value() == PASSWORD
    assert PASSWORD not in repr(s) and URL not in repr(s)
