"""FastAPI app: health, readiness, metrics, request ids, bearer auth (NFR-OPS-01..03).

Middleware order (outermost first): request id + metrics + access log, then bearer auth.
CORS is deliberately not added (NFR-SEC-15). Sub-applications sit behind the same auth:
``endpoints`` are served at exactly one path (the MCP endpoint ``/mcp``), ``extra_mounts``
under a path prefix. Startup/shutdown lives in one ``lifespan``; ``lifespans`` (such as
the MCP session manager) are entered inside it and exited before dependencies close.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any, cast

import asyncpg  # pyright: ignore[reportMissingTypeStubs]
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry.trace import SpanKind, Status, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy.exc import ArgumentError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lakeloom.config import ConfigError, Settings
from lakeloom.core.confirm import (
    ConfirmTokens,
    confirm_tokens_from_settings,
    require_confirm_secret,
)
from lakeloom.core.drift_service import DriftSchedule, DriftService
from lakeloom.core.imports import ImportService
from lakeloom.core.jobs import Job, JobRunner, LeaseBusy
from lakeloom.core.model_policy import ModelPolicyEntry
from lakeloom.core.models import ModelService
from lakeloom.core.obo import OnBehalfOf
from lakeloom.core.proposals import ProposalService
from lakeloom.core.reconcile import ReconcileService, run_committing
from lakeloom.core.releases import ReleaseService
from lakeloom.core.review import ReviewService
from lakeloom.core.scans import ScanService
from lakeloom.core.sources import close_sources, start_sources
from lakeloom.core.sparql import SparqlService
from lakeloom.errors import LakeloomError
from lakeloom.gitstore.layout import Snapshot
from lakeloom.gitstore.links import web_base
from lakeloom.gitstore.publisher import GitPublisher
from lakeloom.gitstore.release_reader import ReleaseReader
from lakeloom.gitstore.snapshot import fill_exports
from lakeloom.gitstore.wiring import FORGE_TIMEOUT, build_publisher
from lakeloom.logging import request_id_var
from lakeloom.metrics import HTTP_REQUESTS, REGISTRY
from lakeloom.om.client import OMClient
from lakeloom.publish.artifacts import GitLinks
from lakeloom.publish.triplestore import GraphStoreSink, SinkWorker
from lakeloom.server.auth import (
    PRM_PATH,
    PUBLIC_PATHS,
    BearerAuthMiddleware,
    TokenValidator,
    protected_resource_metadata,
)
from lakeloom.server.health import (
    Check,
    git_workspace_check,
    om_check,
    run_checks,
    state_store_check,
)
from lakeloom.server.http_api import (
    create_router,
    lakeloom_error_handler,
    request_validation_handler,
)
from lakeloom.server.ratelimit import RateLimiter
from lakeloom.sources.base import LakeSource
from lakeloom.sources.config import SourcesConfig, load_sources_config
from lakeloom.sources.registry import build_sources
from lakeloom.state.audit import AuditRepository
from lakeloom.state.db import create_engine, upgrade
from lakeloom.state.leases import LeaseRepository, instance_id
from lakeloom.state.proposals import ProposalRepository
from lakeloom.state.publications import PublicationRepository
from lakeloom.state.releases import Release, ReleaseRepository
from lakeloom.state.scans import ScanRepository
from lakeloom.telemetry import configure_telemetry, is_active, start_span

_log = logging.getLogger("lakeloom.http")
_IDP_TIMEOUT = 5.0
SINK_TIMEOUT = 30.0  # one Graph Store PUT
# Probes and scrapes would drown the traces.
_UNTRACED_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})
_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
    OperationalError,
    InterfaceError,
    OSError,
    TimeoutError,
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
)


@dataclass
class AppDeps:
    settings: Settings
    om: OMClient
    engine: AsyncEngine
    validator: TokenValidator
    # Closed by the app lifespan; shared by the validator and on-behalf-of exchanges.
    idp_http: httpx.AsyncClient | None = None
    # Operator-configured lake sources (NFR-SEC-13); started and closed by the lifespan.
    sources: dict[str, LakeSource] = field(default_factory=dict[str, LakeSource])
    # Per-model mapping policies (``models:`` in LAKELOOM_CONFIG_FILE, FR-PRO-01).
    policies: list[ModelPolicyEntry] = field(default_factory=list[ModelPolicyEntry])
    # Forge API client for Git publishing (only with LAKELOOM_GIT_REMOTE); closed by the lifespan.
    git_http: httpx.AsyncClient | None = None
    scans: ScanService = field(init=False)
    models: ModelService = field(init=False)
    confirm: ConfirmTokens = field(init=False)
    audit: AuditRepository = field(init=False)
    proposals: ProposalService = field(init=False)
    review: ReviewService = field(init=False)
    obo: OnBehalfOf = field(init=False)
    publications: PublicationRepository = field(init=False)
    publisher: GitPublisher = field(init=False)
    releases_repo: ReleaseRepository = field(init=False)
    # Releases (FR-GIT-06, FR-PUB-02): all None with Git publishing off; ``git_links`` is
    # also None without a forge web base (LAKELOOM_FORGE=none and no LAKELOOM_GIT_WEB_URL).
    reader: ReleaseReader | None = field(init=False, default=None)
    release_service: ReleaseService | None = field(init=False, default=None)
    git_links: GitLinks | None = field(init=False, default=None)
    # Imports (FR-IO-02, FR-GIT-07): always on; import_from_git needs Git publishing.
    import_service: ImportService = field(init=False)
    proposal_repo: ProposalRepository = field(init=False)
    # Drift detection (FR-PRO-07): always on; the scheduled report-only check
    # (``drift-schedule`` job) only with LAKELOOM_DRIFT_INTERVAL_SECONDS > 0.
    drift_service: DriftService = field(init=False)
    drift_schedule: DriftSchedule = field(init=False)
    # Reconciliation of OpenMetadata with Git (FR-GIT-08): None with Git publishing off; the
    # ``reconcile`` job only with LAKELOOM_RECONCILE_INTERVAL_SECONDS > 0. It audits itself.
    reconcile_service: ReconcileService | None = field(init=False, default=None)
    # Read-only SPARQL over a model's glossary (FR-PUB-03): always on; get_server_info says
    # whether this OpenMetadata offers it.
    sparql_service: SparqlService = field(init=False)
    # Optional triple-store sink (FR-TS): only with Git releases and LAKELOOM_TRIPLESTORE_ENABLED.
    sink_http: httpx.AsyncClient | None = field(init=False, default=None)
    sink_worker: SinkWorker | None = field(init=False, default=None)
    sink_tasks: set["asyncio.Task[Any]"] = field(
        init=False, default_factory=set["asyncio.Task[Any]"]
    )
    # Background jobs, each under a lease in the state store (NFR-DEP-07); started and stopped
    # by the app lifespan. ``git-publisher:<repo>`` with Git publishing, ``triplestore-sink``
    # with the sink, ``reconcile`` with Git publishing, ``drift-schedule`` when set.
    jobs: JobRunner = field(init=False)
    leases: LeaseRepository = field(init=False)
    # Per-principal rate limit (NFR-SEC-16), shared by the MCP tools and the /v1 API.
    limiter: RateLimiter = field(init=False)

    def __post_init__(self) -> None:
        self.confirm = confirm_tokens_from_settings(self.settings)
        self.limiter = RateLimiter(
            self.settings.rate_limit_per_minute, self.settings.rate_limit_burst
        )
        if self.idp_http is None:
            self.idp_http = httpx.AsyncClient(timeout=_IDP_TIMEOUT)  # closed by the lifespan
        self.obo = OnBehalfOf(self.settings, self.idp_http, self.om)
        self.audit = AuditRepository(self.engine)
        self.models = ModelService(self.om, self.settings)
        scan_repo = ScanRepository(self.engine)
        self.scans = ScanService(self.sources, scan_repo, self.om, self.settings)
        # audit=None: MCP tools audit through guarded() (one row per write; see proposals.py).
        proposal_repo = self.proposal_repo = ProposalRepository(self.engine)
        self.proposals = ProposalService(
            self.om,
            self.models,
            scan_repo,
            proposal_repo,
            None,
            self.settings,
            confirm=self.confirm,
            sources=self.sources,
            policies=self.policies,
        )
        # Git publishing on apply (FR-GIT-04); disabled without LAKELOOM_GIT_REMOTE.
        if self.settings.git_remote and self.git_http is None:
            self.git_http = httpx.AsyncClient(timeout=FORGE_TIMEOUT)  # closed by the lifespan
        self.publications = PublicationRepository(self.engine)
        self.publisher = build_publisher(
            self.settings,
            self.publications,
            self.git_http,
            self.policies,
            exporter=self._export,
        )
        self.releases_repo = ReleaseRepository(self.engine)
        wc = self.publisher.working_copy
        if wc is not None:
            key = self.publisher.repo_key
            self.reader = ReleaseReader(wc, key)
            on_released = None
            if self.settings.triplestore_enabled and self.settings.triplestore_url:
                self.sink_http = httpx.AsyncClient(timeout=SINK_TIMEOUT)  # closed by the lifespan
                self.sink_worker = SinkWorker(
                    self.settings,
                    self.releases_repo,
                    self.reader,
                    self.models,
                    GraphStoreSink(
                        self.sink_http,
                        self.settings.triplestore_url,
                        self.settings.triplestore_username,
                        self.settings.triplestore_password,
                    ),
                    self.policies,
                )
                on_released = self._sink_inline
            # create_release is audited by guarded() (audit=None here, as for proposals).
            self.release_service = ReleaseService(
                self.settings,
                self.models,
                self.om,
                wc,
                self.reader,
                self.releases_repo,
                self.publications,
                self.confirm,
                None,
                self.policies,
                on_released=on_released,
            )
            base = web_base(self.settings)
            if base is not None:
                self.git_links = GitLinks(base, self.settings.forge, self._last_commit)
        # import_model / import_from_git are audited by guarded() (audit=None here).
        self.import_service = ImportService(
            self.settings,
            self.models,
            self.om,
            self.proposals,
            self.confirm,
            None,
            self.policies,
            reader=self.reader,
            wc=self.publisher.working_copy,
            repo_key=self.publisher.repo_key if self.publisher.working_copy else None,
        )
        # detect_drift is audited by guarded() (audit=None here).
        self.drift_service = DriftService(
            self.scans,
            scan_repo,
            self.proposals,
            proposal_repo,
            self.models,
            self.om,
            self.policies,
        )
        self.drift_schedule = DriftSchedule(self.drift_service, proposal_repo)
        if self.publisher.enabled:
            # The job has no guarded() call around it: the service writes its own audit rows.
            self.reconcile_service = ReconcileService(
                settings=self.settings,
                models=self.models,
                publisher=self.publisher,
                publications=self.publications,
                proposals=proposal_repo,
                audit=self.audit,
                policies=self.policies,
            )
        self.sparql_service = SparqlService(self.settings, self.models, self.om)
        # Same for reviews: review_proposal is audited by guarded() (audit=None here).
        self.review = ReviewService(
            self.proposals,
            proposal_repo,
            self.obo,
            self.models,
            self.confirm,
            self.settings,
            None,
            self.policies,
            publisher=self.publisher,
        )
        self.leases = LeaseRepository(self.engine)
        self.jobs = JobRunner(self.leases, instance_id())
        if self.publisher.enabled:
            # The outbox drain; the inline publish after approve takes no lease (ADR 0013).
            name = f"git-publisher:{self.publisher.repo_key}"
            self.jobs.add(Job(name, self._git_interval, self._publish_tick))
        if self.sink_worker is not None:
            self.jobs.add(Job("triplestore-sink", self._git_interval, self._sink_tick))
        if self.reconcile_service is not None and self.settings.reconcile_interval_seconds > 0:
            self.jobs.add(Job("reconcile", self._reconcile_interval, self._reconcile_tick))
        if self.settings.drift_interval_seconds > 0:
            self.jobs.add(Job("drift-schedule", self._drift_interval, self._drift_tick))

    def _git_interval(self) -> float:
        return float(self.settings.git_retry_interval_seconds)

    async def _publish_tick(self) -> None:
        await self.publisher.process()  # never raises; the runner logs it if it does

    async def _sink_tick(self) -> None:
        if self.sink_worker is not None:
            await self.sink_worker.process()  # never raises; the runner logs it if it does

    def _reconcile_interval(self) -> float:
        return float(self.settings.reconcile_interval_seconds)

    async def _reconcile_tick(self) -> None:
        service = self.reconcile_service
        if service is None:
            return
        if not self.settings.reconcile_commit:
            await service.run(commit=False)
            return
        try:
            await run_committing(service, self.leases, self.jobs.holder)
        except LeaseBusy:
            # ``lakeloom reconcile --commit`` is running: it queues what this run would.
            _log.info("reconcile: a committing reconciliation is already running; run skipped")

    def _drift_interval(self) -> float:
        return float(self.settings.drift_interval_seconds)

    async def _drift_tick(self) -> None:
        await self.drift_schedule.run()  # one model's failure never stops the run

    async def _sink_inline(self, release: Release) -> None:
        """One attempt right after a release, in the background: the tool never waits for it."""
        worker = self.sink_worker
        if worker is None or release.sink_status != "pending":
            return
        task = asyncio.create_task(worker.attempt(release.id), name="lakeloom-sink-inline")
        self.sink_tasks.add(task)
        task.add_done_callback(self.sink_tasks.discard)

    async def _last_commit(self, slug: str) -> str | None:
        """The commit of ``slug``'s last done Git publication (the latest artefacts' link)."""
        pub = await self.publications.last_done(self.publisher.repo_key, slug)
        return None if pub is None else pub.commit_sha

    async def _export(self, snapshot: Snapshot) -> Snapshot:
        """The publisher's exporter: the model as OM holds it now (bot client, reads)."""
        return await fill_exports(self.models, self.om, snapshot)


