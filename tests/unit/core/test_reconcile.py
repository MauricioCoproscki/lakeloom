"""Reconciliation of OpenMetadata against the last Git commit (FR-GIT-08, FR-GIT-03).

A fake OM export (``FakeModels``) and a fake working copy (``FakeWC``); the publication and
audit stores are the real SQLite repositories.
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from rdflib import Graph
from sqlalchemy.ext.asyncio import AsyncEngine

from lakeloom import metrics
from lakeloom.config import Settings
from lakeloom.core.model_policy import ModelPolicyEntry
from lakeloom.core.models import ModelInfo
from lakeloom.core.operations import AddConcept
from lakeloom.core.proposals import Proposal, ProposalPart, Provenance, ReviewRecord
from lakeloom.core.reconcile import Publisher, ReconcileService
from lakeloom.errors import LakeloomError
from lakeloom.gitstore.git import GitError
from lakeloom.gitstore.layout import Snapshot, export_files, render
from lakeloom.gitstore.publisher import GitPublisher
from lakeloom.gitstore.snapshot import take_snapshot
from lakeloom.om.models import OMCapabilities, OMChangeSet
from lakeloom.rdf.canonical import parse_rdf, serialize
from lakeloom.state.audit import AuditRepository
from lakeloom.state.db import create_engine, upgrade
from lakeloom.state.proposals import ProposalRepository
from lakeloom.state.publications import Publication, PublicationRepository

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
BASE = "http://x/ontology/shop/"
REPO = "k" * 64
SHA = "a" * 40

# What Git holds (as Lakeloom exported it at the last publication).
GIT_TTL = f"""
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix om: <https://open-metadata.org/ontology/> .
<{BASE}Customer> a skos:Concept ; skos:prefLabel "Customer" ;
    skos:definition "People who buy" ;
    skos:inScheme <https://open-metadata.org/entity/glossary/g-old> ;
    om:fullyQualifiedName "shop.Customer" .
<{BASE}Order> a skos:Concept ; skos:prefLabel "Order" ; skos:definition "An order" .
<https://open-metadata.org/entity/glossary/g-old> a skos:ConceptScheme .
"""

# The same semantic content, different bookkeeping: glossary id, FQN, prefixes, triple order.
OM_SAME = f"""
@prefix s: <http://www.w3.org/2004/02/skos/core#> .
<{BASE}Order> s:definition "An order" ; s:prefLabel "Order" ; a s:Concept ;
    <https://open-metadata.org/ontology/fullyQualifiedName> "shop.Order" ;
    s:inScheme <https://open-metadata.org/entity/glossary/g-new> .
<{BASE}Customer> s:prefLabel "Customer" ; a s:Concept ; s:definition "People who buy" ;
    s:topConceptOf <https://open-metadata.org/entity/glossary/g-new> .
<https://open-metadata.org/entity/glossary/g-new> a s:ConceptScheme ;
    <https://open-metadata.org/ontology/changeSet> "cs-999" .
