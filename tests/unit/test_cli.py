"""CLI tests (NFR-OSS-01 console entry point; FR-AGT-07 ``lakeloom serve``)."""

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from typer.testing import CliRunner

import lakeloom
from lakeloom.cli import app


def test_cli_version_prints_package_version() -> None:
    result = CliRunner().invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == lakeloom.__version__


def _serve_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name, value in {
        "LAKELOOM_OM_URL": "http://om.test:8585",
        "LAKELOOM_OIDC_ISSUER": "https://idp.test/realms/lakeloom",
        "LAKELOOM_PUBLIC_URL": "https://lakeloom.test",
        "LAKELOOM_DATA_DIR": str(tmp_path),
        "LAKELOOM_DB_URL": f"sqlite:///{tmp_path}/l.db",
        "LAKELOOM_LOG_LEVEL": "WARNING",
    }.items():
        monkeypatch.setenv(name, value)


def test_fr_agt_07_cli_serve_builds_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _serve_env(monkeypatch, tmp_path)
    calls: list[tuple[Any, dict[str, Any]]] = []
    levels: list[str] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.append((app, kw)))
    monkeypatch.setattr("lakeloom.cli.configure_logging", levels.append)

    result = CliRunner().invoke(app, ["serve", "--host", "127.0.0.1", "--port", "9123"])

    assert result.exit_code == 0, result.output
    assert levels == ["WARNING"]
    ((served, kwargs),) = calls
    assert isinstance(served, FastAPI)
    assert {getattr(r, "path", None) for r in served.routes} >= {"/healthz", "/mcp"}
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 9123
    assert kwargs["log_config"] is None


def test_fr_agt_07_cli_serve_defaults_from_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _serve_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LAKELOOM_PORT", "8111")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.append(kw))
    monkeypatch.setattr("lakeloom.cli.configure_logging", lambda level: None)

    result = CliRunner().invoke(app, ["serve"])

    assert result.exit_code == 0, result.output
    assert calls[0]["host"] == "0.0.0.0"  # noqa: S104
    assert calls[0]["port"] == 8111


def test_cli_serve_config_error_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LAKELOOM_DATA_DIR", str(tmp_path))
    for name in ("LAKELOOM_OM_URL", "LAKELOOM_OIDC_ISSUER", "LAKELOOM_PUBLIC_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: pytest.fail("must not start"))
    monkeypatch.setattr("lakeloom.cli.configure_logging", lambda level: None)

    result = CliRunner().invoke(app, ["serve"])

    assert result.exit_code == 2
    assert "LAKELOOM_OM_URL" in result.stderr
    assert "Traceback" not in result.output


def test_cli_serve_invalid_setting_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _serve_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LAKELOOM_PORT", "not-a-port")
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: pytest.fail("must not start"))

    result = CliRunner().invoke(app, ["serve"])

    assert result.exit_code == 2
    assert "port" in result.stderr


@pytest.mark.parametrize(
    "db_url",
    [
        "postgresql://u:hunter2@h:notaport/db",  # bad port -> ValueError
        "nosuchdialect://u:hunter2@h/db",  # unknown dialect -> NoSuchModuleError
        "not a url hunter2",  # unparsable -> ArgumentError
        "mysql://u:hunter2@h/db",  # driver not installed -> ImportError
    ],
)
def test_nfr_sec_11_cli_serve_bad_db_url_exits_2_without_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, db_url: str
) -> None:
    _serve_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LAKELOOM_DB_URL", db_url)
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: pytest.fail("must not start"))
    monkeypatch.setattr("lakeloom.cli.configure_logging", lambda level: None)

    result = CliRunner().invoke(app, ["serve"])

    assert result.exit_code == 2, result.output
    assert "LAKELOOM_DB_URL" in result.stderr
    assert "hunter2" not in result.output
    assert "Traceback" not in result.output


def test_cli_serve_disables_uvicorn_access_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The app's RequestContextMiddleware writes the access log; uvicorn's would duplicate it.
    _serve_env(monkeypatch, tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.append(kw))
    monkeypatch.setattr("lakeloom.cli.configure_logging", lambda level: None)

    result = CliRunner().invoke(app, ["serve"])

    assert result.exit_code == 0, result.output
    assert calls[0]["access_log"] is False


# --- lake source commands (FR-AGT-07, FR-SRC-10) ---------------------------------------------

