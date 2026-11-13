import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from lakeloom.gitstore import GitError, WorkingCopy

PushForeign = Callable[[str, dict[str, str]], None]


async def test_clone_commit_push_roundtrip(wc: WorkingCopy, push_foreign: PushForeign) -> None:
    push_foreign("seed", {"README.md": "hi\n"})
    await wc.ensure()
    assert await wc.remote_head("main") is not None
    await wc.checkout_tip("main")
    assert wc.read("README.md") == b"hi\n"
    wc.write({"ontologies/a/ontology.ttl": b"x\n"})
    sha = await wc.commit("add a\n\nLakeloom-Proposal: p1\n")
    await wc.push("main", "main")
    assert await wc.remote_head("main") == sha
    assert await wc.find_trailer("HEAD", "Lakeloom-Proposal", "p1") == sha


async def test_remote_head_none_for_empty_repo(wc: WorkingCopy) -> None:
    await wc.ensure()
    assert await wc.remote_head("main") is None
    assert await wc.remote_head("nonexist") is None


async def test_init_branch_on_empty_remote(wc: WorkingCopy) -> None:
    await wc.ensure()
    sha = await wc.init_branch("main", {"README.md": b"# new\n"}, "init")
    await wc.push("main", "main")
    assert await wc.remote_head("main") == sha
    assert wc.read("README.md") == b"# new\n"


async def test_push_rejected_when_remote_moved(wc: WorkingCopy, push_foreign: PushForeign) -> None:
    push_foreign("seed", {"a.txt": "1\n"})
    await wc.ensure()
    await wc.checkout_tip("main")
    push_foreign("moved", {"b.txt": "2\n"})
    wc.write({"c.txt": b"3\n"})
    await wc.commit("mine")
    with pytest.raises(GitError) as exc:
        await wc.push("main", "main")
    assert exc.value.kind == "rejected"
    await wc.push("main", "main", force=True)


async def test_start_branch_and_push_to_new_remote_branch(
    wc: WorkingCopy, push_foreign: PushForeign
) -> None:
    push_foreign("seed", {"a.txt": "1\n"})
    await wc.ensure()
    await wc.start_branch("lakeloom/m/p1", "main")
    wc.write({"b.txt": b"2\n"})
    sha = await wc.commit("change")
    await wc.push("lakeloom/m/p1", "lakeloom/m/p1")
    assert await wc.remote_head("lakeloom/m/p1") == sha
    assert await wc.remote_head("main") != sha


async def test_checkout_tip_discards_local_changes(
    wc: WorkingCopy, push_foreign: PushForeign
) -> None:
    push_foreign("seed", {"a.txt": "1\n"})
    await wc.ensure()
    await wc.checkout_tip("main")
    wc.write({"a.txt": b"dirty\n", "junk.txt": b"j"})
    await wc.checkout_tip("main")
    assert wc.read("a.txt") == b"1\n"
    assert wc.read("junk.txt") is None


async def test_find_trailer_exact_match(wc: WorkingCopy) -> None:
    await wc.ensure()
    await wc.init_branch("main", {"a": b"1"}, "one\n\nLakeloom-Proposal: p10\n")
    assert await wc.find_trailer("main", "Lakeloom-Proposal", "p1") is None
    assert await wc.find_trailer("main", "Lakeloom-Proposal", "p10") is not None
    assert await wc.find_trailer("nope", "Lakeloom-Proposal", "p10") is None


def test_write_refuses_parent_paths(wc: WorkingCopy) -> None:
    wc.path.mkdir(parents=True)
    for bad in ("../x", "a/../../x", "/abs", ".git/config", ""):
        with pytest.raises(ValueError, match="unsafe"):
            wc.write({bad: b"x"})
    assert not (wc.path.parent / "x").exists()


async def test_ensure_recovers_corrupt_copy(wc: WorkingCopy, push_foreign: PushForeign) -> None:
    push_foreign("seed", {"a.txt": "1\n"})
    await wc.ensure()
    shutil.rmtree(wc.path / ".git" / "objects")
    (wc.path / ".git" / "HEAD").write_text("garbage")
    await wc.ensure()
    await wc.checkout_tip("main")
    assert wc.read("a.txt") == b"1\n"
    shutil.rmtree(wc.path / ".git")
    await wc.ensure()
    assert (wc.path / ".git").is_dir()


async def test_ensure_repoints_origin(wc: WorkingCopy, other_bare: Path) -> None:
    await wc.ensure()
    other = other_bare
    moved = WorkingCopy(wc._runner, wc.path, f"file://{other}")
    await moved.ensure()
    assert (await moved._git("remote", "get-url", "origin")).strip() == f"file://{other}"


async def test_lf_and_bytes_preserved(wc: WorkingCopy) -> None:
    await wc.ensure()
    payload = b"a\r\nb\nc\x00\xff\n"
    await wc.init_branch("main", {"f.bin": payload, "g.txt": b"x\r\ny\r\n"}, "init")
    await wc.push("main", "main")
    await wc.checkout_tip("main")
    assert wc.read("f.bin") == payload
    assert wc.read("g.txt") == b"x\r\ny\r\n"
    assert (await wc._git("cat-file", "-p", "HEAD:g.txt")).encode() == b"x\r\ny\r\n"


async def test_commit_nothing_staged(wc: WorkingCopy) -> None:
    await wc.ensure()
    await wc.init_branch("main", {"a": b"1"}, "init")
    with pytest.raises(GitError) as exc:
        await wc.commit("empty")
    assert exc.value.kind == "failed"
    assert "nothing to commit" in exc.value.message


async def test_list_files(wc: WorkingCopy) -> None:
    await wc.ensure()
    wc.write({"ontologies/b/m.yaml": b"1", "ontologies/a/m.yaml": b"2", "README.md": b"3"})
    assert wc.list_files("ontologies") == ["ontologies/a/m.yaml", "ontologies/b/m.yaml"]
    assert wc.list_files("missing") == []
    assert ".git/HEAD" not in wc.list_files("")
    assert wc.list_files("") == sorted(wc.list_files(""))
    with pytest.raises(ValueError, match="unsafe"):
        wc.list_files("../x")


async def test_symlinks_cannot_escape_the_repo(
    wc: WorkingCopy,
    push_foreign: PushForeign,
    tmp_path: Path,
    git_cmd: Callable[..., str],
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    push_foreign("seed", {"a.txt": "1\n"})
    # commit a symlink pointing outside the repository through a foreign clone
    clone = tmp_path / "linker"
    git_cmd("clone", wc._remote, str(clone), cwd=tmp_path)
    (clone / "m").mkdir()
    (clone / "m" / "manifest.yaml").symlink_to(secret)
    git_cmd("add", "-A", cwd=clone)
    git_cmd("commit", "-m", "link", cwd=clone)
    git_cmd("push", "origin", "main", cwd=clone)

    await wc.ensure()
    await wc.checkout_tip("main")
    # core.symlinks=false: checked out as a plain file holding the link text
    assert not (wc.path / "m" / "manifest.yaml").is_symlink()
    assert wc.read("m/manifest.yaml") != b"TOP-SECRET"
    # a symlink created behind our back is refused by read() and skipped by list_files()
    (wc.path / "evil").symlink_to(secret)
    with pytest.raises(ValueError, match="unsafe"):
        wc.read("evil")
    assert "evil" not in wc.list_files("")
