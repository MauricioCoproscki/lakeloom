"""Verify an upstream MCP mapping against the server it targets (FR-SRC-05).

``lakeloom sources check`` runs this. Static checks compare each mapped operation with the
server's ``tools/list``: a tool name still a ``<…>`` placeholder, a tool the server does not
have, or arguments that do not fit the tool's input schema. With ``call`` the read operations
run once, in order, through :class:`McpUpstreamSource` itself, so the JMESPath and validation
code exercised is the production code. ``get_table_version`` and ``profile_table`` are never
called. Nothing here prints or stores the bearer token.
"""

import re
from collections.abc import Mapping, Set
from typing import Any, Literal, cast

from mcp.types import Tool
from pydantic import BaseModel

from lakeloom.errors import LakeloomError
from lakeloom.sources import mcp_adapter
from lakeloom.sources.config import McpSourceConfig
from lakeloom.sources.mcp_adapter import McpUpstreamSource, SessionFactory
from lakeloom.sources.mcp_mapping import McpMapping, OperationMapping, OperationName
from lakeloom.sources.models import TableRef

__all__ = [
    "CallCheck",
    "CheckReport",
    "CheckStatus",
    "OperationCheck",
    "ToolInfo",
    "check_mcp_source",
    "static_checks",
]

CheckStatus = Literal["ok", "placeholder", "missing", "schema_mismatch"]

_PLACEHOLDER = re.compile(r"^<.*>$")


class OperationCheck(BaseModel):
    operation: OperationName
    tool: str
    status: CheckStatus
    problems: list[str] = []


class CallCheck(BaseModel):
    operation: OperationName
    status: Literal["ok", "error", "skipped"]
    detail: str | None = None


class ToolInfo(BaseModel):
    name: str
    description: str | None
    input_schema: dict[str, Any]


class CheckReport(BaseModel):
    source: str
    kind: str
    connected: bool
    error: str | None = None
    operations: list[OperationCheck] = []
    calls: list[CallCheck] = []
    tools: list[ToolInfo] = []

    @property
    def ok(self) -> bool:
        return (
            self.connected
            and self.error is None
            and all(c.status == "ok" for c in self.operations)
            and all(c.status != "error" for c in self.calls)
        )


def _schema_problems(op: OperationMapping, schema: Mapping[str, Any]) -> list[str]:
    """Arguments against the input schema; no declared properties means nothing to check."""
    properties = schema.get("properties")
    closed = schema.get("additionalProperties") is False
    if not isinstance(properties, Mapping) or (not properties and not closed):
        return []
    declared = set(cast("Mapping[str, object]", properties))
    required = schema.get("required")
    required_names = (
        [str(r) for r in cast("list[object]", required)] if isinstance(required, list) else []
    )
    problems = [
        f"argument '{name}' is not in the tool's input schema"
        for name in op.arguments
        if name not in declared
    ]
    problems += [
        f"required argument '{name}' is not mapped"
        for name in required_names
        if name not in op.arguments
    ]
    return problems


def static_checks(mapping: McpMapping, tools: Mapping[str, Tool]) -> list[OperationCheck]:
    out: list[OperationCheck] = []
    for name, op in mapping.operations.items():
        if _PLACEHOLDER.match(op.tool):
            out.append(OperationCheck(operation=name, tool=op.tool, status="placeholder"))
        elif op.tool not in tools:
            out.append(OperationCheck(operation=name, tool=op.tool, status="missing"))
        else:
            problems = _schema_problems(op, tools[op.tool].input_schema)
            status: CheckStatus = "schema_mismatch" if problems else "ok"
            out.append(
                OperationCheck(operation=name, tool=op.tool, status=status, problems=problems)
            )
    return out


def _error(exc: LakeloomError) -> str:
    return f"{exc.code}: {exc.message} (hint: {exc.hint})"


