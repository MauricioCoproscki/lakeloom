# Ontology Pipeline Guide: Adapting Lakeloom

Based on Jessica Talisman, *Ontology Pipeline: A Framework for Building Knowledge
Infrastructures* (Technics Publications, 2026; Amazon ASIN B0HJ8QWJ5V) [[1]](#references),
this guide maps the book's 6-stage SKMS framework onto the lakeloom architecture and provides
concrete instructions for extending lakeloom into a full ontology-creation pipeline.

> *The Ontology Pipeline®* is a registered trademark of Jessica Talisman. Lakeloom is an
> independent project, not affiliated with or endorsed by the author or the publisher. The
> summaries below are Lakeloom's own reading of the method. The short quotations are marked
> and attributed: verify them against the published edition before citing them elsewhere.

---

## 1. The Framework

The **Ontology Pipeline** (based on ANSI Z39.19) is a six-stage iterative process for building
a **Semantic Knowledge Management System (SKMS)**: an organized structure of concepts,
definitions, and relationships that lets agents and humans progress from raw data to actionable
knowledge.

```
Data ──► Information ──► Knowledge
         (Data in     (Information you
          context)     can act on)
```

The six stages build on each other; you should not skip ahead without completing earlier stages:

```
Stage 1          Stage 2          Stage 3
Controlled  ──►  Metadata    ──►  Taxonomy
Vocabulary       Schema/MAP

Stage 4          Stage 5          Stage 6
Thesaurus   ──►  Ontology    ──►  Knowledge
                                  Graph
```

Each stage **prepares the material** for the next. Skipping stage 1 means your taxonomy will
be built on ambiguous, uncontrolled terms that break downstream reasoning.

---

## 2. Lakeloom's Current Position

Status after the pipeline audit (2026-11-19). Sections 3–8 below remain the design
reference derived from the book; this table says what is implemented.

| Stage | Implemented in lakeloom | Still open |
|-------|-------------------------|------------|
| 1 – Controlled Vocabulary | `harvest_vocabulary_candidates` (OM classification tags or given strings), `propose_add_concepts` (warns on missing definitions), `import_model`, dedupe and pending checks | NLP concept mining; per-term provenance/status model |
| 2 – Metadata Schema / MAP | `build_map`, `validate_map`, `export_map`: the **implementation layer** (scanned column → concept), computed on demand | Canonical layer (elements, obligations), crosswalks, persisted and versioned MAP documents |
| 3 – Taxonomy | `propose_add_hierarchy`, `propose_add_relation(relation="broader")`, `assess_taxonomy_coverage`, hierarchy signals in `get_next_steps` | Persisted coverage model; polyhierarchy (OM terms have one parent) |
| 4 – Thesaurus | `validate_thesaurus` (ANSI Z39.19 checks), `suggest_related_terms` (asset co-occurrence), `propose_add_relation` (RT as OM `relatedTo`, symmetric), `propose_add_scope_note`, `propose_add_history_note`, `propose_deprecate_concept` (`owl:deprecated` + `dct:isReplacedBy`) | Foreign-key and embedding-based RT mining |
| 5 – Ontology | OWL/SKOS via OpenMetadata + rdflib; `propose_change`, `validate_model`, SHACL shapes, `export_model` (all formats; `skos` carries the governance annotations) | Reasoner integration |
| 6 – Knowledge Graph | Releases in Git, triple-store sink (Fuseki, GraphDB, RDF4J) with SKOS named graphs, `suggest_external_mappings` / `propose_external_mappings` (Wikidata, schema.org) | SPARQL query templates as MCP tools |

Governance annotations (scope notes, history notes, deprecations) are lakeloom-native: they
live in the proposal log and every read path (`explain_concept`, `find_concepts`,
`export_model`, analytics, `get_next_steps`) shows the **effective graph**, i.e. the
OpenMetadata graph plus those annotations. `get_next_steps` reads the model's state and
recommends the next step; the `build_ontology` prompt walks a non-expert through the stages.

---

## 3. Stage 1 – Controlled Vocabulary

### What it is

A **controlled vocabulary (CV)** is a curated, finite list of preferred terms where every
concept has exactly one preferred label (`skos:prefLabel`) and zero or more synonym labels
(`skos:altLabel`). It is the foundation for every downstream stage.

A CV is **not** a taxonomy (no hierarchy), **not** a thesaurus (no associative relationships),
and **not** a free-text tag cloud (folksonomies are inputs, not outputs).

### Construction lifecycle

```
Define Scope ──► Identify Stakeholders ──► Develop ──► Review ──► Approve
     ▲                                                                 │
     └─────────────── Feedback ◄── Monitor ◄── Maintain ◄── Implement ┘
```

### Concept provenance model (add to `core/models.py`)

Each candidate concept should track:

| Field | Description |
|-------|-------------|
| `candidate_term` | The raw term as extracted |
| `source_system` | Where it was found (table name, catalog, wiki) |
| `extraction_method` | `manual`, `nlp_noun_phrase`, `nlp_named_entity`, `folksonomy`, `external_standard` |
| `extracted_at` | ISO 8601 timestamp |
| `authority` | Who validated it (team, standard body) |
| `definition_source` | URL or reference for the definition |
| `preferred_term` | The normalized, disambiguated form |
| `status` | `candidate` → `review` → `approved` → `deprecated` |

### Discovery sources to scan (extend `core/sources.py`)

Lakeloom should harvest candidate terms from:

1. **Table and column names** in the lake (already in `scans.py`) – parse snake_case/camelCase
2. **Column descriptions and table comments** – run NLP noun-phrase extraction
3. **OpenMetadata tags and classifications** – folksonomy harvest (`sources/om_tags.py`)
4. **Data catalog reference tables** – dimension tables, lookup tables, enum columns
5. **Code repositories** (GitHub/GitLab MCP sources) – domain-specific identifiers in SQL, dbt models
6. **Internal wikis** – Confluence/Notion pages via MCP sources
7. **External standards** – SNOMED CT, NIST-NICE, schema.org, industry taxonomies

### NLP integration (new: `core/concept_mining.py`)

```python
# Recommended libraries
# spaCy: industrial-strength noun-phrase and named-entity extraction
#   pip install spacy && python -m spacy download en_core_web_sm
# NLTK: lightweight alternative, good for corpus frequency analysis


def extract_candidate_terms(text: str, method: str = "spacy") -> list[CandidateTerm]:
    """Extract noun phrases and named entities from free text."""
    ...


def harvest_folksonomies(tag_sources: list[TagSource]) -> list[CandidateTerm]:
    """Collect organic tag patterns from OM classifications, column tags, etc."""
    ...
```

### SKOS context clues (use in definitions)

Definitions should include context clues that help LLMs and reasoners:

- **Synonyms**: "also known as" / `skos:altLabel`
- **Antonyms**: "contrast with" in `skos:scopeNote`
- **Explanations**: the definition body — write for humans AND machines
- **Examples**: concrete instances in `skos:example`
- **Scope notes**: domain/context constraints in `skos:scopeNote`
- **Hidden labels**: alternate spellings, typos, deprecated forms in `skos:hiddenLabel`

### Duplicate resolution

Before promoting a candidate to approved:

1. **Exact match**: normalize (lowercase, strip punctuation) and check against existing preferred labels
2. **Fuzzy match**: edit-distance ≥ 0.85 similarity → flag for human review
3. **Semantic match**: embedding cosine similarity > 0.9 on existing definitions → merge candidate
4. **Homonym handling**: if the same string means different things in different contexts, add a
   parenthetical qualifier: `Customer (Retail)` vs `Customer (B2B)`

### Existing lakeloom hooks

- `core/dedupe.py` – extend to use fuzzy + semantic matching
- `core/import_terms.py` – extend to accept provenance metadata
- `core/pending.py` – existing approval workflow; extend status transitions
- SKOS output: `rdf/canonical.py` already serializes to Turtle/JSON-LD

---

## 4. Stage 2 – Metadata Schema / MAP

### What it is

A **Metadata Application Profile (MAP)** is a declaration of the metadata terms an organization
uses, how it uses them, and the constraints that apply in specific contexts. It is the
"Rosetta Stone" between different metadata systems.

A MAP has three layers:

1. **Canonical layer** – the core semantic model; a reference standard, not an operational straitjacket.
   Defines what a "Customer" or "Product" means organizationally.
2. **Implementation layer** – how each system (CRM, data warehouse, BI tool) implements canonical
   concepts, including system-specific extensions.
3. **Translation layer** – crosswalks that maintain semantic meaning across systems when
   the same concept has different field names or encodings.

### Key metadata standards to reference

| Standard | Scope | Use in lakeloom |
|----------|-------|----------------|
| Dublin Core (15 elements) | General-purpose resources | Starter canonical schema; map lake tables to DC elements |
| DCAT 3 | Data catalogs / datasets | Native mapping for Delta Lake / Unity Catalog assets |
| BIBFRAME / RDA | Bibliographic metadata | Reference for relationship modeling |
| schema.org | Web entities | Crosswalk for external integration |
| FRBR / LRM | Entity relationships (Work/Expression/Manifestation/Item) | Concept provenance modeling |

### Implement MAP creation (`core/map.py` – new)

```python
class MetadataApplicationProfile(BaseModel):
    """Declaration of metadata terms, their constraints, and crosswalks."""

    name: str
    version: str
    canonical_schema: list[MetadataElement]  # core elements (Dublin Core + domain-specific)
    system_profiles: list[SystemProfile]  # per-system implementation notes
    crosswalks: list[Crosswalk]  # element mappings between systems
    controlled_vocabularies: list[str]  # slugs of linked CVs
    value_vocabularies: dict[str, list[str]]  # allowed values per element


class MetadataElement(BaseModel):
    name: str
    uri: str  # e.g. "dc:creator"
    definition: str
    obligation: Literal["mandatory", "recommended", "optional"]
    max_occurrences: int | None
    encoding_scheme: str | None  # e.g. "ISO 8601"
    example: str | None
    skos_mapping: str | None  # maps to SKOS property


class Crosswalk(BaseModel):
    source_element: str
    target_element: str
    target_standard: str  # "Dublin Core", "DCAT", "schema.org", etc.
    transformation_rule: str | None  # e.g. "concatenate first + last name"
    semantic_equivalence: Literal["exact", "close", "broad", "narrow"]
```

### MAP governance lifecycle

A MAP is **living documentation**. Add to `core/map.py`:

1. **Version tracking** – semantic versioning; breaking changes bump major version
2. **Change audit** – every element change is logged with rationale
3. **Validation** – automated checks that all mandatory elements are populated, values conform
   to controlled vocabularies, encoding schemes match
4. **Federated ownership** – each system profile is owned by the responsible team; central MAP
   coordinator owns the canonical schema

### Implementation patterns (book recommendations)

1. **Start small** – begin with one critical business entity (e.g. "DataProduct" or "Column") and prove value
2. **Build incrementally** – add elements and relationships gradually
3. **Federate ownership** – domain experts own their metadata, central coordination enforces standards
4. **Automate validation** – `lakeloom sources check` already validates MCP mappings; extend to MAP validation
5. **Visualize relationships** – add a `map export --format mermaid` command for relationship diagrams
6. **Measure impact** – track: search precision/recall before/after, integration time, data quality scores
7. **Version and govern** – treat MAP as a Git artifact; every change gets a commit

### Semantic fragmentation anti-pattern

> *"Semantic fragmentation is where the rules governing data have no meaningful connection to the
> conceptual models that should give that data meaning."*
> — Jessica Talisman, *Ontology Pipeline* [[1]](#references)

Lakeloom should detect and report semantic fragmentation: cases where:
- Two columns describe the same concept but use different names and no crosswalk exists
- A concept appears in one schema with a definition that contradicts another schema
- A tag in OM conflicts with a preferred label in the CV

Add a `lakeloom map check` command that surfaces these conflicts.

---

## 5. Stage 3 – Taxonomy

### What it is

A **taxonomy** is a hierarchical classification system that structures CV terms from broad to
specific, establishing `skos:broader` / `skos:narrower` parent-child relationships.

A taxonomy is NOT:
- A flat list of terms (that is a CV from Stage 1)
- A network of associative relationships (that is a thesaurus from Stage 4)
- A full ontological system with complex rules (that is Stage 5)

### Three functions

1. **Classification** – every term belongs to a category; hierarchy is a classification engine
2. **Navigation** – users (and agents) browse from general to specific with multiple entry points
3. **Reasoning** – AI systems traverse the hierarchy to infer relationships (transitivity)

### Construction prerequisites

Before building a taxonomy, you need:
- ✅ An approved Controlled Vocabulary (Stage 1)
- ✅ A Metadata Application Profile (Stage 2)
- A Coverage Model (see below)
- A Taxonomy Requirements Document

### Coverage model (`core/taxonomy_coverage.py` – new)

A coverage model answers four questions:

1. What topics do we need to include?
2. How deep should each branch go?
3. What level of granularity do users and digital systems need?
4. What are comparable organizations covering that we need to match or exceed?

```python
class CoverageModel(BaseModel):
    taxonomy_name: str
    use_cases: list[TaxonomyUseCase]
    domain_boundaries: list[str]  # what is in scope
    out_of_scope: list[str]  # explicit exclusions
    required_concept_areas: list[ConceptArea]  # must-have categories
    depth_guidelines: dict[str, int]  # branch name → max depth
    granularity_level: Literal["coarse", "medium", "fine"]
    external_benchmarks: list[str]  # competitor/industry taxonomies compared
    coverage_gaps: list[str]  # areas needing more research


class TaxonomyUseCase(BaseModel):
    name: str
    problem_statement: str
    how_taxonomy_solves_it: str
    priority: Literal["high", "medium", "low"]
    stakeholder_owner: str
    status: Literal["in_progress", "complete"]
```

### Taxonomy construction commands to add

```bash
lakeloom taxonomy new <name>              # create taxonomy from approved CV + MAP
lakeloom taxonomy coverage-model <name>  # run coverage model analysis
lakeloom taxonomy check <name>           # validate structure (polyhierarchy, orphans, loops)
lakeloom taxonomy export <name> --format skos-turtle | json-ld | csv
```

### Polyhierarchy

Lakeloom should support polyhierarchy (a concept with multiple parents). Example:
`"Machine Learning Model"` can legitimately be a child of both `"Algorithm"` and `"Data Product"`.

SKOS allows polyhierarchy natively via multiple `skos:broader` triples. Add validation to
detect **unintended** polyhierarchy (usually a sign of ambiguous definition) vs **legitimate**
polyhierarchy.

### Key taxonomy types in lake context

| Taxonomy type | Lake use case |
|--------------|---------------|
| Subject/topic taxonomy | Tag data products by subject domain |
| Data classification taxonomy | Sensitivity: Public → Internal → Confidential → Restricted |
| Data type taxonomy | Structured → Semi-structured → Unstructured |
| Organizational taxonomy | Business unit → Team → Domain |
| Product taxonomy | Line of business → Product family → Product |
| Process taxonomy | Business process → Sub-process → Step |

### Recommended tooling (external)

| Tool | Purpose | Cost |
|------|---------|------|
| TopBraid Composer / VocBench | Professional taxonomy editing | Free/low cost |
| Graphwise Taxonomy Builder | Enterprise taxonomy management | Enterprise |
| OpenRefine | Data cleaning, term reconciliation | Free |
| Excel / Google Sheets | Coverage model, use case tracking | Free |

---

## 6. Stage 4 – Thesaurus

### What it is

A **thesaurus** extends a taxonomy with **associative relationships** (`skos:related`) and
synonym control (`USE`/`UF` = `skos:prefLabel`/`skos:altLabel`). It follows **ANSI Z39.19** and
**ISO 25964**.

### ANSI Z39.19 relationship types

| Abbreviation | SKOS predicate | Meaning |
|-------------|---------------|---------|
| BT (Broader Term) | `skos:broader` | Parent in hierarchy |
| NT (Narrower Term) | `skos:narrower` | Child in hierarchy |
| RT (Related Term) | `skos:related` | Associated concept (non-hierarchical) |
| USE | `skos:prefLabel` on target | Preferred form; USE this term instead |
| UF (Used For) | `skos:altLabel` | Synonym entry; UF points back to preferred |
| SN (Scope Note) | `skos:scopeNote` | Defines scope/context |
| HN (History Note) | `skos:historyNote` | Documents change history |

### RT (Related Term) relationship mining

The RT relationship is the key addition in Stage 4. Mine it from:

1. **Co-occurrence analysis** – two terms that frequently appear in the same column descriptions
2. **Definition cross-references** – when definition of term A mentions term B
3. **Schema relationships** – foreign-key relationships in the lake → candidate RT links
4. **Embedding proximity** – concept embeddings with cosine similarity 0.6–0.9 (high similarity → merge; medium → RT)
5. **Domain expert elicitation** – workshops where domain experts draw concept maps

### Implement thesaurus construction (`core/thesaurus.py` – new)

```python
class ThesaurusRelationship(BaseModel):
    subject_uri: str
    predicate: Literal["broader", "narrower", "related"]
    object_uri: str
    source: str  # how this relationship was discovered
    confidence: float  # 0.0–1.0
    reviewer: str | None
    approved_at: datetime | None


def mine_rt_from_schema(scan_result: ScanResult) -> list[ThesaurusRelationship]:
    """Derive RT candidates from foreign-key and join relationships in the lake schema."""
    ...


def mine_rt_from_cooccurrence(term_corpus: list[str]) -> list[ThesaurusRelationship]:
    """Extract RT candidates from term co-occurrence in column descriptions."""
    ...
```

### Thesaurus commands to add

```bash
lakeloom thesaurus mine <model-slug>     # auto-discover RT candidates from lake schema
lakeloom thesaurus review <model-slug>  # review pending RT relationships
lakeloom thesaurus export --format ansi-z39-19 | skos-turtle | csv
```

---

## 7. Stage 5 – Ontology

### What it is

An **ontology** adds formal axioms, logical constraints, and reasoning rules to the thesaurus.
It answers not just "what categories exist" but "what must be true about every instance of this
class."

Lakeloom is already ontology-capable. The existing stack:
- OpenMetadata Ontology Studio (entity engine)
- `rdflib` with SKOS/OWL namespaces
- Turtle/JSON-LD/N-Triples serialization
- SPARQL read path
- OBO format support
- SHACL shapes generation (planned for M3)
- JSON-LD context generation (planned for M3)

### Key ontology constructs to expose

| Construct | OWL/SKOS | lakeloom location |
|-----------|---------|-------------------|
| Classes | `owl:Class` | OM Concept → `core/models.py` |
| Object properties | `owl:ObjectProperty` | OM relationships → `core/model_create.py` |
| Data properties | `owl:DatatypeProperty` | OM attributes |
| Individuals | `owl:NamedIndividual` | specific table/column instances |
| Axioms | `owl:Restriction`, `rdfs:subClassOf` | extend `core/model_policy.py` |
| Concept schemes | `skos:ConceptScheme` | OM glossaries → `core/models.py` |
| Collections | `skos:Collection`, `skos:OrderedCollection` | groupings within a glossary |

### SHACL shapes (extend for Stage 5)

SHACL shapes enforce ontology constraints at data time. For the ontology pipeline:

```turtle
# Every DataProduct must have a subject taxonomy term
ex:DataProductShape
    a sh:NodeShape ;
    sh:targetClass :DataProduct ;
    sh:property [
        sh:path :subjectTerm ;
        sh:minCount 1 ;
        sh:class skos:Concept ;
        sh:message "DataProduct must be classified with at least one taxonomy term."
    ] .
```

Add a `lakeloom ontology validate <model-slug>` command that runs SHACL validation against the
lake's metadata.

### Reasoner integration (future)

For production ontology reasoning, integrate:

- **ELK** – fast, scalable OWL 2 EL reasoner (good for large class hierarchies)
- **HermiT** – complete OWL 2 DL reasoner (use for axiom consistency checks)
- Python bindings: `py-horned-owl` or `owlready2`

Add `lakeloom ontology reason <model-slug> --reasoner elk` to classify all instances and
surface inferred relationships.

---

## 8. Stage 6 – Knowledge Graph

### What it is

A **knowledge graph** integrates all five previous stages into a navigable, queryable graph
where entities (data products, concepts, people, processes) are linked by typed, directionalized
relationships.

Lakeloom already has the optional triple-store sink (Fuseki, GraphDB, RDF4J). Stage 6 is about
making that sink first-class and adding graph-level capabilities.

### Graph schema for lake ontology

```turtle
# A data product links to its taxonomy term, its owner, its schema, and its quality profile
:my_sales_table
    a :DataProduct ;
    dc:title "Sales Transactions" ;
    :classifiedAs :SalesTaxonomy:Transaction ;
    :ownedBy :TeamSales ;
    :hasSchema [
        a :Schema ;
        :hasColumn :my_sales_table_amount ;
        :hasColumn :my_sales_table_customer_id
    ] ;
    :conformsTo ex:SalesDataContract ;
    :lastScanned "2026-11-18T00:00:00Z"^^xsd:dateTime .
```

### SPARQL patterns to expose via MCP

Add curated SPARQL query templates to `core/sparql.py`:

```sparql
# "Find all data products about customers"
SELECT ?product ?label WHERE {
    ?product a :DataProduct ;
             :classifiedAs ?concept .
    ?concept skos:broader* :CustomerConcept ;
             skos:prefLabel ?label .
}

# "What data products are upstream of my model?"
SELECT ?upstream WHERE {
    :my_model :dependsOn* ?upstream .
    ?upstream a :DataProduct .
}

# "Show all RT relationships to 'Revenue'"
SELECT ?related ?label WHERE {
    :RevenueConcept skos:related ?related .
    ?related skos:prefLabel ?label .
}
```

### Knowledge graph commands to add

```bash
lakeloom kg sync <model-slug>            # push full model to triple store
lakeloom kg query <sparql-file>          # run ad-hoc SPARQL
lakeloom kg path <from-uri> <to-uri>     # find relationship path between concepts
lakeloom kg export --format json-ld | turtle | n-triples
lakeloom kg stats <model-slug>           # node count, edge count, density metrics
```

### Linking to external knowledge bases

Connect the lake ontology to external knowledge graphs for enrichment:

| External KG | Link via | Use for |
|------------|---------|--------|
| Wikidata | `skos:exactMatch` | Ground concepts in world knowledge |
| schema.org | `owl:equivalentClass` | Web-scale interoperability |
| FIBO (finance) | `skos:exactMatch` | Financial domain alignment |
| SNOMED CT | `skos:exactMatch` | Healthcare/clinical alignment |
| DBpedia | `owl:sameAs` | General entity linking |

---

## 9. Pipeline Orchestration

### CLI workflow (extend `cli.py`)

A practitioner building an ontology from scratch should run stages in order:

```bash
# Stage 1: seed the CV from the lake
lakeloom vocab harvest --source unity-catalog --nlp spacy > candidates.jsonl
lakeloom vocab review candidates.jsonl          # human-in-the-loop approval
lakeloom vocab promote candidates.jsonl --model <slug>

# Stage 2: generate a MAP from the lake's metadata schemas
lakeloom map generate --source unity-catalog --standard dublin-core > map.yaml
lakeloom map validate map.yaml
lakeloom map publish map.yaml --model <slug>

# Stage 3: build taxonomy from approved CV + MAP
lakeloom taxonomy coverage-model <slug>         # generates coverage-model.yaml
lakeloom taxonomy build <slug> --coverage-model coverage-model.yaml
lakeloom taxonomy check <slug>

# Stage 4: mine and review thesaurus relationships
lakeloom thesaurus mine <slug>
lakeloom thesaurus review <slug>

# Stage 5: validate ontology axioms
lakeloom ontology validate <slug>
lakeloom ontology reason <slug>

# Stage 6: sync to knowledge graph
lakeloom kg sync <slug>
lakeloom kg stats <slug>
```

### MCP tools per stage (implemented)

The CLI workflow above is the book-inspired design; agents run the pipeline through MCP:

| Stage | Read | Write (draft proposals, human review) |
|-------|------|---------------------------------------|
| Guidance | `get_next_steps`, prompt `build_ontology` | — |
| 1 – Vocabulary | `harvest_vocabulary_candidates`, `find_concepts`, `explain_concept` | `propose_add_concepts`, `import_model` |
| 2 – MAP | `build_map`, `validate_map`, `export_map` | `propose_realization` |
| 3 – Taxonomy | `assess_taxonomy_coverage`, `get_model_stats` | `propose_add_hierarchy`, `propose_add_relation` (`broader`) |
| 4 – Thesaurus | `validate_thesaurus`, `suggest_related_terms`, `get_annotation_stats`, `list_deprecated_concepts` | `propose_add_relation` (`related`), `propose_add_scope_note`, `propose_add_history_note`, `propose_deprecate_concept` |
| 5 – Ontology | `validate_model`, `export_model` | `propose_from_scan`, `propose_change` |
| 6 – Knowledge Graph | `assess_realization_coverage`, `list_releases`, `diff_releases`, `suggest_external_mappings` | `create_release`, `propose_external_mappings` |

Every write tool creates a draft proposal; `submit_proposal` sends it to review and
`review_proposal` (a human, with their own sign-in) applies it.

---

## 10. Governance Principles (from the book [[1]](#references))

### Core principle: governance and semantics are inseparable

> *"Library science's successes with interoperability stem from its commitment to explicit data
> modeling as the foundation of semantic systems."*
> — Jessica Talisman, *Ontology Pipeline* [[1]](#references)

Every metadata decision in lakeloom is a **semantic decision**. Build governance into the
construction process, not on top of it:

- Every CV term has a definition owner (team/individual)
- Every MAP element has a semantic contract (what it means, not just what it stores)
- Every taxonomy change triggers a change-set review
- Every thesaurus RT relationship documents its provenance

### Semantic theater anti-pattern

> *"Semantic theater is a system that appears to provide rich meaning but instead offers better
> search and visualization of the same semantically impoverished data."*
> — Jessica Talisman, *Ontology Pipeline* [[1]](#references)

Avoid this in lakeloom by:
- Requiring definitions (not just labels) for every approved term
- Enforcing SKOS `scopeNote` for all terms with context-dependent meanings
- Flagging "semantic theater" in `audit.py`: terms added without definitions, relationships added without sources

### ALL users are users

> *"ALL users are users, and therefore all systems are designed for all users, be it human,
> machine, or even AI."*
> — Jessica Talisman, *Ontology Pipeline* [[1]](#references)

Design every ontology artifact for dual audiences:
- **Human-readable**: clear natural language definitions with examples and scope notes
- **Machine-readable**: well-formed SKOS/OWL with stable URIs, encoding schemes, SHACL constraints
- **AI-readable**: rich context clues in definitions (synonyms, antonyms, examples, scope notes)
  that give LLMs the semantic scaffolding to reason correctly

---

## 11. File and Module Map

> Planning table from before M5; kept for history. The implemented modules are
> `core/vocabulary.py`, `core/map_profile.py`, `core/taxonomy.py`, `core/thesaurus.py`,
> `core/entity_linking.py`, `core/graph_analytics.py`, `core/annotations.py`,
> `core/next_steps.py` and the matching `server/tools_*.py` (see section 2).

| Book concept | Lakeloom location | Action |
|-------------|------------------|--------|
| CV governance lifecycle | `core/pending.py`, `core/review.py` | Extend status model; add provenance |
| Concept provenance | `core/import_terms.py` | Add provenance fields |
| NLP concept mining | `core/concept_mining.py` | **New module** |
| Folksonomy harvest | `sources/om_tags.py` | **New module** |
| Metadata Application Profile | `core/map.py` | **New module** |
| Coverage model | `core/taxonomy_coverage.py` | **New module** |
| Taxonomy construction | `core/taxonomy.py` | **New module** (extend existing SKOS) |
| Thesaurus RT mining | `core/thesaurus.py` | **New module** |
| SHACL validation | `publish/shacl_gen.py` | Already planned for M3 |
| JSON-LD context | `publish/context_gen.py` | Already planned for M3 |
| Knowledge graph sync | `publish/kg_sync.py` | Extend triple-store sink |
| SPARQL query templates | `core/sparql.py` | Add ontology-pipeline templates |
| MAP check command | `cli.py` | Add `map check` subcommand |
| Thesaurus commands | `cli.py` | Add `thesaurus` subcommand group |
| Taxonomy commands | `cli.py` | Extend `taxonomy` subcommands |
| KG commands | `cli.py` | Add `kg` subcommand group |

---

## 12. Prioritized Roadmap

> Original roadmap, kept for history. M5–M9 delivered most of items 2–7 and 9; the open
> items are listed in the "Still open" column of section 2.

### M4c (recommended next milestone)

1. **Stage 1 hardening** – add provenance tracking model to `import_terms.py`; extend `dedupe.py` with semantic similarity matching
2. **Stage 2 scaffold** – implement `core/map.py` with the three-layer model; add `lakeloom map generate` and `lakeloom map validate` commands
3. **Stage 3 coverage model** – implement `core/taxonomy_coverage.py`; add `lakeloom taxonomy coverage-model` command
4. **NLP concept mining** – add `core/concept_mining.py` with spaCy integration for lake column descriptions

### M5 (next)

5. **Stage 4 thesaurus mining** – implement `core/thesaurus.py`; foreign-key → RT mining; `lakeloom thesaurus mine` command
6. **MCP tools for pipeline** – expose `harvest_candidate_terms`, `explain_concept`, `get_coverage_model` as MCP tools
7. **Semantic theater detection** – add `audit.py` rules that flag terms without definitions or relationships without provenance

### M6 (future)

8. **Stage 5 reasoning** – integrate ELK or HermiT via `py-horned-owl`; `lakeloom ontology reason` command
9. **Stage 6 KG enrichment** – Wikidata entity linking; external KG crosswalks
10. **Full pipeline CLI** – end-to-end `lakeloom pipeline run` command

---

## References

### Method

1. Jessica Talisman. *Ontology Pipeline: A Framework for Building Knowledge Infrastructures*.
   Technics Publications, 2026. Amazon ASIN B0HJ8QWJ5V. Author's site:
   <https://jessicatalisman.substack.com/about>. *The Ontology Pipeline®* is her registered
   trademark.
2. Jessica Talisman. "The Ontology Pipeline®". *Intentional Arrangement*.
   <https://jessicatalisman.substack.com/p/the-ontology-pipeline>, and "The Ontology
   Pipeline™, Refresh", <https://jessicatalisman.substack.com/p/the-ontology-pipeline-refreshed>.

### Standards

3. NISO. *ANSI/NISO Z39.19-2005 (R2010): Guidelines for the Construction, Format, and
   Management of Monolingual Controlled Vocabularies*.
   <https://www.niso.org/publications/ansiniso-z3919-2005-r2010>. This is the standard
   underlying the book.
4. ISO. *ISO 25964-1:2011* and *ISO 25964-2:2013, Thesauri and interoperability with other
   vocabularies*. <https://www.iso.org/standard/53657.html>,
   <https://www.iso.org/standard/53658.html>.
5. W3C. *SKOS Simple Knowledge Organization System Reference*.
   <https://www.w3.org/TR/skos-reference/>
6. W3C. *OWL 2 Web Ontology Language Document Overview*. <https://www.w3.org/TR/owl2-overview/>
7. W3C. *Shapes Constraint Language (SHACL)*. <https://www.w3.org/TR/shacl/>
8. DCMI. *DCMI Metadata Terms* (Dublin Core).
   <https://www.dublincore.org/specifications/dublin-core/dcmi-terms/>
9. W3C. *Data Catalog Vocabulary (DCAT) – Version 3*. <https://www.w3.org/TR/vocab-dcat-3/>

### Tools and vocabularies mentioned (not used by Lakeloom)

These are suggestions from the design discussion above, not dependencies. Check each
license before adopting one.

| Name | Authors | License | Link |
|------|---------|---------|------|
| spaCy | Explosion AI | MIT | <https://spacy.io/> |
| NLTK | The NLTK Project | Apache-2.0 | <https://www.nltk.org/> |
| VocBench | ART Group, University of Rome Tor Vergata | BSD-3-Clause | <https://vocbench.uniroma2.it/> |
| TopBraid Composer / EDG | TopQuadrant | Commercial | <https://www.topquadrant.com/> |
| ELK reasoner | Yevgeny Kazakov, Markus Krötzsch, František Simančík and contributors | Apache-2.0 | <https://github.com/liveontologies/elk-reasoner> |
| HermiT reasoner | University of Oxford (Boris Motik, Rob Shearer, Birte Glimm, Giorgos Stoilos, Ian Horrocks) | LGPL-3.0 | <http://www.hermit-reasoner.com/> |
| py-horned-owl | Phillip Lord, Björn Gehrke and contributors | LGPL-3.0 | <https://github.com/ontology-tools/py-horned-owl> |
| Owlready2 | Jean-Baptiste Lamy | LGPL-3.0 | <https://owlready2.readthedocs.io/> |
| Wikidata | Wikimedia Foundation and contributors | Data CC0 1.0 | <https://www.wikidata.org/> |
| schema.org | schema.org community (W3C Community Group) | CC BY-SA 3.0 | <https://schema.org/> |
| DBpedia | DBpedia Association | CC BY-SA 3.0 and GFDL | <https://www.dbpedia.org/> |
| FIBO | EDM Council | MIT | <https://spec.edmcouncil.org/fibo/> |
| SNOMED CT | SNOMED International | Licensed (affiliate licence required) | <https://www.snomed.org/> |
| NICE Framework | NIST | Public domain (US Government work) | <https://www.nist.gov/itl/applied-cybersecurity/nice/nice-framework-resource-center> |

> **License note for future work.** HermiT, py-horned-owl and Owlready2 are LGPL-3.0. Using
> them as separately installed, dynamically loaded libraries is compatible with Lakeloom's
> Apache-2.0 license. Copying their code into Lakeloom is not. SNOMED CT cannot be
> redistributed without a licence.
