# What you get at each stage

You do not have to finish the whole pipeline before Lakeloom is useful. Every stage
produces a deliverable that people and ordinary tools can use right away: a spreadsheet, a
document, a diagram or a search configuration. No RDF, triple store or Git is needed.

> **Ask the assistant: "What can I already hand out for the retail model?"** It calls
> `get_next_steps`, whose answer lists every deliverable as **ready**, **partial** (usable,
> with visible gaps) or **unavailable** (with what is missing).

## The deliverables

| Stage | Deliverable | Formats | Who uses it | Tool |
|-------|-------------|---------|-------------|------|
| 1. Vocabulary | **Business glossary**: term, definition, synonyms, scope notes, deprecated terms and their replacement | Markdown, CSV | Business teams, onboarding, wikis, catalog imports | `export_glossary` |
| 1. Vocabulary | **Quality score**: share of terms defined, with synonyms, with scope notes; a sample of undefined terms | Included with the glossary | Curators | `export_glossary` |
| 2. Metadata profile | **Data dictionary** of the scanned tables: column, type, business term, definition; unmapped columns marked | Markdown, CSV | Analysts, engineers, audits and data-protection records | `export_data_dictionary` |
| 3. Taxonomy | **Taxonomy tree**, from broad to narrow | Markdown outline, Mermaid diagram, CSV (depth, term, parent) | Portals, navigation menus, BI hierarchies, presentations | `export_taxonomy` |
| 4. Thesaurus | **Search synonyms**: each term with its synonyms; deprecated term ⇒ its replacement | Solr / Elasticsearch / OpenSearch synonym file, CSV | Enterprise search, BI semantic layers | `export_synonyms` |
| 4. Thesaurus | **Query expansion**: a question or search text in, the terms it names plus synonyms, narrower and related terms out; deprecated terms redirected | MCP tool result | Search, RAG retrievers, agents | `expand_query` |
| 4. Thesaurus | **Deprecation map**: old term → replacement, with its history | CSV | Migrating reports and dashboards | `export_synonyms(format="deprecations")` |
| 5. Ontology | SHACL shapes, JSON-LD context, RDF exports | Turtle, JSON-LD, RDF/XML, N-Triples, SKOS | Data engineering, validation, semantic apps | `export_model`, resources |
| 6. Knowledge graph | Releases, triple-store graphs, SPARQL | Git tags, named graphs | Applications, analytics | `create_release`, `sparql_query` |

All deliverables show the **effective model**: approved scope notes, history notes and
deprecations are included even though OpenMetadata does not store them.

## Typical early wins

- **Day one, a vocabulary of 15 terms.** Export the glossary as Markdown and paste it into the
  team wiki. The undefined terms are marked, which makes it easy to ask owners to fill them.
- **After the first scan.** Export the data dictionary as CSV for the analysts. Unmapped
  columns tell you which terms the vocabulary still lacks.
- **Once terms are grouped.** Export the taxonomy as Mermaid for a slide, or as CSV for a
  BI hierarchy.
- **Once synonyms exist.** Load `synonyms.txt` into Elasticsearch or OpenSearch: searching
  for "client" now finds "customer".

## Getting the files

| Surface | How |
|---------|-----|
| MCP tool | `export_glossary(model, format)`, `export_data_dictionary(model, format, scan_id?)`, `export_taxonomy(model, format)`, `export_synonyms(model, format)` |
| MCP resource | `lakeloom://models/{slug}/deliverables/{filename}` |
| HTTP | `GET /v1/models/{slug}/deliverables/{filename}` (with `ETag`, so tools can poll cheaply) |

File names are `glossary.md`, `glossary.csv`, `data-dictionary.md`, `data-dictionary.csv`,
`taxonomy.md`, `taxonomy.mmd`, `taxonomy.csv`, `synonyms.txt`, `synonyms.csv` and
`deprecations.csv`.

`version=latest` (the default) renders the current model. `version=1.2.0` reads the copy
stored in that release.

Every release made with `create_release` stores the glossary, taxonomy, synonym and
deprecation files under `ontologies/<slug>/deliverables/` in Git. So each version of the
vocabulary is published with its release, even if the model has not reached the later
stages. The data dictionary depends on a scan, not on a release, so it is always rendered
live. Releases made before this feature have no deliverables.

## Safety

Deliverables are read-only and need only the `lakeloom:read` scope. Text that comes from
OpenMetadata or the lake is folded to one line and escaped in Markdown, so a definition
cannot inject headings, table rows or links. CSV files keep the exact text, quoted per
RFC 4180.
