# M3c deferred follow-ups

Findings from the M3c work on import, restore from Git, drift and the SPARQL proxy,
deliberately deferred. Triage when planning M4. Decisions, live research and known limits:
[ADR 0016](../decisions/0016-import-drift-sparql.md).

## Out of scope for M3c
- CSV and LinkML import (FR-IO-03). OpenMetadata's `importRdf` and `imports/preview` stay
  unused as write paths.
- Importing terms from other namespaces (`owl:imports` of other ontologies).
- Glossary-only links on import: links between two concepts that no object property backs
  are counted in a warning, not imported.
- Attribute display labels: an attribute's `rdfs:label` is not imported, because attributes
  have no display name.
- A rename operation that keeps provenance. Renames are proposed as a remove plus an add,
  because the IRI is derived from the name.
- Creating the target model inside `import_from_git`. It stays an explicit `create_model`.
- Drift for Unity Catalog and MCP sources against live remotes. Drift is checked live only on
  a Delta source (`fixture_scratch`).
- A parse worker that can be killed on timeout. The import timeout stops waiting, but the
  thread runs on to the end.
- SPARQL over released versions, and a local SPARQL engine. Today the proxy queries the live
  glossary only.
- The reconciliation job (FR-GIT-08) and drift on a schedule (M4).

## Import
- `skos:*Match` targets are not normalised the way `AnyUrl` normalises them. So a merge
  import proposes the same mapping again on every run, and replace mode removes it.
  (Final review should weigh this.)
- An `UpdateConcept` is dropped entirely when its new parent was dropped, not just the parent
  change. (Final review should weigh this.)
- Replace mode can strand the children of a removed concept.
- Replace mode queues axiom removals before it checks for dependent drops.
- Imported axioms get their name as display name and description: rc1 refuses a new axiom
  without them, and the RDF carries neither. An upsert of an existing axiom therefore replaces
  the mapper's display name and description (such as "Customer primary key") with the name.
- A targeted upsert sends Lakeloom's `Concept.attr` name, not OpenMetadata's own name. This
  is not probed for properties created outside Lakeloom.
- `AddRelation` on an existing object property is not targeted, so it still ends
  `APPLY_FAILED`.
- The confirm step parses the document and calls OpenMetadata before it verifies the token
  (the ruled order). A cheap token pre-check would avoid that work on a bad token.
- `lakeloom import` reads the whole file before the size check. Check the size with `stat`
  first.
- `_require_changes` patches a `NothingToPropose` that was already built. Build the right
  error up front.
- `imports.py` and `tools_import.py` import private renderers from `tools_proposals`
  (`_op_line`, `_evidence_lines`, `_confirm_line`, `_result`, `_scope`). Make them public
  helpers.
- Provenance `origin` has an unused value `agent`, and realization proposals get
  `origin="scan"`.

## Drift
- A proposal that spans several sources records a single scan id in its provenance.
  (Final review should weigh this.)
- An added column has no "the graph already has it" guard. A drift run after the attribute
  exists proposes a no-op upsert again. (Final review should weigh this.)
- Deduplication and the pending-operations filter run outside the model lock, for drift and
  for import. (Final review should weigh this.)
- `applied_scan_ids` orders scans by proposal id, not by scan age.
- A table dropped from a one-table schema is reported as removed on every run.
- A schema filter that matches nothing turns every baseline table into a removal, without a
  loud warning.
- Drift without operations, such as a comment-only change, never reaches provenance. So it is
  reported again on every run. At least say so in the tool description.
- A table moved to a schema outside the baseline still shows as removed (dedupe may catch
  the add). A move is missed when the concept was renamed in the model.
- The CLI `lakeloom drift` writes no audit row when `_plan` fails with an error that is not a
  `LakeloomError`.
- `propose_from_scan` fails when a scanned table declares a foreign key to a table that is
  neither in the scan nor in the model. The mapper warns "FK target … not in scan" and then
  proposes a relation to a concept that does not exist (live: the fixture `orders` table
  alone). The M3c acceptance test strips the FK from its scratch copy. Skip the relation, or
  defer it until the target exists.

## SPARQL
- Without `max_tokens`, the tool returns the result twice (text and structured content),
  about twice the payload.
- OpenMetadata 413, 422 and 429 all become `validation_error`. A 429 is better reported as
  `upstream_unavailable`.
- `httpx.DecodingError` escapes `glossary_sparql` untyped.
- The HTTP route reads the whole request body before the size check.
- `lakeloom sparql` writes OpenMetadata's bytes raw to the terminal, escape sequences
  included.

## Tests
- Probe tests depend on file order. The t4 lock omits `leaseSeconds`.
- The probe's tombstone re-create check uses the output of `to_om_all` instead of a
  hand-built payload.
- No UTF-16 RDF/XML test shows that the UTF-8 decode in `parse_rdf` closes the encoding gap
  of the DOCTYPE search.
- The non-JSON-LD wording of "RDF nesting is too deep" is untested.
- The parent round trip is untested against a fixture: the fixture has no `skos:broader`.
- The token-binding tests miss the `base_iri`, `version` and `from_model` cases.
- Drift test gaps: detect, apply, then detect again; an ambiguous table rename; a schema move.
- The SERVICE-after-PREFIX test accepts either error code.
- A provenance test imports `SHOP_STEWARDS` and `Human` from `test_proposals_rebase`
  (cross-file coupling).
- The pre-M3c contract tests take raw token fixtures as arguments, so pytest prints a token
  in the failure frame (seen in `test_m1a_acceptance.py`). Wrap them as M3b and M3c do.
- Live drift checks one added column only. Removed and type-changed columns, and added and
  removed tables, are unit-tested only.
