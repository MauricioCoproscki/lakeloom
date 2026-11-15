"""ReleaseService against a local bare remote, a real ModelService over a fake OM and real
confirm tokens (FR-GIT-06, FR-GIT-09, FR-GIT-02, FR-AGT-03, NFR-SEC-12)."""

import asyncio
import hashlib
import os
import subprocess
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import SecretStr
from rdflib.compare import isomorphic
from sqlalchemy.ext.asyncio import AsyncEngine

from lakeloom.config import Settings
from lakeloom.core.audit import AuditContext, audit_context
from lakeloom.core.callers import Actor
from lakeloom.core.confirm import ConfirmTokens
from lakeloom.core.model_policy import ModelPolicyEntry
from lakeloom.core.models import ModelService
from lakeloom.core.releases import ReleasePlan, ReleaseResult, ReleaseService
from lakeloom.errors import LakeloomError
from lakeloom.gitstore import GitError, GitRunner, Snapshot, WorkingCopy
from lakeloom.gitstore.publisher import repo_lock
from lakeloom.gitstore.release_layout import tag_message
from lakeloom.gitstore.release_reader import ReleaseReader
from lakeloom.gitstore.snapshot import fill_exports
from lakeloom.om.models import OMCapabilities, OMGlossary
from lakeloom.rdf.canonical import parse_rdf
from lakeloom.server.auth import Principal, PrincipalType
from lakeloom.state.audit import AuditRepository
from lakeloom.state.db import create_engine, upgrade
from lakeloom.state.publications import Publication, PublicationRepository, new_publication_id
from lakeloom.state.releases import Release, ReleaseRepository

NOW = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
SECRET = b"s" * 32
BAD_TOKEN = "x"  # noqa: S105
BASE = "http://x/ontology/shop/"
PREFIXES = """\
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
"""
V1 = (
    PREFIXES
    + f"""\
<{BASE}Order> a skos:Concept ; skos:prefLabel "Order"@en .
<{BASE}Customer> a skos:Concept ; skos:prefLabel "Customer"@en .
"""
)
V2_ADDED = V1 + f'<{BASE}Invoice> a skos:Concept ; skos:prefLabel "Invoice"@en .\n'
V2_LABEL = V1.replace('"Order"@en', '"Purchase order"@en')

_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Foreign",
    "GIT_AUTHOR_EMAIL": "f@example.invalid",
    "GIT_COMMITTER_NAME": "Foreign",
    "GIT_COMMITTER_EMAIL": "f@example.invalid",
}


def git(*args: str, cwd: Path) -> str:
    env = {"PATH": os.environ["PATH"], "HOME": str(cwd), **_ENV}
    res = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd, env=env, capture_output=True, text=True, check=True,
    )  # fmt: skip
    return res.stdout


# --- fakes ---------------------------------------------------------------------------------


class FakeOM:
    """A bot OM client that serves one glossary and records every method called on it."""

    def __init__(self) -> None:
        self.turtle = V1
        self.updated_at = 1
        self.calls: list[str] = []

    def change(self, turtle: str) -> None:
        self.turtle = turtle
        self.updated_at += 1

    async def get_glossary_by_name(self, name: str) -> OMGlossary | None:
        self.calls.append("get_glossary_by_name")
        if name != "shop":
            return None
        return OMGlossary.model_validate(
            {"id": "g-shop", "name": "shop", "fullyQualifiedName": "shop",
             "updatedAt": self.updated_at}
        )  # fmt: skip

    async def export_ontology(self, glossary_id: str, fmt: str, **_: Any) -> str:
        self.calls.append("export_ontology")
        return self.turtle

    async def export_native_ontology(self, name: str) -> None:
        self.calls.append("export_native_ontology")
        return None

    async def get_capabilities(self) -> OMCapabilities:
        self.calls.append("get_capabilities")
        return OMCapabilities(reachable=True, version="1.12.0")


