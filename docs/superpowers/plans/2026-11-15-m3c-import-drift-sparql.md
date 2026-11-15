# M3c — Import, Restore from Git, Drift Detection and the SPARQL Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** close M3 with four features:
- `import_model` turns a semantic file into a reviewable proposal, with a dry run first;
- `import_from_git` restores a model version from Git through the same path;
- `detect_drift` compares the lake with the last scan used for a model and proposes the changes;
- `sparql_query` and `POST /v1/models/{slug}/sparql` run read-only SPARQL, proxied to OpenMetadata.

**Architecture:**
- **Pure core:**
  - `rdf/untrusted.py` parses hostile input safely;
  - `rdf/semantic.py` gives the OM-independent view of a model graph;
  - `core/import_terms.py` reads model terms (concepts, attributes, relations, mappings, axioms) from any graph;
  - `core/import_diff.py` turns "current terms vs incoming terms" into typed `ProposalOperation`s;
  - `core/drift.py` compares two scans and binds tables to concepts;
  - `core/sparql.py` classifies and guards a query.
- **One proposal path.** Import, restore and drift all end in a new `ProposalService.propose_operations(...)`, extracted from `_from_scan`. So parts, the hybrid glossary + native change sets, provenance, review, apply and Git publication are reused unchanged. Nothing is applied by these tools, and FR-OM-02 holds: every write is a change set.
- **Services:**
  - `core/imports.py` (`ImportService`: plan and confirm for file and Git sources);
  - `core/drift_service.py` (`DriftService`: scan, diff, propose);
  - the SPARQL proxy lives in `OMClient.glossary_sparql` plus a thin service in `core/sparql.py`.

**Tech Stack:** existing stack (rdflib incl. `rdflib.plugins.sparql.parser`, SQLAlchemy, httpx, FastAPI, MCP SDK, Typer, deltalake). No new runtime dependency.

**Spec:** `REQUIREMENTS.md`:
- FR-IO-02 (import OWL/Turtle/N-Triples/JSON-LD/SKOS as a reviewable change set, dry run first), FR-IO-04 (rdflib validation, line/column), FR-IO-05 (size limit, default 20 MB, and a timeout);
- FR-GIT-07 (`import_from_git` through import, producing a change set for review);
- FR-PRO-07 (`detect_drift`: added/removed/renamed columns and tables), FR-PRO-02/03/08 (dedupe, provenance, size limit), FR-AGT-04 (prompt `drift_check`);
- FR-PUB-03 (`POST /v1/models/{slug}/sparql`, read-only, rejects updates), FR-OM-01 (glossary-scoped SPARQL), FR-OM-02, FR-OM-06 (degrade gracefully);
- NFR-SEC-06/07, NFR-SEC-12 (audit), NFR-SEC-13 (no runtime URLs, so no remote JSON-LD contexts, no `SERVICE`), NFR-SEC-14 (imported text is untrusted);
- §7 tools `import_model`, `detect_drift`, `sparql_query`;
- M3 acceptance criteria:
  - "importing an exported release into a fresh OM round-trips with no semantic diff";
  - "drift detection proposes changes after a fixture column is added".

Earlier decisions it builds on:
- ADR 0011 (hybrid model, operations, parts, declaration order);
- ADR 0012 (review, apply);
- ADR 0013 (Git publication, trailers);
- ADR 0015 (releases, `ReleaseReader`, `release_diff`).

New ADR: `docs/decisions/0016-import-drift-sparql.md`.

## Decisions taken in this plan (controller; owner may overrule at plan review)

- **Import is computed by Lakeloom and not by OM's importers.**
  - `PUT /v1/glossaries/name/{name}/importRdf` writes outside change sets (FR-OM-02), so Lakeloom never calls it with `dryRun=false`.
  - `POST /v1/ontologies/{id}/imports/preview` only yields *native* operations, so glossary terms, relations and mappings would be lost under the hybrid model.
  - So Lakeloom parses the file and reads **model terms** from it (vocabulary below). It diffs them against the terms of the model's current graph and proposes the difference as ordinary typed operations.
  - OM's preview is probed once in Task 1 for the ADR only.
- **Accepted formats** (`format` argument): `turtle`, `rdfxml` (OWL), `ntriples`, `jsonld`. "SKOS" is a vocabulary, not a syntax: a SKOS file in any of the four syntaxes is read through the same vocabulary. CSV and LinkML (FR-IO-03, SHOULD) are deferred.
- **Safe parsing (`rdf/untrusted.py`):**
  - **Size.** Checked on the UTF-8 bytes *before* parsing: `LAKELOOM_IMPORT_MAX_BYTES` (default `20971520`), else `validation_error` "import is <n> bytes; the limit is <max> (LAKELOOM_IMPORT_MAX_BYTES)".
  - **Timeout.** Parse plus term extraction run in `asyncio.to_thread` under `asyncio.wait_for(LAKELOOM_IMPORT_TIMEOUT_SECONDS)` (default `60`, range 1–600), else `validation_error` "import parsing exceeded <n> s". The worker thread is not killed; this is documented.
  - **JSON-LD.** The document is loaded with `json.loads` first. Any `@context` value that is a string or holds a string (a remote context), and any `@import`, is refused: `validation_error` "remote JSON-LD contexts are not allowed; inline the context". Inline object contexts are fine.
  - **RDF/XML.** Refused when the bytes contain `<!DOCTYPE` or `<!ENTITY`, compared case-insensitively (no DTDs, so no XXE or entity expansion).
  - **Errors.** Parse errors keep `parse_rdf`'s line/column message (FR-IO-04).
  - **Triple cap.** At most `LAKELOOM_IMPORT_MAX_TRIPLES` (default `200000`) triples, else `validation_error`.
- **Namespace.**
  - **Source namespace.** Terms are read from the *source namespace*. It is the `base_iri` argument when given. Otherwise it is detected: the longest IRI prefix ending in `/` or `#` that the most `owl:Class`/`skos:Concept` subjects share. Ties go to the lexicographically smaller prefix; no class at all is `validation_error` "no concepts found".
  - **Mapping to the target.** Terms are mapped onto the target model by **local name**: `unquote(iri[len(ns):])`. Their IRIs are re-minted with the target base IRI, so a file from another slug or host imports cleanly.
  - **Other subjects.** Subjects outside the namespace are not imported. The warning is "<n> subjects outside <ns> are not imported", plus up to 10 CURIEs.
