# M3a — SHACL Shapes, JSON-LD Context and Model Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every model publishes:
- generated **SHACL shapes**, which validate instance data or pipeline outputs;
- a full **JSON-LD `@context`**, so applications can emit JSON that maps to ontology IRIs;
- a **`validate_model`** report.

All three are served through the HTTP API, MCP resources and Git (`shapes.ttl` and `context.jsonld` in every publication commit).

**Architecture:**
- **Pure generators** that read the model's merged RDF graph:
  - `publish/shacl_gen.py` → `rdflib.Graph`;
  - `publish/jsonld_context.py` → dict;
  - `core/validation.py` → report.
- **Caching:** `ModelService` caches their results per graph version (`derived`).
- **Serving:** the artefact builders in `publish/artifacts.py` serve them with ETag, Last-Modified and Link, as for the other artefacts.
- **Git:** the M2c snapshot gains two more export files.

**Tech Stack:** existing stack (rdflib, FastAPI, MCP SDK). `pyshacl` is added as a **dev-only** dependency, used by tests to prove that the shapes validate data. No new runtime dependency.

**Spec:** `REQUIREMENTS.md`:
- FR-PUB-01 (`shapes.ttl`, `context.jsonld` resources), FR-PUB-02 (`GET /v1/models/{slug}/shapes`, `/context.jsonld`);
- FR-PUB-04 (SHACL), FR-PUB-05 (JSON-LD context), FR-PUB-07 (ETag / Last-Modified / Link), FR-PUB-08 (OpenAPI);
- FR-CTX-02 ("JSON-LD using the model's `context.jsonld`");
- FR-GIT-02 (`shapes.ttl`, `context.jsonld` in the repository layout), FR-GIT-03 (determinism);
- the `validate_model` tool of §7;
- the M3 acceptance criterion "SHACL shapes validate fixture data".

M3 is split into three parts:
- **M3a** (this plan): shapes, context, validation.
- **M3b**: releases.
  - `create_release` with the semver rules, the annotated tag, the manifest `release` field and `CHANGELOG.md`;
  - `list_releases` / `diff_releases`;
  - released versions served from Git (`?version=`, `/versions`);
  - Git `Link` headers;
  - the optional triple-store sink.
- **M3c**: import and drift.
  - `import_model` (FR-IO-02/04/05) and `import_from_git` (FR-GIT-07), with the round-trip criterion;
  - `detect_drift`;
  - the SPARQL proxy (FR-PUB-03).

## Decisions taken in this plan (controller; owner may overrule at plan review)

- **Input graph.** The generators read `ModelService.graph(slug)`, the merged graph of glossary SKOS and native OWL used by exports. In it:
  - **Classes** are `owl:Class` and/or `skos:Concept` subjects of the model.
  - **Attributes** are `owl:DatatypeProperty` with `rdfs:domain`, `rdfs:range`, `owl:oneOf` (enum) and `qudt:unit`. An identifier is also typed `owl:FunctionalProperty` (`om/native_rdf.py`).
  - **Relations** are `owl:ObjectProperty` with domain and range.
  - **Cardinality** comes from `rdfs:subClassOf` restrictions (`owl:onProperty` plus `owl:minCardinality` / `owl:maxCardinality` / `owl:cardinality` / `owl:qualifiedCardinality`).
- **SHACL (FR-PUB-04)**, shapes with IRIs, no blank-node shapes except `sh:in` lists:
  - **One `sh:NodeShape` per class `C`**, IRI `<C>/shape`. It carries `sh:targetClass C` and `sh:name` (the class label when present).
  - **Per attribute `P` with domain `C`**: a `sh:PropertyShape` `<P>/shape`, linked from the node shape by `sh:property`. It carries:
    - `sh:path P`;
    - `sh:datatype R` when the range is an `xsd:` datatype other than `rdfs:Literal`;
    - `sh:in (values…)` from `owl:oneOf`;
    - `sh:minCount 1` and `sh:maxCount 1` when `P` is `owl:FunctionalProperty` (Lakeloom marks only identifiers functional);
    - `sh:name` (the property label).
  - **Per relation `P` with domain `C` and range `D`**: the same property shape, with `sh:class D` in place of a datatype.
  - **Cardinality restrictions on `C` for `P`** set `sh:minCount` / `sh:maxCount` on that property shape (exact or qualified gives both), creating the shape if it is missing. When both sources set a count, the stricter value wins.
  - **Several domains**: a property with several domains gets one property shape whose path is `P`, linked from each domain's node shape.
  - **Shapes are open** (no `sh:closed`). Units are not constrained.
  - **Serialisation**: `rdf/canonical.serialize(…, "turtle")` with prefix `sh` plus the model prefixes, so the output is byte-deterministic.