class FlakyRunner(GitRunner):
    """Fails (or runs a hook before) git subcommands named in ``on``."""

    fail: GitError | None = None
    fail_after: bool = False  # run the command, then raise (the push landed, the reply was lost)
    fail_times: int = 1_000_000
    before: Callable[[], None] | None = None
    on: tuple[str, ...] = ("push",)

    async def run(self, *args: str, cwd: Path | None = None, input: bytes | None = None) -> str:
        sub = next((a for a in args if not a.startswith("-") and "=" not in a), "")
        if sub in self.on and self.before is not None:
            hook, self.before = self.before, None
            hook()
        if sub in self.on and self.fail is not None and self.fail_times > 0:
            self.fail_times -= 1
            if self.fail_after:
                await super().run(*args, cwd=cwd, input=input)
            raise self.fail
        return await super().run(*args, cwd=cwd, input=input)


def human(name: str = "alice", groups: frozenset[str] = frozenset()) -> Principal:
    return Principal(
        subject=f"sub-{name}", type=PrincipalType.HUMAN, client_id=None, username=name,
        scopes=frozenset({"lakeloom:publish"}), groups=groups, token=SecretStr("t"),
    )  # fmt: skip


def agent(groups: frozenset[str]) -> Principal:
    return Principal(
        subject="agent-1", type=PrincipalType.AGENT, client_id="c", username=None,
        scopes=frozenset({"lakeloom:publish"}), groups=groups, token=SecretStr("t"),
    )  # fmt: skip


# --- fixtures ------------------------------------------------------------------------------


class Env:
    def __init__(self, tmp: Path, engine: AsyncEngine) -> None:
        self.tmp = tmp
        self.bare = tmp / "remote.git"
        git("init", "--bare", "-b", "main", str(self.bare), cwd=tmp)
        self.remote = f"file://{self.bare}"
        self.engine = engine
        self.om = FakeOM()
        self.releases = ReleaseRepository(engine)
        self.pubs = PublicationRepository(engine)
        self.audit = AuditRepository(engine)
        self.confirm = ConfirmTokens(SECRET, 600)
        self.released: list[Release] = []
        self.counter = 0
        self.build()

    def build(
        self,
        *,
        policies: list[ModelPolicyEntry] | None = None,
        on_released: Callable[[Release], Any] | None = None,
        refresh_seconds: float = 30,
        **settings: Any,
    ) -> ReleaseService:
        self.settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            git_remote=self.remote,
            git_branch="main",
            git_mode=settings.pop("git_mode", "direct"),
            model_cache_ttl_seconds=0,
            **settings,
        )
        self.repo_key = hashlib.sha256(self.remote.encode()).hexdigest()
        self.runner = FlakyRunner(self.settings, self.tmp / "work")
        self.wc = WorkingCopy(self.runner, self.tmp / "work" / "repo", self.remote)
        self.reader = ReleaseReader(self.wc, self.repo_key, refresh_seconds=refresh_seconds)
        self.models = ModelService(self.om, self.settings)  # type: ignore[arg-type]

        async def remember(rel: Release) -> None:
            self.released.append(rel)

        self.svc = ReleaseService(
            self.settings, self.models, self.om, self.wc, self.reader,  # type: ignore[arg-type]
            self.releases, self.pubs, self.confirm, self.audit, policies or [],
            clock=lambda: NOW, on_released=on_released or remember,
        )  # fmt: skip
        return self.svc

    def push_foreign(self, message: str, files: dict[str, str]) -> None:
        self.counter += 1
        clone = self.tmp / f"foreign{self.counter}"
        git("clone", self.remote, str(clone), cwd=self.tmp)
        git("checkout", "-B", "main", cwd=clone)
        for rel, text in files.items():
            (clone / rel).parent.mkdir(parents=True, exist_ok=True)
            (clone / rel).write_text(text)
        git("add", "-A", cwd=clone)
        git("commit", "-m", message, cwd=clone)
        git("push", "origin", "main", cwd=clone)

    def remote_tags(self) -> list[str]:
        return git("tag", "-l", cwd=self.bare).split()

    def show(self, ref: str, path: str) -> str:
        return git("show", f"{ref}:{path}", cwd=self.bare)

    async def release(self, principal: Any = None, **kw: Any) -> ReleaseResult:
        who = principal or human()
        version, bump = kw.get("version"), kw.get("bump")
        plan = await self.svc.plan(who, "shop", version=version, bump=bump)
        return await self.svc.create(
            who, "shop", version=version, bump=bump, confirm_token=plan.confirm_token
        )


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    await upgrade(eng)
    yield eng
    await eng.dispose()


@pytest.fixture
def env(tmp_path: Path, engine: AsyncEngine) -> Env:
    return Env(tmp_path, engine)


