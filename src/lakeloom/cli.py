"""Lakeloom command line interface (NFR-OSS-01, FR-AGT-07).

``lakeloom serve`` runs the HTTP app (health, metrics and the MCP endpoint at ``/mcp``).
``lakeloom sources``, ``lakeloom tables SOURCE`` and ``lakeloom scan SOURCE`` use the
sources in ``LAKELOOM_CONFIG_FILE`` directly, as the operator (FR-AGT-07, FR-SRC-10).
Configuration errors are printed to stderr and exit with code 2, without a traceback;
``lakeloom models``, ``concept``, ``context``, ``export`` and ``validate`` read the semantic
models in OpenMetadata (FR-CTX-01..04, FR-IO-01). ``lakeloom propose SCAN_ID --model M``,
``lakeloom proposal ID`` and ``lakeloom submit ID`` are the proposal tools for the operator
(drafts and submissions only; nothing is applied); ``lakeloom pending [--model M]`` lists the
proposals awaiting review and ``lakeloom rebase ID`` rebases one. ``lakeloom git status``
lists the queued and blocked Git publications and ``lakeloom git retry`` requeues the blocked
ones and publishes once (FR-GIT-09). ``lakeloom release create MODEL`` prints the release dry
run and, with ``--yes``, releases (FR-GIT-06); ``lakeloom release list MODEL`` lists the
released versions; ``lakeloom release sink-retry`` requeues failed triple-store pushes
(FR-TS-03). ``lakeloom import MODEL FILE`` and ``lakeloom import-git MODEL`` print the import
dry run and, with ``--yes``, create the draft proposal (FR-IO-02, FR-GIT-07).
``lakeloom reconcile [--model M] [--commit] [--json]`` compares each published model in
OpenMetadata with its last Git commit (FR-GIT-08): exit code 0 when all are in sync (skipped
models do not count), 3 when any drifted, 1 when any failed; ``--commit`` queues a
publication per drift under the server's ``reconcile`` lease.
``lakeloom sparql MODEL FILE|-`` runs a read-only SPARQL query over a model's glossary and
writes OpenMetadata's result bytes (FR-PUB-03).
There is no review command: approving and rejecting are for human reviewers through MCP
(ADR 0012). They write their own audit rows. Typed errors (``not_found``,
``upstream_unavailable``, ...) exit with code 1.
"""

import asyncio
import getpass
import sys
from collections.abc import AsyncGenerator, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import typer
import uvicorn
from pydantic import ValidationError

import lakeloom
from lakeloom.config import ConfigError, Settings, get_settings
from lakeloom.errors import LakeloomError
from lakeloom.logging import configure_logging
from lakeloom.untrusted import code_span, single_line

if TYPE_CHECKING:
    from lakeloom.core.drift_service import DriftService
    from lakeloom.core.imports import ImportService
    from lakeloom.core.models import ModelService
    from lakeloom.core.proposals import Actor, ProposalService
    from lakeloom.core.reconcile import ReconcileResult, ReconcileRun, ReconcileService
    from lakeloom.core.releases import ReleaseService
    from lakeloom.core.sparql import SparqlService
    from lakeloom.gitstore.publisher import GitPublisher
    from lakeloom.gitstore.release_reader import ReleaseReader
    from lakeloom.gitstore.repo import WorkingCopy
    from lakeloom.om.client import OMClient
    from lakeloom.rdf.canonical import InputFormat
    from lakeloom.sources.base import LakeSource
    from lakeloom.state.leases import LeaseRepository
    from lakeloom.state.publications import Publication, PublicationRepository

CLI_CLIENT = "lakeloom-cli"

