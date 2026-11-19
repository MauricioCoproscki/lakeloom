"""Controlled Vocabulary builder: direct-authoring path for adding terms to a model.

Complements the lake-ingest path (mapper.py + proposals.py) by letting agents and users
construct CVs without a lake scan.  All output is a list of ProposalOperations; nothing is
written here — callers pass the result to proposals.propose_operations() as usual.

Stage 1 of the Ontology Pipeline (ANSI Z39.19 / ISO 25964):
  Controlled Vocabulary → MAP → Taxonomy → Thesaurus → Ontology → Knowledge Graph
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, Field

from lakeloom.core.operations import (
    AddConcept,
    AddHistoryNote,
    AddScopeNote,
    ProposalOperation,
)
from lakeloom.untrusted import UntrustedText

__all__ = [
    "MAX_CANDIDATES",
    "MAX_TERMS_PER_BATCH",
    "FolksonomyCandidate",
    "TermDraft",
    "batch_to_ops",
    "harvest_folksonomy",
    "normalize_term_name",
]

MAX_TERMS_PER_BATCH = 500
MAX_CANDIDATES = 1000

# OM term names: letters, digits, spaces, underscores; must start with a letter.
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]{0,127}$")
# Characters to strip when normalising a raw tag/label to a term name.
_STRIP_RE = re.compile(r"[^A-Za-z0-9 _]")
_MULTI_SPACE = re.compile(r" {2,}")


class TermDraft(BaseModel):
    """One concept submitted for direct inclusion in a model (no lake scan required).

    ``name`` must satisfy the OM term-name pattern (letters/digits/spaces/underscores,
    starts with a letter, ≤128 chars).  All other fields are optional.
    """

    name: Annotated[str, Field(min_length=1, max_length=128)]
    definition: Annotated[str | None, Field(max_length=4000)] = None
    broader: Annotated[str | None, Field(max_length=512)] = None
    synonyms: Annotated[list[str], Field(max_length=100)] = Field(default_factory=list)
    scope_note: Annotated[str | None, Field(max_length=2000)] = None
    history_note: Annotated[str | None, Field(max_length=2000)] = None
    # Optional free-text reference to the authority source for this term.
    authority_source: Annotated[str | None, Field(max_length=512)] = None


@dataclass(frozen=True)
class FolksonomyCandidate:
    """A term candidate harvested from OM classification tags or other flat lists.

    ``source_tag`` is the raw tag FQN (e.g. ``PII.Email``), ``frequency`` is how many
    assets carry it.  ``name`` is the normalised candidate term name.
    """

    name: str
    source_tag: str
    frequency: int = 0


def normalize_term_name(raw: str) -> str | None:
    """Convert a raw label or tag name to a valid OM term name, or return ``None`` if
    no valid name can be derived.

    Strategy: strip the classification prefix (``PII.``, ``Domain.``), remove
    non-alphanumeric characters, title-case the result, collapse spaces.

    >>> normalize_term_name("PII.Email")
    'Email'
    >>> normalize_term_name("Domain.Order Management")
    'Order Management'
    >>> normalize_term_name("123bad")  # starts with digit
    # returns None
    """
    # Strip everything up to and including the last dot (classification prefix).
    name = raw.rsplit(".", 1)[-1] if "." in raw else raw
    # Replace underscores/hyphens with spaces, strip other non-alphanumeric chars.
    name = name.replace("_", " ").replace("-", " ")
    name = _STRIP_RE.sub("", name)
    name = _MULTI_SPACE.sub(" ", name).strip()
    if not name:
        return None
    # Title-case words to match OM naming conventions.
    name = " ".join(w.capitalize() for w in name.split())
    if not _NAME_RE.match(name):
        return None
    return name


def harvest_folksonomy(
    tags: list[str],
    *,
    frequency_map: dict[str, int] | None = None,
) -> list[FolksonomyCandidate]:
    """Convert OM classification tag FQNs into ``FolksonomyCandidate`` objects.

    ``tags`` is the list of tag FQNs (e.g. ``["PII.Email", "Domain.Order"]``).
    ``frequency_map`` maps each tag FQN to its asset count (from OM's tag-usage API).
    Tags that cannot be normalised to a valid term name are silently dropped.

    Returns candidates sorted by frequency descending, then name ascending.
    De-duplicates by normalised name: the highest-frequency tag wins.
    """
    freq = frequency_map or {}
    seen: dict[str, FolksonomyCandidate] = {}
    for tag in tags:
        name = normalize_term_name(tag)
        if name is None:
            continue
        f = freq.get(tag, 0)
        existing = seen.get(name)
        if existing is None or f > existing.frequency:
            seen[name] = FolksonomyCandidate(name=name, source_tag=tag, frequency=f)
    return sorted(seen.values(), key=lambda c: (-c.frequency, c.name))


def _parents_first(terms: list[TermDraft]) -> list[TermDraft]:
    """Stable order in which a term whose ``broader`` is in the batch follows that parent."""
    by_name = {t.name: t for t in terms}
    out: list[TermDraft] = []
    seen: set[str] = set()

    def visit(t: TermDraft, path: frozenset[str]) -> None:
        if t.name in seen:
            return
        parent = by_name.get(t.broader or "")
        if parent is not None and parent.name not in path:  # a cycle is left to validation
            visit(parent, path | {t.name})
        seen.add(t.name)
        out.append(t)

    for t in terms:
        visit(t, frozenset())
    return out


def batch_to_ops(
    terms: list[TermDraft],
    *,
    rule_id: str = "R-CV-DIRECT",
) -> list[ProposalOperation]:
    """Convert a batch of ``TermDraft`` objects into proposal operations.

    Returns a flat list of ``ProposalOperation`` objects ready to pass to
    ``proposals.propose_operations()``.  Ordering: ``AddConcept`` for each term
    (parent concepts before children when parents appear in the same batch),
    followed by ``AddSynonym``, ``AddScopeNote``, and ``AddHistoryNote`` ops.

    Raises ``ValueError`` if ``len(terms) > MAX_TERMS_PER_BATCH``.
    """
    if len(terms) > MAX_TERMS_PER_BATCH:
        raise ValueError(
            f"batch_to_ops: {len(terms)} terms exceeds the limit of {MAX_TERMS_PER_BATCH}; "
            "split into smaller batches"
        )

    from lakeloom.core.operations import Evidence  # local import avoids circularity

    concepts: list[ProposalOperation] = []
    enrichments: list[ProposalOperation] = []
    rules = [rule_id] if rule_id else []

    for draft in _parents_first(terms):
        evidence = (
            [
                Evidence(
                    kind="agent", ref="vocabulary", detail=f"authority: {draft.authority_source}"
                )
            ]
            if draft.authority_source
            else []
        )
        description = (
            UntrustedText(text=draft.definition, source="user") if draft.definition else None
        )
        concepts.append(
            AddConcept(
                op="add_concept",
                name=draft.name,
                description=description,
                parent=draft.broader or None,
                synonyms=list(draft.synonyms),  # new concepts carry synonyms inline
                rule_ids=rules,
                evidence=evidence,
            )
        )
        if draft.scope_note:
            enrichments.append(
                AddScopeNote(
                    op="add_scope_note",
                    concept_fqn=draft.name,
                    note=draft.scope_note,
                    rule_ids=rules,
                )
            )
        if draft.history_note:
            enrichments.append(
                AddHistoryNote(
                    op="add_history_note",
                    concept_fqn=draft.name,
                    note=draft.history_note,
                    rule_ids=rules,
                )
            )

    return concepts + enrichments
