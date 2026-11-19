"""Unit tests for entity_linking module (M6a)."""

from __future__ import annotations

import pytest

from lakeloom.core.entity_linking import (
    ApprovedMapping,
    SchemaOrgMatch,
    WikidataMatch,
    _kind_from_score,
    _match_score,
    _parse_schema_org,
    candidates_to_ops,
    clear_schema_org_cache,
    lookup_schema_org,
    lookup_wikidata,
)
from lakeloom.core.operations import AddMapping

# ---------------------------------------------------------------------------
# Score helpers
# ---------------------------------------------------------------------------


class TestMatchScore:
    def test_exact_match(self) -> None:
        assert _match_score("Customer", "Customer") == 1.0

    def test_case_insensitive_exact(self) -> None:
        assert _match_score("customer", "Customer") == 1.0

    def test_substring_match(self) -> None:
        score = _match_score("Order", "Purchase Order")
        assert score == 0.8

    def test_partial_overlap(self) -> None:
        score = _match_score("Customer Account", "Account Manager")
        assert 0.0 < score < 1.0

    def test_no_overlap(self) -> None:
        score = _match_score("xyz123", "abc456")
        assert score == 0.0


class TestKindFromScore:
    def test_exact_match_score(self) -> None:
        assert _kind_from_score(1.0) == "exactMatch"

    def test_close_match(self) -> None:
        assert _kind_from_score(0.7) == "closeMatch"

    def test_related_match(self) -> None:
        assert _kind_from_score(0.3) == "relatedMatch"


# ---------------------------------------------------------------------------
# Wikidata lookup
# ---------------------------------------------------------------------------


async def _fake_wikidata(url: str, params: dict) -> dict:
    term = params.get("search", "unknown")
    return {
        "search": [
            {"id": "Q1", "label": term, "description": f"Test entity for {term}"},
            {"id": "Q2", "label": f"{term} related", "description": "Related entity"},
        ]
    }


async def _error_fetch(url: str, params: dict) -> dict:
    raise RuntimeError("network error")


class TestLookupWikidata:
    @pytest.mark.asyncio
    async def test_returns_matches(self) -> None:
        matches = await lookup_wikidata("Customer", fetch=_fake_wikidata)
        assert len(matches) > 0
        assert all(isinstance(m, WikidataMatch) for m in matches)

    @pytest.mark.asyncio
    async def test_exact_match_kind(self) -> None:
        matches = await lookup_wikidata("Customer", fetch=_fake_wikidata)
        # First result has label == "Customer" (exact).
        assert matches[0].kind == "exactMatch"

    @pytest.mark.asyncio
    async def test_entity_uri_format(self) -> None:
        matches = await lookup_wikidata("Customer", fetch=_fake_wikidata)
        assert matches[0].entity_uri == "http://www.wikidata.org/entity/Q1"

    @pytest.mark.asyncio
    async def test_respects_limit(self) -> None:
        matches = await lookup_wikidata("Customer", fetch=_fake_wikidata, limit=1)
        assert len(matches) == 1

    @pytest.mark.asyncio
    async def test_network_error_returns_empty(self) -> None:
        matches = await lookup_wikidata("Customer", fetch=_error_fetch)
        assert matches == []


# ---------------------------------------------------------------------------
# schema.org lookup
# ---------------------------------------------------------------------------

_SCHEMA_ORG_JSONLD = {
    "@graph": [
        {
            "@id": "schema:Customer",
            "@type": "rdfs:Class",
            "rdfs:label": "Customer",
            "rdfs:comment": "A customer.",
        },
        {
            "@id": "schema:Order",
            "@type": "rdfs:Class",
            "rdfs:label": "Order",
            "rdfs:comment": "A purchase order.",
        },
        {
            "@id": "schema:givenName",
            "@type": "rdf:Property",
            "rdfs:label": "givenName",
            "rdfs:comment": "Given name.",
        },
    ]
}


async def _fake_schema(url: str, params: dict) -> dict:
    return _SCHEMA_ORG_JSONLD


class TestLookupSchemaOrg:
    def setup_method(self) -> None:
        clear_schema_org_cache()

    @pytest.mark.asyncio
    async def test_returns_matches(self) -> None:
        matches = await lookup_schema_org("Customer", fetch=_fake_schema)
        assert len(matches) > 0
        assert all(isinstance(m, SchemaOrgMatch) for m in matches)

    @pytest.mark.asyncio
    async def test_uri_format(self) -> None:
        matches = await lookup_schema_org("Customer", fetch=_fake_schema)
        assert any(m.type_uri == "https://schema.org/Customer" for m in matches)

    @pytest.mark.asyncio
    async def test_sorted_by_score(self) -> None:
        matches = await lookup_schema_org("Order", fetch=_fake_schema)
        scores = [m.match_score for m in matches]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.asyncio
    async def test_cache_reuse(self) -> None:
        called = []

        async def counting_fetch(url: str, params: dict) -> dict:
            called.append(url)
            return _SCHEMA_ORG_JSONLD

        await lookup_schema_org("Customer", fetch=counting_fetch)
        await lookup_schema_org("Order", fetch=counting_fetch)
        # Second call should reuse cache (only 1 HTTP call total).
        assert len(called) == 1

    @pytest.mark.asyncio
    async def test_network_error_returns_empty(self) -> None:
        matches = await lookup_schema_org("Customer", fetch=_error_fetch)
        assert matches == []


class TestParseSchemaOrg:
    def test_parses_classes(self) -> None:
        index = _parse_schema_org(_SCHEMA_ORG_JSONLD)
        assert "customer" in index
        assert "order" in index

    def test_parses_properties(self) -> None:
        index = _parse_schema_org(_SCHEMA_ORG_JSONLD)
        assert "givenname" in index

    def test_uri_is_full_schema_uri(self) -> None:
        index = _parse_schema_org(_SCHEMA_ORG_JSONLD)
        assert index["customer"]["uri"] == "https://schema.org/Customer"


# ---------------------------------------------------------------------------
# candidates_to_ops
# ---------------------------------------------------------------------------


class TestCandidatesToOps:
    def test_produces_add_mapping_ops(self) -> None:
        approved = [
            ApprovedMapping(
                concept_fqn="retail.Customer",
                target_uri="http://www.wikidata.org/entity/Q5",
                kind="exactMatch",
            )
        ]
        ops = candidates_to_ops(approved)
        assert len(ops) == 1
        op = ops[0]
        assert isinstance(op, AddMapping)
        assert op.op == "add_mapping"
        assert op.kind == "exactMatch"
        assert str(op.target_iri) == "http://www.wikidata.org/entity/Q5"

    def test_multiple_ops(self) -> None:
        approved = [
            ApprovedMapping(
                concept_fqn=f"retail.Concept{i}",
                target_uri=f"http://www.wikidata.org/entity/Q{i}",
                kind="closeMatch",
            )
            for i in range(3)
        ]
        ops = candidates_to_ops(approved)
        assert len(ops) == 3

    def test_invalid_uri_raises(self) -> None:
        approved = [
            ApprovedMapping(
                concept_fqn="retail.Customer",
                target_uri="not-a-uri",
                kind="exactMatch",
            )
        ]
        with pytest.raises(ValueError, match="invalid URI"):
            candidates_to_ops(approved)

    def test_default_kind_is_close_match(self) -> None:
        approved = [
            ApprovedMapping(
                concept_fqn="retail.Customer",
                target_uri="http://www.wikidata.org/entity/Q5",
            )
        ]
        ops = candidates_to_ops(approved)
        assert isinstance(ops[0], AddMapping)
        assert ops[0].kind == "closeMatch"
