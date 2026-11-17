"""``lakeloom sources check`` for upstream MCP mappings (FR-SRC-05).

Everything runs against in-process fake MCP servers. The example tests prove the shipped
examples are internally consistent (their arguments and JMESPath fit one another), not that
they match a real Databricks or Snowflake MCP server.
"""

from pathlib import Path
from typing import Any

import pytest
from mcp.types import Tool
from typer.testing import CliRunner

from lakeloom.cli import app
from lakeloom.sources.config import McpSourceConfig
from lakeloom.sources.mcp_check import CheckReport, check_mcp_source, static_checks
from lakeloom.sources.mcp_mapping import load_mapping, parse_mapping

from .fake_upstream import EXAMPLES, ToolsFake, example_tools, filled_example, tools_factory

FIXTURE_LAKE = Path(__file__).parents[2] / "fixtures" / "lake"


def config(**overrides: Any) -> McpSourceConfig:
    values: dict[str, Any] = {
        "type": "mcp",
        "name": "up",
        "transport": "streamable_http",
        "url": "http://upstream.test/mcp",
        "mapping": Path("/unused.yaml"),
    }
    values.update(overrides)
    return McpSourceConfig(**values)


def tool(name: str, schema: dict[str, Any]) -> Tool:
    return Tool(name=name, input_schema=schema)


def describe(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool": tool_name,
        "arguments": arguments,
        "result": {"fields": {"columns": "columns[].{name: name, data_type: type, position: pos}"}},
    }


def test_check_reports_ok_missing_and_mismatch() -> None:
    mapping = parse_mapping(
        {
            "version": 1,
            "operations": {
                "list_catalogs": {
                    "tool": "cats",
                    "result": {"items": "c", "fields": {"name": "n"}},
                },
                "list_schemas": {
                    "tool": "gone",
                    "arguments": {"catalog": "{catalog}"},
                    "result": {"items": "s", "fields": {"name": "n"}},
                },
                "describe_table": describe("desc", {"fqn": "{full_name}", "extra": 1}),
            },
        },
        where="test",
    )
    tools = {
        "cats": tool("cats", {"type": "object", "properties": {}}),
        "desc": tool(
            "desc",
            {
                "type": "object",
                "properties": {"fqn": {}, "entity": {}},
                "required": ["fqn", "entity"],
            },
        ),
    }
    checks = {c.operation: c for c in static_checks(mapping, tools)}
    assert checks["list_catalogs"].status == "ok"
    assert checks["list_schemas"].status == "missing"
    mismatch = checks["describe_table"]
    assert mismatch.status == "schema_mismatch"
    assert mismatch.problems == [
        "argument 'extra' is not in the tool's input schema",
        "required argument 'entity' is not mapped",
    ]


@pytest.mark.parametrize(
    "schema",
    [{}, {"type": "object"}, {"type": "object", "properties": {}}],
)
def test_check_schema_without_properties_is_ok(schema: dict[str, Any]) -> None:
    mapping = parse_mapping(
        {"version": 1, "operations": {"describe_table": describe("desc", {"fqn": "{full_name}"})}},
        where="test",
    )
    (check,) = static_checks(mapping, {"desc": tool("desc", schema)})
    assert check.status == "ok"


def test_check_closed_schema_with_no_properties_flags_arguments() -> None:
    mapping = parse_mapping(
        {"version": 1, "operations": {"describe_table": describe("desc", {"fqn": "{full_name}"})}},
        where="test",
    )
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    (check,) = static_checks(mapping, {"desc": tool("desc", schema)})
    assert check.status == "schema_mismatch"


@pytest.mark.parametrize("name", ["databricks.example.yaml", "snowflake.example.yaml"])
async def test_examples_report_placeholders(name: str) -> None:
    mapping = load_mapping(EXAMPLES / name)
    fake = ToolsFake({"unrelated": lambda: {}})
    report = await check_mcp_source(config(), mapping, session_factory=tools_factory(fake))
    assert report.connected
    assert {c.status for c in report.operations} == {"placeholder"}
    assert not report.ok


@pytest.mark.parametrize("name", ["databricks.example.yaml", "snowflake.example.yaml"])
async def test_examples_are_internally_consistent_with_call(name: str, tmp_path: Path) -> None:
    """Consistency of the example with itself; says nothing about a real server's tools."""
    fake = ToolsFake(example_tools(name))
    report = await check_mcp_source(
        config(), filled_example(name, tmp_path), call=True, session_factory=tools_factory(fake)
    )
    assert [c.status for c in report.operations] == ["ok"] * len(report.operations)
    assert [(c.operation, c.status) for c in report.calls] == [
        ("list_catalogs", "ok"),
        ("list_schemas", "ok"),
        ("list_tables", "ok"),
        ("describe_table", "ok"),
    ]
    assert "2 columns" in (report.calls[-1].detail or "")
    assert report.ok


async def test_check_call_skips_after_empty_list(tmp_path: Path) -> None:
    tools = example_tools("databricks.example.yaml")
    tools["fake_list_catalogs"] = lambda: {"catalogs": []}
    fake = ToolsFake(tools)
    report = await check_mcp_source(
        config(),
        filled_example("databricks.example.yaml", tmp_path),
        call=True,
        session_factory=tools_factory(fake),
    )
    statuses = [(c.operation, c.status) for c in report.calls]
    assert statuses == [
        ("list_catalogs", "ok"),
        ("list_schemas", "skipped"),
        ("list_tables", "skipped"),
        ("describe_table", "skipped"),
    ]
    assert report.ok  # skipped is not an error


