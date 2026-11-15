"""Released versions in a local bare remote, for the server tests (FR-PUB-02, FR-PUB-07).

``Remote`` pushes annotated ``retail/v<version>`` tags with a fixed tagger date through an
independent clone; ``git_deps`` builds ``AppDeps`` with Git publishing on that remote (direct
mode, Gitea links under ``WEB``) over the fake OM of ``test_tools_semantic``.
"""

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kit import DepsFactory
from test_tools_semantic import fake_deps

WEB = "https://git.example/org/onto"
B = "http://x/ontology/retail/"
_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Seeder",
    "GIT_AUTHOR_EMAIL": "s@example.invalid",
    "GIT_COMMITTER_NAME": "Seeder",
    "GIT_COMMITTER_EMAIL": "s@example.invalid",
}


def release_ttl(version: str) -> str:
    return (
        "@prefix skos: <http://www.w3.org/2004/02/skos/core#> .\n"
        f'<{B}Order> a skos:Concept ; skos:prefLabel "Order {version}"@en .\n'
        + (
            f'<{B}Invoice> a skos:Concept ; skos:prefLabel "Invoice"@en .\n'
            if version != "1.0.0"
            else ""
        )
    )


SHAPES = (
    "@prefix sh: <http://www.w3.org/ns/shacl#> .\n"
    f"<{B}OrderShape> a sh:NodeShape ; sh:targetClass <{B}Order> .\n"
)


def release_files(version: str, *, shapes: bool = True) -> dict[str, str]:
    base = "ontologies/retail"
    files = {
        f"{base}/ontology.ttl": release_ttl(version),
        f"{base}/ontology.jsonld": '{"v": "' + version + '"}\n',
        f"{base}/ontology.owl": f"<rdf:RDF v='{version}'/>\n",
        f"{base}/manifest.yaml": f'release: "{version}"\n',
    }
    if shapes:
        files[f"{base}/shapes.ttl"] = SHAPES
        files[f"{base}/context.jsonld"] = '{"@context": {"v": "' + version + '"}}\n'
    return files


class Remote:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.bare = tmp / "remote.git"
        self.git("init", "--bare", "-b", "main", str(self.bare), cwd=tmp)
        self.url = f"file://{self.bare}"
        self.clone = tmp / "seeder"
        self.git("clone", self.url, str(self.clone), cwd=tmp)
        self.git("checkout", "-B", "main", cwd=self.clone)

    def git(self, *args: str, cwd: Path, date: datetime | None = None) -> str:
        env = {"PATH": os.environ["PATH"], "HOME": str(cwd), **_ENV}
        if date is not None:
            env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = date.isoformat()
        res = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=cwd, env=env, capture_output=True, text=True, check=True,
        )  # fmt: skip
        return res.stdout

    def release(self, version: str, date: datetime, *, shapes: bool = True) -> None:
        for rel, text in release_files(version, shapes=shapes).items():
            path = self.clone / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        if not shapes:
            for name in ("shapes.ttl", "context.jsonld"):
                (self.clone / "ontologies/retail" / name).unlink(missing_ok=True)
        tag = f"retail/v{version}"
        self.git("add", "-A", cwd=self.clone)
        self.git("commit", "-m", f"release(retail): v{version}", cwd=self.clone, date=date)
        self.git("tag", "-a", "-m", f"retail v{version}", tag, cwd=self.clone, date=date)
        self.git("push", "origin", "main", tag, cwd=self.clone)

    def show(self, tag: str, name: str) -> bytes:
        return self.git("show", f"{tag}:ontologies/retail/{name}", cwd=self.bare).encode()

    def commit_of(self, tag: str) -> str:
        return self.git("rev-parse", f"{tag}^{{commit}}", cwd=self.bare).strip()


D1 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=UTC)
D2 = datetime(2026, 11, 10, 12, 30, 0, tzinfo=UTC)


def seeded(tmp: Path) -> Remote:
    remote = Remote(tmp)
    remote.release("1.0.0", D1)
    remote.release("1.1.0", D2)
    return remote


def git_deps(make_deps: DepsFactory, remote: Remote, **settings: Any) -> Any:
    values: dict[str, Any] = {
        "git_remote": remote.url,
        "git_mode": "direct",
        "forge": "gitea",
        "git_web_url": WEB,
        **settings,
    }
    return fake_deps(make_deps, **values)