- **JSON-LD context (FR-PUB-05)**. The output is `{"@context": {...}}` (`json_bytes`: sorted keys, compact, LF). It contains, in this order of precedence (a later entry never overrides an earlier key):
  1. `@version: 1.1`;
  2. the existing minimal context: `@vocab` = base IRI, the model prefixes, `dcterms`, `skos`;
  3. `xsd`;
  4. **one term per class**: key = the class IRI with the base IRI removed (when it starts with it), else `split_uri` local name; value = the IRI;
  5. **one term per property**: key = the property IRI minus the base (e.g. `Customer.email`). The value is `{"@id": P, "@type": <datatype IRI>}` for attributes with an `xsd` range, `{"@id": P}` for an `rdfs:Literal` range, and `{"@id": P, "@type": "@id"}` for relations;
  6. **short aliases**: the bare attribute or relation name (`email`), only when exactly one property of the model has it.

  A key that collides with a keyword (`@…`), a prefix or an earlier key is skipped and logged at DEBUG.
- **Asset JSON-LD (FR-CTX-02):** `render_jsonld` embeds the model's full context when the asset's model is known; otherwise it keeps the minimal one. It stays subject to `max_tokens`.
- **`validate_model` (§7, `lakeloom:read`, no side effects).** Report `ValidationReport(model, conforms: bool, counts: dict[str, int], issues: list[ValidationIssue], truncated: bool)`, where `ValidationIssue(severity: Literal["error","warning","info"], code: str, subject: str, message: str)`.
  - `conforms` is false only when an error exists.
  - Issues are sorted by (error < warning < info, code, subject) and capped at 200 (`truncated`).
  - Codes:
    - `duplicate_label` (error): two concepts with the same `skos:prefLabel`, case-folded, same language;
    - `hierarchy_cycle` (error): a cycle in `skos:broader` or `rdfs:subClassOf` among the model's concepts;
    - `dangling_domain` (error): a property whose domain is not a concept of the model;
    - `external_range` (warning): a relation whose range is not a concept of the model;
    - `isolated_concept` (warning): no broader, narrower, related, attribute or relation;
    - `missing_definition` (warning): no `skos:definition`;
    - `untyped_attribute` (warning): range `rdfs:Literal`;
    - `shapes` (info): counts of node and property shapes.

  Messages quote names as untrusted text (code spans), never as raw Markdown.
- **Serving:**
  - **HTTP:** `GET /v1/models/{slug}/shapes` negotiates `text/turtle` (default), `application/ld+json` and `application/n-triples`. `GET /v1/models/{slug}/context.jsonld` serves the full context.
  - **MCP resources:** `lakeloom://models/{slug}/shapes.ttl` is added; `context.jsonld` becomes the full context.
  - **All of them:** `version` accepts only `latest` (M3b), and the ETag, Last-Modified and Link rules are those of ADR 0010.
  - **Tool:** `validate_model(model)` returns Markdown plus the structured report. CLI: `lakeloom validate <model>` (exit code 1 when the model does not conform).
- **Git (FR-GIT-02):**
  - `Snapshot.exports` gains the optional keys `shapes.ttl` and `context.jsonld`, which `fill_exports` produces.
  - `render` writes `ontologies/<slug>/shapes.ttl` and `context.jsonld` when they are present. Older queued payloads without them still render, minus those two files.
  - `ontology.*` stay required.