async def test_check_call_reports_adapter_errors(tmp_path: Path) -> None:
    tools = example_tools("databricks.example.yaml")
    tools["fake_list_schemas"] = lambda catalog: {"wrong_key": []}
    fake = ToolsFake(tools)
    report = await check_mcp_source(
        config(),
        filled_example("databricks.example.yaml", tmp_path),
        call=True,
        session_factory=tools_factory(fake),
    )
    by_op = {c.operation: c for c in report.calls}
    assert by_op["list_schemas"].status == "error"
    assert (by_op["list_schemas"].detail or "").startswith("upstream_unavailable:")
    assert not report.ok


async def test_check_connection_failure() -> None:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def refuse(_config: McpSourceConfig, _token: str | None):  # type: ignore[no-untyped-def]
        raise ConnectionRefusedError("nope")
        yield  # pragma: no cover

    mapping = load_mapping(EXAMPLES / "databricks.example.yaml")
    report = await check_mcp_source(config(), mapping, session_factory=refuse)
    assert not report.connected and not report.ok
    assert report.operations == []
    assert "ConnectionRefusedError" in (report.error or "")


async def test_check_lists_tools_on_request() -> None:
    def search(query: str) -> dict[str, Any]:
        """Search the catalog."""
        return {}

    fake = ToolsFake({"search": search})
    mapping = load_mapping(EXAMPLES / "databricks.example.yaml")
    report = await check_mcp_source(
        config(), mapping, include_tools=True, session_factory=tools_factory(fake)
    )
    (info,) = report.tools
    assert info.name == "search" and info.description == "Search the catalog."
    assert "query" in info.input_schema["properties"]
    assert CheckReport.model_validate_json(report.model_dump_json()).tools == report.tools


async def test_check_call_stops_when_list_catalogs_is_not_ok(tmp_path: Path) -> None:
    tools = example_tools("databricks.example.yaml")
    del tools["fake_list_catalogs"]  # mapped, but the server lacks it: missing
    fake = ToolsFake(tools)
    report = await check_mcp_source(
        config(),
        filled_example("databricks.example.yaml", tmp_path),
        call=True,
        session_factory=tools_factory(fake),
    )
    assert [(c.operation, c.status) for c in report.calls] == [
        ("list_catalogs", "skipped"),
        ("list_schemas", "skipped"),
        ("list_tables", "skipped"),
        ("describe_table", "skipped"),
    ]


def _mcp_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, filled: bool, token: bool = False
) -> list[str | None]:
    """A sources file with the Delta lake and one MCP source on the Databricks example."""
    name = "databricks.example.yaml"
    if filled:
        filled_example(name, tmp_path)
        mapping = tmp_path / name
    else:
        mapping = EXAMPLES / name
    config = tmp_path / "sources.yaml"
    token_line = "    token_env: UP_TOKEN\n" if token else ""
    config.write_text(
        "sources:\n  - type: delta\n    name: lake\n    tables:\n"
        + f"      - {{uri: {(FIXTURE_LAKE / 'orders').as_uri()}, catalog: retail, schema: sales}}\n"
        + "  - type: mcp\n    name: up\n    transport: streamable_http\n"
        + f"    url: http://upstream.test/mcp\n    mapping: {mapping}\n{token_line}"
    )
    for key, value in {
        "LAKELOOM_CONFIG_FILE": str(config),
        "LAKELOOM_DATA_DIR": str(tmp_path),
        "LAKELOOM_DB_URL": f"sqlite:///{tmp_path}/l.db",
        "LAKELOOM_LOG_LEVEL": "WARNING",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("LAKELOOM_OM_URL", raising=False)
    monkeypatch.setattr("lakeloom.cli.configure_logging", lambda *a, **k: None)
    tokens: list[str | None] = []
    factory = tools_factory(ToolsFake(example_tools(name)), tokens)
    monkeypatch.setattr("lakeloom.sources.mcp_adapter.default_session_factory", factory)
    return tokens


def test_cli_sources_check_exit_codes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = CliRunner()
    _mcp_env(monkeypatch, tmp_path, filled=True)
    ok = runner.invoke(app, ["sources", "check", "up", "--call"])
    assert ok.exit_code == 0, ok.output
    assert "describe_table\tfake_describe_table\tok" in ok.output
    as_json = runner.invoke(app, ["sources", "check", "up", "--call", "--json"])
    assert as_json.exit_code == 0, as_json.output
    report = CheckReport.model_validate_json(as_json.output)
    assert report.ok and len(report.calls) == 4

    _mcp_env(monkeypatch, tmp_path, filled=False)
    placeholder = runner.invoke(app, ["sources", "check", "up"])
    assert placeholder.exit_code == 3, placeholder.output
    assert "<tool-from-tools/list>\tplaceholder" in placeholder.output

    unknown = runner.invoke(app, ["sources", "check", "ghost"])
    assert unknown.exit_code == 1
    assert "error not_found" in unknown.output

    delta = runner.invoke(app, ["sources", "check", "lake", "--call"])
    assert delta.exit_code == 0, delta.output
    assert "list_catalogs\tok" in delta.output


def test_cli_sources_check_tools_listing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mcp_env(monkeypatch, tmp_path, filled=False)
    result = CliRunner().invoke(app, ["sources", "check", "up", "--tools"])
    assert result.exit_code == 3  # the shipped example still has placeholders
    assert "tool\tfake_list_tables\t" in result.output


def test_check_never_prints_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tokens = _mcp_env(monkeypatch, tmp_path, filled=True, token=True)
    monkeypatch.setenv("UP_TOKEN", "tok-xyz")
    result = CliRunner().invoke(app, ["sources", "check", "up", "--call", "--tools", "--json"])
    assert result.exit_code == 0, result.output
    assert "tok-xyz" in tokens  # the token reached the upstream
    assert "tok-xyz" not in result.output