- **Vocabulary read by `extract_terms` (exactly what Lakeloom exports, ADR 0011 native appendix, live in Task 1).** For local name `L` in namespace `ns`:
  - **Concept:** `ns:L` with no `.` in `L`, typed `skos:Concept` or `owl:Class`, and `L` a valid `Name` (`^[A-Za-z][A-Za-z0-9_ ]{0,127}$`; invalid names are skipped with a warning).
    - `display_name`: the first `skos:prefLabel` (no language or `en`, lexicographically first) when it differs from `L`.
    - `description`: `skos:definition`, else `rdfs:comment`.
    - `parent`: a `skos:broader` target in `ns`.
    - `synonyms`: `skos:altLabel` values, sorted and deduplicated.
  - **Attribute:** `ns:C.a`, typed `owl:DatatypeProperty`, with `rdfs:domain ns:C`.
    - `datatype`: `xsd:<local>` for an XSD range; `rdfs:Literal` → `xsd:anySimpleType` (not an OWL 2 datatype, so it is minted back as `rdfs:Literal`); no range → `xsd:string` with a warning.
    - `is_identifier`: typed `owl:FunctionalProperty`.
    - `allowed_values`: the `owl:oneOf` list. *(Amended by Task 1, ADR 0016 R1b: today no export carries `owl:oneOf`. rc1 wants `enumValues` as `OntologyLiteral` objects `[{"value": v}]`; `to_om` sends strings (400 "Invalid request format") and `native_rdf` drops the objects OM returns. Both must be fixed for allowed values to round-trip.)*
    - `unit`: `qudt:unit`.
    - `description`: `rdfs:comment`.
    - An `rdfs:label` other than `a` gets the warning "label of <C.a> not imported" (attributes have no display name).
  - **Relation:** `ns:C.r`, typed `owl:ObjectProperty`, with `rdfs:domain ns:C` and `rdfs:range ns:T`. It becomes `(source C, relation_type r, target T)` with `rdfs:comment` as its description.
  - **Glossary links** (`om:relatedTo`, `skos:related` and any other predicate outside the RDF/RDFS/OWL/SKOS/`om:` vocabularies between two namespace concepts; *amended by Task 1, ADR 0016 R1: OM exports a custom relationship type under its `rdfPredicate`, e.g. `http://localhost:8000/ontology/retail/placedBy`, never `om:<type>`, and a link applied through a change set is always stored as `relatedTo`*): ignored when an object property already links the same pair in either direction (the glossary side is derived from it, and `om:relatedTo` is symmetric). Otherwise they get the warning "<n> glossary-only links are not imported".
  - **Mapping:** `skos:exactMatch|closeMatch|broadMatch|narrowMatch|relatedMatch` from a concept to any absolute IRI.
  - **Axiom (one per concept):** every `rdfs:subClassOf` blank node of a concept that is an `owl:Restriction` on an attribute `ns:C.a` of the same concept, with `owl:cardinality` / `owl:qualifiedCardinality` (EXACT), `owl:minCardinality` (MIN) or `owl:maxCardinality` (MAX), becomes one expression `{"kind":"RESTRICTION","restrictionKind":…,"propertyName":"a","cardinality":n}`.
    - Expressions are sorted by (property, kind).
    - The axiom is named `<Pascal(C)>PrimaryKey` when every expression is EXACT 1 on an identifier attribute, else `<Pascal(C)>Restrictions`. Names are not in the RDF; this rule reproduces the mapper's names.
    - A named `rdfs:subClassOf ns:P` equal to the concept's parent is covered by `parent`. Any other `subClassOf` shape gets the warning "<n> class axioms not representable".
  - **Ignored silently (OM bookkeeping):**
    - `om:fullyQualifiedName`, `skos:inScheme`, `skos:topConceptOf`, `skos:hasTopConcept`, `skos:narrower` (*amended by Task 1, ADR 0016 R1: OM writes it on the parent as the inverse of the child's `skos:broader`*);
    - `rdf:type` values `om:GlossaryTerm`, `skos:Concept`, `owl:Class` and `owl:FunctionalProperty` (all read above);
    - `rdfs:label` equal to the local name;
    - the `owl:Ontology` / `skos:ConceptScheme` node, and vocabulary declarations of non-namespace subjects.
  - **Everything else** on a namespace subject is counted: the warning is "<n> triples not representable", plus up to 10 predicate CURIEs.
- **Semantic diff (round-trip criterion).** `semantic_view(graph, base_iri) -> Graph` keeps:
  - every triple whose subject is an IRI in `base_iri`, minus the bookkeeping predicates `om:fullyQualifiedName`, `skos:inScheme` and `skos:topConceptOf`;
  - the blank-node closures reachable from those triples.

  "No semantic diff" means `release_diff.diff_graphs(semantic_view(release), semantic_view(export))` reports `bump is None` (isomorphic). The glossary id, which is new in a fresh OM, therefore never counts.
- **Import diff (`import_operations`).** It compares the current `TermSet` (the model's graph, namespace = the model's base IRI) with the incoming one:
  - `mode="merge"` (default) adds and updates only. `mode="replace"` also removes terms that the model has and the file lacks.
  - **Concept:** missing → `AddConcept`. Present with a different display name, description, synonyms or parent → `UpdateConcept` with `base_version` from OM (`OMClient.get_term("<model>.<L>")`, fail closed when absent). Removed (replace) → `RemoveConcept`.
  - **Attribute** (*amended by Task 1, ADR 0016 R2/R4*): missing → `AddAttribute`. Changed datatype, identifier, values or unit → `AddAttribute` again, whose native `UPSERT_PROPERTY` must carry `targetId` and `baseVersion` of the existing property (from `list_native_members`, `OMContext.native_ids`/`native_versions` under `attr:<iri>`); rc1 then replaces the structural fields in full (omitted fields are cleared), while without them the change set ends `APPLY_FAILED` "Definition already exists". A changed **description** is not proposed: rc1 ignores description and displayName on an existing native definition, so it gets the warning "<n> attribute descriptions cannot be changed on OpenMetadata 2.1.0-rc1" (with other changes, the description stays as in OM). The remove-then-add fallback is **not possible**: a deleted definition is a tombstone whose name stays reserved. Removed → `RemoveAttribute`.
  - **Relation / mapping:** missing → add; removed (replace) → remove; there is no change case (they are keys).
  - **Axiom:** compared by its expressions. Missing → `AddAxiom`; different → `AddAxiom` whose `UPSERT_AXIOM` carries the existing axiom's `targetId`/`baseVersion` (*amended by Task 1, ADR 0016 R2: without them rc1 ends `APPLY_FAILED` "Definition already exists"; with them the expressions are replaced*). In replace mode, current axioms the file lacks → `RemoveAxiom`, by the OM native axiom names (`list_native_members`).
  - **Re-adding a deleted definition** (*added by Task 1, ADR 0016 R4*): a concept, attribute or axiom whose native definition was deleted earlier (listed with `include=deleted`) cannot be created again through a change set on rc1; it is skipped with a warning naming it.
  - **Operation order:** concepts (parents first), attributes, relations, mappings, axioms; removals in reverse order (axioms, mappings, relations, attributes, then concepts, children first).
  - **Rule ids:** `R-IMPORT` (file) or `R-GIT-RESTORE` (Git). Each operation carries one `Evidence(kind="import", ref=<subject IRI as read>)`.
  - **Overlap with open proposals.** Operations whose `op_key` is already in an open proposal of the model (`ProposalRepository.pending_op_keys`) are dropped, with the warning "<n> operations already pending in <proposal ids>". The fuzzy `dedupe()` is *not* applied (an import is an exact statement of the terms).
  - **Nothing left:** `NothingToPropose` ("the model already matches the file").
- **Two-step confirm (FR-IO-02 "dry run first"; FR-AGT-03).**
  - **`import_model(model, content, format, mode="merge", base_iri=None, confirm_token=None)`.** Without a token it is a dry run: an `ImportPlan` with counts by kind, the first 100 operations, the warnings, a Markdown summary and a token.
    - Token args: `{model, format, mode, base_iri, sha256(content)}`.
    - State digest: `sha256(ModelService.version(model))`.
  - **With the token:** the plan is recomputed, the token verified, and `propose_operations` creates a **draft** proposal. The agent then calls `submit_proposal` as for scans.
- **`import_from_git(model, version="latest", from_model=None, mode="merge", confirm_token=None)` (FR-GIT-07).**
  - **Source.** `from_model` defaults to `model`.
    - A semver `version` reads `ontologies/<from_model>/ontology.ttl` from the tag through `ReleaseReader.file`.
    - `"latest"` reads it from the tip of `origin/<LAKELOOM_GIT_BRANCH>`, through `WorkingCopy.fetch` plus `show` under `repo_lock`.
  - **Namespace.** The source namespace is the `base_iri` of that commit's `manifest.yaml`.
  - **Token.** The state digest also covers the source ref's object sha.
  - **Preconditions.**
    - Git enabled ("Git publishing is off", `validation_error`).
    - The target model exists in OM (`not_found`, hint "create it with create_model; the source manifest's base IRI is <iri>").
    - The version exists (`not_found`, as for reads).
  - **Provenance.** `origin="git"`, plus the tag (or branch) and commit sha.
- **Provenance and Git trailers (FR-PRO-03).**
  - **`Provenance` gains two fields:**
    - `origin: Literal["scan","agent","import","git","drift"] = "scan"`. The default keeps stored rows valid; `propose_change`-only proposals never existed.
    - `source_document: SourceDocument | None`, where `SourceDocument(format: str, sha256: str, bytes: int, git_ref: str | None, git_commit: str | None)`.
  - The change-set provenance block carries both.
  - **Publication commits of such proposals** add, after `Lakeloom-Proposed-By`:
    - `Lakeloom-Origin: import|git|drift`;
    - for import, `Lakeloom-Import: sha256:<hex>`, or for git, `Lakeloom-Import: <ref>@<12-char sha>`.

    Scan-origin commits are byte-identical to today's.
- **Drift (FR-PRO-07).**
  - **Baseline.**
    - **Per table:** the table as recorded in the **newest scan referenced by an `applied` proposal of the model** that contains it (`ProposalRepository.applied_scan_ids(model)`, newest first).
    - **No applied proposal from a scan:** `validation_error` "no applied scan-based proposal for <model>", hint "run propose_from_scan and get it approved first".
  - **Binding** (`bind_tables`) tells which concept and attribute names the model uses for each table, from the operations of those applied proposals:
    - a table's concept is the `concept` of its `AddAttribute` operations (evidence `column` ref `<table>.<col>`), else of its `AddConcept` (evidence `table`);
    - a column's attribute is the `name` of that `AddAttribute`;
    - a table without a binding falls back to the mapper's names.
  - **Fresh scan.**
    - For each baseline source, one `ScanService.scan(source, schemas=<catalog.schema of its baseline tables>)` (no profiling) as the calling principal, which gives a new scan record (spec side effect "scan record").
    - Tables of those schemas absent from the baseline are **added tables** (`include_new_tables=True` by default).
    - Baseline tables absent from the new scan, and not in error, are **removed**.
    - A table in error is reported and skipped; it is never treated as removed.
  - **Column diff per table:** added, removed, type changed (raw type string), comment changed. A **rename candidate** is a removed and an added column with the same raw type and the same ordinal position, matched 1:1. A **table rename candidate** is a removed and an added table with identical (name, type) column lists.
  - **Operations:**
    - **Added or type-changed column:** the mapper runs on the new scan of that table, and the `AddAttribute` whose evidence names the column is kept with `concept` rewritten to the bound concept. FK relations whose columns are new are kept the same way.
    - **Comment change:** *amended by Task 1 (ADR 0016 R2)*: not proposed, since rc1 ignores description changes on an existing native property; it is reported in the table's drift and as a warning. (A type change is an `AddAttribute` carrying the existing property's `targetId`/`baseVersion`, as for import.)
    - **Removed column:** `RemoveAttribute(concept, bound name)`, only when the model graph has that attribute.
    - **Added table:** the mapper's operations for it, through `dedupe()` like `propose_from_scan`.
    - **Removed table:** its relations, axioms and attributes are removed, then `RemoveConcept` (with `base_version` from OM), only for terms the graph has.
    - **Renames** are proposed as remove plus add, each carrying `Evidence(kind="column"|"table", detail="possible rename of <old> to <new>")`. The summary has a "Possible renames" section; there is no rename operation (the IRI is derived from the name).
    - **Rule ids:** the mapper's for additions; `R-DRIFT-REMOVE` and `R-DRIFT-RENAME` otherwise.
  - **Result.**
    - `DriftReport{model, baseline_scan_ids, scan_ids, tables: [TableDrift], proposal: Proposal | None, warnings}`, where `TableDrift{fqn, status: unchanged|changed|added|removed|error, added_columns, removed_columns, changed_columns, renames: [(old,new)]}`.
    - No drift → `proposal=None`, with the text "no drift since <scan ids>" (not an error).
    - With drift, `propose_operations(origin="drift", scan_id=<new scan id>)` creates a draft. Its provenance tables are the new versions, so the publication commit keeps `Lake-Source-Version` trailers.
  - **No confirm token:** like `propose_from_scan`, drift only creates a draft.
