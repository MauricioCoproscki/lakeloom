"""Stage deliverables: glossary, data dictionary, taxonomy tree, synonyms, deprecations."""

import csv
import io
from datetime import UTC, datetime

from rdflib import OWL, Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF, SKOS, XSD

from lakeloom.core.deliverables import (
    data_dictionary_csv,
    data_dictionary_markdown,
    deprecations_csv,
    glossary_csv,
    glossary_markdown,
    quality_score,
    synonyms_csv,
    synonyms_solr,
    taxonomy_csv,
    taxonomy_markdown,
    taxonomy_mermaid,
    vocabulary_entries,
)
from lakeloom.core.map_profile import ColumnEntry, MapProfile, TableMapEntry
from lakeloom.rdf.skos_enrichment import OM_FQN

B = "http://x/retail/"
HOSTILE = "Paid orders | # Injected\n## heading [x](http://evil)"


def c(name: str) -> URIRef:
    return URIRef(B + name)


def model() -> Graph:
    g = Graph()

    def concept(name: str, fqn: str, definition: str | None = None, *syn: str) -> None:
        g.add((c(name), RDF.type, SKOS.Concept))
        g.add((c(name), SKOS.prefLabel, Literal(name, lang="en")))
        g.add((c(name), OM_FQN, Literal(fqn)))
        if definition:
            g.add((c(name), SKOS.definition, Literal(definition, lang="en")))
        for s in syn:
            g.add((c(name), SKOS.altLabel, Literal(s, lang="en")))

    concept("Payment", "retail.Payment", "Money received.")
    concept("CardPayment", "retail.Payment.CardPayment", "Paid by card.", "Card, Debit")
    concept("Order", "retail.Order", HOSTILE, "Purchase")
    concept("Client", "retail.Client", "Old name.")
    concept("Customer", "retail.Customer", None, "Account holder")
    g.add((c("CardPayment"), SKOS.broader, c("Payment")))
    g.add((c("Order"), SKOS.related, c("Payment")))
    g.add((c("Order"), SKOS.scopeNote, Literal("Confirmed orders only.", lang="en")))
    g.add((c("Client"), OWL.deprecated, Literal(True, datatype=XSD.boolean)))
    g.add((c("Client"), DCTERMS.isReplacedBy, c("Customer")))
    g.add((c("Client"), SKOS.historyNote, Literal("Merged in 2026.", lang="en")))
    return g


def rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def test_entries_read_every_annotation() -> None:
    by = {e.label: e for e in vocabulary_entries(model())}
    assert by["CardPayment"].broader == ("retail.Payment",)
    assert by["CardPayment"].synonyms == ("Card, Debit",)
    assert by["Order"].related == ("retail.Payment",)
    assert by["Order"].scope_notes == ("Confirmed orders only.",)
    assert by["Client"].deprecated and by["Client"].replaced_by == "retail.Customer"


def test_glossary_markdown_is_inert_and_lists_deprecations_apart() -> None:
    md = glossary_markdown(vocabulary_entries(model()), "retail")
    order = next(line for line in md.splitlines() if line.startswith("| **Order**"))
    assert "\\|" in order and "\n## heading" not in md and order.count(" | ") == 3
    assert "_no definition yet_" in md  # Customer
    assert "## Deprecated terms" in md and "| Client | Customer |" in md


def test_glossary_csv_roundtrips() -> None:
    got = {r["term"]: r for r in rows(glossary_csv(vocabulary_entries(model())))}
    assert got["Order"]["definition"] == HOSTILE  # CSV keeps the exact text, safely quoted
    assert got["Client"]["status"] == "deprecated"
    assert got["Client"]["replaced_by"] == "retail.Customer"


def test_quality_score_counts_current_terms_only() -> None:
    q = quality_score(vocabulary_entries(model()))
    assert (q.total, q.defined, q.deprecated) == (4, 3, 1)
    assert q.undefined == ("Customer",) and q.defined_ratio == 0.75


def test_taxonomy_outline_mermaid_and_csv() -> None:
    entries = vocabulary_entries(model())
    md = taxonomy_markdown(entries, "retail")
    assert "- **Payment**" in md and "  - **CardPayment**" in md and "Client" not in md
    mermaid = taxonomy_mermaid(entries)
    assert mermaid.startswith("flowchart TD") and '["Payment"] --> ' in mermaid
    tree = rows(taxonomy_csv(entries))
    card = next(r for r in tree if r["term"] == "CardPayment")
    assert (card["depth"], card["parent_fqn"]) == ("1", "retail.Payment")


def test_taxonomy_survives_a_cycle() -> None:
    g = model()
    g.add((c("Payment"), SKOS.broader, c("CardPayment")))
    assert taxonomy_csv(vocabulary_entries(g))  # no infinite loop


def test_synonyms_solr_rules() -> None:
    text = synonyms_solr(vocabulary_entries(model()))
    assert "CardPayment, Card Debit" in text  # the comma inside a synonym is removed
    assert "Order, Purchase" in text
    assert "Client => Customer" in text
    assert "Payment\n" not in text  # a term without synonyms needs no rule


def test_synonyms_and_deprecations_csv() -> None:
    entries = vocabulary_entries(model())
    assert {(r["term"], r["synonym"]) for r in rows(synonyms_csv(entries))} >= {
        ("Order", "Purchase")
    }
    (dep,) = rows(deprecations_csv(entries))
    assert (dep["deprecated_term"], dep["use_instead"], dep["history"]) == (
        "Client",
        "Customer",
        "Merged in 2026.",
    )


def test_data_dictionary_joins_map_and_definitions() -> None:
    profile = MapProfile(
        source_id="fx",
        scan_id="scan_1",
        model_slug="retail",
        created_at=datetime(2026, 11, 19, tzinfo=UTC),
        table_entries=[
            TableMapEntry(
                om_fqn="svc.retail.sales.orders",
                local_name="orders",
                source="fx",
                table_concept=B + "Order",
                columns=[
                    ColumnEntry("client_id", "STRING", B + "Client"),
                    ColumnEntry("note", "STRING"),
                ],
            )
        ],
    )
    entries = vocabulary_entries(model())
    got = rows(data_dictionary_csv(profile, entries))
    assert [(r["column"], r["term"], r["status"]) for r in got] == [
        ("client_id", "Client", "deprecated"),
        ("note", "", "unmapped"),
    ]
    md = data_dictionary_markdown(profile, entries)
    assert "1 of 2 columns carry a business term" in md
    assert "## `svc.retail.sales.orders` — Order" in md and "Client (deprecated)" in md
