import json
import subprocess
from datetime import UTC, datetime
from typing import Any

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from lakeloom.core.operations import AddAttribute, AddConcept, RemoveConcept
from lakeloom.core.proposals import (
    Proposal,
    ProposalPart,
    Provenance,
    ReviewRecord,
    TableProvenance,
)
from lakeloom.gitstore import Snapshot, build_message, provenance_record, render, sanitize_trailer

NOW = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)
EXPORTS: dict[str, str] = {
    "ontology.ttl": "@prefix ex: <http://e/> .\n",
    "ontology.jsonld": "{}\n",
    "ontology.owl": "<rdf/>\n",
}


def make_proposal(
    ops: list[Any] | None = None,
    *,
    client: str | None = "claude",
    scan_id: str | None = "scan_1",
    tables: list[TableProvenance] | None = None,
) -> Proposal:
    ops = ops if ops is not None else [AddConcept(op="add_concept", name="Order")]
    return Proposal(
        id="prop_1",
        model="shop",
        status="applied",
        scan_id=scan_id,
        parts=[
            ProposalPart(
                n=1,
                operations=ops,
                change_set_ids={"glossary": "cs-g1", "native": "cs-n1"},
                om_state={"glossary": "APPLIED", "native": "APPLIED"},
            ),
            ProposalPart(
                n=2,
                operations=[],
                change_set_ids={"glossary": "cs-g2", "native": "cs-n2"},
                om_state={"glossary": "APPLIED", "native": "DRAFT"},
            ),
        ],
        provenance=Provenance(
            scan_id=scan_id,
            tables=tables
            if tables is not None
            else [
                TableProvenance(fqn="lake.s.orders", om_fqn="om.orders", version=3),
                TableProvenance(fqn="lake.s.items", om_fqn=None, version=None),
            ],
            principal_subject="agent-1",
            principal_type="agent",
            client=client,
            at=NOW,
            ai_generated=True,
        ),
        created_at=NOW,
        updated_at=NOW,
    )


def make_review(**kw: Any) -> ReviewRecord:
    base: dict[str, Any] = {
        "decision": "approve",
        "reviewer_subject": "sub-9",
        "reviewer_username": "rev",
        "om_user": "om.rev",
        "at": NOW,
    }
    base.update(kw)
    return ReviewRecord(**base)