- **SPARQL proxy (FR-PUB-03).**
  - **Target.** Proxied to `POST /v1/glossaries/{id}/sparql` (`SparqlQuery{query, format, timeout}`) with the bot token. The model's glossary id comes from `ModelService.get_model`.
    - `POST /v1/ontologies/{id}/sparql` queries the *sealed active release* and is never used (Lakeloom never seals).
    - Native attributes, object properties and axioms are **not** visible (Task 1, ADR 0016 R3): the dataset is the glossary alone. This is documented in the tool description and the ADR.
  - **Guard (`core/sparql.py`, before any OM call):**
    - at most `LAKELOOM_SPARQL_MAX_QUERY_CHARS` (default `20000`);
    - parsed with `rdflib.plugins.sparql.parser.parseQuery`. A parse failure is checked with `parseUpdate`: when that succeeds it is `forbidden` "SPARQL updates are not allowed", otherwise `validation_error` with rdflib's message (one line, 300 chars);
    - the form is `SelectQuery|AskQuery|ConstructQuery|DescribeQuery`;
    - any `ServiceGraphPattern` in the tree is `forbidden` "SERVICE is not allowed" (NFR-SEC-13);
    - `FROM` / `FROM NAMED` are passed through (OM scopes the dataset).
  - **Formats.** SELECT/ASK: `json` (default), `xml`, `csv`, `tsv`. CONSTRUCT/DESCRIBE: `turtle` (default), `jsonld`, `ntriples`, `rdfxml`. A format that does not fit the form is `validation_error`.
    - **HTTP** negotiates with `Accept`: `application/sparql-results+json`, `application/sparql-results+xml`, `text/csv`, `text/tab-separated-values`, `text/turtle`, `application/ld+json`, `application/n-triples`, `application/rdf+xml`. Otherwise 406.
    - **HTTP body:** `application/sparql-query` (raw), `application/x-www-form-urlencoded` (`query=`) or JSON `{"query": …}`.
  - **Limits.**
    - Timeout (*amended by Task 1, ADR 0016 R3*): `LAKELOOM_SPARQL_TIMEOUT_SECONDS` (default `30`, range 1–30), sent as OM's `timeout` **in milliseconds** (`seconds * 1000`; OM accepts 1–30000) and used, plus a small margin, as the httpx timeout. An expired OM query is a 500 `UNHANDLED_SERVER_EXCEPTION` (message `[null]`), so it is `upstream_unavailable`.
    - Response cap: `LAKELOOM_SPARQL_MAX_RESULT_BYTES` (default `5242880`), read in a stream; past the cap the call fails with `validation_error` "result larger than <n> bytes; add LIMIT".
    - OM 4xx → `validation_error` with OM's detail; 5xx and transport errors → `upstream_unavailable`.
  - **Tool output.** `sparql_query(model, query, format=None, max_tokens=None)` returns the body as quoted untrusted text (NFR-SEC-14). With `json`, `structuredContent` carries the parsed result, truncated to `max_tokens` with `truncated: true` (FR-CTX-06).
  - **Scope.** `lakeloom:read`; no per-model groups (NFR-SEC-07 only restricts propose/review/publish). Each query is logged as its sha256 and length, never its text.
  - **`get_server_info`.** `features` gains `import`, `drift` and `sparql`. `sparql` is false when OM answers 404 for the endpoint at startup, so it degrades (FR-OM-06).
