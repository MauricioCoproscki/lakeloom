"""The deterministic files of an ontology commit and its commit message (FR-GIT-02..04).

Pure functions over a ``Snapshot`` taken at apply time. The manifest is written by a small
writer of its own (not a YAML library) so key order and quoting never depend on a dependency:
strings are double-quoted JSON scalars (valid YAML), ``null`` is bare, integers are bare.
Text that comes from agents (operation keys, client names, subjects) is sanitised before it
reaches the commit message, so it can neither inject trailers nor extra headers.
"""

import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any, Literal, cast

import yaml
from pydantic import BaseModel, Field

from lakeloom.core.model_create import SLUG_PATTERN
from lakeloom.core.operations import op_key
from lakeloom.core.proposals import (
    SIDES,
    Proposal,
    ReviewRecord,
    TableProvenance,
    provenance_block,
)
from lakeloom.core.release_diff import ReleaseDiff

SUBJECT_MAX = 100
BODY_MAX_OPS = 50
TRAILER_MAX = 200
REQUIRED_EXPORTS = ("ontology.ttl", "ontology.jsonld", "ontology.owl")
# written when present; queued payloads from before M3a carry only the required three
# skos.ttl added in M8: releases enriched with annotation ops from proposals
OPTIONAL_EXPORTS = (
    "shapes.ttl",
    "context.jsonld",
    "skos.ttl",
    # stage deliverables of a release (a release needs no scan, so no data dictionary)
    "deliverables/glossary.md",
    "deliverables/glossary.csv",
    "deliverables/taxonomy.md",
    "deliverables/taxonomy.mmd",
    "deliverables/taxonomy.csv",
    "deliverables/synonyms.txt",
    "deliverables/synonyms.csv",
    "deliverables/deprecations.csv",
)

_SLUG = re.compile(SLUG_PATTERN)
# characters YAML double-quoted scalars may not carry literally (JSON leaves them unescaped)
_YAML_UNSAFE = re.compile("[\x7f-\x9f\\u2028\\u2029\\ufeff\\ufffe\\uffff]")


class Snapshot(BaseModel):
    """Everything a commit needs, captured when the proposal was applied."""

    model: str
    glossary_id: str
    glossary_fqn: str
    base_iri: str | None = None
    prefixes: dict[str, str] = Field(default_factory=dict)
    om_version: str | None = None
    model_version: str
    # UTF-8 text; None when the export at apply failed (the publisher exports at publish time)
    exports: (
        dict[
            Literal[
                "ontology.ttl",
                "ontology.jsonld",
                "ontology.owl",
                "shapes.ttl",
                "context.jsonld",
                "skos.ttl",
            ],
            str,
        ]
        | None
    ) = None
    last_change_set: str | None = None
    proposal_id: str
    source_tables: list[TableProvenance] = Field(default_factory=list[TableProvenance])
    provenance: dict[str, Any] = Field(default_factory=dict)
    message: str


# --- commit message -------------------------------------------------------------------------


def _clean(value: str) -> str:
    """Control characters (CR, LF, NUL, DEL, C1, line/paragraph separators) become spaces."""
    return "".join(" " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c for c in value)


def sanitize_trailer(value: str) -> str:
    """A trailer value on one line: control characters to spaces, trimmed, capped at 200."""
    return _clean(value).strip()[:TRAILER_MAX]


def _trailer(key: str, value: str) -> str:
    return f"{key}: {sanitize_trailer(value) or 'unknown'}"


def applied_change_sets(p: Proposal) -> list[str]:
    """Change sets that are APPLIED, in part order with the glossary side first."""
    return [
        cs
        for part in p.parts
        for side in SIDES
        if (cs := part.change_set_ids.get(side)) is not None
        and part.om_state.get(side) == "APPLIED"
    ]


def approver(approval: ReviewRecord) -> str:
    return approval.om_user or approval.reviewer_username or approval.reviewer_subject


def _subject(p: Proposal) -> str:
    counts: dict[str, int] = {}
    for op in p.operations():
        counts[op.op] = counts.get(op.op, 0) + 1
    kinds = ", ".join(f"{kind} x{n}" for kind, n in counts.items())
    subject = _clean(f"ontology({p.model}): {kinds}").rstrip(" :")
    return subject if len(subject) <= SUBJECT_MAX else subject[: SUBJECT_MAX - 1] + "…"


def _origin_trailers(p: Proposal) -> list[str]:
    """``Lakeloom-Origin`` and ``Lakeloom-Import`` of import, Git, drift and direct proposals
    (FR-PRO-03); scan-origin messages are unchanged."""
    prov = p.provenance
    if prov.origin not in ("import", "git", "drift", "direct"):
        return []
    lines = [_trailer("Lakeloom-Origin", prov.origin)]
    doc = prov.source_document
    if doc is not None and prov.origin == "import":
        lines.append(_trailer("Lakeloom-Import", f"sha256:{doc.sha256}"))
    elif doc is not None and prov.origin == "git":
        commit = (doc.git_commit or "unknown")[:12]
        lines.append(_trailer("Lakeloom-Import", f"{doc.git_ref or 'unknown'}@{commit}"))
    return lines


