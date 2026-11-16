# 0016. Import, restore from Git, drift and the SPARQL proxy

- Status: accepted (M3c, 2026-11-16; live research in § Research, acceptance in § Consequences)
- Date: 2026-11-15
- Requirements: FR-IO-02, FR-IO-03, FR-IO-04, FR-IO-05, FR-GIT-07, FR-PRO-03, FR-PRO-07, FR-PUB-03, FR-OM-02, FR-OM-06, FR-AGT-03, FR-AGT-04, FR-CTX-06, NFR-SEC-07, NFR-SEC-12, NFR-SEC-13, NFR-SEC-14

## Context

M3c adds four things on top of the hybrid model (ADR 0011: an OM glossary plus a paired native
ontology of the same name) and the Git releases of ADR 0015:

- **import** of an RDF file (Turtle, RDF/XML, N-Triples, JSON-LD; OWL or SKOS vocabulary) as a
  reviewable proposal;
- **restore from Git**: the same, reading `ontology.ttl` from a release tag or the branch tip;
- **drift**: rescan the tables a model was built from and propose the difference;
- a **read-only SPARQL proxy** over the model.

FR-OM-02 forbids writes outside change sets, so all three writers only create draft proposals
through `ProposalService`. The plan is `docs/superpowers/plans/2026-11-15-m3c-import-drift-sparql.md`;
its Decisions section is the long form of the Decision below. The OM behaviour these rely on was
checked live on 2.1.0-rc1 (§ Research) and is pinned by
`tests/contract/test_om_import_sparql_probe.py`.

## Decision

### Import is computed by Lakeloom, not by OM's importers

- `PUT /v1/glossaries/name/{name}/importRdf` writes outside change sets (FR-OM-02): never called
  with `dryRun=false`.
- `POST /v1/ontologies/{id}/imports/preview` yields native operations only, with minted names
  (§ Research R5): glossary terms, relations and mappings would be lost. Not used.
- Lakeloom parses the file, reads **model terms** from it, diffs them against the model's current
  graph and proposes the difference as ordinary typed operations.

### Formats and safe parsing (`rdf/untrusted.py`)

- `format`: `turtle`, `rdfxml`, `ntriples`, `jsonld`. SKOS is a vocabulary read the same way in any
  of them. CSV and LinkML (FR-IO-03, SHOULD) are deferred.
- Size checked on the UTF-8 bytes before parsing (`LAKELOOM_IMPORT_MAX_BYTES`, default
  `20971520`); parse plus extraction under `asyncio.wait_for` in a thread
  (`LAKELOOM_IMPORT_TIMEOUT_SECONDS`, default `60`, 1–600; the thread is not killed); at most
  `LAKELOOM_IMPORT_MAX_TRIPLES` (default `200000`) triples.
- JSON-LD: a string (remote) `@context` or any `@import` is refused before rdflib sees it.
  RDF/XML: `<!DOCTYPE` / `<!ENTITY` (case-insensitive) is refused. No URL is ever dereferenced
  (NFR-SEC-13). Parse errors keep line/column (FR-IO-04).

### Namespace

- Source namespace: `base_iri` when given, else the longest `/`- or `#`-terminated prefix shared by
  most `owl:Class` / `skos:Concept` subjects (ties: lexicographically smaller).
- Terms map onto the target by local name and are re-minted under the target base IRI; subjects
  outside the namespace are skipped with a counted warning.

### Vocabulary read by `extract_terms` (what Lakeloom exports; verified in § Research R1)

For local name `L` in namespace `ns`:

- **Concept** `ns:L` (no `.` in `L`, typed `skos:Concept` or `owl:Class`, `L` a valid `Name`):
  `display_name` = first `skos:prefLabel` (no language or `en`, lexicographically first) when it
  differs from `L`; `description` = `skos:definition`, else `rdfs:comment`; `parent` = a
  `skos:broader` target in `ns`; `synonyms` = `skos:altLabel` values, sorted, deduplicated.
- **Attribute** `ns:C.a`, `owl:DatatypeProperty` with `rdfs:domain ns:C`: `datatype` from the
  XSD range (`rdfs:Literal` → `xsd:anySimpleType`; none → `xsd:string` with a warning);
  `is_identifier` = `owl:FunctionalProperty`; `allowed_values` = `owl:oneOf`; `unit` =
  `qudt:unit`; `description` = `rdfs:comment`.