async def _code(coro: Any) -> LakeloomError:
    with pytest.raises(LakeloomError) as info:
        await coro
    return info.value


# --- tests ---------------------------------------------------------------------------------


async def test_first_release_is_1_0_0_with_tag_and_files(env: Env) -> None:
    manifest = (
        'model: "shop"\nlast_change_set: "cs-7"\nlast_proposal: "prop_7"\n'
        "release: null\nreleased_at: null\nsource_tables:\n"
        '  - fqn: "uc.sales.orders"\n    om_fqn: null\n    version: 12\n'
    )
    env.push_foreign(
        "ontology(shop): add_concept x1\n\nLakeloom-Proposal: prop_7\nApproved-By: alice",
        {"ontologies/shop/manifest.yaml": manifest, "ontologies/shop/ontology.ttl": "old"},
    )
    plan = await env.svc.plan(human(), "shop", version=None, bump=None)
    assert isinstance(plan, ReleasePlan)
    assert (plan.version, plan.tag, plan.previous) == ("1.0.0", "shop/v1.0.0", None)
    assert plan.diff.bump is None and {t.label for t in plan.diff.added} == {"Order", "Customer"}
    assert plan.changelog_section.startswith("## [1.0.0] - 2026-11-15\n")
    assert "prop_7" in plan.changelog_section and plan.warnings == []
    assert env.remote_tags() == []  # a dry run changes nothing

    result = await env.svc.create(
        human(), "shop", version=None, bump=None, confirm_token=plan.confirm_token
    )
    assert not result.already_done and result.triplestore == "off"
    rel = result.release
    rid = "rel_" + hashlib.sha256(plan.confirm_token.encode()).hexdigest()[:26]
    assert (rel.id, rel.tag, rel.version, rel.released_by) == (rid, "shop/v1.0.0", "1.0.0", "alice")
    assert rel.sink_status == "none" and await env.releases.get(rid) == rel
    assert env.remote_tags() == ["shop/v1.0.0"]
    tag = "shop/v1.0.0"
    for name in ("ontology.ttl", "ontology.jsonld", "ontology.owl", "shapes.ttl", "context.jsonld"):
        assert env.show(tag, f"ontologies/shop/{name}")
    m = yaml.safe_load(env.show(tag, "ontologies/shop/manifest.yaml"))
    assert m["release"] == "1.0.0" and m["released_at"] == "2026-11-15T10:00:00Z"
    assert m["last_proposal"] == "prop_7" and m["last_change_set"] == "cs-7"
    assert m["source_tables"] == [{"fqn": "uc.sales.orders", "om_fqn": None, "version": 12}]
    changelog = env.show(tag, "ontologies/shop/CHANGELOG.md")
    assert changelog.startswith("# Changelog — shop\n") and "## [1.0.0] - 2026-11-15" in changelog
    assert "| shop |" in env.show(tag, "README.md") and "1.0.0 |" in env.show(tag, "README.md")
    # the branch tip is the tagged commit; nothing appended to provenance
    assert git("rev-parse", "main", cwd=env.bare) == git("rev-parse", f"{tag}^{{}}", cwd=env.bare)
    assert result.release.commit_sha == git("rev-parse", "main", cwd=env.bare).strip()
    files = git("show", "--name-only", "--format=", "main", cwd=env.bare).split()
    assert not any(f.startswith("provenance/") for f in files)
    body = git("log", "-1", "--format=%B", "main", cwd=env.bare)
    assert body.startswith("release(shop): v1.0.0\n\nBump: none (initial release)\n")
    trailers = body.rstrip("\n").split("\n\n")[-1].splitlines()
    assert trailers == [
        "Lakeloom-Release: shop/v1.0.0",
        "Released-By: alice",
        f"Lakeloom-Model-Version: {await env.models.version('shop')}",
        f"Lakeloom-Release-Id: {rid}",
    ]
    # the annotated tag message round-trips verbatim, CHANGELOG '#' lines included
    contents = git("for-each-ref", "--format=%(contents)", f"refs/tags/{tag}", cwd=env.bare)
    assert contents.rstrip("\n") == tag_message("shop", "1.0.0", plan.changelog_section).rstrip(
        "\n"
    )
    assert "\n## [1.0.0] - 2026-11-15\n" in contents
    assert env.released == [rel]