- **Scopes and audit.**
  - `import_model`, `import_from_git` and `detect_drift` need `lakeloom:propose` and the model's propose groups (`require_group(..., "propose")`). They are audited (NFR-SEC-12), with the proposal id and, for import, the content sha256.
  - `sparql_query` needs `lakeloom:read`.
- **Prompt `drift_check(model)` (FR-AGT-04).** In plain language: run `detect_drift`, explain each table's drift and the possible renames, then `submit_proposal` only after the user agrees. The `bootstrap_data_product` prompt's text about M3 is updated.
- **CLI:**
  - `lakeloom import <model> <file> [--format] [--mode] [--base-iri] [--yes]`, where the format is guessed from the extension (`.ttl`, `.owl|.rdf`, `.nt`, `.jsonld`);
  - `lakeloom import-git <model> [--version] [--from-model] [--mode] [--yes]`;
  - `lakeloom drift <model> [--no-new-tables]`;
  - `lakeloom sparql <model> <query-file|-> [--format]`.

  The dry run is printed, and `--yes` confirms.

## Global Constraints

- **Earlier constraints still apply:**
  - Python ≥ 3.12; uv / ruff / pyright strict on `src/`.
  - Conventional Commits with requirement IDs; `git commit -s`; identity `Elcio Abrahao <elcio@tuta.com>`; trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  - Typed `LakeloomError`s with hints; untrusted text quoted in tool output.
  - Tools run through `guarded()` and take no URLs (`base_iri` is a namespace string compared textually, never fetched); every OM endpoint is in `OMClient.PATHS` and in the committed spec (`tests/unit/om/test_spec_paths.py`).
  - Code default and unit tests on SQLite; the compose stack and CI on Postgres. M3c adds no migration (provenance lives in the proposal JSON payload).
- **FR-OM-02:** "All ontology writes go through change sets". Import, restore and drift only create drafts through `ProposalService`, and never call `importRdf`.
- **FR-IO-05:** "Size limits on imports (configurable, default 20 MB) and a timeout."
- **FR-GIT-03:** files are unchanged by M3c; only commit messages of import, git and drift proposals gain trailers.
- **NFR-SEC-13:** no remote JSON-LD context, no XML DTD, no SPARQL `SERVICE`; no URL argument is ever dereferenced.
- **Dev-stack rules (owner):**
  - Throwaway models only (`retail_m3c`, `retail_m3c_drift`, hard-deleted in teardown); never modify `retail`; never seal OM releases.
  - Approve only as alice; no tokens printed or committed.
  - No `down -v`, no prune, never touch `supabase_*`.
  - Tags made by tests are deleted through the Gitea API in teardown; the scratch lake is emptied in teardown.

## Review Focus

1. **A hostile import file:**
   - a JSON-LD with a remote `@context` (`"@context": "http://169.254.169.254/"`);
   - an RDF/XML with `<!DOCTYPE … <!ENTITY … SYSTEM "file:///etc/passwd">`;
   - a 21 MB Turtle, or a billion-laughs-like Turtle with 10⁶ triples.

   Expected: refused before rdflib fetches or expands anything, with a `validation_error` naming the limit, and nothing written to OM. → Task 2 `test_jsonld_remote_context_refused`, `test_rdfxml_doctype_refused`, `test_size_checked_before_parse`, `test_triple_cap`.
2. **Importing a file from another tool** (a plain SKOS vocabulary without OWL, labels in several languages, concepts whose local names have spaces or hyphens, foreign subjects). Expected: valid concepts are proposed, invalid names and foreign subjects are skipped with counted warnings, `prefLabel` is chosen deterministically, and the dry run lists all of it. → Task 3 `test_plain_skos_vocabulary`, `test_invalid_local_names_skipped_with_warning`, `test_preflabel_choice_is_deterministic`.
3. **Re-importing what the model already holds**, or importing while an open proposal already adds the same terms. Expected: `NothingToPropose` ("already matches"), or the overlapping operations are dropped with a warning naming the pending proposal; never a duplicate term. → Task 3 `test_import_of_current_export_is_empty`; Task 5 `test_pending_overlap_dropped`.
4. **Drift when the source is partly unreachable** (one baseline table errors in the fresh scan) or a column was renamed. Expected: the erroring table is reported as `error` and never proposed for removal; a rename shows as one remove plus one add with "possible rename" evidence. → Task 6 `test_error_table_never_removed`, `test_rename_candidate_same_type_and_position`.
5. **A SPARQL text that sneaks in an update or an outbound call** (`INSERT DATA`, `DELETE WHERE`, `LOAD <…>`, `SERVICE <http://…>` nested in `OPTIONAL`, a comment hiding a keyword, a 1 MB query). Expected: `forbidden` or `validation_error` before any OM call; comments never change the outcome. → Task 7 `test_updates_forbidden`, `test_nested_service_forbidden`, `test_keyword_in_comment_or_literal_is_fine`, `test_query_length_cap`.

## File structure

```
src/lakeloom/rdf/untrusted.py           # parse_untrusted (size, JSON-LD contexts, XML DTD, triple cap)
src/lakeloom/rdf/semantic.py            # semantic_view
src/lakeloom/core/import_terms.py       # TermSet, ConceptTerm, AttributeTerm, AxiomTerm, extract_terms, detect_namespace
src/lakeloom/core/import_diff.py        # ImportMode, import_operations
src/lakeloom/core/imports.py            # ImportService: plan_file, confirm_file, plan_git, confirm_git
src/lakeloom/core/drift.py              # TableBinding, bind_tables, TableDrift, diff_scans, drift_operations
src/lakeloom/core/drift_service.py      # DriftService.detect
src/lakeloom/core/sparql.py             # QueryForm, check_query, SparqlService.query
src/lakeloom/core/proposals.py          # + propose_operations, Provenance.origin/source_document (modify)
src/lakeloom/core/operations.py         # Evidence.kind + "import" (modify)
src/lakeloom/gitstore/layout.py         # Lakeloom-Origin / Lakeloom-Import trailers (modify)
src/lakeloom/state/proposals.py         # applied_scan_ids (modify)
src/lakeloom/om/client.py               # glossary_sparql (modify)
src/lakeloom/server/tools_import.py     # import_model, import_from_git, detect_drift, sparql_query
src/lakeloom/server/http_api.py, prompts.py, server_info.py, http_app.py, cli.py, config.py   (modify)
deploy/compose/docker-compose.yml, lakeloom-sources.yaml, .github/workflows/ci.yml, .gitignore   (scratch lake)
docs/decisions/0016-import-drift-sparql.md
```