FIXTURE = Path(__file__).parents[1] / "fixtures" / "lake"
LOG_CALLS: list[tuple[str, Any, Any]] = []


def _sources_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    entries = "\n".join(
        f"      - {{uri: {(FIXTURE / n).as_uri()}, catalog: retail, schema: sales}}"
        for n in ("customers", "products", "orders", "invoices")
    )
    config = tmp_path / "sources.yaml"
    config.write_text(f"sources:\n  - type: delta\n    name: lake\n    tables:\n{entries}\n")
    monkeypatch.delenv("LAKELOOM_OM_URL", raising=False)
    # A real handler would outlive CliRunner's captured stderr; record the call instead.
    monkeypatch.setattr(
        "lakeloom.cli.configure_logging",
        lambda level, stream=None: LOG_CALLS.append((level, stream, sys.stderr)),
    )
    for name, value in {
        "LAKELOOM_CONFIG_FILE": str(config),
        "LAKELOOM_DATA_DIR": str(tmp_path),
        "LAKELOOM_DB_URL": f"sqlite:///{tmp_path}/l.db",
        "LAKELOOM_LOG_LEVEL": "WARNING",
    }.items():
        monkeypatch.setenv(name, value)


def test_cli_scan_prints_summary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _sources_env(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["scan", "lake", "--profile", "--table", "orders"])

    assert result.exit_code == 0, result.output
    assert result.output.startswith("# Scan scan_")
    assert "- Status: completed" in result.output
    assert "`retail.sales.orders`: version 1" in result.output
    assert "customers" not in result.output
    # stdout is the command's output: logs go to stderr.
    level, stream, stderr_at_call = LOG_CALLS[-1]
    assert level == "WARNING"
    assert stream is stderr_at_call


