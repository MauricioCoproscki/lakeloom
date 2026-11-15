from collections.abc import Callable
from pathlib import Path

import pytest

from lakeloom.errors import LakeloomError
from lakeloom.gitstore import WorkingCopy
from lakeloom.gitstore.release_reader import ReleaseReader
from lakeloom.rdf.canonical import parse_rdf

TTL = (
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    "@prefix skos: <http://www.w3.org/2004/02/skos/core#> .\n"
    '<https://x.example/A> a owl:Class, skos:Concept ; skos:prefLabel "A"@en .\n'
)
SHAPES = (
    "@prefix sh: <http://www.w3.org/ns/shacl#> .\n"
    "<https://x.example/AShape> a sh:NodeShape ; sh:targetClass <https://x.example/A> .\n"
)
GitCmd = Callable[..., str]


class Seeder:
    def __init__(self, tmp_path: Path, remote_url: str, git_cmd: GitCmd) -> None:
        self.clone = tmp_path / "seeder"
        self.git = git_cmd
        git_cmd("clone", remote_url, str(self.clone), cwd=tmp_path)
        git_cmd("checkout", "-B", "main", cwd=self.clone)

    def release(self, tag: str, files: dict[str, str] | None, *, annotated: bool = True) -> None:
        for rel, text in (files or {}).items():
            p = self.clone / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self.git("add", "-A", cwd=self.clone)
        self.git("commit", "--allow-empty", "-m", f"c {tag}", cwd=self.clone)
        if annotated:
            self.git("tag", "-a", "-m", f"{tag}\n\nbody", tag, cwd=self.clone)
        else:
            self.git("tag", tag, cwd=self.clone)
        self.git("push", "origin", "main", tag, cwd=self.clone)


def full(slug: str, ttl: str = TTL) -> dict[str, str]:
    base = f"ontologies/{slug}"
    return {
        f"{base}/ontology.ttl": ttl,
        f"{base}/ontology.jsonld": "{}",
        f"{base}/ontology.owl": "<rdf/>",
        f"{base}/shapes.ttl": SHAPES,
        f"{base}/context.jsonld": "{}",
        f"{base}/manifest.yaml": "m: 1\n",
        f"{base}/CHANGELOG.md": "# c\n",
    }


@pytest.fixture
def seed(tmp_path: Path, remote_url: str, git_cmd: GitCmd) -> Seeder:
    return Seeder(tmp_path, remote_url, git_cmd)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def reader(wc: WorkingCopy, clock: Clock) -> ReleaseReader:
    return ReleaseReader(wc, "key-" + str(id(wc)), refresh_seconds=30, clock=clock)


async def test_list_sorted_semver_desc(seed: Seeder, reader: ReleaseReader) -> None:
    for v in ("1.2.0", "1.10.0", "1.9.3", "2.0.0"):
        seed.release(f"retail/v{v}", full("retail"))
    got = await reader.list("retail")
    assert [r.version for r in got] == ["2.0.0", "1.10.0", "1.9.3", "1.2.0"]
    assert got[0].tag == "retail/v2.0.0"
    assert (await reader.get("retail", "1.9.3")).commit_sha != got[0].commit_sha


async def test_list_ignores_non_semver_and_foreign_tags(
    seed: Seeder, reader: ReleaseReader
) -> None:
    seed.release("retail/v1.0.0", full("retail"))
    seed.release("retail/vbeta", full("retail"))
    seed.release("retail_x/v1.0.0", full("retail_x"))
    seed.git("rm", "-rq", "ontologies/retail", cwd=seed.clone)
    seed.release("retail/v1.1.0", {"README.md": "no ontology\n"})
    assert [r.version for r in await reader.list("retail")] == ["1.0.0"]
    assert [r.version for r in await reader.list("retail_x")] == ["1.0.0"]


async def test_list_refresh_is_throttled(seed: Seeder, reader: ReleaseReader, clock: Clock) -> None:
    seed.release("retail/v1.0.0", full("retail"))
    assert [r.version for r in await reader.list("retail")] == ["1.0.0"]
    seed.release("retail/v1.1.0", full("retail", TTL + "# 1.1\n"))
    assert [r.version for r in await reader.list("retail")] == ["1.0.0"]
    clock.now += 31
    assert [r.version for r in await reader.list("retail")] == ["1.1.0", "1.0.0"]
    seed.release("retail/v1.2.0", full("retail", TTL + "# 1.2\n"))
    reader.invalidate("retail")
    assert next(r.version for r in await reader.list("retail")) == "1.2.0"


async def test_file_cache_hits_by_object_sha(
    seed: Seeder, reader: ReleaseReader, wc: WorkingCopy, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed.release("retail/v1.0.0", full("retail"))
    await reader.list("retail")
    calls: list[tuple[str, str]] = []
    real = wc.show

    async def spy(ref: str, path: str) -> bytes | None:
        calls.append((ref, path))
        return await real(ref, path)

    monkeypatch.setattr(wc, "show", spy)
    assert await reader.file("retail", "1.0.0", "ontology.ttl") == TTL.encode()
    assert calls == []  # read while listing
    assert await reader.file("retail", "1.0.0", "CHANGELOG.md") == b"# c\n"
    assert await reader.file("retail", "1.0.0", "CHANGELOG.md") == b"# c\n"
    assert len(calls) == 1


async def test_ntriples_and_skos_converted_from_turtle(seed: Seeder, reader: ReleaseReader) -> None:
    seed.release("retail/v1.0.0", full("retail"))
    nt = await reader.ontology("retail", "1.0.0", "ntriples")
    assert b"<https://x.example/A>" in nt and nt.count(b"\n") >= 2
    skos = await reader.ontology("retail", "1.0.0", "skos")
    assert len(parse_rdf(skos, "turtle")) > 0
    assert await reader.ontology("retail", "1.0.0", "turtle") == TTL.encode()
    assert await reader.ontology("retail", "1.0.0", "rdfxml") == b"<rdf/>"
    assert await reader.ontology("retail", "1.0.0", "jsonld") == b"{}"
    assert await reader.shapes("retail", "1.0.0", "text/turtle") == SHAPES.encode()
    assert b"NodeShape" in await reader.shapes("retail", "1.0.0", "application/n-triples")
    assert b"@id" in await reader.shapes("retail", "1.0.0", "application/ld+json")


async def test_release_without_shapes_is_not_found(seed: Seeder, reader: ReleaseReader) -> None:
    seed.release("retail/v1.0.0", {"ontologies/retail/ontology.ttl": TTL})
    with pytest.raises(LakeloomError) as exc:
        await reader.shapes("retail", "1.0.0", "text/turtle")
    assert exc.value.code == "not_found"
    assert "has no shapes" in exc.value.message
    with pytest.raises(LakeloomError, match="has no shapes"):
        await reader.file("retail", "1.0.0", "context.jsonld")


async def test_reader_rejects_unsafe_version(seed: Seeder, reader: ReleaseReader) -> None:
    seed.release("retail/v1.0.0", full("retail"))
    for bad in ("--output=x", "1.0", "v1.0.0", "1.0.0 ", "9.9.9", "", "../x"):
        with pytest.raises(LakeloomError) as exc:
            await reader.get("retail", bad)
        assert exc.value.code == "not_found"
        assert exc.value.hint == "GET /v1/models/{slug}/versions"
        with pytest.raises(LakeloomError, match="no release"):
            await reader.file("retail", bad, "ontology.ttl")
    assert await reader.list("-bad") == []