- **Caching:** `ModelService.shapes(slug) -> Graph` and `ModelService.jsonld_context(slug) -> dict[str, Any]` go through `derived`, so a model change drops them with the graph.

## Global Constraints

- **Earlier constraints still apply:**
  - Python ≥ 3.12; uv / ruff / pyright strict on `src/`.
  - Conventional Commits with requirement IDs; `git commit -s`; identity `Elcio Abrahao <elcio@tuta.com>`; trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  - Typed `LakeloomError`s with hints; untrusted text quoted in tool output.
  - Tools run through `guarded()` and take no URLs; every OM endpoint is in `OMClient.PATHS`.
- **FR-GIT-03:** "the same ontology state MUST produce byte-identical files". This covers `shapes.ttl` and `context.jsonld`, both in property-based tests.
- **FR-PUB-07:** "Every published artefact has an `ETag`, `Last-Modified`, and a `Link` header". Shapes and context use the same builders as the ontology artefacts.
- **The generators never call OM:** they are pure functions of the graph and the `ModelInfo`.
- **Dev-stack rules (owner):**
  - Throwaway models only (hard-deleted in teardown); never modify `retail`; never seal releases.
  - Approve only as alice; no tokens printed or committed.
  - No `down -v`, no prune, never touch `supabase_*`.

## Review Focus

1. **A class without attributes, or an attribute whose domain is missing** (a paired model edited directly in OM). Expected: a node shape with no property shapes, and no crash; the attribute appears as `dangling_domain` in `validate_model` and in no shape. → Task 1 `test_class_without_attributes_has_empty_node_shape`; Task 3 `test_dangling_domain_is_error`.
2. **The same attribute name on two classes** (`Customer.email`, `Supplier.email`). Expected: two qualified context terms and no `email` alias; two property shapes with distinct IRIs. → Task 2 `test_ambiguous_alias_is_omitted`; Task 1 `test_same_name_on_two_classes`.
3. **Enum values with quotes, Unicode or numeric-looking strings.** Expected: `sh:in` holds exactly those literals, the output stays parseable, and pyshacl accepts a value from the list and rejects others. → Task 1 `test_enum_values_roundtrip_and_validate`.
4. **A model with no base IRI, or concepts whose IRIs do not start with it** (a paired model). Expected: the context falls back to local names, there is no `@vocab`, and no key is empty. → Task 2 `test_no_base_iri_uses_local_names`.
5. **A large model** (thousands of concepts). Expected: generation runs off the event loop (`asyncio.to_thread`, as for exports), the result is cached per graph version, and the report is capped with `truncated`. → Task 4 `test_shapes_cached_per_version_and_built_off_loop`; Task 3 `test_report_capped_at_200`.

## File structure

```
src/lakeloom/publish/shacl_gen.py        # build_shapes(graph, info) -> Graph
src/lakeloom/publish/jsonld_context.py   # build_context(graph, info) -> dict
src/lakeloom/core/validation.py          # validate_model(graph, info, shapes) -> ValidationReport; validation_markdown
src/lakeloom/core/models.py              # + shapes(), jsonld_context() (modify)
src/lakeloom/core/context.py             # render_jsonld uses the full context (modify)
src/lakeloom/publish/artifacts.py        # + shapes_artifact; full model_context_artifact (modify)
src/lakeloom/server/http_api.py, resources.py, tools_semantic.py, cli.py   # surfaces (modify)
src/lakeloom/gitstore/layout.py, snapshot.py                              # two more files (modify)
docs/decisions/0014-shapes-context-validation.md
```

---

### Task 1: SHACL shape generator

**Files:**
- Create `src/lakeloom/publish/shacl_gen.py`, `tests/unit/publish/test_shacl_gen.py`.
- Modify `pyproject.toml` (dev dependency `pyshacl`, current release, `uv add --dev pyshacl`).
- Create `docs/decisions/0014-shapes-context-validation.md` (status proposed; the SHACL and context Decisions above).

