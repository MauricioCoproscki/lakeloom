import pytest

from lakeloom.gitstore import Snapshot
from lakeloom.gitstore.snapshot import fill_exports


class _Caps:
    version = "1.12.0"


class _OM:
    async def get_capabilities(self) -> _Caps:
        return _Caps()


class _Info:
    glossary_id = "g"
    fqn = "retail"
    base_iri = None
    prefixes: dict[str, str] = {}  # noqa: RUF012


class _Models:
    def __init__(self, versions: list[str]) -> None:
        self.versions = versions

    async def get_model(self, slug: str) -> _Info:
        return _Info()

    async def version(self, slug: str) -> str:
        return self.versions.pop(0) if len(self.versions) > 1 else self.versions[0]

    async def export(self, slug: str, fmt: str) -> bytes:
        return b"x"

    async def shapes_bytes(self, slug: str, media: str) -> bytes:
        return b"s"

    async def jsonld_context_bytes(self, slug: str) -> bytes:
        return b"c"


def _base() -> Snapshot:
    return Snapshot(
        model="retail", glossary_id="", glossary_fqn="retail", model_version="",
        proposal_id="p1", message="m",
    )  # fmt: skip


async def test_fill_exports_one_graph_state() -> None:
    ok = await fill_exports(_Models(["v1"]), _OM(), _base())  # pyright: ignore[reportArgumentType]
    assert ok.model_version == "v1"
    with pytest.raises(RuntimeError, match="retail"):
        await fill_exports(_Models(["v1", "v2"]), _OM(), _base())  # pyright: ignore[reportArgumentType]
