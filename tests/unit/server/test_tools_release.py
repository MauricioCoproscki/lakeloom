"""Release MCP tools (FR-GIT-06, FR-PUB-02, FR-AGT-03, FR-AGT-06, NFR-SEC-12).

``create_release`` runs against a recording fake service (ReleaseService has its own tests in
tests/unit/core/test_releases.py); ``list_releases`` and ``diff_releases`` read real tags of a
local bare remote through the real ReleaseReader.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kit import AuthKit, DepsFactory, mcp_session
from releases_kit import git_deps, seeded
from test_tools_semantic import fake_deps, structured, text

from lakeloom.core.audit import current_audit
from lakeloom.core.release_diff import ReleaseDiff
from lakeloom.core.releases import ReleasePlan, ReleaseResult
from lakeloom.server.mcp_app import create_mcp, create_server_app
from lakeloom.state.releases import Release

PUBLISH = "lakeloom:publish"
READ = "lakeloom:read"
TOKEN = "tok-1"  # noqa: S105 - the fake service's confirm token
NOW = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
TOOLS = {"create_release": PUBLISH, "list_releases": READ, "diff_releases": READ}

PLAN = ReleasePlan(
    model="retail",
    version="1.1.0",
    tag="retail/v1.1.0",
    previous="1.0.0",
    diff=ReleaseDiff(
        from_version="1.0.0", to_version="1.1.0", added=[], changed=[], removed=[],
        removed_triples=0, added_triples=2, annotation_only=False, bump="minor",
        reason="1 term added",
    ),
    requested_bump=None,
    warnings=["computed minor, releasing as patch"],
    changelog_section="## [1.1.0] - 2026-11-15\n\nReleased by alice. Bump: minor (1 term added).\n",
    confirm_token=TOKEN,
    expires_at=1_900_000_000,
)  # fmt: skip
RELEASE = Release(
    id="rel_x", repo="k", model="retail", version="1.1.0", tag="retail/v1.1.0",
    commit_sha="d" * 40, released_by="alice", created_at=NOW,
)  # fmt: skip


class FakeReleases:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []

    async def plan(self, principal: Any, model: str, **kw: Any) -> ReleasePlan:
        self.calls.append(("plan", principal, {"model": model, **kw}))
        return PLAN

    async def create(self, principal: Any, model: str, **kw: Any) -> ReleaseResult:
        self.calls.append(("create", principal, {"model": model, **kw}))
        current_audit().change_set_id = RELEASE.tag  # what the real service records
        return ReleaseResult(release=RELEASE, plan=PLAN, already_done=False, triplestore="off")


def with_fake(make_deps: DepsFactory) -> tuple[Any, FakeReleases]:
    deps = fake_deps(make_deps)
    fake = FakeReleases()
    deps.release_service = fake
    return deps, fake


async def audit_rows(deps: Any) -> list[Any]:
    return list((await deps.audit.list(None, None, 100)).items)


async def test_release_tools_annotations_and_schemas(make_deps: DepsFactory) -> None:
    tools = {t.name: t for t in await create_mcp(make_deps()).list_tools()}

    for name, scope in TOOLS.items():
        tool = tools[name]
        ann = tool.annotations
        assert ann is not None and ann.open_world_hint is False
        if name == "create_release":
            assert (ann.read_only_hint, ann.destructive_hint, ann.idempotent_hint) == (
                False, True, False,
            )  # fmt: skip
        else:
            assert (ann.read_only_hint, ann.destructive_hint, ann.idempotent_hint) == (
                True, False, True,
            )  # fmt: skip
        assert f"`{scope}`" in (tool.description or ""), name
        assert tool.output_schema is not None, name
        for prop, schema in tool.input_schema.get("properties", {}).items():
            assert schema.get("description"), f"{name}.{prop}"
    create = tools["create_release"].input_schema
    assert create["required"] == ["model"]
    assert "major" in str(create["properties"]["bump"])
    assert tools["diff_releases"].input_schema["properties"]["to_version"]["default"] == "latest"


async def test_create_release_dry_run_then_confirm(make_deps: DepsFactory, kit: AuthKit) -> None:
    deps, fake = with_fake(make_deps)
    token = kit.token(scope=PUBLISH, sub="alice")
    async with mcp_session(create_server_app(deps), token, client_name="claude") as s:
        preview = await s.call_tool("create_release", {"model": "retail"})
        done = await s.call_tool("create_release", {"model": "retail", "confirm_token": TOKEN})

    assert preview.is_error is not True, preview
    data = structured(preview)
    assert data["released"] is False and data["result"] is None
    assert data["preview"]["confirm_token"] == TOKEN and data["preview"]["version"] == "1.1.0"
    body = text(preview)
    assert "# Release `retail/v1.1.0` of model `retail`" in body
    assert "Bump: minor (1 term added)" in body and "computed minor, releasing as patch" in body
    assert "## [1.1.0] - 2026-11-15" in body
    assert body.rstrip().endswith(
        "Dry run: nothing was changed. Show this to the user; to proceed, call create_release "
        f"again with the same arguments and confirm_token `{TOKEN}`."
    )
    out = structured(done)
    assert out["released"] is True and out["preview"] is None
    assert out["result"]["release"]["tag"] == "retail/v1.1.0"
    assert "Released `retail/v1.1.0`" in text(done) and "`ddddddd`" in text(done)
    (plan_call, create_call) = fake.calls
    assert plan_call[0] == "plan" and plan_call[1].subject == "alice"
    assert plan_call[2] == {"model": "retail", "version": None, "bump": None}
    assert create_call[2] == {
        "model": "retail",
        "version": None,
        "bump": None,
        "confirm_token": TOKEN,
        "client": "claude",
    }
    rows = await audit_rows(deps)
    assert [(r.tool, r.model, r.result) for r in rows] == [
        ("create_release", "retail", "ok"),
        ("create_release", "retail", "ok"),
    ]
    assert rows[1].change_set_id == "retail/v1.1.0" and rows[1].client == "claude"


async def test_create_release_requires_publish_scope(make_deps: DepsFactory, kit: AuthKit) -> None:
    deps, fake = with_fake(make_deps)
    async with mcp_session(create_server_app(deps), kit.token(scope=READ)) as s:
        result = await s.call_tool("create_release", {"model": "retail"})

    assert result.is_error is True
    assert structured(result)["error"]["code"] == "forbidden"
    assert fake.calls == []
    (row,) = await audit_rows(deps)
    assert (row.tool, row.result) == ("create_release", "forbidden")


async def test_release_tools_with_git_off(make_deps: DepsFactory, kit: AuthKit) -> None:
    deps = fake_deps(make_deps)
    token = kit.token(scope=f"{PUBLISH} {READ}")
    async with mcp_session(create_server_app(deps), token) as s:
        results = [
            await s.call_tool("create_release", {"model": "retail"}),
            await s.call_tool("list_releases", {"model": "retail"}),
            await s.call_tool("diff_releases", {"model": "retail", "from_version": "1.0.0"}),
        ]

    for result in results:
        err = structured(result)["error"]
        assert (err["code"], err["message"]) == ("validation_error", "Git publishing is off")
        assert "LAKELOOM_GIT_REMOTE" in err["hint"]


async def test_list_and_diff_releases(make_deps: DepsFactory, kit: AuthKit, tmp_path: Path) -> None:
    remote = seeded(tmp_path)
    app = create_server_app(git_deps(make_deps, remote))
    async with mcp_session(app, kit.token()) as s:
        listed = await s.call_tool("list_releases", {"model": "retail"})
        empty = await s.call_tool("list_releases", {"model": "other"})
        diff = await s.call_tool(
            "diff_releases", {"model": "retail", "from_version": "1.0.0", "to_version": "1.1.0"}
        )
        same = await s.call_tool(
            "diff_releases", {"model": "retail", "from_version": "1.1.0", "to_version": "1.1.0"}
        )
        latest = await s.call_tool("diff_releases", {"model": "retail", "from_version": "1.1.0"})
        bad = await s.call_tool("diff_releases", {"model": "retail", "from_version": "1.0"})
        unknown = await s.call_tool("diff_releases", {"model": "retail", "from_version": "3.0.0"})

    releases = structured(listed)["releases"]
    assert structured(listed)["model"] == "retail"
    assert [(r["version"], r["tag"]) for r in releases] == [
        ("1.1.0", "retail/v1.1.0"),
        ("1.0.0", "retail/v1.0.0"),
    ]
    assert releases[0]["commit_sha"] == remote.commit_of("retail/v1.1.0")
    assert "**2 release(s)** of model `retail`" in text(listed)
    assert structured(empty)["releases"] == [] and "no releases yet" in text(empty)

    d = structured(diff)
    assert (d["from_version"], d["to_version"], d["bump"]) == ("1.0.0", "1.1.0", "minor")
    assert [t["label"] for t in d["added"]] == ["Invoice"]
    assert [t["label"] for t in d["changed"]] == ["Order 1.1.0"]
    assert text(diff).startswith("## 1.0.0 → 1.1.0\n\nBump: minor")
    assert structured(same)["bump"] is None
    to_latest = structured(latest)
    assert to_latest["to_version"] == "latest" and to_latest["bump"] == "major"
    assert {t["label"] for t in to_latest["removed"]} == {"Invoice"}  # not in the OM state
    assert structured(bad)["error"]["code"] == "validation_error"
    assert structured(unknown)["error"]["code"] == "not_found"
