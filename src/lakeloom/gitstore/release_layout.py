"""The deterministic files and messages of a release commit (FR-GIT-02, FR-GIT-06).

Pure functions. A release renders the same five exports as a publication, a manifest that
names the release, ``CHANGELOG.md`` and the README, and appends nothing to the provenance log.
Text from OM or agents (labels, IRIs, subjects, user names) is sanitised before it is written.
"""

import re
from collections.abc import Mapping
from datetime import UTC, date, datetime

from pydantic import BaseModel

from lakeloom.core.release_diff import ReleaseDiff, TermChange
from lakeloom.gitstore.layout import (
    Snapshot,
    export_files,
    load_manifest,
    manifest_bytes,
    readme_bytes,
    sanitize_trailer,
)

__all__ = [
    "ProposalEntry",
    "changelog_section",
    "release_message",
    "render_changelog",
    "render_release",
    "tag_message",
    "tag_name",
]

MAX_LISTED = 50
_SECTION_HEAD = re.compile(r"^## \[", re.MULTILINE)
_INTRO = (
    "All notable changes to this ontology are documented here. The format follows "
    "[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versions follow "
    "[Semantic Versioning](https://semver.org/spec/v2.0.0.html)."
)


class ProposalEntry(BaseModel):
    subject: str
    proposal_id: str
    approved_by: str
    short_sha: str


def _text(value: str) -> str:
    """Untrusted text on one line: controls to spaces, backticks removed, at most 200 chars."""
    return sanitize_trailer(value.replace("`", ""))


def _term_lines(items: list[TermChange]) -> list[str]:
    lines = [f"- `{_text(t.label)}` — {_text(t.iri)}" for t in items[:MAX_LISTED]]
    if len(items) > MAX_LISTED:
        lines.append(f"- … and {len(items) - MAX_LISTED} more")
    return lines


def changelog_section(
    version: str,
    day: date,
    released_by: str,
    diff: ReleaseDiff,
    proposals: list[ProposalEntry],
) -> str:
    """One ``## [version]`` section, ending in a newline."""
    who = f"Released by {_text(released_by) or 'unknown'}."
    first = diff.bump is None and diff.from_version is None
    summary = (
        f"{who} Initial release." if first else f"{who} Bump: {diff.bump} ({_text(diff.reason)})."
    )
    lines = [f"## [{_text(version)}] - {day.isoformat()}", "", summary, ""]
    groups: list[tuple[str, list[TermChange]]]
    if first:
        groups = [(f"Initial release ({len(diff.added)} terms)", diff.added)]
    else:
        groups = [("Added", diff.added), ("Changed", diff.changed), ("Removed", diff.removed)]
    for title, items in groups:
        if items:
            lines += [f"### {title}", *_term_lines(items)]
    if proposals:
        if lines[-1] != "":
            lines.append("")
        lines.append("### Proposals")
        lines += [
            f"- {_text(p.subject)} — {_text(p.proposal_id)}, approved by {_text(p.approved_by)} "
            f"(`{_text(p.short_sha)}`)"
            for p in proposals
        ]
    while lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def _old_sections(previous: bytes | None, slug: str) -> list[str] | None:
    """The sections of an earlier CHANGELOG, or ``None`` when it cannot be parsed."""
    if previous is None:
        return []
    try:
        text = previous.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text.startswith("# Changelog"):
        return None
    starts = [m.start() for m in _SECTION_HEAD.finditer(text)]
    ends = [*starts[1:], len(text)]
    return [text[a:b].strip() + "\n" for a, b in zip(starts, ends, strict=True)]


def render_changelog(slug: str, previous: bytes | None, section: str) -> tuple[bytes, list[str]]:
    """The whole file with ``section`` first, and the warnings (an unparsable file is replaced)."""
    warnings: list[str] = []
    old = _old_sections(previous, slug)
    if old is None:
        old = []
        warnings.append(f"the previous CHANGELOG.md of {slug} could not be parsed and was replaced")
    head = section.splitlines()[0]
    kept = [s for s in old if s.splitlines()[0].split("]")[0] != head.split("]")[0]]
    parts = [f"# Changelog — {slug}\n", f"{_INTRO}\n", section, *kept]
    return "\n".join(parts).encode("utf-8"), warnings


def _iso(moment: datetime) -> str:
    moment = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def render_release(
    snapshot: Snapshot,
    *,
    version: str,
    released_at: datetime,
    changelog: bytes,
    previous_manifest: bytes | None,
    manifests: Mapping[str, bytes],
) -> dict[str, bytes]:
    """The file map of a release commit: ``render`` without the provenance append."""
    slug = snapshot.model
    files = export_files(snapshot)
    # a release changes no proposal: the last proposal and change set stay those of the tip
    previous = load_manifest(previous_manifest) or {}
    kept = {
        key: previous[key]
        for key in ("last_proposal", "last_change_set")
        if isinstance(previous.get(key), str)
    }
    snap = snapshot.model_copy(
        update={
            "proposal_id": kept.get("last_proposal", snapshot.proposal_id),
            "last_change_set": kept.get("last_change_set", snapshot.last_change_set),
        }
    )
    manifest = manifest_bytes(
        snap, previous_manifest, release=version, released_at=_iso(released_at)
    )
    files[f"ontologies/{slug}/manifest.yaml"] = manifest
    files[f"ontologies/{slug}/CHANGELOG.md"] = changelog
    files["README.md"] = readme_bytes({**manifests, slug: manifest})
    return files


def release_message(
    slug: str,
    version: str,
    diff: ReleaseDiff,
    released_by: str,
    model_version: str,
    release_id: str,
) -> str:
    """Subject, one ``Bump:`` line, then the trailers in their fixed order."""

    def trailer(key: str, value: str) -> str:
        return f"{key}: {sanitize_trailer(value) or 'unknown'}"

    bump = diff.bump or "none"
    return "\n".join(
        [
            f"release({_text(slug)}): v{_text(version)}",
            "",
            f"Bump: {bump} ({_text(diff.reason)})",
            "",
            trailer("Lakeloom-Release", f"{slug}/v{version}"),
            trailer("Released-By", released_by),
            trailer("Lakeloom-Model-Version", model_version),
            trailer("Lakeloom-Release-Id", release_id),
            "",
        ]
    )


def tag_name(slug: str, version: str) -> str:
    return f"{slug}/v{version}"


def tag_message(slug: str, version: str, section: str) -> str:
    return f"{slug} v{version}\n\n{section}"