"""

OM_EDITED = OM_SAME.replace('"People who buy"', '"People who buy things"')
OM_ADDED = (
    OM_SAME + f'\n<{BASE}Invoice> a s:Concept ; s:prefLabel "Invoice" ; s:definition "A bill" .\n'
)


class FakeOM:
    def __init__(self) -> None:
        self.states: dict[str, str] = {}  # change set id -> state; unknown: SUBMITTED

    async def get_capabilities(self) -> OMCapabilities:
        return OMCapabilities(reachable=True, version="1.12.0")

    async def get_change_set(self, id: str) -> OMChangeSet | None:
        return OMChangeSet(id=id, name=id, state=self.states.get(id, "SUBMITTED"))


class FakeModels:
    """The model service as reconciliation and ``fill_exports`` use it."""

    def __init__(self, ttl: str) -> None:
        self.ttl = ttl
        self.down = False
        self.invalidated: list[str | None] = []
        self.om = FakeOM()

    def _check(self) -> None:
        if self.down:
            raise LakeloomError("upstream_unavailable", "OpenMetadata is unreachable", "retry")

    def invalidate(self, slug: str | None = None) -> None:
        self.invalidated.append(slug)

    async def get_model(self, slug: str) -> ModelInfo:
        self._check()
        return ModelInfo(
            slug=slug, glossary_id="g-new", fqn=slug, base_iri=BASE, prefixes={"shop": BASE}
        )

    async def graph(self, slug: str) -> Graph:
        self._check()
        return parse_rdf(self.ttl.encode(), "turtle")

    async def version(self, slug: str) -> str:
        self._check()
        return "g1-n-"

    async def export(self, slug: str, fmt: Any) -> bytes:
        return serialize(await self.graph(slug), fmt, prefixes={"shop": BASE})

    async def shapes_bytes(self, slug: str, media: str) -> bytes:
        return b"# shapes\n"

    async def jsonld_context_bytes(self, slug: str) -> bytes:
        return b'{"@context": {}}\n'


class FakeWC:
    """Commits by sha; a sha is only readable once it is local (cloned or fetched)."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, bytes]] = {}
        self.local: set[str] = set()
        self.branches: dict[str, set[str]] = {}
        self.fetched: list[str] = []
        self.broken: GitError | None = None
        self.trailers: dict[tuple[str, str, str], str] = {}  # (ref, key, value) -> sha
        self.searched: list[tuple[str, str, str]] = []

    def add(self, sha: str, ttl: str, *, local: bool = True, branch: str | None = None) -> None:
        self.files[sha] = {"ontologies/shop/ontology.ttl": ttl.encode()}
        if local:
            self.local.add(sha)
        if branch is not None:
            self.branches.setdefault(branch, set()).add(sha)

    async def ensure(self) -> None:
        return None

    async def has_commit(self, sha: str) -> bool:
        return sha in self.local

    async def fetch_branch(self, branch: str) -> str:
        self.fetched.append(branch)
        if branch not in self.branches:
            raise GitError("not_found", "couldn't find remote ref")
        self.local |= self.branches[branch]
        return next(iter(self.branches[branch]), "f" * 40)

    async def find_trailer(self, ref: str, key: str, value: str) -> str | None:
        self.searched.append((ref, key, value))
        return self.trailers.get((ref, key, value))

    async def show(self, ref: str, path: str) -> bytes | None:
        if self.broken is not None:
            raise self.broken
        if ref not in self.local:
            raise GitError("failed", f"fatal: invalid object name '{ref}'")
        return self.files[ref].get(path)


class FakePublisher:
    enabled = True
    repo_key = REPO

    def __init__(self, repo: PublicationRepository, wc: FakeWC) -> None:
        self._repo = repo
        self.working_copy = wc

    async def enqueue(self, pub: Publication) -> Publication:
        return await self._repo.add(pub.model_copy(update={"repo": self.repo_key}))


@pytest.fixture(name="engine")
async def engine_fixture(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    await upgrade(eng)
    yield eng
    await eng.dispose()


class Kit:
    def __init__(self, engine: AsyncEngine, ttl: str = OM_SAME, *, mode: str = "direct") -> None:
        self.pubs = PublicationRepository(engine)
        self.proposals = ProposalRepository(engine)
        self.audit = AuditRepository(engine)
        self.wc = FakeWC()
        self.models = FakeModels(ttl)
        self.publisher = FakePublisher(self.pubs, self.wc)
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            public_url="http://x",  # type: ignore[arg-type]
            git_remote="file:///unused/remote.git",
            git_mode="pull_request",
        )
        policies = [ModelPolicyEntry.model_validate({"name": "shop", "git": {"mode": mode}})]
        self.service = ReconcileService(
            settings=settings,
            models=self.models,  # pyright: ignore[reportArgumentType]
            publisher=self.publisher,
            publications=self.pubs,
            proposals=self.proposals,
            audit=self.audit,
            policies=policies,
            clock=lambda: NOW,
        )

    async def published(
        self, status: str = "done", *, sha: str | None = SHA, n: int = 1, branch: str = "main"
    ) -> Publication:
        snap = Snapshot(
            model="shop",
            glossary_id="g-new",
            glossary_fqn="shop",
            base_iri=BASE,
            model_version="g0",
            last_change_set="cs-g1",
            proposal_id=f"prp_{n}",
            message="m",
        )
        return await self.pubs.add(
            Publication(
                id=f"pub_{n}",
                repo=REPO,
                model="shop",
                proposal_id=f"prp_{n}",
                mode="direct",
                status=status,  # type: ignore[arg-type]
                commit_sha=sha if status == "done" else None,
                branch=branch if status == "done" else None,
                created_at=NOW,
                updated_at=NOW,
                payload=snap,
            )
        )


