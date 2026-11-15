from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from lakeloom.core.proposals import TableProvenance
from lakeloom.core.release_diff import ReleaseDiff, TermChange
from lakeloom.gitstore.layout import Snapshot, render
from lakeloom.gitstore.release_layout import (
    ProposalEntry,
    changelog_section,
    release_message,
    render_changelog,
    render_release,
    tag_message,
    tag_name,
)

DAY = date(2026, 11, 15)
EXPORTS = {
    "ontology.ttl": "@prefix ex: <http://e/> .\n",
    "ontology.jsonld": "{}\n",
    "ontology.owl": "<rdf/>\n",
}


def snap(**kw: Any) -> Snapshot:
    base: dict[str, Any] = {
        "model": "shop",
        "glossary_id": "gid",
        "glossary_fqn": "shop-glossary",
        "base_iri": "http://e/shop#",
        "prefixes": {"a": "http://a/"},
        "om_version": "1.12.0",
        "model_version": "0.3",
        "exports": dict(EXPORTS),
        "last_change_set": "cs-g2",
        "proposal_id": "prop_1",
        "source_tables": [TableProvenance(fqn="lake.s.orders", om_fqn="om.orders", version=3)],
        "provenance": {"publication_id": "pub_1"},
        "message": "msg",
    }
    base.update(kw)
    return Snapshot(**base)


def do_render(s: Snapshot) -> dict[str, bytes]:
    return render(s, "pub_1", previous_manifest=None, previous_provenance=None, manifests={})


def term(label: str, iri: str, change: str = "added") -> TermChange:
    return TermChange(iri=iri, label=label, change=change)  # type: ignore[arg-type]


def diff(**kw: Any) -> ReleaseDiff:
    base: dict[str, Any] = {
        "from_version": "1.0.0",
        "to_version": "1.1.0",
        "added": [],
        "changed": [],
        "removed": [],
        "removed_triples": 0,
        "added_triples": 0,
        "annotation_only": False,
        "bump": "minor",
        "reason": "2 terms added",
    }
    base.update(kw)
    return ReleaseDiff(**base)


def entry(**kw: Any) -> ProposalEntry:
    base = {
        "subject": "ontology(retail): add_attribute x2",
        "proposal_id": "prop_01J",
        "approved_by": "alice",
        "short_sha": "1a2b3c4",
    }
    base.update(kw)
    return ProposalEntry(**base)


def test_section_layout_exact() -> None:
    d = diff(
        added=[term("Customer.email", "http://e/shop#Customer.email")],
        changed=[term("Customer", "http://e/shop#Customer", "changed")],
        removed=[term("Legacy", "http://e/shop#Legacy", "removed")],
    )
    got = changelog_section("1.1.0", DAY, "alice", d, [entry()])
    assert got == (
        "## [1.1.0] - 2026-11-15\n"
        "\n"
        "Released by alice. Bump: minor (2 terms added).\n"
        "\n"
        "### Added\n"
        "- `Customer.email` — http://e/shop#Customer.email\n"
        "### Changed\n"
        "- `Customer` — http://e/shop#Customer\n"
        "### Removed\n"
        "- `Legacy` — http://e/shop#Legacy\n"
        "\n"
        "### Proposals\n"
        "- ontology(retail): add_attribute x2 — prop_01J, approved by alice (`1a2b3c4`)\n"
    )


def test_section_caps_groups_at_50() -> None:
    added = [term(f"T{i}", f"http://e#T{i}") for i in range(53)]
    lines = changelog_section("1.1.0", DAY, "a", diff(added=added), []).splitlines()
    assert sum(line.startswith("- `T") for line in lines) == 50
    assert lines[-1] == "- … and 3 more"
    assert "### Removed" not in lines


def test_section_sanitises_untrusted_text() -> None:
    d = diff(added=[term("a`b\n## [9.9.9]", "http://e/\x00x" + "z" * 300)])
    got = changelog_section(
        "1.1.0", DAY, "eve\n## [6.6.6]", d, [entry(subject="x\r\n### Evil", approved_by="`m`")]
    )
    assert "\n## [" not in got.split("\n", 1)[1]
    assert "\n### Evil" not in got
    assert "`ab ## [9.9.9]`" in got
    assert "\x00" not in got
    assert max(len(line) for line in got.splitlines() if "zzz" in line) < 230


def first_diff(n: int = 3) -> ReleaseDiff:
    return diff(
        from_version=None,
        to_version="1.0.0",
        added=[term(f"T{i}", f"http://e#T{i}") for i in range(n)],
        bump=None,
        reason="initial release",
    )


def test_changelog_first_release_without_proposals() -> None:
    section = changelog_section("1.0.0", DAY, "alice", first_diff(), [])
    assert section.splitlines()[:5] == [
        "## [1.0.0] - 2026-11-15",
        "",
        "Released by alice. Initial release.",
        "",
        "### Initial release (3 terms)",
    ]
    assert "### Proposals" not in section
    assert "### Added" not in section
    data, warnings = render_changelog("shop", None, section)
    assert warnings == []
    assert data.decode().startswith("# Changelog — shop\n\n")
    assert "Keep a Changelog" in data.decode()
    assert data.decode().endswith(section)


def test_changelog_prepends_and_keeps_old_sections() -> None:
    s1 = changelog_section("1.0.0", DAY, "alice", first_diff(), [])
    first, _ = render_changelog("shop", None, s1)
    s2 = changelog_section("1.1.0", DAY, "bob", diff(added=[term("X", "http://e#X")]), [entry()])
    second, warnings = render_changelog("shop", first, s2)
    assert warnings == []
    text = second.decode()
    assert text.index("## [1.1.0]") < text.index("## [1.0.0]")
    assert text.endswith(s1)
    # rendering the same release again over its own output changes nothing
    again, _ = render_changelog("shop", second, s2)
    assert again == second