async def test_second_release_bumps_minor_after_addition(env: Env) -> None:
    await env.release()
    env.om.change(V2_ADDED)
    plan = await env.svc.plan(human(), "shop", version=None, bump=None)
    assert (plan.version, plan.previous, plan.diff.bump) == ("1.1.0", "1.0.0", "minor")
    assert [t.label for t in plan.diff.added] == ["Invoice"]
    res = await env.svc.create(
        human(), "shop", version=None, bump=None, confirm_token=plan.confirm_token
    )
    assert res.release.tag == "shop/v1.1.0"
    assert sorted(env.remote_tags()) == ["shop/v1.0.0", "shop/v1.1.0"]
    changelog = env.show("shop/v1.1.0", "ontologies/shop/CHANGELOG.md")
    assert changelog.index("## [1.1.0]") < changelog.index("## [1.0.0]")


async def test_label_change_bumps_patch(env: Env) -> None:
    await env.release()
    env.om.change(V2_LABEL)
    plan = await env.svc.plan(human(), "shop", version=None, bump=None)
    assert (plan.version, plan.diff.bump) == ("1.0.1", "patch")


async def test_nothing_changed_is_validation_error(env: Env) -> None:
    await env.release()
    err = await _code(env.svc.plan(human(), "shop", version=None, bump=None))
    assert err.code == "validation_error" and err.message == "nothing changed since 1.0.0"
    assert err.hint == "approve a proposal first"
    err = await _code(env.svc.plan(human(), "shop", version=None, bump="major"))
    assert err.message == "nothing changed since 1.0.0"


async def test_unchanged_model_two_exports_is_nothing_changed(env: Env) -> None:
    """Two real exports of one model state diff as nothing (no header or timestamp churn)."""
    from lakeloom.core.release_diff import diff_graphs

    base = Snapshot(
        model="shop", glossary_id="", glossary_fqn="shop", model_version="", proposal_id="",
        message="",
    )  # fmt: skip
    first = await fill_exports(env.models, env.om, base)  # type: ignore[arg-type]
    env.models.clear_cache()
    second = await fill_exports(env.models, env.om, base)  # type: ignore[arg-type]
    assert first.exports == second.exports
    old = parse_rdf(first.exports["ontology.ttl"].encode(), "turtle")  # type: ignore[index]
    new = parse_rdf(second.exports["ontology.ttl"].encode(), "turtle")  # type: ignore[index]
    assert isomorphic(old, new)
    d = diff_graphs(old, new, from_version="1.0.0", to_version="1.0.1")
    assert d.bump is None and d.reason == "nothing changed"


async def test_explicit_version_must_be_greater(env: Env) -> None:
    await env.release()
    env.om.change(V2_ADDED)
    for bad in ("1.0.0", "0.9.0"):
        err = await _code(env.svc.plan(human(), "shop", version=bad, bump=None))
        assert err.code == "validation_error" and "greater than" in err.message
    for hostile in ("1.0", "01.0.0", "1.0.0-rc1", "../x", "v1.0.0", "9" * 100):
        err = await _code(env.svc.plan(human(), "shop", version=hostile, bump=None))
        assert err.code == "validation_error"
    plan = await env.svc.plan(human(), "shop", version="2.0.0", bump=None)
    assert plan.version == "2.0.0" and plan.warnings == []


async def test_bump_and_version_are_exclusive(env: Env) -> None:
    err = await _code(env.svc.plan(human(), "shop", version="1.0.0", bump="minor"))
    assert err.code == "validation_error" and "not both" in err.message
    err = await _code(
        env.svc.create(human(), "shop", version="1.0.0", bump="minor", confirm_token=BAD_TOKEN)
    )
    assert err.code == "validation_error"


async def test_lower_bump_warns(env: Env) -> None:
    await env.release()
    env.om.change(V2_ADDED)
    plan = await env.svc.plan(human(), "shop", version=None, bump="patch")
    assert plan.version == "1.0.1" and plan.requested_bump == "patch"
    assert plan.warnings == ["computed 1.1.0, releasing as 1.0.1"]
    res = await env.svc.create(
        human(), "shop", version=None, bump="patch", confirm_token=plan.confirm_token
    )
    assert res.release.version == "1.0.1"