**Interfaces:**
- **Consumes:** an `rdflib.Graph` shaped like `ModelService.graph` output; `ModelInfo` (`core/models.py`); `rdf/canonical.serialize`; `om/native_rdf.QUDT_UNIT`.
- **Produces:**
  - `build_shapes(graph: Graph, info: ModelInfo) -> Graph`;
  - `SH = Namespace("http://www.w3.org/ns/shacl#")`;
  - `model_classes(graph: Graph) -> list[URIRef]` (sorted; reused by Tasks 2 and 3).

  The test helper builds graphs with `om/native_rdf.build_graph` from OM-shaped members, plus SKOS triples.
- [ ] **Step 1: Tests:**
  - `test_node_shape_per_class` (IRI `<C>/shape`, `sh:targetClass`);
  - `test_datatype_attribute_shape` (`sh:path`, `sh:datatype xsd:decimal`, `sh:name`);
  - `test_literal_range_has_no_datatype`;
  - `test_identifier_is_required_single`;
  - `test_enum_values_roundtrip_and_validate` (values `'a"b'`, `"ünï"`, `"007"`; pyshacl conforms for `"007"` and fails for `"7"`);
  - `test_relation_shape_has_class`;
  - `test_cardinality_restrictions` (min, max, exact, qualified; the stricter count wins over functional);
  - `test_multi_domain_property_shared_shape`;
  - `test_class_without_attributes_has_empty_node_shape`;
  - `test_same_name_on_two_classes`;
  - `test_shapes_validate_instance_data` (pyshacl: a conforming instance passes; a wrong datatype, a missing identifier and a wrong relation target each fail);
  - `test_shapes_deterministic` (hypothesis: random attribute sets and insertion order → identical canonical turtle bytes).
- [ ] **Step 2:** FAIL → implement → pass; ruff and pyright.
- [ ] **Step 3: Commit** `feat(publish): generate SHACL shapes from attributes and relations (FR-PUB-04)`.

---

### Task 2: JSON-LD context generator and asset JSON-LD

**Files:**
- Create `src/lakeloom/publish/jsonld_context.py`, `tests/unit/publish/test_jsonld_context.py`.
- Modify `src/lakeloom/core/context.py` (`render_jsonld`) and its tests.