- **Relation** `ns:C.r`, `owl:ObjectProperty` with `rdfs:domain ns:C`, `rdfs:range ns:T`.
- **Glossary links** — *amended by R1*: `om:relatedTo`, `skos:related`, and any other predicate
  outside the RDF/RDFS/OWL/SKOS/`om:` vocabularies between two namespace concepts (OM exports a
  custom relationship type under its `rdfPredicate`, e.g. `http://localhost:8000/ontology/retail/placedBy`,
  never as `om:<type>`; and a link applied through a change set is always stored as
  `relatedTo`). Ignored when an object property already links the pair in either direction,
  otherwise counted ("<n> glossary-only links are not imported").
- **Mapping**: `skos:exactMatch|closeMatch|broadMatch|narrowMatch|relatedMatch` to an absolute IRI.
- **Axiom (one per concept)**: the concept's `rdfs:subClassOf` `owl:Restriction` blank nodes on its
  own attributes (`owl:cardinality`/`qualifiedCardinality` EXACT, `minCardinality` MIN,
  `maxCardinality` MAX), sorted by (property, kind); named `<Pascal(C)>PrimaryKey` when every
  expression is EXACT 1 on an identifier, else `<Pascal(C)>Restrictions`.
- **Ignored silently** — *amended by R1*: `om:fullyQualifiedName`, `skos:inScheme`,
  `skos:topConceptOf`, `skos:hasTopConcept`, **`skos:narrower`** (OM writes it on the parent as the
  inverse of the child's `skos:broader`); `rdf:type` `om:GlossaryTerm`, `skos:Concept`,
  `owl:Class`, `owl:FunctionalProperty`; `rdfs:label` equal to the local name; the
  `owl:Ontology` / `skos:ConceptScheme` node and vocabulary declarations of non-namespace subjects.
- Everything else on a namespace subject is counted ("<n> triples not representable").

### Semantic diff (round-trip criterion)

`semantic_view(graph, base_iri)` keeps every triple whose subject is an IRI in `base_iri`, minus
`om:fullyQualifiedName`, `skos:inScheme`, `skos:topConceptOf`, plus reachable blank-node closures.
"No semantic diff" = `release_diff.diff_graphs` of the two views reports `bump is None`.

### Import diff (`import_operations`)

- `mode="merge"` adds and updates; `mode="replace"` also removes what the file lacks.
- **Concept**: missing → `AddConcept`; changed display name, description, synonyms or parent →
  `UpdateConcept` (`base_version` from OM, fail closed); removed → `RemoveConcept`.
- **Attribute** — *amended by R2/R4*:
  - missing → `AddAttribute`;
  - changed datatype, identifier, values or unit → `AddAttribute` whose native `UPSERT_PROPERTY`
    carries `targetId` and `baseVersion` of the existing property (from `list_native_members`,
    `OMContext.native_ids`/`native_versions` under `attr:<iri>`). rc1 then replaces the
    property's structural fields in full; without them the change set ends `APPLY_FAILED`
    ("Definition already exists");
  - a changed **description** alone is not proposed (rc1 ignores description and displayName on
    an existing native definition): warning "<n> attribute descriptions cannot be changed on
    OpenMetadata 2.1.0-rc1". When other fields change too, the description stays as it is in OM.
  - removed → `RemoveAttribute`. The plan's fallback (remove in one part, add in the next) is
    **not possible**: a deleted definition is a tombstone whose name stays reserved (R4).
- **Relation / mapping**: missing → add; removed (replace) → remove.
- **Axiom** — *amended by R2*: compared by expressions; different → `AddAxiom` with the existing
  axiom's `targetId`/`baseVersion` (replaced, verified); missing → `AddAxiom`; removed (replace)
  → `RemoveAxiom` by OM native name.
- **Re-adding a tombstoned definition** — *added by R4*: a term, attribute or axiom whose native
  definition was deleted earlier (`include=deleted` lists it) cannot be created again through a
  change set on rc1; it is skipped with a warning naming it.
- **Axiom labels** — *added by the M3c acceptance*: the RDF carries no axiom label or
  description, but rc1 refuses a draft whose new axiom has none (400 `query param displayName
  must not be null`, `… description must not be null`). An imported axiom's `displayName` and
  `description` are its name (as `ProposalService` already does when it validates an axiom).
- Order: concepts (parents first), attributes, relations, mappings, axioms; removals reversed.
- Rule ids `R-IMPORT` / `R-GIT-RESTORE`; each operation has `Evidence(kind="import", ref=<IRI>)`.
- Operations already pending in an open proposal (`pending_op_keys`) are dropped with a warning;
  `dedupe()` is not applied. Nothing left → `NothingToPropose`.
- **Prerequisite (R1b)**: `to_om` must send `enumValues` as OM `OntologyLiteral` objects
  (`[{"value": v}]`) and `native_rdf` must read `value` from them. Today strings are refused
  (400 "Invalid request format") and the objects OM returns are dropped, so no export carries
  `owl:oneOf`.

### Two-step confirm, `import_model` and `import_from_git`

- `import_model(model, content, format, mode="merge", base_iri=None, confirm_token=None)`: without
  a token a dry run (`ImportPlan`: counts, first 100 operations, warnings, Markdown, token over
  `{model, format, mode, base_iri, sha256(content)}` and `sha256(ModelService.version(model))`);
  with it, recompute, verify, and create a **draft** proposal.
- `import_from_git(model, version="latest", from_model=None, mode="merge", confirm_token=None)`
  (FR-GIT-07): reads `ontologies/<from_model>/ontology.ttl` from the tag (`ReleaseReader.file`) or
  the tip of `origin/<LAKELOOM_GIT_BRANCH>`; the namespace is that commit's manifest `base_iri`;
  the token also covers the source object sha. Needs Git on, the target model in OM, the version
  present.
- Provenance gains `origin: scan|agent|import|git|drift` (default `scan`) and `source_document`;
  publication commits add `Lakeloom-Origin` and `Lakeloom-Import` trailers (scan commits
  unchanged).

### Drift (FR-PRO-07)

- Baseline per table: the newest scan referenced by an `applied` proposal of the model that holds
  it; none → `validation_error`. Names come from those proposals' operations (`bind_tables`).
- One fresh scan per baseline source and schema; new tables are *added*, missing (not in error)
  tables *removed*, tables in error reported and never removed.
- Column diff: added, removed, type changed, comment changed; rename candidates (same raw type and
  ordinal) and table rename candidates (same column list) become remove plus add with "possible
  rename" evidence.
- Operations: added / type-changed columns via the mapper with the bound concept (a type change
  is an `AddAttribute` with the existing property's `targetId`/`baseVersion`, as above); removed
  columns and tables as removals of terms the graph has. **Comment change** — *amended by R2*: not
  proposed on rc1 (native descriptions cannot change); reported in the table's drift and as a
  warning.
- `DriftReport`; no drift → no proposal; otherwise a draft with `origin="drift"`.

### SPARQL proxy (FR-PUB-03)

- Proxied to `POST /v1/glossaries/{id}/sparql` with the bot token; never the ontology endpoint
  (it queries a sealed release).
- **Only the glossary is queryable** (R3): concepts, labels, definitions, synonyms, broader /
  narrower, mappings and links. Native attributes, object properties and axioms are not in the
  dataset; the tool description says so.
- Guard before any OM call: length cap (`LAKELOOM_SPARQL_MAX_QUERY_CHARS`, default `20000`);
  `parseQuery`, with `parseUpdate` telling updates (`forbidden`) from syntax errors
  (`validation_error`); only SELECT/ASK/CONSTRUCT/DESCRIBE; any `SERVICE` is `forbidden`.
  OM's own parser also rejects updates (R3), as defence in depth only.
  The guard runs once per request, in a worker thread (`asyncio.to_thread`): rdflib's
  pure-Python parse of a near-cap query takes up to ~2 s and must not block the event loop.
  rdflib 7.6's grammar recurses per triple pattern and per brace; in that thread (a fixed,
  shallow stack, the same for CLI, MCP and HTTP) about 84 `.`-separated patterns in one
  group or 36 nested groups pass (50 patterns and 30 groups are pinned by tests), beyond
  that `RecursionError` is a `validation_error` hinting to split the block with `;` or into
  `{ }` groups (unbounded). The recursion limit is not raised. rdflib also fails to parse
  `SERVICE` after a `PREFIX`; such a query is refused as a `validation_error`.
- Formats: SELECT/ASK `json` (default), `xml`, `csv`, `tsv`; CONSTRUCT/DESCRIBE `turtle`
  (default), `jsonld`, `ntriples`, `rdfxml` (the content types OM returns match the HTTP
  `Accept` list, R3). HTTP bodies: `application/sparql-query`, form `query=`, or JSON.
- **Timeout** — *amended by R3*: `LAKELOOM_SPARQL_TIMEOUT_SECONDS` (default `30`, **range 1–30**),
  sent as OM's `timeout` **in milliseconds** (`seconds * 1000`; OM accepts 1–30000) and used, plus
  a small margin, as the httpx timeout. An expired OM query is a **500**
  `UNHANDLED_SERVER_EXCEPTION` with message `[null]`, which maps to `upstream_unavailable` like
  any 5xx.
- Response cap `LAKELOOM_SPARQL_MAX_RESULT_BYTES` (default `5242880`), streamed; OM 4xx →
  `validation_error` with OM's detail; 5xx / transport → `upstream_unavailable`.
- Tool `sparql_query(model, query, format=None, max_tokens=None)`, `lakeloom:read`; the body is
  quoted untrusted text, `json` results also as truncatable `structuredContent`; queries logged as
  sha256 and length only. `get_server_info.features` gains `import`, `drift`, `sparql`.

### Scopes, audit, prompt, CLI

- `import_model`, `import_from_git`, `detect_drift`: `lakeloom:propose` plus the model's propose
  groups; audited with proposal id (and content sha256). `sparql_query`: `lakeloom:read`.
- Prompt `drift_check(model)`. CLI `lakeloom import`, `import-git`, `drift`, `sparql`.

## Research (OM 2.1.0-rc1, 2026-11-15)

Checked live against the compose dev stack (bot JWT `ingestion-bot`) on throwaway paired models
`llm3c_probe_<hex>`, filled by change sets the bot applied (test setup only) and hard-deleted
afterwards (404 with `include=all`). Nothing was sealed; `retail` was not modified. The contract
test `tests/contract/test_om_import_sparql_probe.py` pins the export vocabulary (R1), the
`enumValues` literal objects and their round trip through `to_om` and `native_rdf` (R1b), the
property and axiom upserts with and without a target, the cleared fields, the description-only
no-op on a property and the one-change-per-definition draft refusal (R2), the SPARQL dataset,
result formats, content types, OM's own 400s, the `timeout` bounds and the fast failure at
`timeout=200` (R3), the property and concept tombstones (R4) and the preview shape (R5). Not
pinned, observed once: the custom `rdfPredicate` export of `retail`'s typed relations (R1), the
`UPSERT_CONCEPT` description no-op (R2), the ~5 s failure at `timeout=5000`, the uncapped 16 s
query, the 56-triple count, `LOAD`, the lexical-error 400 and the 401 (R3), and the preview's
axiom and SKOS cases (R5).

The probe model: concepts `Party`, `Customer` (displayName `Client`, synonym `Buyer`, parent
`Party`, `exactMatch https://schema.org/Person`) and `Order`; attributes `Customer.customerId`
(`xsd:long`, identifier) and `Customer.height` (`xsd:decimal`, enum values, unit `unit:M`); the
relation `Order placedBy Customer`; the axiom `CustomerPrimaryKey` (EXACT 1 on `customerId`).

### R1. Glossary export vocabulary (`GET /v1/glossaries/{id}/exportOntology?format=turtle`)

| Model fact | Exported as |
|---|---|
| term type | `rdf:type skos:Concept, om:GlossaryTerm` (no `owl:Class`; that comes from the native side) |
| display name | `skos:prefLabel "Client"` = displayName when set, else the name (`Party` → `"Party"`) |
| synonym | `skos:altLabel "Buyer"` |
| description | `skos:definition` (no `rdfs:label` / `rdfs:comment` on terms) |
| parent | `skos:broader <Party>` on the child **and `skos:narrower <Customer>` on the parent**; no `rdfs:subClassOf` |
| top concepts | the scheme's `skos:hasTopConcept` and the term's `skos:topConceptOf` for roots only; a child's `om:fullyQualifiedName` nests (`<slug>.Party.Customer`) |
| mapping | `skos:exactMatch <https://schema.org/Person>` |
| typed relation `placedBy` | **`om:relatedTo` and `skos:related`, on both terms**; no `placedBy` predicate |

- **The relation type is lost.** The `ADD_RELATIONSHIP` carried `placedBy`'s id (and the stored
  draft kept it), but after apply the term's `relatedTerms` say `relationType: relatedTo`. A
  relationship created with `POST /v1/glossaryTerms/{id}/relations` (the seeded `retail`) keeps
  its type and exports under the type's `rdfPredicate`, which for the custom types is
  `http://localhost:8000/ontology/retail/placedBy` (not `om:placedBy`). Relations therefore
  round-trip only through the native object property (`Order.placedBy`, kind `OBJECT`, range
  `Customer`), which is what `extract_terms` reads.