async def test_pending_publication_blocks_release(env: Env) -> None:
    plan = await env.svc.plan(human(), "shop", version=None, bump=None)
    snap = Snapshot(
        model="shop", glossary_id="g", glossary_fqn="shop", model_version="1",
        proposal_id="prop_9", message="m",
    )  # fmt: skip
    await env.pubs.add(
        Publication(
            id=new_publication_id(),
            repo=env.repo_key,
            model="shop",
            proposal_id="prop_9",
            mode="direct",
            created_at=NOW,
            updated_at=NOW,
            payload=snap,
        )
    )
    err = await _code(env.svc.plan(human(), "shop", version=None, bump=None))
    assert err.code == "validation_error" and "not in Git yet" in err.message
    assert "lakeloom git retry" in err.hint
    err = await _code(
        env.svc.create(human(), "shop", version=None, bump=None, confirm_token=plan.confirm_token)
    )
    assert err.code == "validation_error" and env.remote_tags() == []


async def test_release_needs_publish_group(env: Env) -> None:
    policies = [
        ModelPolicyEntry.model_validate(
            {"name": "shop", "access": {"publish_groups": ["publishers"]}}
        )
    ]
    env.build(policies=policies)
    outsider = agent(frozenset({"stewards"}))
    err = await _code(env.svc.plan(outsider, "shop", version=None, bump=None))
    assert err.code == "forbidden"
    err = await _code(
        env.svc.create(outsider, "shop", version=None, bump=None, confirm_token=BAD_TOKEN)
    )
    assert err.code == "forbidden"
    # an agent in the group may release; Released-By falls back to the subject
    res = await env.release(agent(frozenset({"publishers"})))
    assert res.release.released_by == "agent-1"
    assert "Released-By: agent-1" in git("log", "-1", "--format=%B", "main", cwd=env.bare)


async def test_stale_token_after_model_change(env: Env) -> None:
    plan = await env.svc.plan(human(), "shop", version=None, bump=None)
    env.om.change(V2_ADDED)
    err = await _code(
        env.svc.create(human(), "shop", version=None, bump=None, confirm_token=plan.confirm_token)
    )
    assert err.code == "conflict" and env.remote_tags() == []
    # a token for other arguments is refused too
    plan = await env.svc.plan(human(), "shop", version=None, bump=None)
    err = await _code(
        env.svc.create(
            human(), "shop", version="3.0.0", bump=None, confirm_token=plan.confirm_token
        )
    )
    assert err.code == "validation_error" and "does not match" in err.message


async def test_release_commits_direct_in_pull_request_mode(env: Env) -> None:
    env.build(git_mode="pull_request", forge="gitea")
    res = await env.release()
    heads = git("for-each-ref", "--format=%(refname)", "refs/heads", cwd=env.bare).split()
    assert heads == ["refs/heads/main"]
    assert git("rev-parse", "main", cwd=env.bare).strip() == res.release.commit_sha


async def test_changelog_lists_proposals_since_previous_tag(env: Env) -> None:
    env.push_foreign(
        "ontology(shop): add_concept x1\n\nLakeloom-Proposal: prop_1\nApproved-By: alice",
        {"ontologies/shop/ontology.ttl": "1"},
    )
    first = await env.release()
    assert "prop_1, approved by alice" in first.plan.changelog_section
    env.push_foreign(
        "ontology(shop): add_concept x2\n\nLakeloom-Proposal: prop_2\nApproved-By: bob",
        {"ontologies/shop/extra.txt": "2"},
    )
    env.push_foreign(
        "ontology(other): x\n\nLakeloom-Proposal: prop_3\nApproved-By: bob",
        {"ontologies/other/ontology.ttl": "3"},
    )
    env.push_foreign("chore: hand edit", {"ontologies/shop/notes.txt": "4"})
    env.om.change(V2_ADDED)
    second = await env.release()
    section = second.plan.changelog_section
    assert "### Proposals" in section
    assert "prop_2, approved by bob" in section
    assert "prop_1" not in section and "prop_3" not in section and "hand edit" not in section
    short = git("log", "-1", "--format=%h", "--abbrev=7", "--grep=prop_2", "main", cwd=env.bare)
    assert f"(`{short.strip()}`)" in section