**Interfaces:**
- **Consumes:** `model_classes` (Task 1); `minimal_jsonld_context` (`core/context.py`).
- **Produces:** `build_context(graph: Graph, info: ModelInfo) -> dict[str, Any]` (the inner `@context` object). `render_jsonld(ctx, model, *, max_tokens, context: dict[str, Any] | None = None)`: when `context` is given it is used as `@context`; callers pass `await models.jsonld_context(slug)` (Task 4 wires it).
- [ ] **Step 1: Tests:**
  - `test_context_terms_for_classes_and_properties` (values exactly as in Decisions);
  - `test_relation_term_is_id_typed`;
  - `test_ambiguous_alias_is_omitted`;
  - `test_unique_alias_present`;
  - `test_keyword_and_prefix_collisions_skipped`;
  - `test_no_base_iri_uses_local_names`;
  - `test_context_expands_to_model_iris` (an example record compacted with the context parses through rdflib's JSON-LD parser into triples with the attribute IRIs and typed literals);
  - `test_context_deterministic` (hypothesis);
  - `test_asset_jsonld_uses_full_context`.
- [ ] **Step 2:** FAIL → implement → pass.
- [ ] **Step 3: Commit** `feat(publish): JSON-LD @context per model (FR-PUB-05, FR-CTX-02)`.

---

### Task 3: Model validation

**Files:**
- Create `src/lakeloom/core/validation.py`, `tests/unit/core/test_validation.py`.

**Interfaces:**
- **Consumes:** `model_classes`, `build_shapes` (Task 1).
- **Produces:**
  - `ValidationIssue`, `ValidationReport` (fields in Decisions);
  - `validate_model(graph: Graph, info: ModelInfo, shapes: Graph) -> ValidationReport`;
  - `validation_markdown(report: ValidationReport) -> str`.

    It renders a heading, a `conforms` line, counts by severity, then one bullet per issue: `` - **error** `duplicate_label` `<subject>`: <message> ``, with names as code spans.
- [ ] **Step 1: Tests:**
  - one test per code: `test_duplicate_label_is_error` (case-folded; different languages are not duplicates), `test_hierarchy_cycle_is_error` (broader and subClassOf), `test_dangling_domain_is_error`, `test_external_range_is_warning`, `test_isolated_concept_warning`, `test_missing_definition_warning`, `test_untyped_attribute_warning`, `test_shapes_info_counts`;
  - `test_clean_model_conforms`;
  - `test_issues_sorted_deterministically`;
  - `test_report_capped_at_200`;
  - `test_markdown_quotes_untrusted_names` (a label with a backtick or `**` cannot break the bullet).
- [ ] **Step 2:** FAIL → implement → pass.
- [ ] **Step 3: Commit** `feat(core): validate_model checks for duplicates, cycles and dangling members`.

---

### Task 4: Serving: ModelService, HTTP, MCP resources, tool and CLI

**Files:**
- Modify:
  - `src/lakeloom/core/models.py`;
  - `src/lakeloom/publish/artifacts.py`;
  - `src/lakeloom/server/http_api.py`, `server/resources.py`, `server/tools_semantic.py` (or the module that registers `export_model`);
  - `src/lakeloom/cli.py`;
  - the asset-context callers of `render_jsonld` (`publish/artifacts.py` `asset_context_artifact`, the asset context tool and resource).
- Tests: extend `tests/unit/core/test_models.py`, `tests/unit/publish/test_artifacts.py`, `tests/unit/server/test_http_api.py`, `tests/unit/server/test_resources.py`, the semantic tools tests and `tests/unit/test_cli.py` (find the existing files and follow them).

**Interfaces:**
- **Consumes:** Tasks 1-3.
- **Produces:**
  - **`ModelService`:**
    - `async shapes(slug: str) -> Graph`;
    - `async jsonld_context(slug: str) -> dict[str, Any]`;
    - `async validation(slug: str) -> ValidationReport`.

    All three are cached via `derived` and built with `asyncio.to_thread`.
  - **Artefact builders:**
    - `shapes_artifact(models, slug, media: Literal["text/turtle","application/ld+json","application/n-triples"], *, version: str | None) -> Artifact`;
    - `model_context_artifact` serves the full context.
  - **HTTP:** `GET /v1/models/{slug}/shapes` (negotiated, with summary and response media types in OpenAPI).
  - **MCP resource** `lakeloom://models/{slug}/shapes.ttl`, listed per model next to the existing ones; the `context.jsonld` description drops "(minimal until M3)".
  - **Tool** `validate_model(model: str)` (scope `lakeloom:read`, read-only, through `guarded()`): Markdown plus structured content `ValidationReport`.
  - **CLI** `lakeloom validate <model>` (exit 1 when not conforming).
- [ ] **Step 1: Tests:**
  - `test_shapes_cached_per_version_and_built_off_loop` (one build per version; invalidate rebuilds; built through `to_thread`);
  - `test_shapes_endpoint_negotiates_three_types`;
  - `test_shapes_etag_and_link` (ETag = sha256 of the body; Link `rel="via"` as for the ontology);
  - `test_shapes_version_other_than_latest_is_not_found`;
  - `test_context_endpoint_full_context`;
  - `test_shapes_resource_bytes_equal_http`;
  - `test_resource_list_has_shapes`;
  - `test_validate_model_tool_markdown_and_structured`;
  - `test_validate_model_requires_read_scope`;
  - `test_cli_validate_exit_codes`;
  - `test_openapi_lists_shapes_route`;
  - `test_asset_context_jsonld_has_model_terms`.
- [ ] **Step 2:** FAIL → implement → pass; full unit suite.
- [ ] **Step 3: Commit** `feat(server): serve shapes, full JSON-LD context and validate_model (FR-PUB-01, FR-PUB-02, FR-PUB-07)`.

---

### Task 5: Git publication carries shapes and context

**Files:**
- Modify `src/lakeloom/gitstore/layout.py`, `gitstore/snapshot.py`, their tests (`tests/unit/gitstore/test_layout.py`, `test_snapshot.py` or the existing equivalents), and `tests/unit/gitstore/test_publisher.py` (one end-to-end assertion).

**Interfaces:**
- **Consumes:** `ModelService.shapes` and `jsonld_context` (Task 4); `canonical.serialize`; `json_bytes`.
- **Produces:**
  - `Snapshot.exports` key type gains `"shapes.ttl"` and `"context.jsonld"`; `REQUIRED_EXPORTS = ("ontology.ttl", "ontology.jsonld", "ontology.owl")`.
  - `render` writes the two new files when they are present. A missing required key raises `ValueError` (this closes the m2c backlog item about the `KeyError`).
  - `fill_exports` adds both.
- [ ] **Step 1: Tests:**
  - `test_render_writes_shapes_and_context`;
  - `test_render_old_payload_without_new_files`;
  - `test_missing_required_export_is_value_error`;
  - `test_fill_exports_adds_shapes_and_context`;
  - `test_publisher_commit_contains_shapes_and_context` (bare remote).
- [ ] **Step 2:** FAIL → implement → pass.
- [ ] **Step 3: Commit** `feat(gitstore): commit shapes.ttl and context.jsonld with each publication (FR-GIT-02)`.

---

### Task 6: M3a acceptance (live) and wrap-up

**Files:**
- Create `tests/contract/test_m3a_acceptance.py`.
- Modify ADR 0014 (accepted; consequences), ADR 0010 (context and shapes rows), README (shapes, context, validate), CHANGELOG, create `docs/backlog/m3a-deferred.md`.

- [ ] **Step 1: `test_m3a_acceptance.py`** runs through MCP and HTTP against the running stack, on a throwaway model `retail_m3a`. The setup is: agent scan of `fixture_delta`, `create_model`, `propose_from_scan`, submit, alice approves. Teardown hard-deletes the glossary and the ontology and never seals.
  - `test_m3a_shapes_validate_fixture_data`:
    - `GET /v1/models/retail_m3a/shapes` (turtle);
    - build instance data from the fixture Delta tables, one node per row typed with the table's concept class. Columns map to attribute IRIs through the model's own properties: the column name goes through the mapper's attribute naming (`core/mapper._attribute_name` with the default `MappingPolicy`), and the result must equal the property's `rdfs:label` (the native display name). Literals are typed from the property ranges;
    - pyshacl conforms;
    - mutating one `orders` row (a non-numeric amount, or a missing identifier) makes it fail on that focus node.
  - `test_m3a_context_maps_record_to_iris`: `GET /v1/models/retail_m3a/context.jsonld`, then a JSON record of one `customers` row using the context's short terms parses (rdflib JSON-LD) into triples with the attribute IRIs.
  - `test_m3a_validate_model`: the tool returns `conforms: true` for the fresh model, or only warnings; the structured counts equal the Markdown counts.
  - `test_m3a_git_commit_has_shapes_and_context`: the publication commit of the approve contains `ontologies/retail_m3a/shapes.ttl` (equal to the HTTP body) and `context.jsonld`.
- [ ] **Step 2:** Rebuild Lakeloom (`docker compose -f deploy/compose/docker-compose.yml up -d --build --wait lakeloom`), then run the full unit and contract suites green. Never `down -v`.
- [ ] **Step 3:** Docs: ADR, README, CHANGELOG, backlog.
- [ ] **Step 4: Commit** `test(contract): M3a acceptance — shapes validate fixture data (FR-PUB-04, FR-PUB-05)`.

---

## Out of scope for M3a

- Releases, tags, CHANGELOG, `?version=` beyond `latest`, Git `Link` headers and the triple-store sink (M3b).
- Import, `import_from_git`, drift detection and the SPARQL proxy (M3c).
- Validating lake data from Lakeloom itself (a `validate_data` tool); shapes are published for others to run.
- Units as SHACL constraints, and closed shapes.