async def _calls(source: McpUpstreamSource, ready: Set[str], mapped: Set[str]) -> list[CallCheck]:
    """list_catalogs → list_schemas → list_tables → describe_table, each fed by the last."""
    out: list[CallCheck] = []
    catalog: str | None = None
    schema: str | None = None
    table: TableRef | None = None
    blocked: str | None = None  # why the rest of the chain cannot run

    def skip(op: OperationName, why: str) -> None:
        out.append(CallCheck(operation=op, status="skipped", detail=why))

    # list_catalogs is optional: a schema-only mapping lists schemas with catalog None.
    if "list_catalogs" in mapped and "list_catalogs" not in ready:
        skip("list_catalogs", "not ok")
        blocked = "list_catalogs is not ok"
    elif "list_catalogs" in ready:
        try:
            catalogs = await source.list_catalogs()
            out.append(
                CallCheck(
                    operation="list_catalogs", status="ok", detail=f"{len(catalogs)} catalogs"
                )
            )
            if catalogs:
                catalog = catalogs[0]
            else:
                blocked = "list_catalogs returned no catalog"
        except LakeloomError as exc:
            out.append(CallCheck(operation="list_catalogs", status="error", detail=_error(exc)))
            blocked = "list_catalogs failed"
    if blocked:
        skip("list_schemas", blocked)
    elif "list_schemas" not in ready:
        skip("list_schemas", "not mapped, or not ok")
        blocked = "list_schemas did not run"
    else:
        try:
            schemas = await source.list_schemas(catalog)
            detail = f"{len(schemas)} schemas"
            out.append(CallCheck(operation="list_schemas", status="ok", detail=detail))
            schema = schemas[0] if schemas else None
            if schema is None:
                blocked = "list_schemas returned no schema"
        except LakeloomError as exc:
            out.append(CallCheck(operation="list_schemas", status="error", detail=_error(exc)))
            blocked = "list_schemas failed"
    if blocked or schema is None:
        skip("list_tables", blocked or "no schema")
    elif "list_tables" not in ready:
        skip("list_tables", "not mapped, or not ok")
        blocked = "list_tables did not run"
    else:
        try:
            tables = await source.list_tables(catalog, schema)
            detail = f"{len(tables)} tables"
            out.append(CallCheck(operation="list_tables", status="ok", detail=detail))
            table = tables[0] if tables else None
            if table is None:
                blocked = "list_tables returned no table"
        except LakeloomError as exc:
            out.append(CallCheck(operation="list_tables", status="error", detail=_error(exc)))
            blocked = "list_tables failed"
    if blocked or table is None:
        skip("describe_table", blocked or "no table")
    elif "describe_table" not in ready:
        skip("describe_table", "not mapped, or not ok")
    else:
        try:
            described = await source.describe_table(table)
            detail = f"{table.full_name}: {len(described.columns)} columns"
            out.append(CallCheck(operation="describe_table", status="ok", detail=detail))
        except LakeloomError as exc:
            out.append(CallCheck(operation="describe_table", status="error", detail=_error(exc)))
    return out


async def check_mcp_source(
    config: McpSourceConfig,
    mapping: McpMapping,
    *,
    call: bool = False,
    include_tools: bool = False,
    session_factory: SessionFactory | None = None,
) -> CheckReport:
    factory = session_factory or mcp_adapter.default_session_factory
    report = CheckReport(source=config.name, kind="mcp", connected=False)
    probe = McpUpstreamSource(config, mapping, session_factory=factory)
    try:
        tools = await probe.list_upstream_tools()
    except LakeloomError as exc:
        report.error = _error(exc)
        return report
    report.connected = True
    report.operations = static_checks(mapping, tools)
    if include_tools:
        report.tools = [
            ToolInfo(name=t.name, description=t.description, input_schema=dict(t.input_schema))
            for t in sorted(tools.values(), key=lambda t: t.name)
        ]
    if call:
        ready = {c.operation for c in report.operations if c.status == "ok"}
        runnable = McpMapping(
            version=1, operations={k: v for k, v in mapping.operations.items() if k in ready}
        )
        source = McpUpstreamSource(config, runnable, session_factory=factory)
        await source.start()
        try:
            if not source.available:
                detail = _error(source.last_error) if source.last_error else "unavailable"
                report.calls = [
                    CallCheck(operation=op, status="error", detail=detail)
                    for op in ("list_catalogs", "list_schemas", "list_tables", "describe_table")
                    if op in ready
                ]
            else:
                report.calls = await _calls(source, ready, set(mapping.operations))
        finally:
            await source.aclose()
    return report