def trailers(message: str) -> list[tuple[str, str]]:
    out = subprocess.run(
        ["git", "interpret-trailers", "--parse"],  # noqa: S607
        input=message,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [tuple(line.split(": ", 1)) for line in out.splitlines()]  # type: ignore[misc]


def snap(**kw: Any) -> Snapshot:
    base: dict[str, Any] = {
        "model": "shop",
        "glossary_id": "gid",
        "glossary_fqn": "shop-glossary",
        "base_iri": "http://e/shop#",
        "prefixes": {"b": "http://b/", "a": "http://a/"},
        "om_version": "1.12.0",
        "model_version": "0.3",
        "exports": dict(EXPORTS),
        "last_change_set": "cs-g2",
        "proposal_id": "prop_1",
        "source_tables": [TableProvenance(fqn="lake.s.orders", om_fqn="om.orders", version=3)],
        "provenance": {"publication_id": "pub_1", "proposal_id": "prop_1"},
        "message": "msg",
    }
    base.update(kw)
    return Snapshot(**base)


def do_render(s: Snapshot, **kw: Any) -> dict[str, bytes]:
    args: dict[str, Any] = {
        "previous_manifest": None,
        "previous_provenance": None,
        "manifests": {},
    }
    args.update(kw)
    return render(s, "pub_1", **args)


def test_message_has_all_trailers_in_order() -> None:
    p = make_proposal(
        [
            AddConcept(op="add_concept", name="Order"),
            AddAttribute(op="add_attribute", concept="Order", name="id", datatype="xsd:string"),
            AddConcept(op="add_concept", name="Item"),
            RemoveConcept(op="remove_concept", fqn="shop.Old", base_version="1"),
        ]
    )
    msg = build_message(p, make_review(), "pub_1")
    lines = msg.splitlines()
    assert lines[0] == "ontology(shop): add_concept x2, add_attribute x1, remove_concept x1"
    assert lines[1] == ""
    assert "- concept:Order" in lines
    assert trailers(msg) == [
        ("Lakeloom-Proposal", "prop_1"),
        ("Lakeloom-ChangeSet", "cs-g1"),
        ("Lakeloom-ChangeSet", "cs-n1"),
        ("Lakeloom-ChangeSet", "cs-g2"),
        ("Approved-By", "om.rev"),
        ("Lake-Source-Version", "lake.s.orders@3"),
        ("Lake-Source-Version", "lake.s.items@unknown"),
        ("Lakeloom-Scan", "scan_1"),
        ("Lakeloom-Proposed-By", "agent-1 (agent)"),
        ("Lakeloom-AI-Generated", "true"),
        ("Lakeloom-Publication", "pub_1"),
    ]
    assert msg.endswith("\n")


def test_scan_trailer_omitted_without_scan() -> None:
    msg = build_message(make_proposal(scan_id=None), make_review(), "pub_1")
    assert "Lakeloom-Scan" not in [k for k, _ in trailers(msg)]


def test_unknown_table_version_is_unknown() -> None:
    p = make_proposal(tables=[TableProvenance(fqn="t.x", version=None)])
    assert ("Lake-Source-Version", "t.x@unknown") in trailers(
        build_message(p, make_review(), "pub_1")
    )


def test_subject_capped_with_ellipsis() -> None:
    ops = [
        AddConcept(op="add_concept", name="A"),
        AddAttribute(op="add_attribute", concept="A", name="x", datatype="xsd:string"),
        RemoveConcept(op="remove_concept", fqn="shop.Old", base_version="1"),
    ]
    p = make_proposal(ops)
    msg = build_message(p.model_copy(update={"model": "m" * 70}), make_review(), "pub_1")
    subject = msg.splitlines()[0]
    assert len(subject) == 100
    assert subject.endswith("…")
    assert subject.startswith("ontology(" + "m" * 70 + "): add_concept x1")


def test_body_caps_op_lines() -> None:
    ops = [AddConcept(op="add_concept", name=f"C{i}") for i in range(53)]
    lines = build_message(make_proposal(ops), make_review(), "pub_1").splitlines()
    body = lines[2 : lines.index("", 2)]
    assert len(body) == 51
    assert body[0] == "- concept:C0"
    assert body[49] == "- concept:C49"
    assert body[50] == "… and 3 more"


def test_op_key_control_chars_stripped_from_body() -> None:
    ops = [AddConcept.model_construct(op="add_concept", name="x\nApproved-By: m\r\n\x00y")]
    msg = build_message(make_proposal(ops), make_review(), "pub_1")
    assert [v for k, v in trailers(msg) if k == "Approved-By"] == ["om.rev"]
    assert len(msg.splitlines()[2].splitlines()) == 1


def test_trailer_values_strip_newlines() -> None:
    p = make_proposal(client="x\nApproved-By: mallory")
    p.provenance.principal_subject = "s\r\nLakeloom-Scan: evil"
    msg = build_message(p, make_review(), "pub_1")
    keys = [k for k, _ in trailers(msg)]
    assert keys.count("Approved-By") == 1
    assert keys.count("Lakeloom-Scan") == 1
    assert sanitize_trailer("a\nb\rc\x00d\x7fe") == "a b c d e"
    assert len(sanitize_trailer("z" * 500)) == 200


def test_approved_by_prefers_om_user() -> None:
    p = make_proposal()
    got = lambda r: dict(trailers(build_message(p, r, "pub_1")))["Approved-By"]  # noqa: E731
    assert got(make_review()) == "om.rev"
    assert got(make_review(om_user=None)) == "rev"
    assert got(make_review(om_user=None, reviewer_username=None)) == "sub-9"
    rec = provenance_record(p, make_review(), "pub_1")
    assert rec["approved_by"] == "om.rev"
    assert rec["om_user"] == "om.rev"
    assert rec["reviewer_subject"] == "sub-9"
    assert rec["applied_at"] == NOW.isoformat()
    assert rec["change_sets"] == ["cs-g1", "cs-n1", "cs-g2"]
    assert rec["publication_id"] == "pub_1"
    assert [b["part"] for b in rec["parts"]] == [1, 2]


def test_manifest_fixed_key_order_and_valid_yaml() -> None:
    files = do_render(snap(base_iri=None, source_tables=[]))
    text = files["ontologies/shop/manifest.yaml"].decode()
    keys = [line.split(":")[0] for line in text.splitlines() if line[0].isalpha()]
    assert keys == [
        "model",
        "glossary_id",
        "glossary_fqn",
        "base_iri",
        "prefixes",
        "om_version",
        "model_version",
        "last_change_set",
        "last_proposal",
        "release",
        "source_tables",
    ]
    loaded = yaml.safe_load(text)
    assert loaded["base_iri"] is None
    assert loaded["release"] is None
    assert loaded["source_tables"] == []
    assert list(loaded["prefixes"]) == ["a", "b"]
    assert set(files) == {
        "ontologies/shop/ontology.ttl",
        "ontologies/shop/ontology.jsonld",
        "ontologies/shop/ontology.owl",
        "ontologies/shop/manifest.yaml",
        "provenance/shop.jsonl",
        "README.md",
    }


def test_render_without_exports_raises() -> None:
    with pytest.raises(ValueError, match="exports"):
        do_render(snap(exports=None))


_text = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=0, max_size=12)