async def test_rejected_push_rerenders_on_new_tip(env: Env) -> None:
    await env.release()  # creates the branch
    env.om.change(V2_ADDED)
    plan = await env.svc.plan(human(), "shop", version=None, bump=None)
    env.runner.before = lambda: env.push_foreign("foreign", {"other.txt": "x"})
    res = await env.svc.create(
        human(), "shop", version=None, bump=None, confirm_token=plan.confirm_token
    )
    parent_subject = git("log", "-1", "--format=%s", "main^", cwd=env.bare).strip()
    assert parent_subject == "foreign"
    assert env.show("shop/v1.1.0", "other.txt") == "x"
    assert git("rev-parse", "shop/v1.1.0^{}", cwd=env.bare).strip() == res.release.commit_sha


async def test_rejected_push_three_times_is_upstream_unavailable(env: Env) -> None:
    await env.release()
    env.om.change(V2_ADDED)
    plan = await env.svc.plan(human(), "shop", version=None, bump=None)
    env.runner.fail = GitError("rejected", "! [rejected] main -> main (fetch first)")
    err = await _code(
        env.svc.create(human(), "shop", version=None, bump=None, confirm_token=plan.confirm_token)
    )
    assert err.code == "upstream_unavailable" and "kept moving" in err.message
    assert git("tag", "-l", cwd=env.wc.path).split() == ["shop/v1.0.0"]


@pytest.mark.parametrize(
    ("kind", "code"),
    [("auth", "forbidden"), ("not_found", "not_found"), ("network", "upstream_unavailable"),
     ("timeout", "upstream_unavailable")],
)  # fmt: skip
async def test_push_failures_are_classified(env: Env, kind: str, code: str) -> None:
    plan = await env.svc.plan(human(), "shop", version=None, bump=None)
    env.runner.fail = GitError(kind, "boom")  # type: ignore[arg-type]
    env.runner.on = ("push",)
    err = await _code(
        env.svc.create(human(), "shop", version=None, bump=None, confirm_token=plan.confirm_token)
    )
    assert err.code == code


async def test_push_failure_leaves_no_local_tag_and_retry_is_idempotent(env: Env) -> None:
    await env.release()
    env.om.change(V2_ADDED)
    plan = await env.svc.plan(human(), "shop", version=None, bump=None)
    tip = git("rev-parse", "main", cwd=env.bare).strip()
    env.runner.fail = GitError("network", "fatal: unable to access")
    env.runner.fail_times = 1
    args: dict[str, Any] = {"version": None, "bump": None, "confirm_token": plan.confirm_token}
    err = await _code(env.svc.create(human(), "shop", **args))
    assert err.code == "upstream_unavailable"
    assert git("tag", "-l", cwd=env.wc.path).split() == ["shop/v1.0.0"]
    assert git("rev-parse", "HEAD", cwd=env.wc.path).strip() == tip  # reset to the remote tip
    rid = "rel_" + hashlib.sha256(plan.confirm_token.encode()).hexdigest()[:26]
    assert await env.releases.get(rid) is None
    assert env.remote_tags() == ["shop/v1.0.0"]
    # the retry with the same token creates exactly one tag
    res = await env.svc.create(human(), "shop", **args)
    assert not res.already_done and res.release.id == rid
    assert sorted(env.remote_tags()) == ["shop/v1.0.0", "shop/v1.1.0"]
    # and a further retry is complete already
    again = await env.svc.create(human(), "shop", **args)
    assert again.already_done and again.release == res.release
    assert again.plan.version == "1.1.0" and again.plan.previous == "1.0.0"
    assert sorted(env.remote_tags()) == ["shop/v1.0.0", "shop/v1.1.0"]


async def test_push_landed_but_reply_lost_completes_on_retry(env: Env) -> None:
    env.push_foreign("seed", {"README.md": "hi"})  # the release push is the only push
    plan = await env.svc.plan(human(), "shop", version=None, bump=None)
    env.runner.fail = GitError("network", "the remote end hung up")
    env.runner.fail_after = True  # the push lands, then the reply is lost
    env.runner.fail_times = 1
    args: dict[str, Any] = {"version": None, "bump": None, "confirm_token": plan.confirm_token}
    err = await _code(env.svc.create(human(), "shop", **args))
    assert err.code == "upstream_unavailable"
    assert env.remote_tags() == ["shop/v1.0.0"]  # it landed
    assert env.released == []
    rid = "rel_" + hashlib.sha256(plan.confirm_token.encode()).hexdigest()[:26]
    assert await env.releases.get(rid) is None
    res = await env.svc.create(human(), "shop", **args)
    assert res.already_done and res.release.id == rid and res.release.version == "1.0.0"
    assert await env.releases.get(rid) == res.release
    assert env.released == [res.release]  # the row was stored now, the hook runs once
    again = await env.svc.create(human(), "shop", **args)
    assert again.already_done and env.released == [res.release]