def build_deps(settings: Settings, *, http_auth: bool = True) -> AppDeps:
    """Wire settings into live dependencies; fail fast when required config is missing.

    ``http_auth=False`` (stdio) does not require the OIDC issuer or public URL: no
    bearer token is ever validated, so the validator is a placeholder that is never used.
    """
    required = {"LAKELOOM_OM_URL": settings.om_url}
    if http_auth:
        required |= {
            "LAKELOOM_OIDC_ISSUER": settings.oidc_issuer,
            "LAKELOOM_PUBLIC_URL": settings.public_url,
        }
    missing = [name for name, value in required.items() if not value]
    if missing or settings.om_url is None or (http_auth and settings.oidc_issuer is None):
        raise ConfigError(f"missing required configuration: set {', '.join(missing)}")
    require_confirm_secret(settings)  # fail fast in production without the secret
    config = load_config(settings)
    sources = build_sources(config, settings)
    engine = _create_engine_or_config_error(settings.db_url)
    idp_http = httpx.AsyncClient(timeout=_IDP_TIMEOUT)
    validator = TokenValidator(
        issuer=settings.oidc_issuer or "urn:lakeloom:stdio",
        audience=settings.oidc_audience,
        http=idp_http,
        jwks_url=settings.oidc_jwks_url,
        leeway=settings.oidc_clock_skew_seconds,
        agent_claim=settings.oidc_agent_claim,
        scope_group_map=settings.scope_group_map,
    )
    return AppDeps(
        settings=settings,
        om=OMClient(str(settings.om_url), settings.om_bot_token),
        engine=engine,
        validator=validator,
        idp_http=idp_http,
        sources=sources,
        policies=list(config.models),
    )