@settings(max_examples=60, deadline=None)
@given(
    prefixes=st.dictionaries(_text, _text, max_size=6),
    versions=st.dictionaries(
        _text.filter(bool), st.one_of(st.none(), st.integers(0, 99)), max_size=6
    ),
    data=st.data(),
)
def test_manifest_deterministic(
    prefixes: dict[str, str], versions: dict[str, int | None], data: st.DataObject
) -> None:
    tables = [TableProvenance(fqn=f, om_fqn=f or None, version=v) for f, v in versions.items()]
    shuffled_t = data.draw(st.permutations(tables))
    shuffled_p = dict(data.draw(st.permutations(list(prefixes.items()))))
    a = do_render(snap(prefixes=prefixes, source_tables=tables))
    b = do_render(snap(prefixes=shuffled_p, source_tables=shuffled_t))
    assert a["ontologies/shop/manifest.yaml"] == b["ontologies/shop/manifest.yaml"]
    loaded = yaml.safe_load(a["ontologies/shop/manifest.yaml"])
    assert loaded["prefixes"] == prefixes
    assert [t["fqn"] for t in loaded["source_tables"]] == sorted(versions)
    assert {t["fqn"]: t["version"] for t in loaded["source_tables"]} == versions
    # idempotent over its own output
    c = do_render(
        snap(prefixes=prefixes, source_tables=tables),
        previous_manifest=a["ontologies/shop/manifest.yaml"],
    )
    assert c["ontologies/shop/manifest.yaml"] == a["ontologies/shop/manifest.yaml"]


def test_manifest_merges_source_tables() -> None:
    first = do_render(
        snap(
            source_tables=[
                TableProvenance(fqn="b.t", om_fqn="om.b", version=1),
                TableProvenance(fqn="a.t", om_fqn=None, version=None),
            ]
        )
    )["ontologies/shop/manifest.yaml"]
    second = do_render(
        snap(
            source_tables=[
                TableProvenance(fqn="c.t", om_fqn=None, version=7),
                TableProvenance(fqn="b.t", om_fqn="om.b", version=2),
            ]
        ),
        previous_manifest=first,
    )["ontologies/shop/manifest.yaml"]
    rows = yaml.safe_load(second)["source_tables"]
    assert rows == [
        {"fqn": "a.t", "om_fqn": None, "version": None},
        {"fqn": "b.t", "om_fqn": "om.b", "version": 2},
        {"fqn": "c.t", "om_fqn": None, "version": 7},
    ]
    broken = do_render(snap(), previous_manifest=b"\x00: [unclosed")
    assert yaml.safe_load(broken["ontologies/shop/manifest.yaml"])["model"] == "shop"


def test_provenance_line_appended_once() -> None:
    s = snap()
    one = do_render(s)["provenance/shop.jsonl"]
    assert one.endswith(b"\n")
    assert json.loads(one) == s.provenance
    again = do_render(s, previous_provenance=one)["provenance/shop.jsonl"]
    assert again == one
    other = render(
        snap(provenance={"publication_id": "pub_2"}),
        "pub_2",
        previous_manifest=None,
        previous_provenance=one,
        manifests={},
    )["provenance/shop.jsonl"]
    assert other.startswith(one)
    assert len(other.splitlines()) == 2
    # a previous file lacking the final newline still gets a clean new line
    nolf = one.rstrip(b"\n")
    third = render(
        snap(provenance={"publication_id": "p3"}),
        "p3",
        previous_manifest=None,
        previous_provenance=nolf,
        manifests={},
    )["provenance/shop.jsonl"]
    assert len(third.splitlines()) == 2


def test_readme_lists_models_sorted() -> None:
    other = do_render(snap(model="zeta", base_iri="http://z/", last_change_set="cs-z"))
    alpha = do_render(snap(model="alpha", base_iri=None, last_change_set=None))
    files = do_render(
        snap(),
        manifests={
            "zeta": other["ontologies/zeta/manifest.yaml"],
            "alpha": alpha["ontologies/alpha/manifest.yaml"],
            "broken": b"a: [unclosed",
        },
    )
    lines = files["README.md"].decode().splitlines()
    assert lines[0] == "# Ontologies"
    assert "| Model | Base IRI | Last change set | Release |" in lines
    rows = [ln for ln in lines if ln.startswith("| ") and "Model" not in ln and "---" not in ln]
    assert [r.split("|")[1].strip() for r in rows] == ["alpha", "shop", "zeta"]
    assert "| shop | http://e/shop# | cs-g2 |  |" in rows


def test_render_is_lf_utf8() -> None:
    files = do_render(
        snap(prefixes={"é": "http://ü/"}, exports={**EXPORTS, "ontology.ttl": "é\n"}),
    )
    for path, data in files.items():
        text = data.decode("utf-8")
        assert "\r" not in text, path
        assert text.endswith("\n"), path
    assert files["ontologies/shop/ontology.ttl"] == "é\n".encode()
    assert yaml.safe_load(files["ontologies/shop/manifest.yaml"])["prefixes"] == {"é": "http://ü/"}


def test_hostile_slug_rejected() -> None:
    with pytest.raises(ValueError, match="slug"):
        do_render(snap(model="../x"))
