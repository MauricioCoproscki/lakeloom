"""Prometheus metrics (NFR-OPS-02). Kept apart from ``server/`` so ``om/`` can import them.

A dedicated registry avoids duplicate-registration errors when apps are built repeatedly
(tests) and keeps default process collectors out of the exposition.
"""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry()

HTTP_REQUESTS = Counter(
    "lakeloom_http_requests_total",
    "HTTP requests handled.",
    ["method", "route", "status"],
    registry=REGISTRY,
)
TOOL_CALLS = Counter(
    "lakeloom_tool_calls_total",
    "MCP tool calls.",
    ["tool", "outcome"],
    registry=REGISTRY,
)
RATE_LIMITED = Counter(
    "lakeloom_rate_limited_total",
    "Requests refused by the per-principal rate limit (NFR-SEC-16), by surface (mcp, http).",
    ["surface"],
    registry=REGISTRY,
)
RESOURCE_READS = Counter(
    "lakeloom_resource_reads_total",
    "MCP resource reads.",
    ["resource", "outcome"],
    registry=REGISTRY,
)
OM_REQUEST_SECONDS = Histogram(
    "lakeloom_om_request_seconds",
    "OpenMetadata request latency by operation.",
    ["operation"],
    registry=REGISTRY,
)
SCAN_SECONDS = Histogram(
    "lakeloom_scan_duration_seconds",
    "Lake source scan duration by outcome (completed, partial, failed, error).",
    ["source", "outcome"],
    registry=REGISTRY,
)
GIT_PUBLICATIONS = Counter(
    "lakeloom_git_publications_total",
    "Git publication attempts by result (done, retry, blocked).",
    ["result"],
    registry=REGISTRY,
)
GIT_PUSH_FAILURES = Counter(
    "lakeloom_git_push_failures_total",
    "Failed git push invocations (NFR-OPS-02).",
    registry=REGISTRY,
)
RECONCILE_RUNS = Counter(
    "lakeloom_reconcile_runs_total",
    "Reconciliation runs (FR-GIT-08) by outcome (in_sync, drifted, error).",
    ["outcome"],
    registry=REGISTRY,
)
RECONCILE_DRIFTED = Gauge(
    "lakeloom_reconcile_drifted_models",
    "Models whose OpenMetadata state differed from Git at the last reconciliation run.",
    registry=REGISTRY,
)
