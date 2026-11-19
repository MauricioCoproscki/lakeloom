"""Applied proposal operations of a model: the input of SKOS enrichment.

Scope notes, history notes and deprecations are lakeloom-native: OpenMetadata never stores
them, so the proposal log is their only source. Every reader that shows a model with its
governance annotations (exports, releases, analytics, explanations) collects them here, in
the order they were applied, so a later note or deprecation is replayed after an earlier one.
"""

import asyncio
import copy
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

from rdflib import OWL, Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, SKOS

from lakeloom.core.concepts import OM_FQN, ConceptGovernance, ConceptRef
from lakeloom.core.operations import ProposalOperation
from lakeloom.rdf.skos_enrichment import concept_labels, enrich_graph_from_ops
from lakeloom.untrusted import UntrustedText

if TYPE_CHECKING:
    from lakeloom.core.models import ModelService
    from lakeloom.core.proposals import Proposal

__all__ = [
    "ProposalLister",
    "applied_operations",
    "deprecated_concepts",
    "effective_graph",
    "governance_of",
]

_PAGE_SIZE = 200


class ProposalLister(Protocol):
    async def list_by_status(
        self, statuses: Sequence[str], model: str | None, cursor: str | None, limit: int
    ) -> "tuple[list[Proposal], str | None]": ...


async def applied_operations(repo: ProposalLister, model: str) -> list[ProposalOperation]:
    """Every operation of ``model``'s applied proposals, oldest application first."""
    applied: list[Proposal] = []
    cursor: str | None = None
    while True:
        page, cursor = await repo.list_by_status(["applied"], model, cursor, _PAGE_SIZE)
        applied.extend(page)
        if cursor is None:
            break
    applied.sort(key=lambda p: (p.updated_at, p.id))
    return [op for p in applied for op in p.operations()]


async def effective_graph(models: "ModelService", repo: ProposalLister | None, model: str) -> Graph:
    """``model``'s graph as users should see it: OpenMetadata's terms plus the scope notes,
    history notes and deprecations of its applied proposals. The cached OM graph is never
    mutated; without a proposal log (``repo`` None) the OM graph itself is returned."""
    graph = await models.graph(model)
    if repo is None:
        return graph
    ops = await applied_operations(repo, model)
    if not ops:
        return graph
    return await asyncio.to_thread(lambda: enrich_graph_from_ops(copy.deepcopy(graph), ops))


def _ref(graph: Graph, node: URIRef, model: str, labels: dict[str, str]) -> ConceptRef:
    fqn = graph.value(node, OM_FQN)
    return ConceptRef(
        iri=str(node),
        fqn=str(fqn) if fqn is not None else str(node),
        model=model,
        label=labels.get(str(node), str(node)),
    )


def governance_of(graph: Graph, iri: str, model: str) -> ConceptGovernance | None:
    """The lakeloom-native annotations of concept ``iri`` in an effective graph, or None."""
    node = URIRef(iri)
    source = f"lakeloom proposal log ({model})"

    def notes(pred: URIRef) -> list[UntrustedText]:
        return [
            UntrustedText(text=str(o), source=source)
            for o in sorted(graph.objects(node, pred), key=str)
            if isinstance(o, Literal)
        ]

    deprecated = any(
        isinstance(o, Literal) and o.toPython() is True for o in graph.objects(node, OWL.deprecated)
    )
    successor = next((o for o in graph.objects(node, DCTERMS.isReplacedBy)), None)
    gov = ConceptGovernance(
        deprecated=deprecated,
        replaced_by=(
            _ref(graph, successor, model, concept_labels(graph))
            if isinstance(successor, URIRef)
            else None
        ),
        scope_notes=notes(SKOS.scopeNote),
        history_notes=notes(SKOS.historyNote),
        change_notes=notes(SKOS.changeNote),
    )
    empty = not (gov.deprecated or gov.scope_notes or gov.history_notes or gov.change_notes)
    return None if empty else gov


def deprecated_concepts(graph: Graph) -> set[str]:
    """IRIs of the concepts flagged ``owl:deprecated true``."""
    return {
        str(s)
        for s, o in graph.subject_objects(OWL.deprecated)
        if isinstance(o, Literal) and o.toPython() is True
    }