def load_config(settings: Settings) -> SourcesConfig:
    """``LAKELOOM_CONFIG_FILE`` (sources and model policies; empty when unset)."""
    path = settings.config_file
    if path is not None and not path.is_file():
        raise ConfigError(f"LAKELOOM_CONFIG_FILE {path} does not exist or is not a file")
    return load_sources_config(path)


def load_lake_sources(settings: Settings) -> dict[str, LakeSource]:
    """Build the sources in ``LAKELOOM_CONFIG_FILE`` (none when unset); bad config fails fast."""
    return build_sources(load_config(settings), settings)


def _create_engine_or_config_error(db_url: str) -> AsyncEngine:
    """Turn an unusable ``db_url`` into a ConfigError that never echoes the URL.

    The URL may embed a password (NFR-SEC-11), so only the scheme and the error type
    are reported: bad port (ValueError), unparsable URL or unknown dialect
    (ArgumentError / NoSuchModuleError), driver not installed (ImportError).
    """
    try:
        return create_engine(db_url)
    except (ValueError, ArgumentError, ImportError) as exc:
        scheme = db_url.partition("://")[0] if "://" in db_url else "?"
        raise ConfigError(
            f"LAKELOOM_DB_URL is unusable ({type(exc).__name__}; scheme {scheme!r}): "
            "check the dialect, driver, host and port"
        ) from None