def build_message(p: Proposal, approval: ReviewRecord, publication_id: str) -> str:
    """Subject, a body of operation keys, then the trailers (FR-GIT-04, FR-PRO-03 c)."""
    lines = [_subject(p), ""]
    keys = [op_key(op) for op in p.operations()]
    if keys:
        lines += [f"- {sanitize_trailer(k)}" for k in keys[:BODY_MAX_OPS]]
        if len(keys) > BODY_MAX_OPS:
            lines.append(f"… and {len(keys) - BODY_MAX_OPS} more")
        lines.append("")
    prov = p.provenance
    lines.append(_trailer("Lakeloom-Proposal", p.id))
    lines += [_trailer("Lakeloom-ChangeSet", cs) for cs in applied_change_sets(p)]
    lines.append(_trailer("Approved-By", approver(approval)))
    lines += [
        _trailer(
            "Lake-Source-Version",
            f"{t.fqn}@{t.version if t.version is not None else 'unknown'}",
        )
        for t in prov.tables
    ]
    if prov.scan_id:
        lines.append(_trailer("Lakeloom-Scan", prov.scan_id))
    lines.append(
        _trailer("Lakeloom-Proposed-By", f"{prov.principal_subject} ({prov.principal_type})")
    )
    lines += _origin_trailers(p)
    lines.append(_trailer("Lakeloom-AI-Generated", "true" if prov.ai_generated else "false"))
    lines.append(_trailer("Lakeloom-Publication", publication_id))
    return "\n".join(lines) + "\n"


RECONCILE_ORIGIN = "om-direct-edit"


def build_reconcile_message(
    slug: str, diff: ReleaseDiff, reconcile_id: str, publication_id: str
) -> str:
    """The message of a reconcile commit: direct edits found in OpenMetadata (FR-GIT-08).

    The body lists up to 50 term keys (``added|changed|removed: <IRI>``), like
    :func:`build_message`; ``Lakeloom-Publication`` is written here, as there.
    """
    subject = _clean(f"ontology({slug}): direct edits in OpenMetadata")
    subject = subject if len(subject) <= SUBJECT_MAX else subject[: SUBJECT_MAX - 1] + "…"
    lines = [subject, ""]
    keys = [f"{t.change}: {t.iri}" for t in (*diff.added, *diff.changed, *diff.removed)]
    if keys:
        lines += [f"- {sanitize_trailer(k)}" for k in keys[:BODY_MAX_OPS]]
        if len(keys) > BODY_MAX_OPS:
            lines.append(f"… and {len(keys) - BODY_MAX_OPS} more")
        lines.append("")
    lines.append(_trailer("Lakeloom-Origin", RECONCILE_ORIGIN))
    lines.append(_trailer("Lakeloom-Reconcile", reconcile_id))
    lines.append(_trailer("Lakeloom-Publication", publication_id))
    return "\n".join(lines) + "\n"


def provenance_record(p: Proposal, approval: ReviewRecord, publication_id: str) -> dict[str, Any]:
    """The line appended to ``provenance/<slug>.jsonl``."""
    return {
        "publication_id": publication_id,
        "proposal_id": p.id,
        "model": p.model,
        "approved_by": approver(approval),
        "om_user": approval.om_user,
        "reviewer_subject": approval.reviewer_subject,
        "applied_at": approval.at.isoformat(),
        "change_sets": applied_change_sets(p),
        "parts": [provenance_block(p, part) for part in p.parts],
    }


# --- manifest -------------------------------------------------------------------------------


def yaml_scalar(value: str | int | None) -> str:
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    quoted = json.dumps(value, ensure_ascii=False)
    return _YAML_UNSAFE.sub(lambda m: f"\\u{ord(m.group()):04x}", quoted)


def _merged_tables(
    previous: bytes | None, new: list[TableProvenance]
) -> list[dict[str, str | int | None]]:
    rows: dict[str, dict[str, str | int | None]] = {}
    for row in _manifest_tables(previous):
        rows[row["fqn"]] = row  # type: ignore[index]
    for t in new:
        rows[t.fqn] = {"fqn": t.fqn, "om_fqn": t.om_fqn, "version": t.version}
    return [rows[k] for k in sorted(rows)]


def _manifest_tables(previous: bytes | None) -> list[dict[str, str | int | None]]:
    loaded = load_manifest(previous)
    out: list[dict[str, str | int | None]] = []
    for item in cast("list[Any]", loaded.get("source_tables") or []) if loaded else []:
        if not isinstance(item, dict):
            continue
        row = cast("dict[str, Any]", item)
        if not isinstance(row.get("fqn"), str):
            continue
        om_fqn, version = row.get("om_fqn"), row.get("version")
        out.append(
            {
                "fqn": row["fqn"],
                "om_fqn": om_fqn if isinstance(om_fqn, str) else None,
                "version": version
                if isinstance(version, int) and not isinstance(version, bool)
                else None,
            }
        )
    return out