def test_unparsable_changelog_replaced_with_warning() -> None:
    section = changelog_section("1.1.0", DAY, "a", diff(), [])
    for junk in (b"\xff\xfe not utf8", b"just some notes\n## [1.0.0] - x\n"):
        data, warnings = render_changelog("shop", junk, section)
        assert len(warnings) == 1
        assert data.decode().count("## [") == 1


def release_files(**kw: Any) -> dict[str, bytes]:
    base: dict[str, Any] = {
        "version": "1.1.0",
        "released_at": datetime(2026, 11, 15, 10, 20, 30, tzinfo=UTC),
        "changelog": b"# Changelog\n",
        "previous_manifest": None,
        "manifests": {},
    }
    base.update(kw)
    return render_release(snap(), **base)


def test_render_release_manifest_fields() -> None:
    prev = do_render(snap(proposal_id="prop_7", last_change_set="cs-7"))[
        "ontologies/shop/manifest.yaml"
    ]
    files = release_files(previous_manifest=prev)
    text = files["ontologies/shop/manifest.yaml"].decode()
    m = yaml.safe_load(text)
    assert m["release"] == "1.1.0"
    assert m["released_at"] == "2026-11-15T10:20:30Z"
    assert m["last_proposal"] == "prop_7"
    assert m["last_change_set"] == "cs-7"
    assert m["source_tables"][0]["fqn"] == "lake.s.orders"
    keys = [line.split(":")[0] for line in text.splitlines() if line[0].isalpha()]
    assert keys[-3:] == ["release", "released_at", "source_tables"]
    assert files["ontologies/shop/CHANGELOG.md"] == b"# Changelog\n"
    assert "| shop | http://e/shop# | cs-7 | 1.1.0 |" in files["README.md"].decode()


def test_released_at_converted_to_utc() -> None:
    at = datetime(2026, 11, 15, 12, 20, 30, 999, tzinfo=timezone(timedelta(hours=2)))
    m = yaml.safe_load(release_files(released_at=at)["ontologies/shop/manifest.yaml"])
    assert m["released_at"] == "2026-11-15T10:20:30Z"


def test_render_release_has_no_provenance_append() -> None:
    files = release_files()
    assert not any(path.startswith("provenance/") for path in files)
    assert set(files) == {
        "ontologies/shop/ontology.ttl",
        "ontologies/shop/ontology.jsonld",
        "ontologies/shop/ontology.owl",
        "ontologies/shop/manifest.yaml",
        "ontologies/shop/CHANGELOG.md",
        "README.md",
    }


def test_publication_keeps_last_release() -> None:
    released = release_files()["ontologies/shop/manifest.yaml"]
    files = render(
        snap(proposal_id="prop_9"),
        "pub_2",
        previous_manifest=released,
        previous_provenance=None,
        manifests={},
    )
    m = yaml.safe_load(files["ontologies/shop/manifest.yaml"])
    assert m["release"] == "1.1.0"
    assert m["released_at"] == "2026-11-15T10:20:30Z"
    assert m["last_proposal"] == "prop_9"
    assert "| 1.1.0 |" in files["README.md"].decode()


def test_release_message_trailers_in_order() -> None:
    msg = release_message(
        "shop", "1.1.0", diff(), "alice\nReleased-By: eve", "0.3", "rel_0123456789abcdef01234567"
    )
    assert msg.splitlines() == [
        "release(shop): v1.1.0",
        "",
        "Bump: minor (2 terms added)",
        "",
        "Lakeloom-Release: shop/v1.1.0",
        "Released-By: alice Released-By: eve",
        "Lakeloom-Model-Version: 0.3",
        "Lakeloom-Release-Id: rel_0123456789abcdef01234567",
    ]
    assert msg.endswith("\n")
    assert "Bump: none (initial release)" in release_message(
        "shop", "1.0.0", first_diff(), "a", "1", "rel_x"
    )


def test_tag_name_and_message() -> None:
    assert tag_name("shop", "1.1.0") == "shop/v1.1.0"
    section = changelog_section("1.1.0", DAY, "a", diff(), [])
    assert tag_message("shop", "1.1.0", section) == f"shop v1.1.0\n\n{section}"


_terms = st.lists(
    st.builds(
        TermChange, iri=st.text(max_size=30), label=st.text(max_size=30), change=st.just("added")
    ),
    max_size=60,
)
_entries = st.lists(
    st.builds(
        ProposalEntry,
        subject=st.text(max_size=40),
        proposal_id=st.text(max_size=20),
        approved_by=st.text(max_size=20),
        short_sha=st.text(max_size=7),
    ),
    max_size=5,
)


@settings(max_examples=50, deadline=None)
@given(added=_terms, changed=_terms, removed=_terms, proposals=_entries, user=st.text(max_size=20))
def test_release_files_deterministic(
    added: list[TermChange],
    changed: list[TermChange],
    removed: list[TermChange],
    proposals: list[ProposalEntry],
    user: str,
) -> None:
    d = diff(added=added, changed=changed, removed=removed)
    a = changelog_section("1.1.0", DAY, user, d, proposals)
    assert a == changelog_section("1.1.0", DAY, user, d, proposals)
    assert "\r" not in a
    assert sum(line.startswith("## [") for line in a.splitlines()) == 1
    data, _ = render_changelog("shop", None, a)
    assert render_changelog("shop", data, a)[0] == data
    assert release_files(changelog=data) == release_files(changelog=data)
    msg = release_message("shop", "1.1.0", d, user, "0.3", "rel_x")
    assert msg == release_message("shop", "1.1.0", d, user, "0.3", "rel_x")
    assert len(msg.splitlines()) == 8