class RequestContextMiddleware:
    """Pure ASGI: request id (accept/generate/echo), request counter, access log.

    Never logs headers. The route label is the route template, the mount prefix for
    mounted sub-applications, or ``unmatched``.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = next((v for k, v in scope["headers"] if k.lower() == b"x-request-id"), None)
        request_id = _clean_request_id(incoming) or str(uuid.uuid4())
        token = request_id_var.set(request_id)
        status = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [(k, v) for k, v in message["headers"] if k.lower() != b"x-request-id"]
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        traced = scope["path"] not in _UNTRACED_PATHS
        span_cm = (
            start_span(f"HTTP {scope['method']}", kind=SpanKind.SERVER)
            if traced
            else nullcontext(None)
        )
        with span_cm as span:
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                label = _route_label(scope)
                if span is not None:
                    span.update_name(f"HTTP {scope['method']} {label or 'unmatched'}")
                    span.set_attribute("http.method", scope["method"])
                    span.set_attribute("http.route", label or "unmatched")
                    span.set_attribute("http.status_code", status)
                    if status >= 500:
                        span.set_status(Status(StatusCode.ERROR, f"HTTP {status}"))
                HTTP_REQUESTS.labels(
                    method=scope["method"], route=label or "unmatched", status=str(status)
                ).inc()
                _log.info(
                    "request",
                    extra={
                        "method": scope["method"],
                        "route": label or "unmatched",
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    },
                )
                request_id_var.reset(token)


def _route_label(scope: Scope) -> str | None:
    # Starlette sets scope["route"] to the matched route; for a Mount that is the Mount,
    # whose ``path`` is the prefix, unless the mounted app routes again and overwrites it.
    template = getattr(scope.get("route"), "path", None)
    return template if isinstance(template, str) and template else None


def _clean_request_id(raw: bytes | None) -> str | None:
    if not raw:
        return None
    value = raw.decode("latin-1").strip()
    if not value or len(value) > 128 or not value.isprintable():
        return None
    return value


async def _validation_handler(request: Request, exc: Exception) -> Response:
    """Typed errors on ``/v1``; FastAPI's default 422 elsewhere."""
    if request.url.path.startswith("/v1/"):
        return await request_validation_handler(request, exc)
    return await request_validation_exception_handler(request, cast(RequestValidationError, exc))