async def test_existing_foreign_tag_is_validation_error(env: Env) -> None:
    env.push_foreign("seed", {"README.md": "hi"})
    plan = await env.svc.plan(human(), "shop", version=None, bump=None)
    clone = env.tmp / "tagger"
    git("clone", env.remote, str(clone), cwd=env.tmp)
    git("tag", "-a", "shop/v1.0.0", "-m", "by hand", cwd=clone)
    git("push", "origin", "shop/v1.0.0", cwd=clone)
    err = await _code(
        env.svc.create(human(), "shop", version=None, bump=None, confirm_token=plan.confirm_token)
    )
    assert err.code == "validation_error" and err.message == "release shop/v1.0.0 already exists"
    err = await _code(env.svc.plan(human(), "shop", version=None, bump=None))
    assert err.message == "release shop/v1.0.0 already exists"
    # a different version is fine
    ok = await env.release(version="1.0.1")
    assert ok.release.tag == "shop/v1.0.1"


async def test_concurrent_releases_serialised(env: Env) -> None:
    a = await env.svc.plan(human(), "shop", version=None, bump=None)
    b = await env.svc.plan(human("bob"), "shop", version=None, bump=None)
    assert a.confirm_token != b.confirm_token
    results = await asyncio.gather(
        env.svc.create(human(), "shop", version=None, bump=None, confirm_token=a.confirm_token),
        env.svc.create(
            human("bob"), "shop", version=None, bump=None, confirm_token=b.confirm_token
        ),
        return_exceptions=True,
    )
    done = [r for r in results if isinstance(r, ReleaseResult)]
    failed = [r for r in results if isinstance(r, LakeloomError)]
    assert len(done) == 1 and len(failed) == 1
    assert failed[0].code == "validation_error"
    assert "nothing changed" in failed[0].message or "already exists" in failed[0].message
    assert env.remote_tags() == ["shop/v1.0.0"]
    assert await env.releases.sink_counts(env.repo_key) == {
        "none": 1, "pending": 0, "done": 0, "failed": 0,
    }  # fmt: skip


async def test_release_waits_for_publication_lock(env: Env) -> None:
    plan = await env.svc.plan(human(), "shop", version=None, bump=None)
    lock = repo_lock(env.repo_key)
    await lock.acquire()
    try:
        task = asyncio.create_task(
            env.svc.create(
                human(), "shop", version=None, bump=None, confirm_token=plan.confirm_token
            )
        )
        await asyncio.sleep(0.3)
        assert not task.done() and env.remote_tags() == []
    finally:
        lock.release()
    res = await asyncio.wait_for(task, 30)
    assert res.release.tag == "shop/v1.0.0"


async def test_create_completes_when_reader_refresh_expired(env: Env) -> None:
    env.build(refresh_seconds=0)  # every reader call wants to fetch under the lock
    assert await env.reader.list("shop") == []
    res = await asyncio.wait_for(env.release(), 30)
    assert [r.version for r in await env.reader.list("shop")] == ["1.0.0"]
    assert res.release.version == "1.0.0"


async def test_release_is_audited(env: Env) -> None:
    res = await env.release()
    rows = (await env.audit.list(None, None, 10)).items
    ok = [r for r in rows if r.tool == "create_release"]
    assert len(ok) == 1
    rec = ok[0]
    assert (rec.principal_subject, rec.model, rec.change_set_id, rec.result) == (
        "sub-alice", "shop", res.release.tag, "ok",
    )  # fmt: skip
    env.build(
        policies=[
            ModelPolicyEntry.model_validate({"name": "shop", "access": {"publish_groups": ["p"]}})
        ]
    )
    with pytest.raises(LakeloomError):
        await env.svc.create(
            agent(frozenset()), "shop", version=None, bump=None, confirm_token=BAD_TOKEN
        )
    results = [r.result for r in (await env.audit.list(None, None, 10)).items]
    assert sorted(results) == ["forbidden", "ok"]