- **R1b. `enumValues` are `OntologyLiteral` objects.** The spec types them as
  `[{"value", "datatypeIri"?}]`; a draft with plain strings (what `to_om` sends) is refused at
  creation with **400 `Invalid request format`**. With objects, `GET /v1/ontologyProperties` returns
  `[{"value": "1.5"}, {"value": "2.0"}]`, which `native_rdf` drops (it keeps only strings).
  `unit` is a plain string (`"unit:M"`) and round-trips.

### R2. UPSERT on an existing native definition

| Payload | Result |
|---|---|
| `UPSERT_PROPERTY` as `to_om` emits it (no `targetId`/`baseVersion`) on an existing IRI | apply HTTP **200**, change set **`APPLY_FAILED`**, `operationsFailed: 1`, message `Definition already exists: <ontology>."Customer.height"`; property unchanged |
| same, with a fresh property id | same failure |
| with `targetId` = existing id, `baseVersion` = its version | `APPLIED`; same id, version +0.1; new range only; **fields the upsert omits are cleared** (`unit`, `enumValues`) |
| same, but the change is only `description` / `displayName` | `APPLIED`, **nothing changes**, version unchanged |
| `UPSERT_AXIOM` on an existing name without / with target | `APPLY_FAILED` `Definition already exists: <ontology>.CustomerPrimaryKey` / `APPLIED`, expressions replaced (EXACT → MIN) |
| `UPSERT_CONCEPT` with target, new description | `APPLIED`, description unchanged, version unchanged |
| `DELETE_PROPERTY` + `UPSERT_PROPERTY` of the same definition in one draft | draft refused, 400 `A native draft may change each definition only once` |