def create_app(
    deps: AppDeps,
    *,
    extra_mounts: Sequence[tuple[str, ASGIApp]] = (),
    endpoints: Sequence[tuple[str, ASGIApp]] = (),
    lifespans: Sequence[Callable[[], AbstractAsyncContextManager[None]]] = (),
    checks: Sequence[Check] | None = None,
    upgrade_fn: Callable[[AsyncEngine], Awaitable[None]] = upgrade,
    upgrade_backoff: tuple[float, float] = (1.0, 30.0),
) -> FastAPI:
    """Build the app. ``checks`` overrides the default readiness checks (tests).

    ``endpoints`` are ASGI apps served at exactly their path (all methods); a Starlette
    ``Mount`` cannot serve its bare prefix without a redirect, which MCP clients must
    not need to follow. ``lifespans`` run for the lifetime of the app.
    """
    settings = deps.settings
    resource_metadata_url = f"{settings.public_base_url}{PRM_PATH}"
    upgrade_pending = False
    readiness: Sequence[Check] = (
        checks
        if checks is not None
        else (
            om_check(deps.om),
            state_store_check(deps.engine, lambda: upgrade_pending),
            git_workspace_check(settings.data_dir / "git"),
        )
    )

    async def retry_upgrade() -> None:
        nonlocal upgrade_pending
        delay, maximum = upgrade_backoff
        while True:
            await asyncio.sleep(delay)
            delay = min(delay * 2, maximum)
            try:
                await upgrade_fn(deps.engine)
            except _CONNECTION_ERRORS as exc:
                _log.warning("schema upgrade retry failed (%s)", type(exc).__name__)
            except Exception as exc:
                _log.error("state store migration failed (%s)", type(exc).__name__)
            else:
                upgrade_pending = False
                _log.info("state store schema upgraded")
                return

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        nonlocal upgrade_pending
        retry: asyncio.Task[None] | None = None
        # Off unless an OTLP endpoint is set; an already-active setup is not ours to stop.
        owns_telemetry = not is_active()
        telemetry = configure_telemetry(settings)
        try:
            try:
                await upgrade_fn(deps.engine)
            except _CONNECTION_ERRORS as exc:
                # Never log the exception text: it can embed the DB URL (NFR-SEC-11).
                _log.warning(
                    "state store unreachable at startup (%s); retrying schema upgrade",
                    type(exc).__name__,
                )
                upgrade_pending = True
                retry = asyncio.create_task(retry_upgrade())
            except Exception as exc:
                # Class only, and a sanitized error: uvicorn prints the exception text.
                _log.error("state store migration failed (%s)", type(exc).__name__)
                raise RuntimeError(f"state store migration failed ({type(exc).__name__})") from None
            # Upstream MCP sources connect here, bounded; an unavailable one never fails startup.
            await start_sources(deps.sources)
            # Queued rows stay in the store on shutdown (NFR-OPS-04).
            await deps.jobs.start()
            async with AsyncExitStack() as stack:
                for factory in lifespans:
                    await stack.enter_async_context(factory())
                yield
        finally:
            try:
                await deps.jobs.stop(grace=settings.shutdown_timeout_seconds)
            finally:  # a failing stop never leaves the clients and the engine open
                for task in (retry, *deps.sink_tasks):
                    if task is not None:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                await close_sources(deps.sources)
                if deps.idp_http is not None:
                    await deps.idp_http.aclose()
                if deps.git_http is not None:
                    await deps.git_http.aclose()
                if deps.sink_http is not None:
                    await deps.sink_http.aclose()
                await deps.om.aclose()
                await deps.engine.dispose()
                if owns_telemetry:
                    telemetry.shutdown()

    # No web UI (spec §0.7): Swagger/ReDoc pages off; /openapi.json stays, behind auth.
    app = FastAPI(title="Lakeloom", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        results = await run_checks(readiness)
        ok = all(r.ok for r in results)
        body = {
            "status": "ok" if ok else "fail",
            "checks": [{"name": r.name, "ok": r.ok, "detail": r.detail} for r in results],
        }
        return JSONResponse(body, status_code=200 if ok else 503)

    @app.get("/metrics")
    async def metrics() -> Response:  # pyright: ignore[reportUnusedFunction]
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    @app.get(PRM_PATH)
    async def prm() -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        return protected_resource_metadata(settings)

    app.include_router(create_router(deps))
    app.add_exception_handler(LakeloomError, lakeloom_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    for path, endpoint in endpoints:
        app.router.routes.append(Route(path, endpoint=endpoint))
    for path, mounted in extra_mounts:
        app.mount(path, mounted)

    app.add_middleware(
        BearerAuthMiddleware,
        validator=deps.validator,
        resource_metadata_url=resource_metadata_url,
        public_paths=PUBLIC_PATHS,
    )
    app.add_middleware(RequestContextMiddleware)
    return app
