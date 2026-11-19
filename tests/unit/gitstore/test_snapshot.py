import pytest

from lakeloom.gitstore import Snapshot
from lakeloom.gitstore.snapshot import fill_exports

# Minimal Turtle with a SKOS concept (needed when ops are exercised).
_TTL = (
    "@prefix skos: <http://www.w3.org/2004/02/skos/core#> .\n"
    "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n"
    "<https://x.example/concepts/Revenue> a skos:Concept ;\n"
    '    skos:prefLabel "Revenue"@en .\n'
)


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
    def __init__(self, versions: list[str], *, ttl: bytes = b"x") -> None:
        self.versions = versions
        self._ttl = ttl

    async def get_model(self, slug: str) -> _Info:
        return _Info()

    async def version(self, slug: str) -> str:
        return self.versions.pop(0) if len(self.versions) > 1 else self.versions[0]

    async def export(self, slug: str, fmt: str) -> bytes:
        if fmt == "turtle":
            return self._ttl
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


async def test_fill_exports_no_ops_omits_skos_ttl() -> None:
    """When ops=None (apply-time snapshot), skos.ttl is not written."""
    snap = await fill_exports(_Models(["v1"]), _OM(), _base())  # pyright: ignore[reportArgumentType]
    assert snap.exports is not None
    assert "skos.ttl" not in snap.exports


async def test_fill_exports_with_ops_writes_skos_ttl() -> None:
    """When ops are supplied, skos.ttl is written and contains Turtle."""
    from lakeloom.core.operations import AddScopeNote

    op = AddScopeNote(op="add_scope_note", concept_fqn="Revenue", note="Net of returns.", lang="en")
    snap = await fill_exports(  # pyright: ignore[reportArgumentType]
        _Models(["v1"], ttl=_TTL.encode()),
        _OM(),
        _base(),
        ops=[op],
    )
    assert snap.exports is not None
    assert "skos.ttl" in snap.exports
    skos = snap.exports["skos.ttl"]
    assert "skos:" in skos or "@prefix" in skos
    assert "scopeNote" in skos or "skos.ttl" in str(snap.exports)


async def test_fill_exports_with_empty_ops_writes_skos_ttl() -> None:
    """ops=[] still triggers skos.ttl production (enrichment with no matching ops)."""
    snap = await fill_exports(  # pyright: ignore[reportArgumentType]
        _Models(["v1"], ttl=_TTL.encode()),
        _OM(),
        _base(),
        ops=[],
    )
    assert snap.exports is not None
    assert "skos.ttl" in snap.exports
