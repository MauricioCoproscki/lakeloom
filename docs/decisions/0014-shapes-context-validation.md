# 0014. SHACL shapes, JSON-LD context and model validation

- Status: proposed
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
  shared by all its domains: `sh:path`, `sh:datatype` (a single `xsd:` range), `sh:class`
  (relations), `sh:in` from `owl:oneOf`, `sh:minCount 1` / `sh:maxCount 1` for functional
  properties, `sh:name`. Restrictions set `sh:minCount` / `sh:maxCount` (creating the shape when
  missing); the stricter count wins. Shapes are open (no `sh:closed`) and units are not
  constrained. Output is `rdf/canonical.serialize` turtle through `shapes_turtle`, which HTTP and
  Git both call, so their bytes match.
- **JSON-LD context.** `{"@context": {...}}` with sorted keys: `@version` 1.1, the minimal context
  (`@vocab`, model prefixes, `dcterms`, `skos`), `xsd`, one term per class and per property (key =
  IRI minus the base IRI), typed by datatype or `@id`, plus bare-name aliases only when unique. A
  later entry never overrides an earlier key.
- **Validation.** `pyshacl` is a dev-only dependency: tests prove the shapes validate instance
  data. No new runtime dependency.

## Consequences

- A cardinality restriction on one domain of a shared property shape applies to every domain of
  that property, since the shape is shared (accepted for the first version).
- A property with several distinct `xsd:` ranges gets no `sh:datatype` (the conjunction would be
  unsatisfiable).
- Properties whose domain is not a model class produce no shape.