async def test_release_fills_the_running_audit_context(env: Env) -> None:
    env.svc = ReleaseService(
        env.settings, env.models, env.om, env.wc, env.reader,  # type: ignore[arg-type]
        env.releases, env.pubs, env.confirm, None, [], clock=lambda: NOW,
    )  # fmt: skip
    trail = AuditContext()
    token = audit_context.set(trail)
    try:
        res = await env.release()
    finally:
        audit_context.reset(token)
    assert (trail.model, trail.change_set_id) == ("shop", res.release.tag)
    assert (await env.audit.list(None, None, 10)).items == []


async def test_release_never_calls_om_writes(env: Env) -> None:
    await env.release()
    env.om.change(V2_ADDED)
    await env.release()
    reads = {
        "get_glossary_by_name",
        "export_ontology",
        "export_native_ontology",
        "get_capabilities",
    }
    assert env.om.calls and set(env.om.calls) <= reads


async def test_sink_queued_when_enabled(env: Env) -> None:
    calls: list[str] = []

    async def boom(rel: Release) -> None:
        calls.append(rel.id)
        raise RuntimeError("sink hook failed")

    env.build(triplestore_enabled=True, on_released=boom)
    res = await env.release()
    assert res.triplestore == "queued" and res.release.sink_status == "pending"
    assert calls == [res.release.id]  # the hook's failure never fails the release
    stored = await env.releases.get(res.release.id)
    assert stored is not None and stored.sink_status == "pending"


async def test_sink_off_for_model_policy(env: Env) -> None:
    policies = [
        ModelPolicyEntry.model_validate({"name": "shop", "triplestore": {"enabled": False}})
    ]
    env.build(triplestore_enabled=True, policies=policies)
    res = await env.release()
    assert res.triplestore == "off" and res.release.sink_status == "none"


async def test_git_disabled_and_unknown_model(env: Env) -> None:
    off = ReleaseService(
        Settings(_env_file=None), env.models, env.om, None, None,  # type: ignore[arg-type,call-arg]
        env.releases, env.pubs, env.confirm, None, [],
    )  # fmt: skip
    err = await _code(off.plan(Actor("op"), "shop", version=None, bump=None))
    assert err.code == "validation_error" and "Git publishing is off" in err.message
    err = await _code(env.svc.plan(human(), "nope", version=None, bump=None))
    assert err.code == "not_found"
    err = await _code(env.svc.plan(human(), "../x", version=None, bump=None))
    assert err.code == "not_found"


class _FlakyReleases(ReleaseRepository):
    """A store whose first ``add`` fails (the push already landed)."""

    adds = 0

    async def add(self, rel: Release) -> Release:
        self.adds += 1
        if self.adds == 1:
            raise RuntimeError("state store down")
        return await super().add(rel)


async def test_unrecorded_release_raises_and_retry_records(env: Env) -> None:
    flaky = _FlakyReleases(env.engine)
    env.releases = flaky
    env.build()
    plan = await env.svc.plan(human(), "shop", version=None, bump=None)
    args: dict[str, Any] = {"version": None, "bump": None, "confirm_token": plan.confirm_token}
    err = await _code(env.svc.create(human(), "shop", **args))
    assert err.code == "upstream_unavailable"
    assert err.message == "release shop/v1.0.0 was pushed but could not be recorded"
    assert err.hint == "retry with the same confirm_token"
    assert env.remote_tags() == ["shop/v1.0.0"]  # the pushed tag is kept
    assert git("tag", "-l", cwd=env.wc.path).split() == ["shop/v1.0.0"]
    assert env.released == []
    audits = [r for r in (await env.audit.list(None, None, 10)).items]
    assert [(r.result, r.error_code, r.change_set_id) for r in audits] == [
        ("error", "upstream_unavailable", "shop/v1.0.0")
    ]
    res = await env.svc.create(human(), "shop", **args)
    assert res.already_done and res.release.tag == "shop/v1.0.0"
    assert flaky.adds == 2 and await env.releases.get(res.release.id) == res.release
    assert env.released == [res.release]
    again = await env.svc.create(human(), "shop", **args)
    assert again.already_done and flaky.adds == 2 and env.released == [res.release]
    assert env.remote_tags() == ["shop/v1.0.0"]