async def _kit(engine: AsyncEngine, ttl: str = OM_SAME, **kw: Any) -> Kit:
    kit = Kit(engine, ttl, **kw)
    kit.wc.add(SHA, GIT_TTL)
    await kit.published()
    return kit


# --- compare ----------------------------------------------------------------------------------


async def test_bookkeeping_only_is_in_sync(engine: AsyncEngine) -> None:
    kit = await _kit(engine, OM_SAME)
    result = await kit.service.check("shop")
    assert result.status == "in_sync"
    assert (result.added, result.changed, result.removed, result.reason) == ([], [], [], None)
    assert kit.models.invalidated == ["shop"]  # a cached export never hides a direct edit


async def test_description_edit_is_drifted(engine: AsyncEngine) -> None:
    kit = await _kit(engine, OM_EDITED)
    result = await kit.service.check("shop")
    assert result.status == "drifted"
    assert result.changed == [f"{BASE}Customer"]
    assert (result.added, result.removed) == ([], [])


async def test_added_concept_is_drifted(engine: AsyncEngine) -> None:
    kit = await _kit(engine, OM_ADDED)
    result = await kit.service.check("shop")
    assert result.status == "drifted"
    assert result.added == [f"{BASE}Invoice"]


# --- skips and errors -------------------------------------------------------------------------


async def test_pending_publication_skips(engine: AsyncEngine) -> None:
    kit = await _kit(engine, OM_EDITED)
    await kit.published("pending", n=2)
    result = await kit.service.check("shop")
    assert (result.status, result.reason) == ("skipped", "publication in flight")
    run = await kit.service.run(commit=True)
    assert [r.status for r in run.results] == ["skipped"]
    assert await kit.pubs.counts(REPO) == {"pending": 1, "done": 1, "blocked": 0}


async def test_blocked_publication_skips(engine: AsyncEngine) -> None:
    kit = await _kit(engine, OM_EDITED)
    await kit.published("blocked", n=2)
    result = await kit.service.check("shop")
    assert (result.status, result.reason) == ("skipped", "publication in flight")


async def test_approval_during_check_skips(engine: AsyncEngine) -> None:
    """OM moved by an approval whose publication row lands during the compare."""
    kit = await _kit(engine, OM_EDITED)
    read = kit.models.graph

    async def graph_then_approve(slug: str) -> Graph:
        g = await read(slug)
        await kit.published("pending", n=2)
        return g

    kit.models.graph = graph_then_approve  # type: ignore[method-assign]
    result = await kit.service.check("shop")
    assert (result.status, result.reason) == ("skipped", "publication in flight")


async def test_applied_proposal_without_row_skips(engine: AsyncEngine) -> None:
    """Applied in OM, its publication row not inserted yet (GitHook runs after apply)."""
    kit = await _kit(engine, OM_EDITED)
    await kit.proposals.add(_applied("shop")[0])
    result = await kit.service.check("shop")
    assert (result.status, result.reason) == ("skipped", "publication in flight")
    assert (await kit.service.run(commit=True)).results[0].status == "skipped"
    assert await kit.pubs.counts(REPO) == {"pending": 0, "done": 1, "blocked": 0}


async def test_applied_proposal_with_done_row_is_not_in_flight(engine: AsyncEngine) -> None:
    kit = await _kit(engine, OM_EDITED)
    await kit.proposals.add(_applied("shop", pid="prp_1")[0])  # pub_1 (done) is its row
    assert (await kit.service.check("shop")).status == "drifted"


async def test_mid_apply_proposal_skips(engine: AsyncEngine) -> None:
    """Still ``submitted`` in the store, but OM already applied one of its change sets."""
    kit = await _kit(engine, OM_EDITED)
    await kit.proposals.add(_applied("shop", "submitted", om_state="SUBMITTED")[0])
    assert (await kit.service.check("shop")).status == "drifted"  # not applied in OM yet
    kit.models.om.states["cs-g9"] = "APPLIED"
    result = await kit.service.check("shop")
    assert (result.status, result.reason) == ("skipped", "publication in flight")


async def test_partly_applied_failed_proposal_skips(engine: AsyncEngine) -> None:
    kit = await _kit(engine, OM_EDITED)
    await kit.proposals.add(_applied("shop", "apply_failed", om_state="APPLIED")[0])
    result = await kit.service.check("shop")
    assert (result.status, result.reason) == ("skipped", "publication in flight")