So `UPSERT_PROPERTY` replaces only when addressed to the existing definition, and native
descriptions cannot be edited through change sets on rc1.

### R3. Glossary SPARQL (`POST /v1/glossaries/{id}/sparql`, `SparqlQuery`)

- `SELECT ?s WHERE { ?s a skos:Concept }`, `format=json` → 200
  `application/sparql-results+json`, exactly the model's three concepts: the dataset is **this
  glossary only** (56 triples; `retail` is not visible) and has no named graphs
  (`GRAPH ?g {…}` → no bindings).
- **Native members are not visible**: the only `owl:DatatypeProperty` subject is the vocabulary
  declaration `om:fullyQualifiedName`; no object property of the model; no restrictions.
- Content types: `xml` `application/sparql-results+xml`, `csv` `text/csv`, `tsv`
  `text/tab-separated-values`; CONSTRUCT/DESCRIBE `turtle` `text/turtle`, `jsonld`
  `application/ld+json`, `ntriples` `application/n-triples`, `rdfxml` `application/rdf+xml`.
  ASK `json` gives `{"head": {}, "boolean": true}`.
- A format that does not fit the form is OM's own 400: `Format 'json' is not valid for a
  CONSTRUCT query`, `Format 'turtle' is not valid for a SELECT query`.
- `INSERT DATA {…}`, `DELETE WHERE {…}`, `LOAD <…>` → **400** `Invalid SPARQL query: Encountered
  " <INSERT_DATA> "INSERT DATA "" at line 1, column 1. Was expecting one of: …` (Jena query
  parser); nothing is written. Garbage → 400 `Invalid SPARQL query: Lexical error …`.
- `timeout` is an integer in **milliseconds**, 1–30000 (`0` → 400 `query param timeout must be
  greater than or equal to 1`; `30001` → 400 `… less than or equal to 30000`). A ~1 s query with
  `timeout=200` fails after ~0.2 s, with `timeout=5000` after ~5 s, both as **500**
  `{"errorType": "UNHANDLED_SERVER_EXCEPTION", "message": "An exception with message [null] was
  thrown …"}`. Without `timeout` a 16 s query ran to completion (no default cap observed).
- `inference` is a lower-case enum (`none|rdfs|owl|custom`); `NONE` → 400 `Invalid request format`.
- Unknown glossary id → 404 `glossary instance for <id> not found`; a bad bearer → 401.
- `SERVICE` was deliberately not sent to OM (it would make OM call out); Lakeloom refuses it.

### R4. Deleted native definitions are tombstones

`DELETE_PROPERTY` applied: the property leaves the default listing but stays with
`include=deleted` (`deleted: true`, same id, version +0.1). Upserting the same name again as a new
definition → `APPLY_FAILED` `Definition already exists: …`; with the tombstone as `targetId` →
apply **400** `Definition is deleted, outside the draft's ontology, or has changed since
drafting`. The same holds for `DELETE_CONCEPT` then `UPSERT_CONCEPT` (`APPLY_FAILED`,
`Definition already exists: <ontology>.V`). Only `/v1/ontologyProperties/restore` (outside change
sets, so excluded by FR-OM-02) could bring one back; it was not tried.

### R5. Native import preview (`POST /v1/ontologies/{id}/imports/preview`)

On a two-class Turtle (`Alpha`, `Beta` as `owl:Class`): 200 with an unsaved change set shaped
like `OntologyChangeSet` (`name: native-import-<uuid>`, no `id`, `state: DRAFT`,
`glossaries: []`, `ontology: <name>`, `baseWorkingRevision` = current), whose `operations` are
two `UPSERT_CONCEPT` (`OWL_CLASS`, the file's IRIs, names minted as
`imported-<local>-<sha256>`). Nothing is persisted (`GET …/name/<name>` 404, working revision
unchanged). A `rdfs:subClassOf` between the two adds an `UPSERT_AXIOM` (`NAMED_CLASS`
expression, provenance `Imported`); SKOS concepts become `SKOS_CONCEPT` with `displayName` from
`skos:prefLabel`. No terms, relations or mappings: confirms why Lakeloom does not use it.

## Consequences

- Import, restore and drift reuse `ProposalService`; FR-OM-02 holds (no `importRdf`, no
  `/restore`, no preview). Every writer only creates a **draft**; a human approves it as for
  scans.
- The controller follow-up of Task 1 was done in M3c (Task 2b): `to_om` sends `enumValues` as
  `OntologyLiteral` objects and `native_rdf` reads them back (R1b); the `UPSERT_PROPERTY` /
  `UPSERT_AXIOM` of a definition that already exists carry its `targetId`/`baseVersion` (R2).
  A targeted upsert replaces the definition in full, so fields it omits are cleared in OM.
- Attribute description edits, re-adding deleted names (tombstones) and typed glossary links are
  rc1 limits that import and drift report as warnings instead of proposing operations that would
  fail.
- The SPARQL tool answers questions about the vocabulary only (the glossary dataset); attributes,
  relations and axioms are read with `explain_concept` or the exports.
- The guard's parse limits above were measured on macOS's worker-thread stack. The image runs on
  Debian (glibc), whose default thread stack differs; the pinned 50 patterns / 30 groups are
  well inside both, the upper bounds may differ.
- **Live acceptance** (`tests/contract/test_m3c_acceptance.py`, dev stack, Git mode `direct`):
  - `retail_m3c` is built from `fixture_delta`, approved by alice and released as `1.0.0`. Its
    glossary and ontology are then hard-deleted and `create_model` makes them again with the
    same name and base IRI: OM gives a new glossary id with no terms, and the name is free
    again (the tombstones of R4 live inside the deleted ontology).
  - `import_from_git(retail_m3c, version="1.0.0")` plans exactly the terms `extract_terms` reads
    from the tagged `ontology.ttl` (4 concepts, 20 attributes, 2 relations, 4 axioms, no
    warnings). Approved by alice, its publication commit carries `Lakeloom-Origin: git` and
    `Lakeloom-Import: retail_m3c/v1.0.0@<12-char sha>`, and the export has **no semantic
    diff** from the tagged file (`semantic_view` of both: 156 triples each, `bump is None`).
  - The first live run found the axiom label rule above: without it the import's native change
    set was refused at creation.
  - `import_model` of the same Turtle is `validation_error` "the model already matches"; a
    JSON-LD with a remote `@context` is refused before parsing.
  - Drift: `fixture_scratch` reads a copy of the fixture `orders` table in `.scratch-lake/`
    (git-ignored, mounted read-only at `/scratch-lake`); the copy drops the table's declared
    foreign key to `sales.customers`, since the mapper would otherwise propose a relation to a
    `Customer` concept the model lacks. After one row with a new `channel` column is appended
    (delta-rs, schema merge), `detect_drift` reports the table `changed` with
    `added_columns == ["channel"]` and drafts exactly one `add_attribute` `channel` on the bound
    concept, `origin="drift"`, provenance table version baseline + 1.
  - SPARQL: `POST /v1/models/retail_m3c/sparql` with a SELECT over `skos:Concept` is 200
    `application/sparql-results+json` with the export's four concepts, the MCP tool returns the
    same rows, and `INSERT DATA` is 403 from Lakeloom (`forbidden`, before any OM call).
- Deferred follow-ups: [M3c backlog](../backlog/m3c-deferred.md).
