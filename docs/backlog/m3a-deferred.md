# M3a deferred follow-ups

Findings from the M3a work on SHACL shapes, the JSON-LD context and `validate_model`,
deliberately deferred. Triage when planning M3b. Decisions and known limits:
[ADR 0014](../decisions/0014-shapes-context-validation.md).

## Out of scope for M3a
- Releases, tags, the repository `CHANGELOG.md`, `?version=` beyond `latest`, Git `Link`
  headers and the triple-store sink (M3b).
- Import, `import_from_git`, drift detection and the SPARQL proxy (M3c).
- Validating lake data from Lakeloom itself (a `validate_data` tool): shapes are published for
  consumers to run.
- Units as SHACL constraints, and closed shapes.

## Shapes
- Keep every `owl:oneOf` member: non-literal members (IRIs) are dropped today, so the `sh:in`
  list accepts less than the model allows.
- Emit `sh:class` only for ranges that are model classes; `owl:Thing` or `skos:Concept` ranges
  should give no constraint (or `sh:nodeKind sh:IRI`).
- Honour `owl:onClass` / `owl:onDataRange` on qualified cardinalities (today they count every
  value, which can be stricter than the model).
- Make the `owl:oneOf` choice fully deterministic when two lists share lexical values but differ
  in datatype or language.
- Make the vocabulary denylist (`VOCABULARY_NAMESPACES`: SKOS, OWL, RDF, RDFS, XSD,
  OpenMetadata's ontology namespace) configurable per model, for models that import other
  vocabularies.
- Revisit the IRI scheme of class-specific property shapes, `<C>/shape/<percent-encoded P IRI>`
  (introduced instead of one shared multi-domain shape so one class's cardinality restriction
  does not leak onto other domains): long, opaque IRIs that change if the scheme changes.
- Give `date` attributes a datatype constraint: their OpenMetadata range is `rdfs:Literal`, so
  `shapes.ttl` cannot reject a malformed date. A side channel for the precise datatype (kept in
  the proposal) would let the shapes carry `xsd:date`.

## JSON-LD context
- Reject or re-map term keys containing `:` (strict JSON-LD processors read them as compact
  IRIs) and decide on percent-encoded local names (`Cust%20Name` is kept encoded).
- Count every model property, not only those that received a term, when deciding whether a
  short alias is unique.
- Fall back to the minimal context in asset JSON-LD if building the full context fails (still
  open: the pruned context is built from the full one, so a failed build still fails the asset).

## validate_model
- Do not report a concept as isolated when its only links are external parents or restriction
  blank nodes.
- Drop the redundant loop over `(SKOS.related,)` in the isolation check.
- Quote untrusted label text in the structured `message` too (today only the Markdown escapes
  it).

## Serving, caching and Git
- Retrieve the exception of a shared build whose awaiters were all cancelled (no "Task exception
  was never retrieved" warning).
- Read a Git snapshot's five exports and the model version from one graph state.

## Tests
- Cover: a failed build is not cached, a cancelled awaiter, the tool and CLI JSON-LD paths, a
  snapshot with only some optional exports, `validate_model` with hostile labels, self-loops,
  deep chains and dual-typed concepts, and language-tagged or typed `owl:oneOf` values.
- Make the large-model timing test independent of `build_shapes` (it may flake on slow CI), and
  move the CLI cases into `test_cli`.
- Extend `test_artifacts` to `shapes_artifact` (media types, ETag, Link) instead of covering it
  only through the HTTP and resource tests.
- Simplify the `Customer.misc` context test, and make the context determinism property test
  generate non-ASCII names too.