async def test_row_between_export_and_enqueue_enqueues_nothing(engine: AsyncEngine) -> None:
    kit = await _kit(engine, OM_EDITED)
    context = kit.models.jsonld_context_bytes  # the last export fill_exports reads

    async def export_then_approve(slug: str) -> bytes:
        await kit.published("pending", n=2)
        return await context(slug)

    kit.models.jsonld_context_bytes = export_then_approve  # type: ignore[method-assign]
    [result] = (await kit.service.run(commit=True)).results
    assert (result.status, result.reason) == ("skipped", "publication in flight")
    assert [p.proposal_id for p in await kit.pubs.list("pending")] == ["prp_2"]
    assert (await kit.audit.list(None, None, 50)).items == []


async def test_export_failure_commits_nothing_and_audits_error(engine: AsyncEngine) -> None:
    kit = await _kit(engine, OM_EDITED)

    async def no_shapes(slug: str, media: str) -> bytes:
        raise RuntimeError("shapes")

    kit.models.shapes_bytes = no_shapes  # type: ignore[method-assign]
    [result] = (await kit.service.run(commit=True)).results
    assert (result.status, result.reason) == ("drifted", "not committed (export failed)")
    assert result.publication_id is None
    assert await kit.pubs.counts(REPO) == {"pending": 0, "done": 1, "blocked": 0}
    [row] = (await kit.audit.list(None, None, 50)).items
    assert (row.result, row.error_code) == ("error", "export_failed")


async def test_git_failure_is_error_not_skip(engine: AsyncEngine) -> None:
    kit = await _kit(engine, OM_EDITED)
    kit.wc.broken = GitError("failed", "fatal: bad object store")
    result = await kit.service.check("shop")
    assert (result.status, result.reason) == ("error", "Git unavailable (failed)")


async def test_never_published_skips(engine: AsyncEngine) -> None:
    kit = Kit(engine, OM_EDITED)
    result = await kit.service.check("shop")
    assert (result.status, result.reason) == ("skipped", "never published")
    assert (await kit.service.run(commit=True)).results == []  # out of the run's scope


async def test_baseline_fetched_from_the_publication_branch(engine: AsyncEngine) -> None:
    kit = Kit(engine, OM_SAME)
    kit.wc.add(SHA, GIT_TTL, local=False, branch="lakeloom/shop/prp_1")
    await kit.published(branch="lakeloom/shop/prp_1")
    assert (await kit.service.check("shop")).status == "in_sync"
    assert kit.wc.fetched == ["main", "lakeloom/shop/prp_1"]


async def test_baseline_missing_after_fetch_skips(engine: AsyncEngine) -> None:
    kit = Kit(engine, OM_EDITED)
    kit.wc.add(SHA, GIT_TTL, local=False)  # on no branch the remote still has
    kit.wc.branches["main"] = set()
    await kit.published(branch="lakeloom/shop/prp_1")
    result = await kit.service.check("shop")
    assert (result.status, result.reason) == ("skipped", "baseline commit not found")
    assert kit.wc.fetched == ["main", "lakeloom/shop/prp_1"]


async def test_squash_merged_baseline_found_by_trailer(engine: AsyncEngine) -> None:
    """The PR head commit is gone (squash merge, branch deleted): the target branch's commit
    with the publication's trailer is the baseline."""
    kit = Kit(engine, OM_SAME)
    squash = "c" * 40
    kit.wc.add(squash, GIT_TTL, branch="main")
    await kit.published(branch="lakeloom/shop/prp_1")  # SHA is on no branch any more
    kit.wc.trailers[("origin/main", "Lakeloom-Publication", "pub_1")] = squash
    assert (await kit.service.check("shop")).status == "in_sync"
    assert kit.wc.searched == [("origin/main", "Lakeloom-Publication", "pub_1")]
    kit.models.ttl = OM_EDITED
    assert (await kit.service.check("shop")).status == "drifted"


async def test_model_deleted_in_om_is_skipped(engine: AsyncEngine) -> None:
    kit = await _kit(engine, OM_EDITED)

    async def gone(slug: str) -> ModelInfo:
        raise LakeloomError("not_found", "no such glossary", "check the slug")

    kit.models.get_model = gone  # type: ignore[method-assign]
    [result] = (await kit.service.run(commit=True)).results
    assert (result.status, result.reason) == ("skipped", "model not found in OpenMetadata")
    assert await kit.pubs.counts(REPO) == {"pending": 0, "done": 1, "blocked": 0}


