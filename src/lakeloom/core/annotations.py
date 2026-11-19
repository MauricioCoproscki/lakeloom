"""Applied proposal operations of a model: the input of SKOS enrichment.

Scope notes, history notes and deprecations are lakeloom-native: OpenMetadata never stores
them, so the proposal log is their only source. Every reader that shows a model with its
governance annotations (exports, releases, analytics, explanations) collects them here, in
the order they were applied, so a later note or deprecation is replayed after an earlier one.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

from lakeloom.core.operations import ProposalOperation

if TYPE_CHECKING:
    from lakeloom.core.proposals import Proposal

__all__ = ["ProposalLister", "applied_operations"]

_PAGE_SIZE = 200


class ProposalLister(Protocol):
    async def list_by_status(
        self, statuses: Sequence[str], model: str | None, cursor: str | None, limit: int
    ) -> "tuple[list[Proposal], str | None]": ...


async def applied_operations(repo: ProposalLister, model: str) -> list[ProposalOperation]:
    """Every operation of ``model``'s applied proposals, oldest application first."""
    applied: list[Proposal] = []
    cursor: str | None = None
    while True:
        page, cursor = await repo.list_by_status(["applied"], model, cursor, _PAGE_SIZE)
        applied.extend(page)
        if cursor is None:
            break
    applied.sort(key=lambda p: (p.updated_at, p.id))
    return [op for p in applied for op in p.operations()]
