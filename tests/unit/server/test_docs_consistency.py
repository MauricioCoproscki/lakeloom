"""The documentation matches what the server implements.

Every MCP tool, prompt, resource template and HTTP route must appear in the MCP reference,
and every tool-like name the docs mention in backticks must exist. Renaming a tool without
updating the docs, or documenting a tool that was never built, fails here.
"""

import re
from pathlib import Path

from kit import DepsFactory
from test_tools_semantic import fake_deps

from lakeloom.server.http_app import create_app
from lakeloom.server.mcp_app import create_mcp

ROOT = Path(__file__).parents[3]
REFERENCE = ROOT / "docs/guide/reference/README.md"
USER_DOCS = [
    ROOT / "README.md",
    *sorted((ROOT / "docs/guide").rglob("*.md")),
    ROOT / "docs/ontology-pipeline-guide.md",
    ROOT / "docs/technical-reference.md",
]
# Names in backticks that look like tools but are not: arguments, ops, fields, CLI, files.
_TOOL_PREFIXES = (
    "get_", "list_", "find_", "explain_", "export_", "validate_", "assess_", "suggest_",
    "harvest_", "propose_", "submit_", "discard_", "rebase_", "review_", "create_",
    "describe_", "scan_", "build_", "detect_", "import_", "sparql_", "expand_", "diff_",
)  # fmt: skip
_NOT_TOOLS = {
    "get_asset", "list_by_status", "create_app", "build_ontology", "create_change_set",
    "review_groups", "propose_groups", "import_terms", "list_terms", "scan_id",
    "export_map_json", "export_map_markdown", "build_fqn", "build_server_info",
    "review_identity",  # a get_server_info field
    "create_entity",  # a tool of OpenMetadata's own MCP server
}  # fmt: skip


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


async def _registered(make_deps: DepsFactory) -> tuple[set[str], set[str], list[str]]:
    mcp = create_mcp(fake_deps(make_deps))
    tools = {t.name for t in await mcp.list_tools()}
    prompts = {p.name for p in await mcp.list_prompts()}
    templates = [t.uri_template for t in await mcp.list_resource_templates()]
    return tools, prompts, templates


async def test_every_tool_and_prompt_is_in_the_reference(make_deps: DepsFactory) -> None:
    tools, prompts, templates = await _registered(make_deps)
    ref = _text(REFERENCE)
    missing = sorted(n for n in tools | prompts if f"`{n}`" not in ref and f"`{n}(" not in ref)
    assert not missing, f"document in {REFERENCE.name}: {missing}"
    lacking = [t for t in templates if t not in ref]
    assert not lacking, f"resource templates missing from the reference: {lacking}"


def test_every_http_route_is_documented(make_deps: DepsFactory) -> None:
    app = create_app(fake_deps(make_deps))
    routes = {
        r.path  # pyright: ignore[reportAttributeAccessIssue]
        for r in app.routes
        if getattr(r, "path", "").startswith("/v1/")
    }
    docs = "\n".join(_text(p) for p in USER_DOCS)
    missing = sorted(r for r in routes if r not in docs)
    assert not missing, f"HTTP routes not documented: {missing}"


async def test_docs_name_only_real_tools(make_deps: DepsFactory) -> None:
    tools, prompts, _ = await _registered(make_deps)
    bad: dict[str, list[str]] = {}
    for path in USER_DOCS:
        for name in set(re.findall(r"`([a-z]+(?:_[a-z]+)+)(?:\(|`)", _text(path))):
            if not name.startswith(_TOOL_PREFIXES) or name in _NOT_TOOLS:
                continue
            if name not in tools and name not in prompts:
                bad.setdefault(str(path.relative_to(ROOT)), []).append(name)
    assert not bad, f"docs mention tools that do not exist: {bad}"


def test_documented_settings_exist() -> None:
    from lakeloom.config import Settings

    fields = {f.upper() for f in Settings.model_fields}
    docs = "\n".join(_text(p) for p in USER_DOCS)
    named = set(re.findall(r"LAKELOOM_([A-Z0-9_]+)", docs))
    unknown = sorted(
        n
        for n in named
        if n not in fields
        and n.removesuffix("_FILE") not in fields
        and not n.endswith("_")
        and n not in {"SVC", "READER", "WH"}  # Snowflake user/role/warehouse in an example
    )
    assert not unknown, f"docs name settings that do not exist: {unknown}"


def test_documented_cli_commands_exist() -> None:
    import typer.main

    from lakeloom.cli import app

    group = typer.main.get_command(app)
    names = set(getattr(group, "commands", {}))
    sub = {
        f"{n} {s}"
        for n, cmd in getattr(group, "commands", {}).items()
        for s in getattr(cmd, "commands", {})
    }
    # the pipeline guide's sections 3-9 are a design proposal (it says so); skip them
    docs = "\n".join(_text(p) for p in USER_DOCS if p.name != "ontology-pipeline-guide.md")
    used = set(re.findall(r"`lakeloom ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?", docs))
    bad = sorted(
        f"{a} {b}".strip()
        for a, b in used
        if a not in names or (b and a in {"git", "release"} and f"{a} {b}" not in sub)
    )
    assert not bad, f"docs name CLI commands that do not exist: {bad}"


def test_reference_scopes_match_the_code() -> None:
    src = ROOT / "src/lakeloom/server"
    actual: dict[str, str] = {}
    for f in src.glob("tools_*.py"):
        for name, scope in re.findall(
            r'guarded\(\s*"([a-z_]+)",\s*ctx,\s*([A-Z]+)_SCOPE', f.read_text(encoding="utf-8")
        ):
            actual[name] = scope.lower()
    documented = dict(
        re.findall(
            r"^\| `([a-z_]+)`[^|]*\| (read|propose|review|publish|admin) \|", _text(REFERENCE), re.M
        )
    )
    wrong = {n: (d, actual[n]) for n, d in documented.items() if n in actual and actual[n] != d}
    assert not wrong, f"reference scope differs from the code (doc, code): {wrong}"