async def test_om_unreachable_is_error_without_commit(engine: AsyncEngine) -> None:
    kit = await _kit(engine, OM_EDITED)
    kit.models.down = True
    run = await kit.service.run(commit=True)
    [result] = run.results
    assert result.status == "error"
    assert result.reason == "OpenMetadata unavailable"
    assert result.publication_id is None
    assert await kit.pubs.counts(REPO) == {"pending": 0, "done": 1, "blocked": 0}


def _wiring(publisher: GitPublisher) -> Publisher:
    return publisher  # type check only: the real publisher fits the protocol


async def test_one_model_failure_is_an_error_result(engine: AsyncEngine) -> None:
    kit = await _kit(engine, OM_EDITED)

    async def broken(slug: str) -> Graph:
        raise KeyError(slug)

    kit.models.graph = broken  # type: ignore[method-assign]
    [result] = (await kit.service.run(commit=True)).results
    assert (result.status, result.reason) == ("error", "internal error (KeyError)")


async def test_git_disabled_is_validation_error(engine: AsyncEngine) -> None:
    kit = await _kit(engine)
    kit.publisher.enabled = False
    with pytest.raises(LakeloomError) as err:
        await kit.service.run(commit=False)
    assert (err.value.code, err.value.message) == (
        "validation_error",
        "Git publishing is not configured",
    )


# --- commit -----------------------------------------------------------------------------------


async def test_commit_enqueues_publication_with_trailers(engine: AsyncEngine) -> None:
    kit = await _kit(engine, OM_EDITED, mode="pull_request")
    run = await kit.service.run(commit=True)
    [result] = run.results
    assert result.status == "drifted" and result.publication_id is not None
    pub = next(p for p in await kit.pubs.list("pending"))
    assert pub.id == result.publication_id
    assert pub.proposal_id.startswith("rec_") and len(pub.proposal_id) == 30
    assert pub.mode == "pull_request"
    snap = pub.payload
    assert snap.exports is not None and "People who buy things" in snap.exports["ontology.ttl"]
    assert snap.last_change_set == "cs-g1"  # carried from the last publication
    subject, _, rest = snap.message.partition("\n")
    assert subject == "ontology(shop): direct edits in OpenMetadata"
    assert f"- changed: {BASE}Customer" in rest
    trailers = rest.strip().splitlines()[-3:]
    assert trailers == [
        "Lakeloom-Origin: om-direct-edit",
        f"Lakeloom-Reconcile: {pub.proposal_id}",
        f"Lakeloom-Publication: {pub.id}",
    ]
    assert snap.provenance == {
        "publication_id": pub.id,
        "model": "shop",
        "origin": "om-direct-edit",
        "reconcile_id": pub.proposal_id,
        "at": NOW.isoformat(),
    }


async def test_check_and_run_without_commit_enqueue_nothing(engine: AsyncEngine) -> None:
    kit = await _kit(engine, OM_EDITED)
    run = await kit.service.run(commit=False)
    assert [r.status for r in run.results] == ["drifted"]
    assert run.results[0].publication_id is None
    assert await kit.pubs.counts(REPO) == {"pending": 0, "done": 1, "blocked": 0}


async def test_in_sync_commits_nothing(engine: AsyncEngine) -> None:
    kit = await _kit(engine, OM_SAME)
    run = await kit.service.run(commit=True)
    assert [r.status for r in run.results] == ["in_sync"]
    assert await kit.pubs.counts(REPO) == {"pending": 0, "done": 1, "blocked": 0}
    assert kit.service.last_run == run and run.at == NOW


def _applied(
    model: str, status: str = "applied", *, om_state: str = "APPLIED", pid: str = "prp_9"
) -> tuple[Proposal, ReviewRecord]:
    review = ReviewRecord(
        decision="approve", reviewer_subject="sub-1", reviewer_username="alice", at=NOW
    )
    p = Proposal(
        id=pid,
        model=model,
        status=status,  # type: ignore[arg-type]
        parts=[
            ProposalPart(
                n=1,
                operations=[AddConcept(op="add_concept", name="Order")],
                change_set_ids={"glossary": "cs-g9"},
                om_state={"glossary": om_state},
            )
        ],
        provenance=Provenance(scan_id=None, principal_subject="a", principal_type="agent", at=NOW),
        created_at=NOW,
        updated_at=NOW,
        reviews=[review],
    )
    return p, review