---

### Task 1: Live research on OM rc1 and ADR 0016 (proposed)

**Files:**
- Create `docs/decisions/0016-import-drift-sparql.md` (status proposed; Decisions above, plus a Research section).
- Create `tests/contract/test_om_import_sparql_probe.py`. It pins the rc1 behaviour the later tasks rely on, so an OM upgrade that changes it fails loudly. It is skipped when the stack is down, like the other contract tests.

**Interfaces:**
- **Consumes:** `tests/contract/stack.py` helpers (bot token, throwaway model create/delete, apply as bot for test setup, as in ADR 0011).
- **Produces:** recorded facts only. Tasks 3, 5 and 7 cite them.

- [ ] **Step 1: Probe on a throwaway paired model `llm3c_probe_<hex>`** (hard-deleted in teardown, never sealed). Create one concept with a display name different from its name, a synonym, a parent, an `exactMatch` mapping, one attribute with `enumValues` and `unit`, one `placedBy` relation, and the PK axiom; apply as the bot. Then record:
  - `test_glossary_export_vocabulary`: the predicates used for display name (`skos:prefLabel` = displayName or name?), synonyms (`skos:altLabel`?), parent (`skos:broader`? plus `rdfs:subClassOf`?), mapping (`skos:exactMatch`?), and a typed relation (`om:placedBy`?). Assert the exact predicates found.
  - `test_upsert_property_replaces_existing`: a second change set with `UPSERT_PROPERTY` on the same IRI, a new range and a new description, applied → `GET /v1/ontologyProperties` shows the new range only.
  - `test_glossary_sparql_select_and_formats`: `SELECT ?s WHERE { ?s a <http://www.w3.org/2004/02/skos/core#Concept> }` with `format=json` lists the concept. Record the response content type, whether native `owl:DatatypeProperty` triples are visible, the behaviour with `INSERT DATA {…}` (status and body), and `format=turtle` for a `CONSTRUCT`.
  - `test_imports_preview_shape` (research only): `POST /v1/ontologies/{id}/imports/preview` on a two-class Turtle returns `operations` with `UPSERT_CONCEPT` only. This documents why it is not used.
- [ ] **Step 2:** Write the findings into ADR 0016 § Research. If a finding contradicts a Decision (for example the prefLabel rule, or `UPSERT_PROPERTY` not replacing), amend the Decision in the ADR **and in this plan** before Task 3 starts, and tell the controller.
- [ ] **Step 3: Commit** `docs(adr): 0016 import, drift and SPARQL — OM rc1 research (FR-IO-02, FR-PUB-03)`.

---

### Task 2: Safe parsing and the semantic view

**Files:**
- Create `src/lakeloom/rdf/untrusted.py`, `src/lakeloom/rdf/semantic.py`, `tests/unit/rdf/test_untrusted.py`, `tests/unit/rdf/test_semantic.py`.
- Modify `src/lakeloom/config.py`: `import_max_bytes: int = 20971520` (≥ 1), `import_timeout_seconds: float = 60` (1–600), `import_max_triples: int = 200000` (≥ 1), `sparql_timeout_seconds: float = 30` (1–300), `sparql_max_result_bytes: int = 5242880` (≥ 1024), `sparql_max_query_chars: int = 20000` (≥ 100). Extend `tests/unit/test_config.py`.

**Interfaces:**
- **Consumes:** `parse_rdf`, `InputFormat` (`rdf/canonical.py`).
- **Produces:**
  - `def parse_untrusted(data: bytes, fmt: InputFormat, *, max_bytes: int, max_triples: int) -> Graph` (sync, no network; raises `LakeloomError("validation_error")`);
  - `async def parse_untrusted_async(data: bytes, fmt: InputFormat, *, max_bytes: int, max_triples: int, timeout: float) -> Graph` (size first, then `to_thread` under `wait_for`);
  - `BOOKKEEPING: frozenset[URIRef]` (the three predicates in Decisions);
  - `def semantic_view(graph: Graph, base_iri: str) -> Graph`.
- [ ] **Step 1: Tests:**
  - `test_size_checked_before_parse` (patched `parse_rdf` is never called for `max_bytes + 1` bytes);
  - `test_jsonld_remote_context_refused` (string context, list with a string, nested `@context` in a node, `@import`);
  - `test_jsonld_inline_context_ok`;
  - `test_rdfxml_doctype_refused` (`<!DOCTYPE`, `<!doctype`, `<!ENTITY`);
  - `test_parse_error_keeps_line_and_column`;
  - `test_triple_cap`;
  - `test_timeout_is_validation_error` (patched slow parse, `timeout=0.05`);
  - `test_no_network_during_parse` (a socket guard fixture that fails on `socket.connect`, over all four formats);
  - `test_semantic_view_drops_bookkeeping_and_foreign_subjects`;
  - `test_semantic_view_keeps_restriction_closure`;
  - `test_semantic_view_ignores_glossary_id` (two exports differing only in the glossary IRI → isomorphic views).
- [ ] **Step 2:** FAIL → implement → pass; ruff and pyright.
- [ ] **Step 3: Commit** `feat(rdf): safe parsing of untrusted RDF and the semantic view (FR-IO-04, FR-IO-05, NFR-SEC-13)`.

---

### Task 3: Model terms from a graph, and the import diff

**Files:**
- Create `src/lakeloom/core/import_terms.py`, `src/lakeloom/core/import_diff.py`, `tests/unit/core/test_import_terms.py`, `tests/unit/core/test_import_diff.py`.
- Test data: build graphs with `tests/unit/publish/_graphs.py`, plus one fixture `tests/fixtures/rdf/retail_m3b_1.1.0.ttl` (a copy of the M3b release Turtle shown in ADR 0016 § Research, with the glossary uuid kept).
- Modify `src/lakeloom/core/operations.py`: `Evidence.kind` gains `"import"`.

**Interfaces:**
- **Consumes:** `AddConcept … RemoveAxiom`, `Name` pattern, `op_key` (`core/operations.py`); `QUDT_UNIT` (`om/native_rdf.py`); the mapper's `_pascal(_words(...))`, made public as `mapper.pascal_name(text: str) -> str`.
- **Produces:**
  - Frozen dataclasses:
    - `ConceptTerm(name, display_name: str | None, description: str | None, parent: str | None, synonyms: tuple[str, ...], iri: str)`;
    - `AttributeTerm(concept, name, datatype, is_identifier, allowed_values: tuple[str, ...], unit: str | None, description: str | None, iri: str)`;
    - `RelationTerm(source, relation_type, target, description: str | None, iri: str)`;
    - `MappingTerm(concept, kind: MappingKind, target_iri: str)`;
    - `AxiomTerm(concept, name, expressions: tuple[tuple[str, str, int], ...])` (property, kind, cardinality);
  - `TermSet(concepts: dict[str, ConceptTerm], attributes: dict[tuple[str, str], AttributeTerm], relations: dict[tuple[str, str, str], RelationTerm], mappings: set[MappingTerm], axioms: dict[str, AxiomTerm], warnings: list[str])`;
  - `def detect_namespace(graph: Graph) -> str` (`validation_error` when no concepts);
  - `def extract_terms(graph: Graph, namespace: str) -> TermSet`;
  - `ImportMode = Literal["merge", "replace"]`;
  - `class ImportOps(BaseModel): operations: list[ProposalOperation]; counts: dict[str, int]; warnings: list[str]` (counts keyed by op name);
  - `def import_operations(current: TermSet, incoming: TermSet, *, mode: ImportMode, versions: Mapping[str, float], axiom_names: Mapping[str, list[str]], rule_id: Literal["R-IMPORT", "R-GIT-RESTORE"]) -> ImportOps`. Here `versions` maps a concept name to its OM term version and `axiom_names` maps a concept to its OM native axiom names; a needed entry that is missing raises `validation_error`.