def load_manifest(data: bytes | None) -> dict[str, Any] | None:
    if data is None:
        return None
    try:
        loaded = yaml.safe_load(data)
    except (yaml.YAMLError, UnicodeDecodeError):
        return None
    return cast("dict[str, Any]", loaded) if isinstance(loaded, dict) else None


def _carried(previous: bytes | None, key: str) -> str | None:
    loaded = load_manifest(previous)
    value = loaded.get(key) if loaded else None
    return value if isinstance(value, str) else None


def manifest_bytes(
    s: Snapshot,
    previous: bytes | None,
    *,
    release: str | None = None,
    released_at: str | None = None,
) -> bytes:
    """The manifest; ``release`` and ``released_at`` default to the previous manifest's values."""
    if release is None:
        release, released_at = _carried(previous, "release"), _carried(previous, "released_at")
    out = [
        f"model: {yaml_scalar(s.model)}",
        f"glossary_id: {yaml_scalar(s.glossary_id)}",
        f"glossary_fqn: {yaml_scalar(s.glossary_fqn)}",
        f"base_iri: {yaml_scalar(s.base_iri)}",
    ]
    if s.prefixes:
        out.append("prefixes:")
        out += [f"  {yaml_scalar(k)}: {yaml_scalar(v)}" for k, v in sorted(s.prefixes.items())]
    else:
        out.append("prefixes: {}")
    out += [
        f"om_version: {yaml_scalar(s.om_version)}",
        f"model_version: {yaml_scalar(s.model_version)}",
        f"last_change_set: {yaml_scalar(s.last_change_set)}",
        f"last_proposal: {yaml_scalar(s.proposal_id)}",
        f"release: {yaml_scalar(release)}",
        f"released_at: {yaml_scalar(released_at)}",
    ]
    rows = _merged_tables(previous, s.source_tables)
    if rows:
        out.append("source_tables:")
        for row in rows:
            out.append(f"  - fqn: {yaml_scalar(row['fqn'])}")
            out.append(f"    om_fqn: {yaml_scalar(row['om_fqn'])}")
            out.append(f"    version: {yaml_scalar(row['version'])}")
    else:
        out.append("source_tables: []")
    return ("\n".join(out) + "\n").encode("utf-8")


# --- provenance log and README --------------------------------------------------------------


def _provenance_log(previous: bytes | None, record: dict[str, Any], publication_id: str) -> bytes:
    base = previous or b""
    for raw in base.splitlines():
        try:
            if json.loads(raw).get("publication_id") == publication_id:
                return base
        except (ValueError, AttributeError):
            continue
    if base and not base.endswith(b"\n"):
        base += b"\n"
    line = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return base + line.encode("utf-8") + b"\n"


def _cell(value: Any) -> str:
    return "" if value is None else _clean(str(value)).replace("|", "\\|")


def readme_bytes(rows: Mapping[str, bytes]) -> bytes:
    lines = [
        "# Ontologies",
        "",
        "Generated by Lakeloom. Do not edit by hand: every applied proposal rewrites these files.",
        "",
        "| Model | Base IRI | Last change set | Release |",
        "| --- | --- | --- | --- |",
    ]
    for slug in sorted(rows):
        m = load_manifest(rows[slug])
        if m is None:
            continue
        lines.append(
            f"| {_cell(slug)} | {_cell(m.get('base_iri'))} | "
            f"{_cell(m.get('last_change_set'))} | {_cell(m.get('release'))} |"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def export_files(snapshot: Snapshot) -> dict[str, bytes]:
    """The exports of a snapshot by repository path; refuses an unsafe slug or missing exports."""
    slug = snapshot.model
    if not _SLUG.fullmatch(slug):
        raise ValueError(f"model slug {slug!r} is not a safe path component")
    if snapshot.exports is None:
        raise ValueError("snapshot has no exports: the publisher must export before rendering")
    missing = [name for name in REQUIRED_EXPORTS if name not in snapshot.exports]
    if missing:
        raise ValueError(f"snapshot exports lack {', '.join(missing)}")
    return {
        f"ontologies/{slug}/{name}": snapshot.exports[name].encode("utf-8")
        for name in (*REQUIRED_EXPORTS, *OPTIONAL_EXPORTS)
        if name in snapshot.exports
    }


def render(
    snapshot: Snapshot,
    publication_id: str,
    *,
    previous_manifest: bytes | None,
    previous_provenance: bytes | None,
    manifests: Mapping[str, bytes],
) -> dict[str, bytes]:
    """The full file map of one commit. ``manifests`` holds the other models' manifests."""
    slug = snapshot.model
    manifest = manifest_bytes(snapshot, previous_manifest)
    files = export_files(snapshot)
    files[f"ontologies/{slug}/manifest.yaml"] = manifest
    files[f"provenance/{slug}.jsonl"] = _provenance_log(
        previous_provenance, snapshot.provenance, publication_id
    )
    files["README.md"] = readme_bytes({**manifests, slug: manifest})
    return files
