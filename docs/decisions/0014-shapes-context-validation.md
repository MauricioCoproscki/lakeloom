# 0014. SHACL shapes, JSON-LD context and model validation

- Status: accepted
- Date: 2026-11-14
- Requirements: FR-PUB-01, FR-PUB-02, FR-PUB-04, FR-PUB-05, FR-PUB-07, FR-CTX-02, FR-GIT-02, FR-GIT-03

## Context

M3a publishes `shapes.ttl`, `context.jsonld` and a `validate_model` report per model. The plan is
`docs/superpowers/plans/2026-11-14-m3a-shapes-and-context.md`. All three are pure functions of
`ModelService.graph(slug)`, the merged glossary SKOS and native OWL graph used by the exports.

## Decision

- **Input graph.** Classes are `owl:Class` and `skos:Concept` subjects (blank nodes excluded).
  Attributes are `owl:DatatypeProperty` (range, `owl:oneOf`, `qudt:unit`; identifiers are also
  `owl:FunctionalProperty`), relations `owl:ObjectProperty`. Cardinality comes from
  `rdfs:subClassOf` `owl:Restriction`s.
- **SHACL.** One `sh:NodeShape` `<C>/shape` per class (`sh:targetClass`, `sh:name`: `skos:prefLabel`
  first, then `rdfs:label`, the sorted first). One `sh:PropertyShape` `<P>/shape` per property,
  shared by the domains without a cardinality restriction on it: `sh:path`, `sh:datatype` (a single `xsd:` range), `sh:class`
  (relations), `sh:in` from `owl:oneOf`, `sh:minCount 1` / `sh:maxCount 1` for functional
  properties, `sh:name`. A class `C` with restrictions on `P` gets its own shape
  `<C>/shape/<percent-encoded P>` (path, copy of the base constraints, counts merged with the
  functional 1..1, the stricter wins) linked instead of the shared one, so a restriction never
  leaks to other domains; it is created even when `C` is not a domain of `P`. Counts that are
  not non-negative integers are ignored. Shapes are open (no `sh:closed`) and units are not
  constrained. Output is `rdf/canonical.serialize` turtle through `shapes_turtle`, which HTTP and
  Git both call, so their bytes match.
- **JSON-LD context.** `{"@context": {...}}` with sorted keys: `@version` 1.1, the minimal context
  (`@vocab`, model prefixes, `dcterms`, `skos`), `xsd`, one term per class and per property (key =
  IRI minus the base IRI), typed by datatype or `@id`, plus bare-name aliases only when unique. A
  later entry never overrides an earlier key.
- **Validation.** `pyshacl` is a dev-only dependency: tests prove the shapes validate instance
  data. No new runtime dependency.

## Consequences

- A malformed `owl:oneOf` list (cycle, gap, non-literal) is skipped; among several well-formed
  lists the one whose sorted values sort first is used, so the output is deterministic.
- A property with several distinct `xsd:` ranges gets no `sh:datatype` (the conjunction would be
  unsatisfiable).
- Properties whose domain is not a model class produce no shape.
- `shapes.ttl` and `context.jsonld` are produced by the same functions for HTTP, MCP resources
  and Git (`shapes_turtle`, `build_context`), so the committed files equal the served bytes
  (checked live by the M3a acceptance test).
- The shapes are a contract for consumers: Lakeloom publishes them and does not validate lake
  data itself. The M3a acceptance test builds instance data from the four fixture Delta tables
  (one node per row, columns mapped through the mapper's attribute naming to the model's
  `rdfs:label`s), and the published shapes accept it with `pyshacl`; one broken `orders` row
  (a non-numeric `total_amount`) fails on exactly that focus node.
- The context gives property terms the key `<Class>.<attribute>` (for example
  `Customer.customer_id`); a short alias (`email`) exists only when the attribute name is unique
  in the model, so a JSON record of a table whose columns repeat elsewhere uses the qualified
  keys.

### Known limits

- Units (`qudt:unit`) are not constraints, and shapes are open; a closed-shape option is a
  follow-up.
- Some modelling forms are simplified: non-literal `owl:oneOf` members are dropped (the `sh:in`
  list is narrower), a qualified cardinality ignores `owl:onClass` / `owl:onDataRange`, and a
  relation whose range is not a model class (`owl:Thing`, `skos:Concept`) still becomes
  `sh:class`. When two `owl:oneOf` lists have the same lexical values with different datatypes
  or languages, the one used depends on iteration order.
- An attribute whose datatype has no OWL 2 equivalent (such as `xsd:date`) is stored with the
  range `rdfs:Literal` (M2b), so its shape has no `sh:datatype` and `validate_model` reports it
  as `untyped_attribute`.
- The merged model graph types the OpenMetadata vocabulary classes (`skos:Concept`,
  `skos:ConceptScheme`, `om:Glossary`, `om:GlossaryTerm`) as classes, so every model gets node
  shapes for them and `validate_model` warns that they are isolated and undefined.
- Context keys that contain `:` or percent-encoded characters are kept as they are; strict
  JSON-LD processors may treat a `:` key as a compact IRI. Alias uniqueness counts only the
  properties that received a term.
- `validate_model` reports a concept linked only to external parents or restriction blank
  nodes as isolated, and its messages carry the untrusted label text in the structured output
  (the Markdown quotes it).
- Shapes turtle is re-serialised per request, and an invalidation during a build can pair a new
  graph with the previous shapes counts in one report. A Git snapshot reads its five exports and
  the model version in separate calls, so a model change in between can mix states.
