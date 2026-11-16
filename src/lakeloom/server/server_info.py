"""What this Lakeloom instance is and can do, for ``get_server_info`` (FR-OM-06, §7).

``build_server_info`` has no side effects beyond probing OpenMetadata, and never raises
when OpenMetadata is down: ``openmetadata.reachable`` is then ``False`` with an error.
The caller section names the principal and its scopes but never carries its token
(NFR-SEC-11).
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

import lakeloom
from lakeloom.core.obo import OboMode
from lakeloom.core.sources import SourceStatus, source_status
from lakeloom.core.sparql import sparql_available
from lakeloom.om.models import OMCapabilities
from lakeloom.server.auth import Principal, PrincipalType
from lakeloom.server.http_app import AppDeps

__all__ = ["CallerInfo", "ServerInfo", "build_server_info", "summarize"]


class CallerInfo(BaseModel):
    """The authenticated caller, without credentials."""

    subject: str
    type: PrincipalType
    scopes: list[str]


class ServerInfo(BaseModel):
    """Versions, enabled features, OpenMetadata capabilities, sources and models."""

    name: Literal["lakeloom"] = "lakeloom"
    version: str = Field(description="Lakeloom version.")
    environment: str = Field(description="Deployment environment (development or production).")
    openmetadata: OMCapabilities = Field(description="OpenMetadata version and RDF status.")
    features: dict[str, bool] = Field(
        description=(
            "Optional features (triplestore, stdio, token_exchange, releases, import, drift, "
            "sparql) as configuration flags, not live capabilities: a flag can be on before "
            "the feature ships. `releases` is on with Git publishing (create_release, "
            "list_releases, ?version= reads); `import` (import_model, and import_from_git "
            "with Git publishing) and `drift` (detect_drift) are always on. `sparql` "
            "(sparql_query, POST /v1/models/{slug}/sparql) is on with OpenMetadata 2.1 or "
            "later, and off once OpenMetadata answered 404 for its glossary SPARQL endpoint."
        )
    )
    git: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Git publishing (FR-GIT-04): enabled, mode, pending and blocked publication counts "
            "and the queue head's last error. Never carries the remote URL or credentials."
        ),
    )
    triplestore: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Triple-store sink (FR-TS): enabled, pending, failed and done release counts and "
            "the last error. Never carries the endpoint URL or the password."
        ),
    )
    jobs: dict[str, Any] = Field(
        default_factory=dict[str, Any],
        description=(
            "Background jobs of this replica (NFR-DEP-07), by name (git-publisher, "
            "triplestore-sink, reconcile, drift-schedule; only the configured ones): `held` is "
            "whether this replica held the job's lease at its last renewal (renewed once per "
            "interval, so it can lag a takeover by one interval), `last_run_at` the end of its "
            "last run here and `last_error` that run's error class. `reconcile` adds the last "
            "reconciliation of OpenMetadata with Git (FR-GIT-08: `last_run`, `counts` per "
            "status, the `drifted` model slugs) and `drift` is the last scheduled drift check "
            "(FR-PRO-07: tables changed, added or removed per model, error codes). Slugs and "
            "counts only, never term text."
        ),
    )
    review_identity: OboMode = Field(
        description=(
            "How human review reaches OpenMetadata: 'exchange' (RFC 8693), 'forward' (the "
            "caller's token) or 'unavailable' (review fails closed)."
        )
    )
    sources: list[SourceStatus] = Field(
        description="Configured lake sources with kind, capabilities and availability."
    )
    models: list[str] = Field(description="Registered semantic models (none in M0).")
    caller: CallerInfo | None = Field(description="Who is calling, with granted scopes.")


async def build_server_info(deps: AppDeps, principal: Principal | None) -> ServerInfo:
    settings = deps.settings
    caller = (
        None
        if principal is None
        else CallerInfo(
            subject=principal.subject,
            type=principal.type,
            scopes=sorted(principal.scopes),
        )
    )
    capabilities = await deps.om.get_capabilities()
    # SPARQL (FR-PUB-03): from the detected OM version, then off for good once a real query
    # met a 404 for the endpoint itself (SparqlService.endpoint_missing, cached there). No
    # probe query is sent: a 404 for a made-up glossary would not tell the two apart.
    sparql = sparql_available(capabilities.version) and not deps.sparql_service.endpoint_missing
    return ServerInfo(
        version=lakeloom.__version__,
        environment=settings.env,
        openmetadata=capabilities,
        features={
            "triplestore": settings.triplestore_enabled,
            "stdio": bool(settings.stdio_enabled),
            "token_exchange": settings.oidc_token_exchange,
            "releases": deps.release_service is not None,
            "import": True,
            "drift": True,
            "sparql": sparql,
            "mcp_stateless": settings.mcp_stateless,
        },
        git=await _git_status(deps),
        triplestore=await deps.sink_worker.status() if deps.sink_worker is not None else None,
        jobs=_jobs(deps),
        review_identity=deps.obo.mode,
        sources=[source_status(n, s) for n, s in deps.sources.items()],
        models=[],
        caller=caller,
    )


async def _git_status(deps: AppDeps) -> dict[str, Any]:
    """``GitPublisher.status()`` (no remote URL); a store failure is reported, not raised."""
    try:
        return await deps.publisher.status()
    except Exception as exc:
        return {
            "enabled": deps.publisher.enabled,
            "mode": deps.settings.git_mode,
            "error": f"publication queue unavailable ({type(exc).__name__})",
        }


def _jobs(deps: AppDeps) -> dict[str, Any]:
    """Job states and the reconcile and drift summaries. ``git-publisher:<repo key>`` is shown
    as ``git-publisher``: the key is a hash of the remote URL, which is never shown."""
    out: dict[str, Any] = {}
    for name, status in deps.jobs.status().items():
        key = "git-publisher" if name.startswith("git-publisher:") else name
        out[key] = {
            "held": status.held,
            "last_run_at": status.last_run_at.isoformat() if status.last_run_at else None,
            "last_error": status.last_error,
        }
    service = deps.reconcile_service
    if service is not None and service.last_run is not None:
        out.setdefault("reconcile", {}).update(service.last_run.summary())
    if deps.drift_schedule.last_run is not None:
        out["drift"] = deps.drift_schedule.last_run.summary()
    return out


def summarize(info: ServerInfo) -> str:
    """Short Markdown summary that accompanies the structured content (FR-AGT-02)."""
    om = info.openmetadata
    if om.reachable:
        rdf = {True: "enabled", False: "disabled", None: "unknown"}[om.rdf_enabled]
        om_line = f"OpenMetadata {om.version or 'unknown'} reachable, RDF {rdf}"
        if om.error:
            om_line += f" (warning: {om.error})"
    else:
        om_line = f"OpenMetadata **unreachable**: {om.error or 'unknown error'}"
    enabled = ", ".join(k for k, v in info.features.items() if v) or "none"
    lines = [
        f"**Lakeloom {info.version}** ({info.environment})",
        f"- {om_line}",
        f"- Features enabled: {enabled}",
        f"- Sources: {len(info.sources)} ({sum(s.available for s in info.sources)} available), "
        f"models: {len(info.models)}",
    ]
    git = info.git
    if git is not None and git.get("enabled"):
        lines.append(
            f"- Git: {git.get('mode')} mode, {git.get('pending', 0)} pending, "
            f"{git.get('blocked', 0)} blocked"
        )
    else:
        lines.append("- Git: off")
    ts = info.triplestore
    if ts is not None and ts.get("enabled"):
        lines.append(
            f"- Triple store: {ts.get('pending', 0)} pending, {ts.get('failed', 0)} failed"
        )
    if info.jobs:
        names = ", ".join(sorted(k for k in info.jobs if k != "drift"))
        line = f"- Jobs: {names}"
        last = info.jobs.get("reconcile", {})
        if "counts" in last:
            line += f" (last reconciliation: {last['counts'].get('drifted', 0)} drifted)"
        lines.append(line)
    if info.caller is not None:
        scopes = ", ".join(info.caller.scopes) or "none"
        lines.append(f"- Caller: {info.caller.type} `{info.caller.subject}` (scopes: {scopes})")
    return "\n".join(lines)
