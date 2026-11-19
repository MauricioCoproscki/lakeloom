"""applied_operations: real Proposal objects, every page, application order."""

from datetime import timedelta
from typing import Any

from test_tools_proposals import NOW, proposal

from lakeloom.core.annotations import applied_operations
from lakeloom.core.operations import AddScopeNote


def note(text: str) -> AddScopeNote:
    return AddScopeNote(op="add_scope_note", concept_fqn="shop.Order", note=text)


class Repo:
    def __init__(self, proposals: list[Any], page: int) -> None:
        self.proposals, self.page = proposals, page
        self.calls = 0

    async def list_by_status(self, statuses: Any, model: Any, cursor: Any, limit: int) -> Any:
        self.calls += 1
        start = int(cursor or 0)
        rows = [p for p in self.proposals if p.status in statuses][start : start + self.page]
        more = start + self.page < len(self.proposals)
        return rows, str(start + self.page) if more else None


async def test_reads_every_page_in_application_order() -> None:
    late = proposal("applied", [note("second")]).model_copy(
        update={"id": "p_a", "updated_at": NOW + timedelta(days=1)}
    )
    early = proposal("applied", [note("first")]).model_copy(update={"id": "p_b"})
    extra = [
        proposal("applied", [note(f"n{i}")]).model_copy(
            update={"id": f"p_{i:03}", "updated_at": NOW + timedelta(days=2 + i)}
        )
        for i in range(3)
    ]
    repo = Repo([late, early, *extra], page=2)
    ops = await applied_operations(repo, "shop")
    assert [o.note for o in ops] == ["first", "second", "n0", "n1", "n2"]  # type: ignore[attr-defined]
    assert repo.calls == 3
