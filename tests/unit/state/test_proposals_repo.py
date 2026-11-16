"""Proposal repository queries (FR-PRO-07 drift baseline)."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from lakeloom.core.proposals import Proposal, Provenance
from lakeloom.state.db import create_engine, upgrade
from lakeloom.state.proposals import ProposalRepository

NOW = datetime(2026, 11, 15, tzinfo=UTC)


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    await upgrade(eng)
    yield eng
    await eng.dispose()


def _proposal(pid: str, status: str, scan_id: str | None, model: str = "shop") -> Proposal:
    return Proposal.model_validate(
        {
            "id": pid,
            "model": model,
            "status": status,
            "scan_id": scan_id,
            "provenance": Provenance(
                scan_id=scan_id, principal_subject="a", principal_type="agent", at=NOW
            ),
            "created_at": NOW,
            "updated_at": NOW,
        }
    )


async def test_applied_scan_ids_newest_first_distinct(engine: AsyncEngine) -> None:
    repo = ProposalRepository(engine)
    rows = [
        ("prop_01A", "applied", "scan_a"),
        ("prop_01B", "applied", "scan_b"),
        ("prop_01C", "applied", "scan_a"),  # newer proposal of an older scan
        ("prop_01D", "draft", "scan_d"),  # not applied
        ("prop_01E", "applied", None),  # an import: no scan
        ("prop_01F", "rejected", "scan_f"),
    ]
    for pid, status, scan in rows:
        await repo.add(_proposal(pid, status, scan))
    await repo.add(_proposal("prop_01G", "applied", "scan_other", model="retail"))
    assert await repo.applied_scan_ids("shop") == ["scan_a", "scan_b"]
    assert await repo.applied_scan_ids("retail") == ["scan_other"]
    assert await repo.applied_scan_ids("none") == []


async def test_models_with_applied_scans(engine: AsyncEngine) -> None:
    """The models the drift-schedule job checks: those with an applied scan (FR-PRO-07)."""
    repo = ProposalRepository(engine)
    await repo.add(_proposal("prop_01A", "applied", "scan_a"))
    await repo.add(_proposal("prop_01B", "applied", "scan_b"))
    await repo.add(_proposal("prop_01C", "applied", None, model="imported"))  # no scan
    await repo.add(_proposal("prop_01D", "draft", "scan_d", model="drafty"))  # not applied
    await repo.add(_proposal("prop_01E", "applied", "scan_e", model="retail"))
    assert await repo.models_with_applied_scans() == ["retail", "shop"]