def test_cli_sources_and_tables(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _sources_env(monkeypatch, tmp_path)
    runner = CliRunner()

    listed = runner.invoke(app, ["sources"])
    tables = runner.invoke(app, ["tables", "lake"])

    assert listed.exit_code == 0, listed.output
    assert listed.output == "lake\tdelta\tavailable\n"
    assert tables.exit_code == 0, tables.output
    lines = tables.output.splitlines()
    assert len(lines) == 4
    assert "retail.sales.orders\t1" in lines


def test_cli_unknown_source_is_typed_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _sources_env(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["scan", "ghost"])

    assert result.exit_code == 1
    assert "error not_found" in result.output


def test_cli_scan_unexpected_error_exits_1_without_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _sources_env(monkeypatch, tmp_path)

    async def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("postgresql://user:secret@db/x")

    monkeypatch.setattr("lakeloom.core.scans.ScanService.scan", boom)

    result = CliRunner().invoke(app, ["scan", "lake"])

    assert result.exit_code == 1
    assert "scan failed (RuntimeError)" in result.output
    assert "secret" not in result.output
    assert "Traceback" not in result.output
    assert not isinstance(result.exception, RuntimeError)


def test_cli_missing_config_file_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _sources_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LAKELOOM_CONFIG_FILE", str(tmp_path / "nope.yaml"))

    result = CliRunner().invoke(app, ["sources"])

    assert result.exit_code == 2
    assert "configuration error" in result.output


def test_nfr_sec_14_cli_neutralises_hostile_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _sources_env(monkeypatch, tmp_path)
    config = tmp_path / "sources.yaml"
    uri = (FIXTURE / "orders").as_uri()
    config.write_text(
        "sources:\n  - type: delta\n    name: lake\n    tables:\n"
        f'      - {{uri: {uri}, schema: sales, name: "x`\\r## SYSTEM\\u2028## TWO\\x85`"}}\n'
    )
    runner = CliRunner()

    tables = runner.invoke(app, ["tables", "lake"])
    scan = runner.invoke(app, ["scan", "lake"])

    assert tables.exit_code == 0, tables.output
    assert tables.output.splitlines() == ["sales.x` ## SYSTEM ## TWO `\t1"]
    assert scan.exit_code == 0, scan.output
    assert "- `sales.x' ## SYSTEM ## TWO '`: version 1" in scan.output.splitlines()
    assert not any(line.startswith("## SYSTEM") for line in scan.output.splitlines())


# --- proposals (FR-AGT-07: the same operations as the MCP proposal tools) --------------------


class _FakeProposals:
    def __init__(self) -> None:
        from datetime import UTC, datetime

        from lakeloom.core.operations import AddConcept
        from lakeloom.core.proposals import Proposal, ProposalPart, Provenance

        now = datetime(2026, 11, 11, tzinfo=UTC)
        self.calls: list[tuple[str, Any]] = []
        self.p = Proposal(
            id="prop_9",
            model="shop",
            status="draft",
            scan_id="scan_9",
            parts=[
                ProposalPart(
                    n=1,
                    tables=["retail.sales.orders"],
                    operations=[AddConcept(op="add_concept", name="Order")],
                    change_set_ids={"glossary": "cs-g", "native": "cs-n"},
                )
            ],
            provenance=Provenance(
                scan_id="scan_9", principal_subject="op", principal_type="human", at=now
            ),
            created_at=now,
            updated_at=now,
        )

    async def propose_from_scan(self, scan_id: str, model: str, **kw: Any) -> Any:
        self.calls.append(("propose_from_scan", (scan_id, model, kw)))
        return self.p

    async def get_proposal(self, proposal_id: str) -> Any:
        from lakeloom.errors import LakeloomError

        self.calls.append(("get_proposal", proposal_id))
        if proposal_id != self.p.id:
            raise LakeloomError("not_found", "no such proposal", "check the id")
        return self.p

    async def submit(self, proposal_id: str, **kw: Any) -> Any:
        self.calls.append(("submit", (proposal_id, kw)))
        return self.p.model_copy(update={"status": "submitted"})

    def summary_markdown(self, p: Any) -> str:
        from lakeloom.core.proposals import summary_markdown

        return summary_markdown(p)


def _fake_proposals(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _FakeProposals:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    _sources_env(monkeypatch, tmp_path)
    fake = _FakeProposals()

    @asynccontextmanager
    async def opened(_settings: Any) -> AsyncIterator[Any]:
        yield fake

    monkeypatch.setattr("lakeloom.cli._open_proposals", opened)
    return fake


def test_cli_propose_prints_summary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _fake_proposals(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["propose", "scan_9", "--model", "shop", "--table", "orders"])

    assert result.exit_code == 0, result.output
    assert result.output.startswith("# Proposal `prop_9` for model `shop`")
    name, (scan_id, model, kw) = fake.calls[0]
    assert (name, scan_id, model, kw["tables"], kw["client"]) == (
        "propose_from_scan",
        "scan_9",
        "shop",
        ["orders"],
        "lakeloom-cli",
    )
    assert kw["principal"].type == "human"


def test_cli_proposal_and_submit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _fake_proposals(monkeypatch, tmp_path)
    runner = CliRunner()

    shown = runner.invoke(app, ["proposal", "prop_9"])
    submitted = runner.invoke(app, ["submit", "prop_9"])
    missing = runner.invoke(app, ["proposal", "prop_x"])

    assert shown.exit_code == 0, shown.output
    assert "# Proposal `prop_9`" in shown.output and "- Status: draft" in shown.output
    assert submitted.exit_code == 0, submitted.output
    assert "- Status: submitted" in submitted.output
    assert [c[0] for c in fake.calls] == ["get_proposal", "submit", "get_proposal"]
    assert missing.exit_code == 1 and "error not_found" in missing.output


def test_cli_operator_acts_as_admin_on_proposals() -> None:
    """The operator may submit an agent's proposal (owner-or-admin rule, ADR 0011)."""
    from lakeloom.cli import _actor  # pyright: ignore[reportPrivateUsage]

    actor = _actor()
    assert actor.type == "human" and "lakeloom:admin" in actor.scopes


def test_cli_pending_and_rebase(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lakeloom.core.pending import PendingPage, PendingReview

    fake = _fake_proposals(monkeypatch, tmp_path)

    async def list_pending(principal: Any, **kw: Any) -> Any:
        fake.calls.append(("list_pending", (principal, kw)))
        entry = PendingReview(
            id="prop_9",
            model="shop",
            status="submitted",
            data_product="retail.sales",
            data_products=["retail.sales"],
            parts=1,
            operations={"add_concept": 1},
            proposer="op",
            proposer_type="human",
            age_seconds=7200,
        )
        return PendingPage(items=[entry])

    async def rebase(proposal_id: str, **kw: Any) -> Any:
        fake.calls.append(("rebase", (proposal_id, kw)))
        return fake.p.model_copy(update={"warnings": ["Customer base refreshed"]})

    fake.list_pending = list_pending  # pyright: ignore[reportAttributeAccessIssue]
    fake.rebase = rebase  # pyright: ignore[reportAttributeAccessIssue]
    runner = CliRunner()

    pending = runner.invoke(app, ["pending", "--model", "shop"])
    rebased = runner.invoke(app, ["rebase", "prop_9"])

    assert pending.exit_code == 0, pending.output
    assert "shop" in pending.output and "retail.sales" in pending.output
    assert "prop_9" in pending.output and "submitted" in pending.output
    name, (principal, kw) = fake.calls[0]
    assert name == "list_pending" and kw["model"] == "shop" and kw["exclude_own"] is False
    assert principal.type == "human"
    assert rebased.exit_code == 0, rebased.output
    assert "# Proposal `prop_9`" in rebased.output and "Customer base refreshed" in rebased.output
    assert fake.calls[1][0] == "rebase" and fake.calls[1][1][1]["client"] == "lakeloom-cli"


def test_cli_git_status_and_retry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import asyncio
    import hashlib
    import subprocess
    from datetime import UTC, datetime

    from lakeloom.gitstore.layout import Snapshot
    from lakeloom.state.db import create_engine, upgrade
    from lakeloom.state.publications import Publication, PublicationRepository

    bare = tmp_path / "remote.git"
    subprocess.run(  # noqa: S603
        ["git", "init", "--bare", "-b", "main", str(bare)],  # noqa: S607
        check=True,
        capture_output=True,
    )
    remote = f"file://{bare}"
    for name, value in {
        "LAKELOOM_DATA_DIR": str(tmp_path),
        "LAKELOOM_DB_URL": f"sqlite:///{tmp_path}/l.db",
        "LAKELOOM_GIT_REMOTE": remote,
        "LAKELOOM_GIT_MODE": "direct",
        "LAKELOOM_FORGE": "none",
        "LAKELOOM_LOG_LEVEL": "WARNING",
    }.items():
        monkeypatch.setenv(name, value)
    now = datetime.now(UTC)
    snap = Snapshot(
        model="shop",
        glossary_id="g",
        glossary_fqn="shop",
        model_version="v1",
        exports={"ontology.ttl": "ttl\n", "ontology.jsonld": "{}\n", "ontology.owl": "<x/>\n"},
        proposal_id="prop_9",
        message="ontology(shop): add_concept x1\n\nLakeloom-Publication: pub_9\n",
    )

    async def seed() -> None:
        engine = create_engine(f"sqlite:///{tmp_path}/l.db")
        await upgrade(engine)
        repo = PublicationRepository(engine)
        pub = Publication(
            id="pub_9",
            repo=hashlib.sha256(remote.encode()).hexdigest(),
            model="shop",
            proposal_id="prop_9",
            mode="direct",
            created_at=now,
            updated_at=now,
            payload=snap,
        )
        await repo.add(pub)
        await repo.update(
            pub.model_copy(
                update={"status": "blocked", "attempts": 1, "last_error": "git auth: refused"}
            )
        )
        await engine.dispose()

    asyncio.run(seed())
    runner = CliRunner()

    status = runner.invoke(app, ["git", "status"])
    retry = runner.invoke(app, ["git", "retry"])
    after = runner.invoke(app, ["git", "status"])

    assert status.exit_code == 0, status.output
    assert "pub_9" in status.output and "shop" in status.output and "prop_9" in status.output
    assert "blocked" in status.output and "git auth: refused" in status.output
    assert retry.exit_code == 0, retry.output
    assert "requeued 1 blocked publication(s)" in retry.output
    assert "pub_9 done" in retry.output and "main" in retry.output
    assert after.exit_code == 0 and "No pending or blocked Git publications." in after.output
    assert str(bare) not in status.output + retry.output + after.output


def test_cli_git_status_when_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LAKELOOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LAKELOOM_DB_URL", f"sqlite:///{tmp_path}/l.db")
    monkeypatch.delenv("LAKELOOM_GIT_REMOTE", raising=False)
    result = CliRunner().invoke(app, ["git", "status"])
    assert result.exit_code == 0, result.output
    assert "Git publishing is off" in result.output


# --- releases (FR-GIT-06: the same operations as the MCP release tools) -----------------------


TOK = "tok"  # the fake release service's confirm token


def test_cli_release_create_and_list(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager
    from datetime import UTC, datetime

    from lakeloom.core.release_diff import ReleaseDiff
    from lakeloom.core.releases import ReleasePlan, ReleaseResult
    from lakeloom.gitstore.release_reader import ReleaseInfo
    from lakeloom.state.releases import Release

    now = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
    plan = ReleasePlan(
        model="shop", version="1.0.0", tag="shop/v1.0.0", previous=None,
        diff=ReleaseDiff(
            from_version=None, to_version="1.0.0", added=[], changed=[], removed=[],
            removed_triples=0, added_triples=3, annotation_only=False, bump=None,
            reason="initial release",
        ),
        requested_bump=None, warnings=[], changelog_section="## [1.0.0] - 2026-11-15\n",
        confirm_token=TOK, expires_at=1,
    )  # fmt: skip
    release = Release(
        id="rel_1", repo="k", model="shop", version="1.0.0", tag="shop/v1.0.0",
        commit_sha="e" * 40, released_by="op", created_at=now,
    )  # fmt: skip
    calls: list[tuple[str, Any, dict[str, Any]]] = []

    class FakeService:
        async def plan(self, principal: Any, model: str, **kw: Any) -> ReleasePlan:
            calls.append(("plan", principal, {"model": model, **kw}))
            return plan

        async def create(self, principal: Any, model: str, **kw: Any) -> ReleaseResult:
            calls.append(("create", principal, {"model": model, **kw}))
            return ReleaseResult(release=release, plan=plan, already_done=False, triplestore="off")

    class FakeReader:
        async def list(self, slug: str) -> list[ReleaseInfo]:
            if slug != "shop":
                return []
            return [
                ReleaseInfo(version="1.0.0", tag="shop/v1.0.0", object_sha="a" * 40,
                            commit_sha="e" * 40, released_at=now)
            ]  # fmt: skip

    @asynccontextmanager
    async def opened(_settings: Any) -> AsyncIterator[Any]:
        yield FakeService(), FakeReader()

    _sources_env(monkeypatch, tmp_path)
    monkeypatch.setattr("lakeloom.cli._open_releases", opened)
    runner = CliRunner()

    dry = runner.invoke(app, ["release", "create", "shop", "--bump", "minor"])
    confirmed = runner.invoke(app, ["release", "create", "shop", "--yes"])
    bad_bump = runner.invoke(app, ["release", "create", "shop", "--bump", "huge"])
    listed = runner.invoke(app, ["release", "list", "shop"])
    empty = runner.invoke(app, ["release", "list", "other"])

    assert dry.exit_code == 0, dry.output
    assert "# Release `shop/v1.0.0` of model `shop`" in dry.output
    assert "## [1.0.0] - 2026-11-15" in dry.output
    assert dry.output.rstrip().endswith("Re-run with --yes to release.")
    assert confirmed.exit_code == 0, confirmed.output
    assert "Released `shop/v1.0.0`." in confirmed.output and "`eeeeeee`" in confirmed.output
    assert [(c[0], c[2].get("bump")) for c in calls] == [
        ("plan", "minor"),
        ("plan", None),
        ("create", None),
    ]
    assert calls[2][2]["confirm_token"] == TOK and calls[2][2]["client"] == "lakeloom-cli"
    assert calls[0][1].type == "human"
    assert bad_bump.exit_code == 1 and "unknown bump" in bad_bump.output
    assert listed.exit_code == 0, listed.output
    assert listed.output.splitlines() == [
        "version\ttag\tcommit\treleased_at",
        "1.0.0\tshop/v1.0.0\teeeeeee\t2026-11-15T10:00:00+00:00",
    ]
    assert empty.exit_code == 0 and "No releases of other." in empty.output


def test_cli_release_when_git_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LAKELOOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LAKELOOM_DB_URL", f"sqlite:///{tmp_path}/l.db")
    monkeypatch.delenv("LAKELOOM_GIT_REMOTE", raising=False)
    runner = CliRunner()
    for args in (["release", "create", "shop"], ["release", "list", "shop"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        assert "Git publishing is off" in result.output


def test_sink_retry_cli_requeues_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import asyncio
    import hashlib
    from datetime import UTC, datetime

    from lakeloom.state.db import create_engine, upgrade
    from lakeloom.state.releases import Release, ReleaseRepository

    remote = "file:///nowhere/remote.git"
    repo_key = hashlib.sha256(remote.encode()).hexdigest()
    db = f"sqlite:///{tmp_path}/l.db"
    for name, value in {
        "LAKELOOM_DATA_DIR": str(tmp_path),
        "LAKELOOM_DB_URL": db,
        "LAKELOOM_GIT_REMOTE": remote,
        "LAKELOOM_LOG_LEVEL": "WARNING",
    }.items():
        monkeypatch.setenv(name, value)

    async def seed() -> None:
        engine = create_engine(db)
        await upgrade(engine)
        repo = ReleaseRepository(engine)
        now = datetime.now(UTC)
        for n, status in enumerate(("failed", "failed", "done")):
            await repo.add(
                Release(
                    id=f"rel_{n}", repo=repo_key, model="shop", version=f"1.{n}.0",
                    tag=f"shop/v1.{n}.0", commit_sha="b" * 40, released_by="alice",
                    created_at=now, sink_status=status, sink_attempts=10,  # type: ignore[arg-type]
                )
            )  # fmt: skip
        await engine.dispose()

    async def counts() -> dict[str, int]:
        engine = create_engine(db)
        try:
            return await ReleaseRepository(engine).sink_counts(repo_key)
        finally:
            await engine.dispose()

    asyncio.run(seed())
    runner = CliRunner()

    off = runner.invoke(app, ["release", "sink-retry"])
    assert off.exit_code == 0 and "triple store is off" in off.output
    assert asyncio.run(counts())["failed"] == 2

    monkeypatch.setenv("LAKELOOM_TRIPLESTORE_ENABLED", "true")
    monkeypatch.setenv("LAKELOOM_TRIPLESTORE_URL", "http://ts:3030/x/data")
    out = runner.invoke(app, ["release", "sink-retry"])

    assert out.exit_code == 0, out.output
    assert "requeued 2 failed triple-store push(es)" in out.output
    after = asyncio.run(counts())
    assert (after["failed"], after["pending"], after["done"]) == (0, 2, 1)
    assert "ts:3030" not in out.output and str(tmp_path) not in out.output


def test_cli_releases_use_their_own_working_copy(tmp_path: Path) -> None:
    """The repository lock is per process: the CLI must never share the server's clone."""
    from lakeloom.cli import _cli_working_copy
    from lakeloom.config import Settings
    from lakeloom.gitstore.wiring import build_publisher
    from lakeloom.state.db import create_engine
    from lakeloom.state.publications import PublicationRepository

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, data_dir=tmp_path, git_remote="file:///tmp/remote.git", git_mode="direct"
    )
    server = build_publisher(
        settings, PublicationRepository(create_engine("sqlite+aiosqlite://")), None
    )
    assert server.working_copy is not None
    cli = _cli_working_copy(settings)
    assert cli.path == tmp_path / "git" / "cli-repo"
    assert cli.path != server.working_copy.path


# --- imports (FR-IO-02, FR-GIT-07: the same operations as the MCP import tools) ---------------


def test_cli_import_dry_run_then_yes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager
    from datetime import UTC, datetime

    from lakeloom.core.imports import ImportPlan
    from lakeloom.core.proposals import Proposal, Provenance, SourceDocument

    now = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
    doc = SourceDocument(format="turtle", sha256="ab" * 32, bytes=10)
    plan = ImportPlan(
        model="shop", origin="import", namespace="http://x/ontology/shop/", mode="merge",
        counts={"add_concept": 1}, operations=[], total_operations=1, warnings=[],
        summary="# Import into model `shop`\n", source_document=doc, confirm_token=TOK,
        expires_at=1,
    )  # fmt: skip
    proposal = Proposal(
        id="prop_cli", model="shop", status="draft",
        provenance=Provenance(scan_id=None, principal_subject="op", principal_type="human",
                              at=now, origin="import", source_document=doc),
        created_at=now, updated_at=now,
    )  # fmt: skip
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeImports:
        async def plan_file(self, principal: Any, model: str, data: bytes, fmt: str, **kw: Any):
            calls.append(("plan_file", {"model": model, "data": data, "fmt": fmt, **kw}))
            return plan

        async def confirm_file(self, principal: Any, client: Any, model: str, data: bytes,
                               fmt: str, **kw: Any) -> Proposal:  # fmt: skip
            calls.append(("confirm_file", {"client": client, "fmt": fmt, **kw}))
            return proposal

        async def plan_git(self, principal: Any, model: str, **kw: Any) -> ImportPlan:
            calls.append(("plan_git", {"model": model, **kw}))
            return plan

        async def confirm_git(self, principal: Any, client: Any, model: str, **kw: Any):
            calls.append(("confirm_git", {"client": client, **kw}))
            return proposal

    @asynccontextmanager
    async def opened(_settings: Any) -> AsyncIterator[Any]:
        yield FakeImports()

    _sources_env(monkeypatch, tmp_path)
    monkeypatch.setattr("lakeloom.cli._open_imports", opened)
    owl = tmp_path / "shop.owl"
    owl.write_bytes(b"<rdf:RDF/>")
    odd = tmp_path / "shop.txt"
    odd.write_bytes(b"x")
    runner = CliRunner()

    dry = runner.invoke(app, ["import", "shop", str(owl)])
    yes = runner.invoke(app, ["import", "shop", str(owl), "--mode", "replace", "--yes"])
    unknown = runner.invoke(app, ["import", "shop", str(odd)])
    forced = runner.invoke(app, ["import", "shop", str(odd), "--format", "turtle"])
    git_dry = runner.invoke(app, ["import-git", "shop", "--version", "1.0.0"])
    git_yes = runner.invoke(app, ["import-git", "shop", "--from-model", "other", "--yes"])

    assert dry.exit_code == 0, dry.output
    assert "# Import into model `shop`" in dry.output
    assert dry.output.rstrip().endswith("Re-run with --yes to import.")
    assert yes.exit_code == 0, yes.output
    assert "Draft `prop_cli`; submit it with `lakeloom submit <id>`." in yes.output
    assert unknown.exit_code == 1 and "cannot tell the format" in unknown.output
    assert forced.exit_code == 0, forced.output
    assert git_dry.exit_code == 0 and "Re-run with --yes to import." in git_dry.output
    assert git_yes.exit_code == 0 and "Draft `prop_cli`" in git_yes.output
    assert [(name, kw.get("fmt"), kw.get("mode")) for name, kw in calls] == [
        ("plan_file", "rdfxml", "merge"),
        ("plan_file", "rdfxml", "replace"),
        ("confirm_file", "rdfxml", "replace"),
        ("plan_file", "turtle", "merge"),
        ("plan_git", None, "merge"),
        ("plan_git", None, "merge"),
        ("confirm_git", None, "merge"),
    ]
    assert calls[0][1]["data"] == b"<rdf:RDF/>"
    assert calls[2][1]["confirm_token"] == TOK and calls[2][1]["client"] == "lakeloom-cli"
    assert calls[4][1]["version"] == "1.0.0"
    assert calls[6][1]["from_model"] == "other" and calls[6][1]["confirm_token"] == TOK


# --- drift (FR-PRO-07: the same operation as the MCP detect_drift tool) ----------------------


def test_cli_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    from lakeloom.core.drift import TableDrift
    from lakeloom.core.drift_service import DriftReport

    calls: list[dict[str, Any]] = []

    class FakeDrift:
        async def detect(self, principal: Any, client: Any, model: str, **kw: Any) -> DriftReport:
            calls.append({"client": client, "model": model, **kw})
            return DriftReport(
                model=model, baseline_scan_ids=["scan_1"], scan_ids=["scan_2"],
                tables=[TableDrift(fqn="c.s.orders", status="unchanged")], proposal=None,
                warnings=[],
            )  # fmt: skip

    @asynccontextmanager
    async def opened(_settings: Any) -> AsyncIterator[Any]:
        yield FakeDrift()

    _sources_env(monkeypatch, tmp_path)
    monkeypatch.setattr("lakeloom.cli._open_drift", opened)
    runner = CliRunner()

    plain = runner.invoke(app, ["drift", "shop"])
    narrow = runner.invoke(app, ["drift", "shop", "--no-new-tables"])

    assert plain.exit_code == 0, plain.output
    assert "# Drift of model `shop`" in plain.output and "no drift since scan_1" in plain.output
    assert narrow.exit_code == 0, narrow.output
    assert calls == [
        {"client": "lakeloom-cli", "model": "shop", "include_new_tables": True},
        {"client": "lakeloom-cli", "model": "shop", "include_new_tables": False},
    ]
