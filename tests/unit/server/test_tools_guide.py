"""The next_steps MCP tool: state gathering, caller rights, and the end-to-end call."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from kit import AuthKit, DepsFactory, mcp_session
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, SKOS
from test_tools_semantic import fake_deps, structured, text

from lakeloom.core.callers import Actor
from lakeloom.core.map_profile import MapIssue, MapReport
from lakeloom.core.model_policy import ModelPolicyEntry
from lakeloom.server import tools_guide
from lakeloom.server.mcp_app import create_mcp, create_server_app
from lakeloom.server.tools_guide import gather_state, guide_caller

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
EX = "http://x/retail/"


def proposal(pid: str, status: str, author: str = "carol", days: int = 0, scan: str | None = None):
    return SimpleNamespace(
        id=pid,
        status=status,
        scan_id=scan,
        updated_at=NOW - timedelta(days=days),
        provenance=SimpleNamespace(principal_subject=author),
    )


def graph(n: int, *, defined: int = 0, linked: bool = True) -> Graph:
    g = Graph()
    for i in range(n):
        c = URIRef(f"{EX}C{i}")
        g.add((c, RDF.type, SKOS.Concept))
        g.add((c, SKOS.prefLabel, Literal(f"C{i}", lang="en")))
        if i < defined:
            g.add((c, SKOS.definition, Literal("d", lang="en")))
        if linked and i:
            g.add((c, SKOS.broader, URIRef(f"{EX}C0")))
    return g


class Repo:
    def __init__(self, proposals: list[Any]) -> None:
        self.proposals = proposals

    async def list_by_status(self, statuses, model, cursor, limit):
        return [p for p in self.proposals if p.status in statuses], None


def deps(
    *,
    proposals: list[Any] | None = None,
    errors: list[str] | None = None,
    released_at: datetime | None = None,
    git: bool = True,
    sink_failed: int | None = None,
    drift: dict[str, int] | None = None,
    policies: list[ModelPolicyEntry] | None = None,
    broken: frozenset[str] = frozenset(),
) -> Any:
    async def get_model(model: str) -> None:
        return None

    async def validation(model: str) -> Any:
        if "validation" in broken:
            raise RuntimeError("OM down")
        issues = [SimpleNamespace(severity="error", code=c, subject="s") for c in errors or []]
        return SimpleNamespace(issues=issues)

    async def reader_list(model: str) -> list[Any]:
        return [SimpleNamespace(released_at=released_at)] if released_at else []

    async def sink_counts(repo: str) -> dict[str, int]:
        return {"failed": sink_failed or 0}

    async def scan_get(scan_id: str) -> Any:
        return SimpleNamespace(id=scan_id)

    return SimpleNamespace(
        models=SimpleNamespace(get_model=get_model, validation=validation),
        proposal_repo=Repo(proposals or []),
        reader=SimpleNamespace(list=reader_list) if git else None,
        sink_worker=SimpleNamespace(repo_key="r") if sink_failed is not None else None,
        releases_repo=SimpleNamespace(sink_counts=sink_counts),
        drift_schedule=SimpleNamespace(
            last_run=SimpleNamespace(tables=drift) if drift is not None else None
        ),
        scans=SimpleNamespace(get=scan_get),
        policies=policies or [],
    )


@pytest.fixture
def analytics(monkeypatch: pytest.MonkeyPatch):
    """Replace the OM-backed loader and the MAP with fixed values per test."""
    box: dict[str, Any] = {"graph": graph(30, defined=30), "real": {}, "map": MapReport()}

    async def load(deps: Any, model: str) -> tuple[Graph, dict[str, list[str]]]:
        if box.get("broken"):
            raise RuntimeError("OM down")
        return box["graph"], box["real"]

    monkeypatch.setattr(tools_guide, "load_graph_and_realizations", load)
    monkeypatch.setattr(tools_guide, "build_map", lambda *a, **k: None)
    monkeypatch.setattr(tools_guide, "validate_map", lambda _: box["map"])
    return box


async def test_gather_reads_every_source(analytics: dict[str, Any]) -> None:
    analytics["real"] = {f"{EX}C{i}": ["t"] for i in range(10)}
    analytics["map"] = MapReport(
        total_columns=10,
        mapped_columns=4,
        issues=[MapIssue("warning", "MAP-02", "m", "svc.db.s.orders")],
    )
    d = deps(
        proposals=[
            proposal("p1", "changes_requested", "alice", 2),
            proposal("p2", "submitted", "carol", 4),
            proposal("p3", "draft", "alice", 9),
            proposal("p4", "applied", days=1, scan="scan_01"),
            proposal("p5", "applied", days=20, scan="scan_02"),
        ],
        errors=["cycle"],
        released_at=NOW - timedelta(days=5),
        sink_failed=1,
        drift={"retail": 3},
    )
    state, unavailable = await gather_state(d, "retail", now=NOW)
    assert unavailable == []
    assert state.structural_errors == ["cycle: s"]
    assert [p.id for p in state.changes_requested] == ["p1"]
    assert state.submitted[0].age_days == 4 and state.drafts[0].author == "alice"
    assert state.has_release and state.unreleased_changes == 1  # p4 only: p5 predates it
    assert state.sink_failed == 1 and state.drift is not None and state.drift.total == 3
    assert state.concepts == 30 and state.defined_concepts == 30 and state.realized_concepts == 10
    assert state.max_depth == 1 and state.orphan_concepts == 0
    assert state.latest_scan_id == "scan_02"
    assert (state.total_columns, state.mapped_columns) == (10, 4)
    assert state.unmapped_tables == ["svc.db.s.orders"]


async def test_git_off_and_no_sink_skip_those_signals(analytics: dict[str, Any]) -> None:
    state, unavailable = await gather_state(deps(git=False), "retail", now=NOW)
    assert unavailable == []
    assert not state.has_release and state.unreleased_changes == 0 and state.sink_failed == 0
    assert state.drift is None and state.latest_scan_id is None


async def test_failing_sources_are_reported_not_raised(analytics: dict[str, Any]) -> None:
    analytics["broken"] = True
    state, unavailable = await gather_state(
        deps(broken=frozenset({"validation"})), "retail", now=NOW
    )
    assert unavailable == ["validation", "analytics"]
    assert state.structural_errors == [] and state.concepts == 0


def test_guide_caller_combines_scopes_and_groups() -> None:
    locked = [
        ModelPolicyEntry.model_validate(
            {"name": "retail", "access": {"review_groups": ["reviewers"]}}
        )
    ]
    p = SimpleNamespace(
        subject="alice",
        scopes=frozenset({"lakeloom:read", "lakeloom:propose", "lakeloom:review"}),
        groups=frozenset({"stewards"}),
    )
    c = guide_caller(p, deps(policies=locked), "retail")  # type: ignore[arg-type]
    assert c.id == "alice" and c.can == {"propose"} and not c.operator
    admin = SimpleNamespace(subject="ops", scopes=frozenset({"lakeloom:admin"}), groups=frozenset())
    assert guide_caller(admin, deps(), "retail").operator  # type: ignore[arg-type]
    assert guide_caller(Actor(subject="cli"), deps(), "retail").operator


# --- through MCP ---------------------------------------------------------------------------


async def test_next_steps_registered_read_only(make_deps: DepsFactory) -> None:
    tools = {t.name: t for t in await create_mcp(fake_deps(make_deps)).list_tools()}
    ann = tools["get_next_steps"].annotations
    assert ann is not None and ann.read_only_hint is True and ann.destructive_hint is False


async def test_next_steps_end_to_end(make_deps: DepsFactory, kit: AuthKit) -> None:
    app = create_server_app(fake_deps(make_deps))
    async with mcp_session(app, kit.token(scope="lakeloom:read")) as session:
        result = await session.call_tool("get_next_steps", {"model": "retail", "limit": 5})
    data = structured(result)
    assert "error" not in data, data
    assert data["model"] == "retail" and data["stage"]
    assert len(data["steps"]) <= 5
    assert text(result).startswith("## Next steps — retail")
    for step in data["steps"]:
        # read-only caller: nothing that needs propose/review/publish is actionable
        assert not (step["actionable"] and step["who"] in {"propose", "review", "publish"})


async def test_next_steps_requires_read_scope(make_deps: DepsFactory, kit: AuthKit) -> None:
    app = create_server_app(fake_deps(make_deps))
    async with mcp_session(app, kit.token(scope="lakeloom:propose")) as session:
        result = await session.call_tool("get_next_steps", {"model": "retail"})
    assert result.is_error and "lakeloom:read" in text(result)