- [ ] **Step 1: Tests (`test_import_terms.py`):**
  - `test_extract_m3b_release_fixture` (4 concepts, 21 attributes with the exact datatypes incl. `Order.order_date` → `xsd:anySimpleType`, 4 identifiers, 2 relations `Invoice.hasOrder`, `Order.hasCustomer`, 4 `…PrimaryKey` axioms, and no warnings);
  - `test_glossary_links_explained_by_object_property_are_ignored`;
  - `test_glossary_only_link_warns`;
  - `test_plain_skos_vocabulary` (no OWL: concepts, broader, altLabel, exactMatch);
  - `test_invalid_local_names_skipped_with_warning`;
  - `test_preflabel_choice_is_deterministic` (`@de`, `@en` and plain labels in shuffled insertion orders);
  - `test_foreign_subjects_counted`;
  - `test_unrepresentable_triples_counted_with_curies`;
  - `test_detect_namespace_majority_and_tie`;
  - `test_restrictions_become_one_axiom_sorted`;
  - `test_axiom_name_primary_key_vs_restrictions`.
- [ ] **Step 2: Tests (`test_import_diff.py`):**
  - `test_import_into_empty_model_adds_everything_in_order` (parents before children; attributes after concepts);
  - `test_import_of_current_export_is_empty` (extract the fixture twice → no operations);
  - `test_changed_description_is_update_with_base_version`;
  - `test_missing_version_fails_closed`;
  - `test_changed_attribute_is_upsert` (or remove + add, per the Task 1 result);
  - `test_merge_never_removes`;
  - `test_replace_removes_in_reverse_order`;
  - `test_replace_removes_axioms_by_om_name`;
  - `test_rule_id_and_import_evidence_on_every_op`;
  - `test_round_trip_through_to_om` (the fixture's operations pass `to_om_all` with a fake `OMContext` for an empty paired model: no exception, `UPSERT_CONCEPT` count 4, `UPSERT_PROPERTY` count 23);
  - `test_minted_back_graph_is_semantically_equal`. This is the unit-level round trip: the operations are turned into a graph with a small helper that applies `to_om_all` output to `native_rdf.build_graph` plus glossary triples, and `semantic_view` of it is isomorphic to `semantic_view` of the fixture, ignoring glossary bookkeeping.
- [ ] **Step 3:** FAIL → implement → pass.
- [ ] **Step 4: Commit** `feat(core): read model terms from RDF and diff them into proposal operations (FR-IO-02)`.

---

### Task 4: One proposal path, with origin and source document

**Files:**
- Modify `src/lakeloom/core/proposals.py`:
  - extract the creation half of `_from_scan` (from `split_parts` to the end) into `_create(trail, target, ops, *, provenance, matches, warnings, tables)`;
  - add the public `propose_operations`;
  - `Provenance.origin` and `source_document`, plus `SourceDocument`;
  - the provenance block writes both.
- Modify `src/lakeloom/gitstore/layout.py` (trailers in Decisions).
- Modify `src/lakeloom/state/proposals.py`: `async applied_scan_ids(model: str) -> list[str]`, the distinct `scan_id`s of `applied` proposals of the model, newest proposal first.
- Tests: `tests/unit/core/test_proposals.py` (extend), `tests/unit/gitstore/test_layout.py`, `tests/unit/state/test_proposals_repo.py` (or the existing state test file).

**Interfaces:**
- **Consumes:** `split_parts`, `to_om_all`, `_write_part`, `_save_or_undo`, `_require_pair`, `_require_propose`, `_audited`, `_lock`.
- **Produces:**
  - `class SourceDocument(BaseModel): format: str; sha256: str; bytes: int; git_ref: str | None = None; git_commit: str | None = None`;
  - `async def propose_operations(self, model: str, operations: Sequence[ProposalOperation], *, origin: Literal["import", "git", "drift"], principal: Caller, client: str | None, tool: str, scan_id: str | None = None, tables: Sequence[TableProvenance] = (), source_document: SourceDocument | None = None, matches: Sequence[ProposalMatch] = (), warnings: Sequence[str] = ()) -> Proposal`.

    It runs under `self._lock(f"model:{model}")`, is audited as `tool`, and raises `NothingToPropose` on an empty list. Operations are split with `split_parts(ops, [t.fqn for t in tables], max_ops)`, so import operations (no table) are packed in order.
  - `propose_from_scan` calls `_create`, and its behaviour is unchanged.
- [ ] **Step 1: Tests:**
  - `test_propose_from_scan_unchanged_after_refactor` (the existing suite stays green; one golden test compares the stored proposal JSON of a fixture scan before and after);
  - `test_propose_operations_creates_draft_with_origin`;
  - `test_propose_operations_splits_parts_in_order` (120 import ops, limit 50 → parts of 50, 50, 20, declaration order kept);
  - `test_propose_operations_requires_propose_group`;
  - `test_provenance_block_has_origin_and_document`;
  - `test_old_rows_default_origin_scan` (a stored payload without `origin` loads as `scan`);
  - `test_trailers_for_import_and_git_origin`;
  - `test_scan_commit_message_unchanged`;
  - `test_applied_scan_ids_newest_first_distinct`.
- [ ] **Step 2:** FAIL → implement → pass.
- [ ] **Step 3: Commit** `refactor(core): one proposal path for scans, imports and drift; origin in provenance and trailers (FR-PRO-03)`.

---

### Task 5: `import_model` and `import_from_git`

**Files:**
- Create `src/lakeloom/core/imports.py`, `src/lakeloom/server/tools_import.py` (import tools; Tasks 6 and 7 add theirs here).
- Modify `server/http_app.py` (AppDeps `import_service`), `server/mcp_app.py` (register), `server/server_info.py` (`features.import`), `cli.py` (`import`, `import-git`).
- Tests: `tests/unit/core/test_imports.py` (fake OM / `ModelService`, real `ConfirmTokens`, a bare local Git remote with one release tag as in the M3b tests), `tests/unit/server/test_tools_import.py`, `tests/unit/test_cli.py`, the MCP conformance test.

**Interfaces:**
- **Consumes:**
  - Tasks 2–4;
  - `ModelService.graph`, `version` and `get_model`;
  - `OMClient.get_term` and `list_native_members`;
  - `ReleaseReader.get` and `file`;
  - `WorkingCopy.fetch` and `show`, `repo_lock`;
  - `ConfirmTokens`.
- **Produces:**
  - `class ImportPlan(BaseModel): model: str; origin: Literal["import", "git"]; namespace: str; mode: ImportMode; counts: dict[str, int]; operations: list[ProposalOperation]` (first 100); `total_operations: int; warnings: list[str]; summary: str; source_document: SourceDocument; confirm_token: str; expires_at: int`;
  - `ImportService(settings, models, om, proposals, confirm, audit, policies, *, reader: ReleaseReader | None, wc: WorkingCopy | None, repo_key: str | None)`, with:
    - `async plan_file(principal, model, content: bytes, fmt: InputFormat, *, mode, base_iri: str | None) -> ImportPlan`;
    - `async confirm_file(principal, client, model, content, fmt, *, mode, base_iri, confirm_token) -> Proposal`;
    - `async plan_git(principal, model, *, version: str, from_model: str | None, mode) -> ImportPlan`;
    - `async confirm_git(principal, client, model, *, version, from_model, mode, confirm_token) -> Proposal`;
  - Tools:
    - `import_model(model, content: str, format: InputFormat, mode="merge", base_iri=None, confirm_token=None)`;
    - `import_from_git(model, version="latest", from_model=None, mode="merge", confirm_token=None)`.

    Both are `lakeloom:propose`, carry the destructive annotation and are audited. They return `ImportToolResult(preview: ImportPlan | None, proposal: Proposal | None)`; the text follows `_confirm_line`, and after confirm it says "draft <id>; submit it with submit_proposal".
- [ ] **Step 1: Tests:**
  - `test_dry_run_counts_and_token`;
  - `test_confirm_creates_draft_proposal_with_import_origin`;
  - `test_token_bound_to_content_and_model_version` (different content → `validation_error`; model changed → stale);
  - `test_size_limit_before_anything` (no OM call);
  - `test_pending_overlap_dropped`;
  - `test_nothing_to_import`;
  - `test_import_from_git_release_tag` (namespace from the tagged manifest; `git_ref` = tag, `git_commit` = the commit sha);
  - `test_import_from_git_latest_branch_tip`;
  - `test_import_from_git_other_slug_remints_iris`;
  - `test_import_from_git_requires_git_and_existing_model` (both hints);
  - `test_import_from_git_unknown_version_not_found`;
  - `test_tools_require_propose_scope_and_group`;
  - `test_tool_output_quotes_untrusted_labels`;
  - `test_import_is_audited_with_sha256`;
  - `test_cli_import_dry_run_then_yes`;
  - `test_server_info_import_feature`.
- [ ] **Step 2:** FAIL → implement → pass.
- [ ] **Step 3: Commit** `feat(core): import_model and import_from_git as reviewable proposals (FR-IO-02, FR-IO-05, FR-GIT-07)`.

---

### Task 6: Drift detection

**Files:**
- Create `src/lakeloom/core/drift.py`, `src/lakeloom/core/drift_service.py`, `tests/unit/core/test_drift.py`, `tests/unit/core/test_drift_service.py`.
- Modify `server/tools_import.py` (`detect_drift`), `server/prompts.py` (`drift_check`), `server/server_info.py` (`features.drift`), `cli.py` (`drift`).

**Interfaces:**
- **Consumes:**
  - `ScanRecord`, `ScannedTable`, `ScanService.scan`, `ScanRepository.get`;
  - `map_scan`, `policy_for`, `dedupe`;
  - `ProposalRepository.applied_scan_ids` and `get`;
  - `ModelService.graph`, `extract_terms` (Task 3; "the graph has the term");
  - `OMClient.get_term`, `propose_operations` (Task 4).
- **Produces:**
  - `TableBinding(concept: str, attributes: dict[str, str])` (frozen dataclass);
  - `def bind_tables(proposals: Sequence[Proposal]) -> dict[str, TableBinding]`;
  - `class TableDrift(BaseModel): fqn: str; status: Literal["unchanged", "changed", "added", "removed", "error"]; added_columns: list[str]; removed_columns: list[str]; changed_columns: list[str]; renames: list[tuple[str, str]]; error: str | None = None`;
  - `def diff_scans(baseline: Mapping[str, ScannedTable], current: Mapping[str, ScannedTable], errors: Mapping[str, str]) -> list[TableDrift]` (sorted by fqn; table renames carried as a `renames` entry on the added table);
  - `def drift_operations(drifts: Sequence[TableDrift], current_scan: ScanRecord, bindings: Mapping[str, TableBinding], model_terms: TermSet, policy: MappingPolicy, versions: Mapping[str, float]) -> tuple[list[ProposalOperation], list[ProposalOperation], list[str]]` (removal and rewritten-addition operations, the additions for brand-new tables that still need `dedupe`, and the warnings);
  - `class DriftReport(BaseModel): model: str; baseline_scan_ids: list[str]; scan_ids: list[str]; tables: list[TableDrift]; proposal: Proposal | None; warnings: list[str]`;
  - `DriftService(scans: ScanService, scan_repo: ScanRepository, proposals: ProposalService, proposal_repo: ProposalRepository, models: ModelService, om: OMClient, policies)`, with `async detect(principal, client, model, *, include_new_tables: bool = True) -> DriftReport`;
  - Tool `detect_drift(model, include_new_tables=True)` (`lakeloom:propose`, audited), returning `DriftReport`.
- [ ] **Step 1: Tests (`test_drift.py`, pure):**
  - `test_added_column_yields_add_attribute_on_bound_concept` (the mapper's name, `concept` rewritten);
  - `test_removed_column_yields_remove_attribute_only_if_in_model`;
  - `test_type_change_is_upsert`;
  - `test_comment_change_updates_description`;
  - `test_rename_candidate_same_type_and_position` (two removals and two additions with mixed types → only the 1:1 pair is a candidate);
  - `test_table_rename_candidate`;
  - `test_removed_table_removes_members_then_concept`;
  - `test_error_table_never_removed`;
  - `test_new_fk_column_adds_relation`;
  - `test_bind_tables_prefers_attribute_evidence`;
  - `test_unchanged_tables_produce_nothing`.
- [ ] **Step 2: Tests (`test_drift_service.py`, fakes):**
  - `test_no_applied_scan_is_validation_error`;
  - `test_baseline_is_newest_applied_scan_per_table`;
  - `test_one_scan_per_source_limited_to_baseline_schemas`;
  - `test_no_drift_returns_report_without_proposal` (a new scan record still exists);
  - `test_drift_creates_draft_with_drift_origin_and_new_versions`;
  - `test_new_tables_go_through_dedupe`;
  - `test_include_new_tables_false_only_reports`;
  - `test_detect_drift_tool_scope_and_audit`;
  - `test_drift_check_prompt_registered`.
- [ ] **Step 3:** FAIL → implement → pass.
- [ ] **Step 4: Commit** `feat(core): detect_drift proposes changes for added, removed and renamed columns and tables (FR-PRO-07, FR-AGT-04)`.

---

### Task 7: Read-only SPARQL proxy

**Files:**
- Create `src/lakeloom/core/sparql.py`, `tests/unit/core/test_sparql.py`.
- Modify:
  - `src/lakeloom/om/client.py`: `PATHS["glossary_sparql"] = ("POST", "/v1/glossaries/{id}/sparql")` and `glossary_sparql`, a streamed read with the cap;
  - `server/tools_import.py` (`sparql_query`);
  - `server/http_api.py` (`POST /v1/models/{slug}/sparql`);
  - `server/server_info.py` (`features.sparql`, probed once at startup and cached);
  - `cli.py` (`sparql`);
  - `docs/api/openapi.json` (regenerated).
- Tests: `tests/unit/om/test_client_semantic.py` (MockTransport), `tests/unit/server/test_http_api.py`, `test_tools_import.py`, `test_server_info.py`, `test_cli.py`, the OpenAPI up-to-date test.

**Interfaces:**
- **Consumes:** `ModelService.get_model` (glossary id), `Settings.sparql_*`, `UntrustedText` / quoting helpers, the `max_tokens` truncation helper of `tools_common`.
- **Produces:**
  - `QueryForm = Literal["select", "ask", "construct", "describe"]`;
  - `def check_query(text: str, *, max_chars: int) -> QueryForm` (raises `forbidden` or `validation_error`);
  - `ResultFormat = Literal["json", "xml", "csv", "tsv", "turtle", "jsonld", "ntriples", "rdfxml"]`;
  - `def pick_format(form: QueryForm, requested: ResultFormat | None) -> ResultFormat`;
  - `MEDIA: dict[ResultFormat, str]`, the media types in Decisions;
  - `async OMClient.glossary_sparql(glossary_id: str, query: str, fmt: ResultFormat, *, timeout: float, max_bytes: int) -> tuple[bytes, str]` (body, content type);
  - `SparqlService(settings, models, om)`, with `async query(model: str, text: str, fmt: ResultFormat | None) -> SparqlResult`, where `SparqlResult(BaseModel): form: QueryForm; format: ResultFormat; media_type: str; body: bytes` (body excluded from the JSON dump);
  - Tool `sparql_query(model, query, format=None, max_tokens=None)` (`lakeloom:read`, read-only annotation).
- [ ] **Step 1: Tests:**
  - `test_forms_detected` (select, ask, construct, describe, with prefixes and a `BASE`);
  - `test_updates_forbidden` (`INSERT DATA`, `DELETE WHERE`, `LOAD`, `CLEAR`, `DROP`, `CREATE`, `WITH … DELETE … INSERT`);
  - `test_nested_service_forbidden` (inside `OPTIONAL` and `UNION`);
  - `test_keyword_in_comment_or_literal_is_fine` (`# INSERT`, `"SERVICE"`);
  - `test_query_length_cap`;
  - `test_format_must_fit_form`;
  - `test_client_sends_format_and_timeout` (body JSON `{query, format, timeout}` and the bot token);
  - `test_client_result_cap_streams_and_fails`;
  - `test_client_4xx_validation_5xx_upstream`;
  - `test_http_sparql_bodies_and_accept` (three body types; Accept → format; 406 for `image/png`);
  - `test_http_sparql_requires_read_scope`;
  - `test_tool_json_truncated_with_flag`;
  - `test_tool_text_is_quoted_untrusted`;
  - `test_query_text_never_logged` (caplog holds the sha256 and the length only);
  - `test_server_info_sparql_degrades_on_404`;
  - `test_openapi_up_to_date`.
- [ ] **Step 2:** FAIL → implement → pass.
- [ ] **Step 3: Commit** `feat(server): read-only SPARQL over a model, proxied to OpenMetadata (FR-PUB-03, FR-OM-01)`.

---

### Task 8: M3c acceptance (live) and wrap-up

**Files:**
- Create `tests/contract/test_m3c_acceptance.py`.
- Modify `deploy/compose/docker-compose.yml` (lakeloom volume `../../.scratch-lake:/scratch-lake:ro`), `deploy/compose/lakeloom-sources.yaml`, `.github/workflows/ci.yml` (`mkdir -p .scratch-lake` before `compose up`), `.gitignore` (`.scratch-lake/`).
  - The new source in `lakeloom-sources.yaml` is `fixture_scratch` (type `delta`, `om_service` unset, table `{uri: "file:///scratch-lake/orders_drift", catalog: retail, schema: drift}`).
  - A comment in the sources file says the directory is written only by the M3c contract test.
- Modify ADR 0016 (accepted; Consequences), README (import, restore, drift, SPARQL), CHANGELOG (repository), `docs/backlog/m3b-deferred.md` (triage), `REQUIREMENTS.md` §10 (the six new settings, in the table).
- Create `docs/backlog/m3c-deferred.md`.

- [ ] **Step 1: `test_m3c_acceptance.py`** runs through MCP and HTTP against the running stack (Git mode `direct`). Teardown hard-deletes the glossaries and ontologies, deletes test tags through the Gitea API and empties `.scratch-lake/`; it never seals.
  - `test_m3c_import_of_release_round_trips`:
    - setup as in M3b on `retail_m3c`: agent scan of `fixture_delta`, `create_model`, `propose_from_scan`, submit, alice approves, publication `done`, `create_release` `1.0.0` confirmed;
    - hard-delete the OM glossary and ontology `retail_m3c`, then `create_model retail_m3c` again (same default base IRI). This is the "fresh OM" for the model: a new glossary id, no terms;
    - `import_from_git(retail_m3c, version="1.0.0")`: the dry run counts 4 concepts, 21 attributes, 2 relations and 4 axioms; confirm → draft → submit → alice approves → publication `done`;
    - `export_model(retail_m3c, turtle)`, then `diff_graphs(semantic_view(tagged ontology.ttl), semantic_view(export))` → `bump is None`, so there is no semantic diff;
    - the publication commit carries `Lakeloom-Origin: git` and `Lakeloom-Import: retail_m3c/v1.0.0@<sha>`.
  - `test_m3c_import_model_file_dry_run`: `import_model(retail_m3c, <the same Turtle>, "turtle")` → `NothingToPropose` "already matches"; a JSON-LD with a remote `@context` → `validation_error`.
  - `test_m3c_drift_after_fixture_column_added`:
    - copy `tests/fixtures/lake/orders` to `.scratch-lake/orders_drift`;
    - scan `fixture_scratch`, `create_model retail_m3c_drift`, propose, submit, alice approves;
    - append one row with a new column `channel: string` with `deltalake.write_deltalake(..., mode="append", schema_mode="merge")`;
    - `detect_drift(retail_m3c_drift)` → `orders_drift` is `changed` with `added_columns == ["channel"]`, and the proposal has exactly one `add_attribute` named `channel` on the bound concept, `origin == "drift"`, and a provenance table version one above the baseline.
  - `test_m3c_sparql_select_and_update_refused`: `POST /v1/models/retail_m3c/sparql` with a SELECT over `skos:Concept` → 200 `application/sparql-results+json` listing the 4 concepts; an `INSERT DATA` → 403; the MCP `sparql_query` gives the same rows.
- [ ] **Step 2:** `mkdir -p .scratch-lake`, rebuild Lakeloom (`docker compose -f deploy/compose/docker-compose.yml up -d --build --wait lakeloom`), then run the full unit and contract suites green. Never `down -v`.
- [ ] **Step 3: Docs:** ADR 0016 accepted; README; CHANGELOG; REQUIREMENTS §10. Triage `m3b-deferred.md` and write `m3c-deferred.md` (at least: CSV and LinkML import (FR-IO-03); glossary-only links on import; attribute display labels; a rename operation that keeps provenance; drift for Unity Catalog and MCP sources against live remotes; parse worker that can be killed on timeout; SPARQL over released versions; the reconciliation job FR-GIT-08).
- [ ] **Step 4: Commit** `test(contract): M3c acceptance — release import round-trips, drift after an added column (FR-IO-02, FR-GIT-07, FR-PRO-07, FR-PUB-03)`.

---

## Out of scope for M3c

- CSV and LinkML import (FR-IO-03); OM's `importRdf` and `imports/preview` as write paths.
- Importing terms from other namespaces (`owl:imports` of other ontologies), glossary-only links and attribute display labels.
- Creating the target model inside `import_from_git` (it stays an explicit `create_model`).
- The reconciliation job (FR-GIT-08) and drift on a schedule (M4).
- SPARQL over released versions and a local SPARQL engine.