async def test_reconcile_render_equals_apply_render(engine: AsyncEngine) -> None:
    """Same OM state: the reconcile commit's files equal an applied proposal's (FR-GIT-03).

    The exports (ontology x3, shapes, context) are byte-identical. ``manifest.yaml`` and the
    provenance line differ by design: they name the ``rec_`` id instead of a ``prp_`` id.
    """
    kit = await _kit(engine, OM_EDITED)
    await kit.service.run(commit=True)
    [rec] = await kit.pubs.list("pending")
    p, review = _applied("shop")
    info = await kit.models.get_model("shop")
    applied = await take_snapshot(kit.models, kit.models.om, p, review, "pub_x", info=info)  # pyright: ignore[reportArgumentType]
    assert export_files(rec.payload) == export_files(applied)
    rendered = {
        snap.proposal_id: render(
            snap, pid, previous_manifest=None, previous_provenance=None, manifests={}
        )
        for snap, pid in ((rec.payload, rec.id), (applied, "pub_x"))
    }
    rec_files, app_files = rendered[rec.proposal_id], rendered["prp_9"]
    assert rec_files.keys() == app_files.keys()
    for path in rec_files:
        if path.endswith((".ttl", ".jsonld", ".owl")):
            assert rec_files[path] == app_files[path], path


# --- report -----------------------------------------------------------------------------------


async def test_audit_row_per_drifted_model(engine: AsyncEngine) -> None:
    kit = await _kit(engine, OM_EDITED)
    before = metrics.REGISTRY.get_sample_value(
        "lakeloom_reconcile_runs_total", {"outcome": "drifted"}
    )
    run = await kit.service.run(commit=True)
    page = await kit.audit.list(None, None, 50)
    [row] = page.items
    assert (row.principal_subject, row.principal_type) == ("system:reconcile", "system")
    assert (row.tool, row.model, row.result) == ("reconcile", "shop", "ok")
    [pub] = await kit.pubs.list("pending")
    assert (row.proposal_id, row.change_set_id) == (pub.proposal_id, pub.id)
    assert pub.id == run.results[0].publication_id
    assert metrics.REGISTRY.get_sample_value("lakeloom_reconcile_drifted_models") == 1
    after = metrics.REGISTRY.get_sample_value(
        "lakeloom_reconcile_runs_total", {"outcome": "drifted"}
    )
    assert (after or 0) - (before or 0) == 1
    assert run.summary() == {  # names slugs only, never term IRIs
        "last_run": NOW.isoformat(),
        "counts": {"in_sync": 0, "drifted": 1, "skipped": 0, "error": 0},
        "drifted": ["shop"],
    }


async def test_in_sync_run_writes_no_audit_row(engine: AsyncEngine) -> None:
    kit = await _kit(engine, OM_SAME)
    await kit.service.run(commit=False)
    assert (await kit.audit.list(None, None, 50)).items == []
    assert metrics.REGISTRY.get_sample_value("lakeloom_reconcile_drifted_models") == 0


async def test_drift_log_line_has_counts_not_term_text(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    kit = await _kit(engine, OM_EDITED)
    with caplog.at_level("INFO", logger="lakeloom.reconcile"):
        await kit.service.run(commit=False)
    [rec] = [r for r in caplog.records if r.name == "lakeloom.reconcile"]
    assert rec.__dict__["model"] == "shop"
    assert (rec.__dict__["added"], rec.__dict__["changed"], rec.__dict__["removed"]) == (0, 1, 0)
    assert "Customer" not in rec.getMessage() and "Customer" not in str(rec.__dict__)


async def test_run_limited_to_models(engine: AsyncEngine) -> None:
    """``lakeloom reconcile --model``: only the named models, published or not."""
    kit = await _kit(engine, OM_EDITED)
    run = await kit.service.run(commit=False, models=["shop", "other"])
    assert [(r.model, r.status, r.reason) for r in run.results] == [
        ("shop", "drifted", None),
        ("other", "skipped", "never published"),
    ]
    assert kit.service.last_run is None  # a partial run is not the job's last run
    with pytest.raises(LakeloomError) as err:
        await kit.service.run(commit=False, models=["Not A Slug"])
    assert err.value.code == "validation_error"