app = typer.Typer(name="lakeloom", help="Lakeloom operator CLI.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Lakeloom operator CLI."""


@app.command()
def version() -> None:
    """Print the installed Lakeloom version."""
    typer.echo(lakeloom.__version__)


@app.command()
def serve(
    host: Annotated[
        str | None, typer.Option(help="Bind address [default: LAKELOOM_HOST or 0.0.0.0].")
    ] = None,
    port: Annotated[
        int | None, typer.Option(help="Bind port [default: LAKELOOM_PORT or 8000].")
    ] = None,
    transport: Annotated[
        Literal["http", "stdio"],
        typer.Option(help="http: /mcp, health and metrics; stdio: local MCP over stdin/stdout."),
    ] = "http",
) -> None:
    """Serve the MCP endpoint (/mcp), health checks and metrics over HTTP, or MCP over stdio."""
    # Imported here so `lakeloom version` stays fast and needs no server dependencies.
    from lakeloom.server.http_app import build_deps
    from lakeloom.server.mcp_app import create_server_app

    if transport == "stdio":
        _serve_stdio()
        return
    try:
        settings = get_settings()
        configure_logging(settings.log_level)
        server_app = create_server_app(build_deps(settings))
    except (ConfigError, ValidationError) as exc:
        # Settings hide input values in validation errors, so no secret is echoed.
        typer.echo(f"configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from None
    uvicorn.run(
        server_app,
        host=settings.host if host is None else host,
        port=settings.port if port is None else port,
        log_config=None,
        # RequestContextMiddleware writes the access log; uvicorn's would duplicate it.
        access_log=False,
        # NFR-OPS-04: in-flight requests get this long; the lifespan then stops the jobs.
        timeout_graceful_shutdown=settings.shutdown_timeout_seconds,
    )


def _serve_stdio() -> None:
    """MCP over stdio as the configured local principal (NFR-SEC-04); stdout is protocol only."""
    from lakeloom.server.http_app import build_deps
    from lakeloom.server.stdio import StdioStartupError, run_stdio

    try:
        settings = get_settings()
        configure_logging(settings.log_level, stream=sys.stderr)
        if not settings.stdio_enabled:
            typer.echo("stdio transport is disabled (set LAKELOOM_STDIO_ENABLED=true)", err=True)
            raise typer.Exit(code=2)
        deps = build_deps(settings, http_auth=False)
    except (ConfigError, ValidationError) as exc:
        typer.echo(f"configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from None
    try:
        asyncio.run(run_stdio(deps))
    except StdioStartupError as exc:
        typer.echo(f"stdio startup failed: {exc}", err=True)
        raise typer.Exit(code=1) from None


db_app = typer.Typer(name="db", help="State store commands.", no_args_is_help=True)
app.add_typer(db_app)


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Apply state store migrations (Alembic upgrade head)."""
    from lakeloom.state.db import create_engine, upgrade

    try:
        settings = get_settings()
    except (ConfigError, ValidationError) as exc:
        typer.echo(f"configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from None

    async def _run() -> None:
        engine = create_engine(settings.db_url)
        try:
            await upgrade(engine)
        finally:
            await engine.dispose()

    try:
        asyncio.run(_run())
    except Exception as exc:
        # The DB URL may hold a password, so only the error type is printed.
        typer.echo(f"state store upgrade failed ({type(exc).__name__})", err=True)
        raise typer.Exit(code=1) from None
    typer.echo("state store at head")


# --- lake sources (operator side; the same operations as the MCP source tools) -------------


def _cli_settings() -> Settings:
    try:
        settings = get_settings()
    except (ConfigError, ValidationError) as exc:
        typer.echo(f"configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from None
    # stdout carries the command's output; logs go to stderr, WARNING and up unless DEBUG.
    debug = settings.log_level.upper() == "DEBUG"
    configure_logging("DEBUG" if debug else "WARNING", stream=sys.stderr)
    return settings


@asynccontextmanager
async def _open_sources(settings: Settings) -> AsyncGenerator[dict[str, "LakeSource"]]:
    from lakeloom.core.sources import close_sources, start_sources
    from lakeloom.server.http_app import load_lake_sources

    sources = load_lake_sources(settings)
    try:
        await start_sources(sources)
        yield sources
    finally:
        await close_sources(sources)


def _run(coro: Coroutine[Any, Any, None]) -> None:
    """Run ``coro``; typed errors exit 1, configuration errors exit 2."""
    try:
        asyncio.run(coro)
    except ConfigError as exc:
        typer.echo(f"configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from None
    except LakeloomError as exc:
        typer.echo(f"error {exc.code}: {exc.message} (hint: {exc.hint})", err=True)
        raise typer.Exit(code=1) from None


def _operator() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError):  # no passwd entry (e.g. an arbitrary container uid)
        return "operator"


@app.command("sources")
def sources_cmd() -> None:
    """List the configured lake sources with kind and availability."""
    from lakeloom.core.sources import source_status

    settings = _cli_settings()

    async def _go() -> None:
        async with _open_sources(settings) as sources:
            if not sources:
                typer.echo("no sources configured (set LAKELOOM_CONFIG_FILE)")
            for name, src in sources.items():
                status = source_status(name, src)
                state = "available"
                if status.error is not None:
                    state = f"unavailable ({status.error.code}: {status.error.hint})"
                typer.echo(f"{single_line(name)}\t{single_line(status.kind)}\t{state}")

    _run(_go())


@app.command("tables")
def tables_cmd(
    source: Annotated[str, typer.Argument(help="Configured source name.")],
    catalog: Annotated[str | None, typer.Option(help="Only this catalog.")] = None,
    schema: Annotated[str | None, typer.Option(help="Only this schema.")] = None,
) -> None:
    """List a source's tables with their current versions."""
    from lakeloom.core.sources import get_source, list_all_tables

    settings = _cli_settings()

    async def _go() -> None:
        async with _open_sources(settings) as sources:
            src = get_source(sources, source)
            for ref in await list_all_tables(src, catalog, schema):
                try:
                    v = (await src.get_table_version(ref)).version
                    version = "unknown" if v is None else str(v)
                except LakeloomError as exc:
                    version = f"error {exc.code}"
                typer.echo(f"{single_line(ref.full_name)}\t{version}")

    _run(_go())


@app.command("scan")
def scan_cmd(
    source: Annotated[str, typer.Argument(help="Configured source name.")],
    profile: Annotated[bool, typer.Option("--profile", help="Also profile columns.")] = False,
    table: Annotated[
        list[str] | None, typer.Option("--table", help="Only this table (repeatable).")
    ] = None,
    catalog: Annotated[
        list[str] | None, typer.Option("--catalog", help="Only this catalog (repeatable).")
    ] = None,
    schema: Annotated[
        list[str] | None, typer.Option("--schema", help="Only this schema (repeatable).")
    ] = None,
) -> None:
    """Scan a source, store the scan record and print its summary."""
    from lakeloom.core.scans import ScanService, summarize
    from lakeloom.om.client import OMClient
    from lakeloom.server.auth import PrincipalType
    from lakeloom.state.db import create_engine, upgrade
    from lakeloom.state.scans import ScanRepository

    settings = _cli_settings()
    operator = _operator()

    async def _go() -> None:
        async with _open_sources(settings) as sources:
            engine = create_engine(settings.db_url)
            om = (
                None
                if settings.om_url is None
                else OMClient(str(settings.om_url), settings.om_bot_token)
            )
            try:
                await upgrade(engine)
                service = ScanService(sources, ScanRepository(engine), om, settings)
                record = await service.scan(
                    source,
                    catalogs=catalog,
                    schemas=schema,
                    tables=table,
                    profile=profile,
                    principal_subject=operator,
                    principal_type=PrincipalType.HUMAN.value,
                    client=CLI_CLIENT,
                )
            finally:
                if om is not None:
                    await om.aclose()
                await engine.dispose()
            typer.echo(summarize(record), nl=False)
            if record.status == "failed":
                raise typer.Exit(code=1)

    try:
        _run(_go())
    except typer.Exit:
        raise
    except Exception as exc:
        # Like `db upgrade`: no traceback, and only the error type (the message may hold
        # a DB URL or upstream text).
        typer.echo(f"scan failed ({type(exc).__name__})", err=True)
        raise typer.Exit(code=1) from None


# --- semantic read path (operator side; the same operations as the MCP semantic tools) -----


@asynccontextmanager
async def _open_semantic(
    settings: Settings, *, with_sources: bool = False
) -> AsyncGenerator[tuple["OMClient", "ModelService", dict[str, "LakeSource"]]]:
    from lakeloom.core.models import ModelService
    from lakeloom.om.client import OMClient

    if settings.om_url is None:
        raise ConfigError("LAKELOOM_OM_URL is required")
    om = OMClient(str(settings.om_url), settings.om_bot_token)
    try:
        if with_sources:
            async with _open_sources(settings) as sources:
                yield om, ModelService(om, settings), sources
        else:
            yield om, ModelService(om, settings), {}
    finally:
        await om.aclose()


@app.command("models")
def models_cmd() -> None:
    """List the semantic models (OpenMetadata glossaries)."""
    settings = _cli_settings()

    async def _go() -> None:
        async with _open_semantic(settings) as (_, models, _s):
            cursor: str | None = None
            while True:
                page = await models.list_models(cursor, 100)
                for m in page.items:
                    terms = "?" if m.term_count is None else str(m.term_count)
                    typer.echo(f"{single_line(m.slug)}\t{terms}\t{single_line(m.base_iri or '')}")
                cursor = page.next_cursor
                if cursor is None:
                    break

    _run(_go())


@app.command("concept")
def concept_cmd(
    query: Annotated[str, typer.Argument(help="Concept name, FQN or IRI, or a search text.")],
    max_tokens: Annotated[int | None, typer.Option(help="Shorten the output.")] = None,
) -> None:
    """Explain a concept; when QUERY is not one concept, list the matching concepts."""
    from lakeloom.core.concepts import concept_markdown, explain_concept, find_concepts

    settings = _cli_settings()

    async def _go() -> None:
        async with _open_semantic(settings) as (om, models, _s):
            try:
                detail = await explain_concept(models, om, query)
            except LakeloomError as exc:
                # not_found or ambiguous: fall back to a search; other errors are real.
                if exc.code not in ("not_found", "validation_error"):
                    raise
                page = await find_concepts(models, om, query)
                if not page.items:
                    raise
                for m in page.items:
                    c = m.concept
                    typer.echo(
                        f"{single_line(c.fqn)}\t{single_line(c.label)}\t{m.matched_on}"
                        f"\t{m.score:.2f}"
                    )
                return
            text, _ = concept_markdown(detail, max_tokens=max_tokens)
            typer.echo(text, nl=False)

    _run(_go())


@app.command("context")
def context_cmd(
    asset: Annotated[str, typer.Argument(help="OM FQN, Unity Catalog name or storage URI.")],
    format: Annotated[
        Literal["md", "json", "jsonld"], typer.Option("--format", help="Output format.")
    ] = "md",
    max_tokens: Annotated[int | None, typer.Option(help="Cut the output to ~N tokens.")] = None,
) -> None:
    """Print the semantic context of an asset (Markdown by default)."""
    import json

    from lakeloom.core.context import (
        get_asset_context,
        render_json,
        render_jsonld,
        render_markdown,
    )

    settings = _cli_settings()

    async def _go() -> None:
        async with _open_semantic(settings, with_sources=True) as (om, models, sources):
            context = await get_asset_context(asset, models=models, om=om, sources=sources)
            if format == "md":
                typer.echo(render_markdown(context, max_tokens=max_tokens).payload, nl=False)
                return
            if format == "json":
                payload = render_json(context, max_tokens=max_tokens).payload
            else:
                info = await models.get_model(context.model) if context.model else None
                full = await models.jsonld_context(info.slug) if info else None
                payload = render_jsonld(context, info, max_tokens=max_tokens, context=full).payload
            typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))

    _run(_go())


@app.command("export")
def export_cmd(
    model: Annotated[str, typer.Argument(help="Model slug (see `lakeloom models`).")],
    format: Annotated[
        Literal["turtle", "rdfxml", "ntriples", "jsonld", "skos"],
        typer.Option("--format", help="RDF serialisation."),
    ] = "turtle",
    out: Annotated[Path | None, typer.Option("--out", help="Write here instead of stdout.")] = None,
) -> None:
    """Export a model as deterministic RDF; the bytes are written exactly."""
    settings = _cli_settings()

    async def _go() -> None:
        async with _open_semantic(settings) as (_, models, _s):
            data = await models.export(model, format)
        if out is None:
            sys.stdout.flush()
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        else:
            try:
                out.write_bytes(data)
            except OSError as exc:
                typer.echo(f"cannot write {out} ({type(exc).__name__})", err=True)
                raise typer.Exit(code=1) from None

    _run(_go())


@app.command("validate")
def validate_cmd(
    model: Annotated[str, typer.Argument(help="Model slug (see `lakeloom models`).")],
) -> None:
    """Validate a model's structure; exit code 1 when it does not conform."""
    from lakeloom.core.validation import validation_markdown

    settings = _cli_settings()

    async def _go() -> None:
        async with _open_semantic(settings) as (_, models, _s):
            report = await models.validation(model)
        typer.echo(validation_markdown(report), nl=False)
        if not report.conforms:
            raise typer.Exit(code=1)

    _run(_go())


# --- proposals (operator side; the same operations as the MCP proposal tools) --------------


@asynccontextmanager
async def _open_proposals(settings: Settings) -> AsyncGenerator["ProposalService"]:
    """A proposal service that writes its own audit rows (CLI mode, see core/proposals.py)."""
    import secrets

    from lakeloom.core.confirm import MIN_SECRET_BYTES, ConfirmTokens
    from lakeloom.core.proposals import ProposalService
    from lakeloom.server.http_app import load_config
    from lakeloom.sources.registry import build_sources
    from lakeloom.state.audit import AuditRepository
    from lakeloom.state.db import create_engine, upgrade
    from lakeloom.state.proposals import ProposalRepository
    from lakeloom.state.scans import ScanRepository

    # The CLI never confirms a destructive call, so an ephemeral key is enough without one.
    secret = settings.confirm_secret
    key = (
        secret.get_secret_value().encode("utf-8")
        if secret is not None
        else secrets.token_bytes(MIN_SECRET_BYTES)
    )
    config = load_config(settings)
    sources = build_sources(config, settings)
    engine = create_engine(settings.db_url)
    try:
        await upgrade(engine)
        async with _open_semantic(settings) as (om, models, _s):
            yield ProposalService(
                om,
                models,
                ScanRepository(engine),
                ProposalRepository(engine),
                AuditRepository(engine),
                settings,
                confirm=ConfirmTokens(key, settings.confirm_ttl_seconds),
                sources=sources,
                policies=config.models,
            )
    finally:
        await engine.dispose()


def _actor() -> "Actor":
    from lakeloom.core.proposals import Actor
    from lakeloom.server.auth import PrincipalType

    # The operator holds the state store and the OM bot credentials: lakeloom:admin (ADR 0011).
    return Actor(
        subject=_operator(),
        type=PrincipalType.HUMAN.value,
        scopes=frozenset({"lakeloom:admin"}),
    )


@app.command("propose")
def propose_cmd(
    scan_id: Annotated[str, typer.Argument(help="Scan id (see `lakeloom scan`).")],
    model: Annotated[str, typer.Option("--model", help="Target model slug.")],
    table: Annotated[
        list[str] | None, typer.Option("--table", help="Only this table (repeatable).")
    ] = None,
) -> None:
    """Propose concepts from a scan as OpenMetadata draft change sets; print the summary."""
    settings = _cli_settings()

    async def _go() -> None:
        async with _open_proposals(settings) as service:
            p = await service.propose_from_scan(
                scan_id, model, principal=_actor(), client=CLI_CLIENT, tables=table
            )
            typer.echo(service.summary_markdown(p), nl=False)

    _run(_go())


@app.command("proposal")
def proposal_cmd(
    proposal_id: Annotated[str, typer.Argument(help="Proposal id (prop_...).")],
) -> None:
    """Print a proposal's summary, with its change-set states refreshed from OpenMetadata."""
    settings = _cli_settings()

    async def _go() -> None:
        async with _open_proposals(settings) as service:
            p = await service.get_proposal(proposal_id)
            typer.echo(service.summary_markdown(p), nl=False)

    _run(_go())


@app.command("submit")
def submit_cmd(
    proposal_id: Annotated[str, typer.Argument(help="Proposal id (prop_...).")],
) -> None:
    """Submit a draft proposal for review (nothing is applied) and print its summary."""
    settings = _cli_settings()

    async def _go() -> None:
        async with _open_proposals(settings) as service:
            p = await service.submit(proposal_id, principal=_actor(), client=CLI_CLIENT)
            typer.echo(service.summary_markdown(p), nl=False)

    _run(_go())


@app.command("pending")
def pending_cmd(
    model: Annotated[str | None, typer.Option("--model", help="Only this model.")] = None,
) -> None:
    """List the submitted and apply_failed proposals, by model and data product (the operator
    reviews nothing here: approving or rejecting is for human reviewers through MCP)."""
    from lakeloom.core.pending import group_pending

    settings = _cli_settings()

    async def _go() -> None:
        async with _open_proposals(settings) as service:
            cursor: str | None = None
            items: list[Any] = []
            while True:
                page = await service.list_pending(
                    _actor(), model=model, cursor=cursor, limit=100, exclude_own=False
                )
                items += page.items
                cursor = page.next_cursor
                if cursor is None:
                    break
            if not items:
                typer.echo("No proposals await review.")
                return
            for group in group_pending(items):
                typer.echo(f"model {single_line(group.model)}")
                for dp in group.data_products:
                    typer.echo(f"  data product {single_line(dp.data_product)}")
                    for e in dp.proposals:
                        ops = ", ".join(f"{n} {k}" for k, n in e.operations.items())
                        typer.echo(
                            f"    {e.id}  {e.status}  {e.parts} part(s)  {ops or 'no operations'}  "
                            f"by {single_line(e.proposer)}  {e.age_seconds // 3600}h old"
                        )

    _run(_go())


@app.command("rebase")
def rebase_cmd(
    proposal_id: Annotated[str, typer.Argument(help="Proposal id (prop_...).")],
) -> None:
    """Rebase a proposal on OpenMetadata's current state (it becomes a draft) and print it."""
    settings = _cli_settings()

    async def _go() -> None:
        async with _open_proposals(settings) as service:
            p = await service.rebase(proposal_id, principal=_actor(), client=CLI_CLIENT)
            typer.echo(service.summary_markdown(p), nl=False)

    _run(_go())


# --- Git publishing (operator side, FR-GIT-09) -----------------------------------------------

git_app = typer.Typer(name="git", help="Git publication queue commands.", no_args_is_help=True)
app.add_typer(git_app)


@asynccontextmanager
async def _open_publisher(
    settings: Settings,
) -> AsyncGenerator[tuple["GitPublisher", "PublicationRepository"]]:
    """The publisher as the server builds it (same factory); exports through OM when set."""
    import httpx

    from lakeloom.gitstore.layout import Snapshot
    from lakeloom.gitstore.snapshot import fill_exports
    from lakeloom.gitstore.wiring import FORGE_TIMEOUT, build_publisher
    from lakeloom.server.http_app import load_config
    from lakeloom.state.db import create_engine, upgrade
    from lakeloom.state.publications import PublicationRepository

    config = load_config(settings)
    engine = create_engine(settings.db_url)
    http = httpx.AsyncClient(timeout=FORGE_TIMEOUT)
    try:
        await upgrade(engine)
        repo = PublicationRepository(engine)
        if settings.om_url is None:
            yield build_publisher(settings, repo, http, config.models), repo
            return
        async with _open_semantic(settings) as (om, models, _s):

            async def exporter(snapshot: Snapshot) -> Snapshot:
                return await fill_exports(models, om, snapshot)

            yield build_publisher(settings, repo, http, config.models, exporter=exporter), repo
    finally:
        await http.aclose()
        await engine.dispose()


@git_app.command("status")
def git_status_cmd() -> None:
    """List the pending and blocked Git publications, oldest first."""
    settings = _cli_settings()

    async def _go() -> None:
        async with _open_publisher(settings) as (publisher, repo):
            if not publisher.enabled:
                typer.echo("Git publishing is off (LAKELOOM_GIT_REMOTE is not set).")
                return
            rows = [*await repo.list("pending", 500), *await repo.list("blocked", 500)]
            rows.sort(key=lambda r: r.seq or 0)
            if not rows:
                typer.echo("No pending or blocked Git publications.")
                return
            typer.echo("id\tmodel\tproposal\tstatus\tattempts\tlast_error")
            for r in rows:
                error = single_line(r.last_error) if r.last_error else "-"
                typer.echo(
                    f"{r.id}\t{single_line(r.model)}\t{r.proposal_id}\t{r.status}\t"
                    f"{r.attempts}\t{error}"
                )

    _run(_go())


@git_app.command("retry")
def git_retry_cmd() -> None:
    """Requeue the blocked Git publications, then publish the queue once."""
    settings = _cli_settings()

    async def _go() -> None:
        async with _open_publisher(settings) as (publisher, _repo):
            if not publisher.enabled:
                typer.echo("Git publishing is off (LAKELOOM_GIT_REMOTE is not set).")
                return
            requeued = await publisher.retry_blocked()
            typer.echo(f"requeued {requeued} blocked publication(s)")
            results = await publisher.process()
            if not results:
                typer.echo("nothing was due for publishing")
            for r in results:
                if r.status == "done":
                    where = r.pr_url or f"{(r.commit_sha or '')[:7]} on {r.branch}"
                    typer.echo(f"{r.id} done: {where}")
                else:
                    error = single_line(r.last_error) if r.last_error else "-"
                    typer.echo(f"{r.id} {r.status} (attempt {r.attempts}): {error}")

    _run(_go())


# --- releases (operator side, FR-GIT-06; the same operations as the MCP release tools) -------

release_app = typer.Typer(name="release", help="Release commands.", no_args_is_help=True)
app.add_typer(release_app)


def _cli_working_copy(settings: Settings) -> "WorkingCopy":
    """The CLI's own clone (``data_dir/git/cli-repo``): the repository lock serialises only
    one process, so a CLI release next to a running server must not share the server's
    working copy. Remote updates still serialise: a rejected push re-renders on the new tip."""
    from lakeloom.gitstore.git import GitRunner
    from lakeloom.gitstore.repo import WorkingCopy

    root = settings.data_dir / "git"
    return WorkingCopy(GitRunner(settings, root), root / "cli-repo", settings.git_remote or "")


@asynccontextmanager
async def _open_releases(
    settings: Settings,
) -> AsyncGenerator[tuple["ReleaseService", "ReleaseReader"] | None]:
    """The release service and reader (``None`` with Git publishing off); it writes its own
    audit rows. The release commit goes straight to the branch, so no forge is needed."""
    import hashlib
    import secrets

    from lakeloom.core.confirm import MIN_SECRET_BYTES, ConfirmTokens
    from lakeloom.core.releases import ReleaseService
    from lakeloom.gitstore.release_reader import ReleaseReader
    from lakeloom.server.http_app import load_config
    from lakeloom.state.audit import AuditRepository
    from lakeloom.state.db import create_engine, upgrade
    from lakeloom.state.publications import PublicationRepository
    from lakeloom.state.releases import ReleaseRepository

    if not settings.git_remote:
        yield None
        return
    # The dry run and the confirmation run in this one process: an ephemeral key will do.
    secret = settings.confirm_secret
    key = (
        secret.get_secret_value().encode("utf-8")
        if secret is not None
        else secrets.token_bytes(MIN_SECRET_BYTES)
    )
    config = load_config(settings)
    engine = create_engine(settings.db_url)
    try:
        await upgrade(engine)
        wc = _cli_working_copy(settings)
        reader = ReleaseReader(wc, hashlib.sha256(settings.git_remote.encode()).hexdigest())
        async with _open_semantic(settings) as (om, models, _s):
            service = ReleaseService(
                settings,
                models,
                om,
                wc,
                reader,
                ReleaseRepository(engine),
                PublicationRepository(engine),
                ConfirmTokens(key, settings.confirm_ttl_seconds),
                AuditRepository(engine),
                config.models,
            )
            yield service, reader
    finally:
        await engine.dispose()


_GIT_OFF = "Git publishing is off (LAKELOOM_GIT_REMOTE is not set)."


@release_app.command("create")
def release_create_cmd(
    model: Annotated[str, typer.Argument(help="Model slug.")],
    version: Annotated[
        str | None, typer.Option("--version", help="Explicit MAJOR.MINOR.PATCH.")
    ] = None,
    bump: Annotated[
        str | None, typer.Option("--bump", help="Override the computed bump: major|minor|patch.")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Release after printing the plan.")] = False,
) -> None:
    """Print the release plan (dry run); with --yes, commit, tag and push the release."""
    settings = _cli_settings()

    async def _go() -> None:
        from lakeloom.server.tools_release import plan_text, released_text

        if bump is not None and bump not in ("major", "minor", "patch"):
            raise LakeloomError(
                "validation_error", f"unknown bump {bump!r}", "pass major, minor or patch"
            )
        chosen = bump
        async with _open_releases(settings) as opened:
            if opened is None:
                typer.echo(_GIT_OFF)
                return
            service, _reader = opened
            actor = _actor()
            plan = await service.plan(actor, model, version=version, bump=chosen)
            typer.echo(plan_text(plan), nl=False)
            if not yes:
                typer.echo("\nDry run: nothing was changed. Re-run with --yes to release.")
                return
            out = await service.create(
                actor,
                model,
                version=version,
                bump=chosen,
                confirm_token=plan.confirm_token,
                client=CLI_CLIENT,
            )
            typer.echo("")
            typer.echo(released_text(out), nl=False)

    _run(_go())


@release_app.command("list")
def release_list_cmd(model: Annotated[str, typer.Argument(help="Model slug.")]) -> None:
    """List the released versions of a model, highest first."""
    settings = _cli_settings()

    async def _go() -> None:
        async with _open_releases(settings) as opened:
            if opened is None:
                typer.echo(_GIT_OFF)
                return
            _service, reader = opened
            releases = await reader.list(model)
            if not releases:
                typer.echo(f"No releases of {single_line(model)}.")
                return
            typer.echo("version\ttag\tcommit\treleased_at")
            for r in releases:
                typer.echo(f"{r.version}\t{r.tag}\t{r.commit_sha[:7]}\t{r.released_at.isoformat()}")

    _run(_go())


@release_app.command("sink-retry")
def release_sink_retry_cmd() -> None:
    """Requeue the releases whose triple-store push failed; the server retries them."""
    settings = _cli_settings()

    async def _go() -> None:
        import hashlib

        from lakeloom.state.db import create_engine, upgrade
        from lakeloom.state.releases import ReleaseRepository

        if not settings.git_remote:
            typer.echo(_GIT_OFF)
            return
        if not settings.triplestore_enabled:
            typer.echo("The triple store is off (LAKELOOM_TRIPLESTORE_ENABLED is not true).")
            return
        engine = create_engine(settings.db_url)
        try:
            await upgrade(engine)
            repo = hashlib.sha256(settings.git_remote.encode()).hexdigest()
            requeued = await ReleaseRepository(engine).requeue_failed_sinks(repo)
        finally:
            await engine.dispose()
        typer.echo(f"requeued {requeued} failed triple-store push(es); the server retries them")

    _run(_go())


# --- imports (operator side, FR-IO-02, FR-GIT-07; the same operations as the MCP tools) ------

_FORMATS: tuple["InputFormat", ...] = ("turtle", "rdfxml", "ntriples", "jsonld")
_EXTENSIONS: dict[str, "InputFormat"] = {
    ".ttl": "turtle",
    ".owl": "rdfxml",
    ".rdf": "rdfxml",
    ".nt": "ntriples",
    ".jsonld": "jsonld",
}


@asynccontextmanager
async def _open_imports(settings: Settings) -> AsyncGenerator["ImportService"]:
    """The import service (CLI mode: it and its proposal service write their own audit
    rows); ``import-git`` uses the CLI's own clone, like ``release``."""
    import hashlib
    import secrets

    from lakeloom.core.confirm import MIN_SECRET_BYTES, ConfirmTokens
    from lakeloom.core.imports import ImportService
    from lakeloom.core.proposals import ProposalService
    from lakeloom.gitstore.release_reader import ReleaseReader
    from lakeloom.server.http_app import load_config
    from lakeloom.state.audit import AuditRepository
    from lakeloom.state.db import create_engine, upgrade
    from lakeloom.state.proposals import ProposalRepository
    from lakeloom.state.scans import ScanRepository

    # The dry run and the confirmation run in this one process: an ephemeral key will do.
    secret = settings.confirm_secret
    key = (
        secret.get_secret_value().encode("utf-8")
        if secret is not None
        else secrets.token_bytes(MIN_SECRET_BYTES)
    )
    confirm = ConfirmTokens(key, settings.confirm_ttl_seconds)
    config = load_config(settings)
    engine = create_engine(settings.db_url)
    remote = settings.git_remote or ""
    wc = _cli_working_copy(settings) if remote else None
    repo_key = hashlib.sha256(remote.encode()).hexdigest() if remote else None
    try:
        await upgrade(engine)
        audit = AuditRepository(engine)
        async with _open_semantic(settings) as (om, models, _s):
            proposals = ProposalService(
                om,
                models,
                ScanRepository(engine),
                ProposalRepository(engine),
                audit,
                settings,
                confirm=confirm,
                policies=config.models,
            )
            yield ImportService(
                settings,
                models,
                om,
                proposals,
                confirm,
                audit,
                config.models,
                reader=ReleaseReader(wc, repo_key) if wc is not None and repo_key else None,
                wc=wc,
                repo_key=repo_key,
            )
    finally:
        await engine.dispose()


def _import_mode(mode: str) -> Literal["merge", "replace"]:
    if mode not in ("merge", "replace"):
        raise LakeloomError("validation_error", f"unknown mode {mode!r}", "pass merge or replace")
    return "merge" if mode == "merge" else "replace"


_SUBMIT = "`lakeloom submit <id>`"


@app.command("import")
def import_cmd(
    model: Annotated[str, typer.Argument(help="Target model slug.")],
    file: Annotated[Path, typer.Argument(help="RDF file to import.")],
    fmt: Annotated[
        str | None,
        typer.Option(
            "--format",
            help="turtle|rdfxml|ntriples|jsonld (default: from the extension "
            ".ttl, .owl/.rdf, .nt, .jsonld).",
        ),
    ] = None,
    mode: Annotated[str, typer.Option("--mode", help="merge|replace.")] = "merge",
    base_iri: Annotated[
        str | None, typer.Option("--base-iri", help="Namespace the terms are read from.")
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Create the draft proposal after the dry run.")
    ] = False,
) -> None:
    """Print the import dry run; with --yes, create the draft proposal."""
    settings = _cli_settings()

    async def _go() -> None:
        from lakeloom.server.tools_import import imported_text, plan_text

        chosen: InputFormat | None = (
            next((f for f in _FORMATS if f == fmt), None)
            if fmt
            else _EXTENSIONS.get(file.suffix.lower())
        )
        if chosen is None:
            raise LakeloomError(
                "validation_error",
                f"unknown format {single_line(fmt)!r}"
                if fmt
                else f"cannot tell the format of {single_line(file.name)!r}",
                "pass --format turtle|rdfxml|ntriples|jsonld",
            )
        how = _import_mode(mode)
        try:
            data = file.read_bytes()
        except OSError as exc:
            raise LakeloomError(
                "not_found", f"cannot read {single_line(str(file))!r}", type(exc).__name__
            ) from None
        actor = _actor()
        async with _open_imports(settings) as service:
            plan = await service.plan_file(actor, model, data, chosen, mode=how, base_iri=base_iri)
            typer.echo(plan_text(plan), nl=False)
            if not yes:
                typer.echo("\nDry run: nothing was changed. Re-run with --yes to import.")
                return
            p = await service.confirm_file(
                actor,
                CLI_CLIENT,
                model,
                data,
                chosen,
                mode=how,
                base_iri=base_iri,
                confirm_token=plan.confirm_token,
            )
            typer.echo("")
            typer.echo(imported_text(p, submit=_SUBMIT), nl=False)

    _run(_go())


@app.command("import-git")
def import_git_cmd(
    model: Annotated[str, typer.Argument(help="Target model slug.")],
    version: Annotated[
        str, typer.Option("--version", help="Release MAJOR.MINOR.PATCH, or latest.")
    ] = "latest",
    from_model: Annotated[
        str | None, typer.Option("--from-model", help="Read this model's files instead.")
    ] = None,
    mode: Annotated[str, typer.Option("--mode", help="merge|replace.")] = "merge",
    yes: Annotated[
        bool, typer.Option("--yes", help="Create the draft proposal after the dry run.")
    ] = False,
) -> None:
    """Print the dry run of restoring a model from Git; with --yes, create the draft."""
    settings = _cli_settings()

    async def _go() -> None:
        from lakeloom.server.tools_import import imported_text, plan_text

        how = _import_mode(mode)
        actor = _actor()
        async with _open_imports(settings) as service:
            plan = await service.plan_git(
                actor, model, version=version, from_model=from_model, mode=how
            )
            typer.echo(plan_text(plan), nl=False)
            if not yes:
                typer.echo("\nDry run: nothing was changed. Re-run with --yes to import.")
                return
            p = await service.confirm_git(
                actor,
                CLI_CLIENT,
                model,
                version=version,
                from_model=from_model,
                mode=how,
                confirm_token=plan.confirm_token,
            )
            typer.echo("")
            typer.echo(imported_text(p, submit=_SUBMIT), nl=False)

    _run(_go())


# --- drift (operator side, FR-PRO-07; the same operation as the MCP detect_drift tool) --------


@asynccontextmanager
async def _open_drift(settings: Settings) -> AsyncGenerator["DriftService"]:
    """The drift service over the configured sources (CLI mode: it and its proposal service
    write their own audit rows)."""
    import secrets

    from lakeloom.core.confirm import MIN_SECRET_BYTES, ConfirmTokens
    from lakeloom.core.drift_service import DriftService
    from lakeloom.core.proposals import ProposalService
    from lakeloom.core.scans import ScanService
    from lakeloom.server.http_app import load_config
    from lakeloom.state.audit import AuditRepository
    from lakeloom.state.db import create_engine, upgrade
    from lakeloom.state.proposals import ProposalRepository
    from lakeloom.state.scans import ScanRepository

    # drift never confirms a destructive call: an ephemeral key is enough without one
    secret = settings.confirm_secret
    key = (
        secret.get_secret_value().encode("utf-8")
        if secret is not None
        else secrets.token_bytes(MIN_SECRET_BYTES)
    )
    config = load_config(settings)
    engine = create_engine(settings.db_url)
    try:
        await upgrade(engine)
        audit = AuditRepository(engine)
        scan_repo = ScanRepository(engine)
        proposal_repo = ProposalRepository(engine)
        async with _open_semantic(settings, with_sources=True) as (om, models, sources):
            proposals = ProposalService(
                om,
                models,
                scan_repo,
                proposal_repo,
                audit,
                settings,
                confirm=ConfirmTokens(key, settings.confirm_ttl_seconds),
                sources=sources,
                policies=config.models,
            )
            yield DriftService(
                ScanService(sources, scan_repo, om, settings),
                scan_repo,
                proposals,
                proposal_repo,
                models,
                om,
                config.models,
                audit=audit,
            )
    finally:
        await engine.dispose()


@app.command("drift")
def drift_cmd(
    model: Annotated[str, typer.Argument(help="Model slug.")],
    no_new_tables: Annotated[
        bool,
        typer.Option("--no-new-tables", help="Report tables new in the lake, do not propose them."),
    ] = False,
) -> None:
    """Scan a model's tables again; print the drift and the draft proposal it created."""
    settings = _cli_settings()

    async def _go() -> None:
        from lakeloom.core.drift_service import drift_text

        async with _open_drift(settings) as service:
            report = await service.detect(
                _actor(), CLI_CLIENT, model, include_new_tables=not no_new_tables
            )
            typer.echo(drift_text(report, submit=_SUBMIT), nl=False)

    _run(_go())


# --- reconciliation (operator side, FR-GIT-08; the same service as the ``reconcile`` job) -----

_LISTED_TERMS = 50
EXIT_DRIFT = 3


@dataclass
class _CliPublisher:
    """The server's publication queue with the CLI's own clone for reading baselines: the
    repository lock serialises one process only, so the CLI never touches the server's
    working copy (as for releases). Enqueued publications are published by the server."""

    enabled: bool
    repo_key: str
    wc: "WorkingCopy"
    inner: "GitPublisher"

    @property
    def working_copy(self) -> "WorkingCopy":
        return self.wc

    async def enqueue(self, pub: "Publication") -> "Publication":
        return await self.inner.enqueue(pub)


@asynccontextmanager
async def _open_reconcile(
    settings: Settings,
) -> AsyncGenerator[tuple["ReconcileService", "LeaseRepository"]]:
    """The reconcile service (it writes its own audit rows) and the job leases."""
    import httpx

    from lakeloom.core.reconcile import ReconcileService
    from lakeloom.gitstore.wiring import FORGE_TIMEOUT, build_publisher
    from lakeloom.server.http_app import load_config
    from lakeloom.state.audit import AuditRepository
    from lakeloom.state.db import create_engine, upgrade
    from lakeloom.state.leases import LeaseRepository
    from lakeloom.state.proposals import ProposalRepository
    from lakeloom.state.publications import PublicationRepository

    config = load_config(settings)
    engine = create_engine(settings.db_url)
    http = httpx.AsyncClient(timeout=FORGE_TIMEOUT)
    try:
        await upgrade(engine)
        pubs = PublicationRepository(engine)
        inner = build_publisher(settings, pubs, http, config.models)
        publisher = _CliPublisher(inner.enabled, inner.repo_key, _cli_working_copy(settings), inner)
        async with _open_semantic(settings) as (_om, models, _s):
            service = ReconcileService(
                settings=settings,
                models=models,
                publisher=publisher,
                publications=pubs,
                proposals=ProposalRepository(engine),
                audit=AuditRepository(engine),
                policies=config.models,
            )
            yield service, LeaseRepository(engine)
    finally:
        await http.aclose()
        await engine.dispose()


async def _reconcile_run(
    service: "ReconcileService",
    leases: "LeaseRepository",
    *,
    commit: bool,
    models: list[str] | None,
) -> "ReconcileRun":
    """Run once. A committing run holds the ``reconcile-commit`` lease, so it never races a
    committing server job (or another ``--commit``) into queueing a second publication of the
    same drift; the server's ``reconcile`` job lease is not needed. A check-only run writes no
    Git row and takes no lease."""
    if not commit:
        return await service.run(commit=False, models=models)
    from lakeloom.core.jobs import LeaseBusy, LeaseLost
    from lakeloom.core.reconcile import run_committing
    from lakeloom.state.leases import instance_id

    try:
        return await run_committing(service, leases, instance_id(), models=models)
    except LeaseBusy:
        raise LakeloomError(
            "conflict",
            "a committing reconciliation is already running; try again later",
            "the server's reconcile job (LAKELOOM_RECONCILE_COMMIT=true) or another "
            "`lakeloom reconcile --commit` holds the reconcile-commit lease; run without "
            "--commit to only report",
        ) from None
    except LeaseLost:
        raise LakeloomError(
            "conflict",
            "the reconcile-commit lease was lost during the run; it was stopped",
            "check the state store, then run lakeloom reconcile --commit again",
        ) from None


def _terms_text(label: str, iris: list[str]) -> list[str]:
    """Term IRIs come from OpenMetadata: each one an inert code span (NFR-SEC-14)."""
    lines = [f"    {label}: {code_span(iri)}" for iri in iris[:_LISTED_TERMS]]
    if len(iris) > _LISTED_TERMS:
        lines.append(f"    ... and {len(iris) - _LISTED_TERMS} more {label}")
    return lines


def _reconcile_text(run: "ReconcileRun", *, commit: bool) -> str:
    lines = [f"Reconciliation of OpenMetadata with Git at {run.at.isoformat()}:"]
    if not run.results:
        lines.append("- no published model")
    for r in run.results:
        model = single_line(r.model)
        if r.status == "drifted":
            lines.append(
                f"- {model}: drifted ({len(r.added)} added, {len(r.changed)} changed, "
                f"{len(r.removed)} removed)"
            )
            lines += _terms_text("added", r.added)
            lines += _terms_text("changed", r.changed)
            lines += _terms_text("removed", r.removed)
            if r.publication_id:
                lines.append(f"    queued publication {r.publication_id} (the server publishes it)")
            elif r.reason:
                lines.append(f"    {single_line(r.reason)}")
        else:
            reason = f" ({single_line(r.reason)})" if r.reason else ""
            lines.append(f"- {model}: {r.status}{reason}")
    if not commit and any(r.status == "drifted" for r in run.results):
        lines.append("Nothing was committed. Re-run with --commit to queue the drift for Git.")
    return "\n".join(lines) + "\n"


def _reconcile_exit(results: list["ReconcileResult"]) -> int:
    """0 in sync (skips included), 3 any drift, 1 any error (an error wins over drift)."""
    if any(r.status == "error" for r in results):
        return 1
    return EXIT_DRIFT if any(r.status == "drifted" for r in results) else 0


@app.command("reconcile")
def reconcile_cmd(
    model: Annotated[str | None, typer.Option("--model", help="Only this model (slug).")] = None,
    commit: Annotated[
        bool, typer.Option("--commit", help="Queue a Git publication for each drifted model.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the run as JSON.")] = False,
) -> None:
    """Compare OpenMetadata with the last Git commit of each published model (FR-GIT-08).

    Exit code 0: in sync (skipped models do not count); 3: drift found; 1: an error."""
    settings = _cli_settings()
    code: list[int] = []

    async def _go() -> None:
        if not settings.git_remote:
            raise LakeloomError(
                "validation_error",
                "Git publishing is not configured",
                "set LAKELOOM_GIT_REMOTE to reconcile OpenMetadata with Git",
            )
        models = [model] if model is not None else None
        async with _open_reconcile(settings) as (service, leases):
            run = await _reconcile_run(service, leases, commit=commit, models=models)
        if as_json:
            typer.echo(run.model_dump_json(indent=2))
        else:
            typer.echo(_reconcile_text(run, commit=commit), nl=False)
        if run.results and all(r.status == "skipped" for r in run.results):
            # Exit 0 all the same (skips do not count), but nothing was compared at all.
            typer.echo(
                "warning: every model was skipped; OpenMetadata was not compared with Git",
                err=True,
            )
        code.append(_reconcile_exit(run.results))

    _run(_go())
    if code and code[0]:
        raise typer.Exit(code=code[0])


# --- SPARQL (operator side, FR-PUB-03; the same operation as the MCP sparql_query tool) -----

SparqlFormatOption = Literal["json", "xml", "csv", "tsv", "turtle", "jsonld", "ntriples", "rdfxml"]


@asynccontextmanager
async def _open_sparql(settings: Settings) -> AsyncGenerator["SparqlService"]:
    from lakeloom.core.sparql import SparqlService

    async with _open_semantic(settings) as (om, models, _s):
        yield SparqlService(settings, models, om)


@app.command("sparql")
def sparql_cmd(
    model: Annotated[str, typer.Argument(help="Model slug.")],
    query_file: Annotated[str, typer.Argument(help="File with the SPARQL query, or - for stdin.")],
    format: Annotated[
        SparqlFormatOption | None,
        typer.Option("--format", help="Result format (default: json or turtle by form)."),
    ] = None,
) -> None:
    """Run a read-only SPARQL query over a model's glossary; the result bytes are written."""
    settings = _cli_settings()
    try:
        text = sys.stdin.read() if query_file == "-" else Path(query_file).read_text("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        typer.echo(f"cannot read {query_file} ({type(exc).__name__})", err=True)
        raise typer.Exit(code=1) from None

    async def _go() -> None:
        async with _open_sparql(settings) as service:
            result = await service.query(model, text, format)
        sys.stdout.flush()
        sys.stdout.buffer.write(result.body)
        sys.stdout.buffer.flush()

    _run(_go())


if __name__ == "__main__":
    app()
