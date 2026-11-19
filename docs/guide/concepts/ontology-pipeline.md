# The ontology pipeline in plain language

Lakeloom follows the method of the book *Ontology Pipeline*, which builds on the ANSI/NISO
Z39.19 standard for controlled vocabularies. This page explains the method without jargon,
and shows which Lakeloom tools belong to each step. You do not need to call the tools
yourself: the assistant does, after you agree.

## Why a pipeline?

A data lake has thousands of columns named `cust_id`, `customer_no`, `client_ref`. People
and AI agents keep guessing what they mean. An ontology is the agreed answer: a list of
business terms, what each one means, how they relate, and which data carries them.

Building it in one go fails. The pipeline builds it **in stages, each resting on the one
before**:

```
1. Controlled      2. Metadata        3. Taxonomy      4. Thesaurus     5. Ontology    6. Knowledge
   vocabulary  ──►    profile (MAP) ──►             ──►              ──►            ──►   graph
   agreed terms      which data          broad to         related terms,   formal         published,
   + definitions     carries each term   narrow           notes, history   model          linked, queried
```

> **Do not skip ahead.** A hierarchy built on undefined terms has to be redone once people
> discover they meant different things. Lakeloom warns you when you start structuring a
> model whose terms are mostly undefined.

## Stage 1 — Controlled vocabulary: agree on the words

**What it is.** A list of business terms, one preferred name each, with a one-sentence
definition and the synonyms people actually use ("Client", "Account holder" → *Customer*).

**Why it matters.** A term without a definition is a label, not shared meaning. The book
calls a vocabulary of undefined labels *semantic theater*: it looks governed, but nobody
agrees on anything.

**In Lakeloom**

| You ask | The assistant uses |
|---------|--------------------|
| "Which terms are we already using?" | `harvest_vocabulary_candidates` reads the classification tags in OpenMetadata, ranked by how often they are used |
| "Add these terms" | `propose_add_concepts`, which warns about any term without a definition |
| "Build terms from these lake tables" | `scan_lake_source` and `propose_from_scan` |

## Stage 2 — Metadata profile (MAP): connect terms to data

**What it is.** The *Metadata Application Profile* says which tables and columns carry
each term. For example, `sales.orders.order_total` carries *Order amount*.

**Why it matters.** A term nobody can find in the data cannot be used. A column without a
term is invisible to the vocabulary.

**In Lakeloom**

| You ask | The assistant uses |
|---------|--------------------|
| "Which columns have no term yet?" | `build_map` and `validate_map` on the latest scan |
| "Give me the MAP document" | `export_map` |
| "This column is the customer id" | `propose_realization` |

Lakeloom builds the **implementation layer** of the MAP: physical column → term, computed
from a scan. The book's canonical layer (elements and obligations) and its crosswalks
between standards are not modelled yet.

## Stage 3 — Taxonomy: from broad to narrow

**What it is.** Terms arranged in a hierarchy. *Payment* is broader than *Card payment*,
which is broader than *Contactless payment*.

**Why it matters.** People browse and filter by hierarchy, and an agent can widen or narrow
a question ("all payments" or "only card payments").

**In Lakeloom**

| You ask | The assistant uses |
|---------|--------------------|
| "Group these terms under *Payment*" | `propose_add_hierarchy` |
| "Make *Refund* a kind of *Payment*" | `propose_add_relation` with `relation: broader` |
| "Do we cover the topics we care about?" | `assess_taxonomy_coverage` |
| "Is the hierarchy too flat or too deep?" | `get_model_stats`, and the hierarchy signals of `get_next_steps` |

## Stage 4 — Thesaurus: related terms and their history

**What it is.** The taxonomy, plus:

- **related terms** (RT): *Invoice* is related to *Payment*, but neither is a kind of the
  other;
- **scope notes**: when to use a term and when not to ("Only paid orders; quotes are
  *Quotation*");
- **history notes**: how a term changed ("Renamed from *Client* in 2026");
- **deprecations**: a term that should no longer be used, and what replaces it.

**Why it matters.** This is what keeps a vocabulary trustworthy as the organisation
changes. A deprecated term that still looks current misleads every agent that reads it.

**In Lakeloom**

| You ask | The assistant uses |
|---------|--------------------|
| "Which terms are probably related?" | `suggest_related_terms` (terms that appear on the same data) |
| "Relate *Invoice* and *Payment*" | `propose_add_relation` with `relation: related` |
| "Explain when to use *Order*" | `propose_add_scope_note` |
| "Record that *Client* was renamed" | `propose_add_history_note` |
| "Stop using *Client*, use *Customer*" | `propose_deprecate_concept` with a successor |
| "Is the thesaurus well formed?" | `validate_thesaurus` (ANSI Z39.19 checks) |

## Stage 5 — Ontology: the formal model

**What it is.** The thesaurus made precise enough for software: attributes with data types,
typed relations between concepts ("an *Order* is placed by a *Customer*"), rules, and
shapes that check the data.

**In Lakeloom.** Here OpenMetadata is the ontology engine. `propose_change` makes any
precise edit, `validate_model` checks the structure, and `export_model` exports Turtle,
RDF/XML, N-Triples, JSON-LD or SKOS. Lakeloom also generates SHACL shapes and a JSON-LD
context.

## Stage 6 — Knowledge graph: publish and link

**What it is.** The model is published, versioned, linked to public vocabularies (Wikidata,
schema.org) and queried together with the data.

**In Lakeloom**

| You ask | The assistant uses |
|---------|--------------------|
| "Tag a release" | `create_release` (dry run first), `list_releases`, `diff_releases` |
| "Link our terms to Wikidata" | `suggest_external_mappings`, then `propose_external_mappings` |
| "How much of the model is backed by data?" | `assess_realization_coverage` |

An operator can also mirror every release to a triple store (Fuseki, GraphDB, RDF4J).

## Not sure where you are?

Ask the assistant **"what should I do next?"**. It calls `get_next_steps`, which reads the
model's live state and returns:

- the **stage** the model is in;
- a short, ranked list of **steps**, each with a plain-language *why* and the tool to use.

Blocking problems come first: structural errors, proposals waiting for you, applied changes
not yet released. Steps meant for someone else (another author, a reviewer, an operator)
are labelled as such.

Or start the guided conversation: the `build_ontology` prompt walks you through the
stages one approved step at a time.

Next: [How Lakeloom works](how-lakeloom-works.md).
