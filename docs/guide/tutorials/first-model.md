# Tutorial: your first model, step by step

This tutorial follows a business user, Maria, who owns the *Retail* data product. She has no
ontology background. She talks to an AI assistant (Claude, Copilot or any other MCP client)
that her operator has connected to Lakeloom. The assistant calls the tools; Maria decides.

You can follow along on the development stack. The [operator guide](../operators/README.md)
starts it, and it includes a fixture lake with the tables `orders`, `customers`, `products`
and `invoices`.

> **Tip.** You can ask the assistant to start the guided conversation: *"Use the
> build_ontology prompt for the retail model."* It runs the loop described here.

## 0. Ask where to start

> **Maria:** I want a shared vocabulary for our retail data. Where do I start?

The assistant calls `get_next_steps(model="retail")`. If the model does not exist, the
answer is the **bootstrap** stage: the model must be created first. Creating a model needs
the `lakeloom:admin` scope. If Maria does not have it, the assistant says so and asks her
to contact an operator. After the operator creates it, the next steps say: *no concepts
yet — start the vocabulary*.

## 1. Controlled vocabulary: agree on the words

There are two ways to get candidate terms, and they combine well.

**From the tags people already use**

> **Maria:** What terms do we already use?

The assistant calls `harvest_vocabulary_candidates()` with no tags. Lakeloom reads the
OpenMetadata classification tags, normalises them (`Sales.order_total` → *Order Total*) and
ranks them by how many assets use them. The assistant shows the list.

**From the lake tables**

> **Maria:** Build terms from the orders and customers tables.

The assistant calls `scan_lake_source`, then `propose_from_scan`. Lakeloom maps tables to
concepts, columns to attributes and foreign keys to relations, and skips what already exists.

**Agree and define**

The assistant goes through the candidates with Maria and writes a one-sentence definition
for each term she keeps. Then it calls `propose_add_concepts`:

```json
{
  "model": "retail",
  "terms": [
    {"name": "Customer", "definition": "A person or company that has placed at least one order.",
     "synonyms": ["Client", "Account holder"]},
    {"name": "Order", "definition": "A customer's request to buy one or more products.",
     "scope_note": "Only confirmed orders; quotes are Quotation."},
    {"name": "Voucher"}
  ]
}
```

The draft comes back with a **warning**: *1 concept(s) have no definition (Voucher)*. A
concept cannot be edited inside the same proposal that creates it. So Maria dictates a
definition, and the assistant discards the draft (`discard_proposal`, after her
confirmation) and calls `propose_add_concepts` again with the definition included. When she
is happy, the
assistant calls `submit_proposal` and tells her that a reviewer still has to approve it.

**Already a deliverable.** Once the terms are approved:

> **Maria:** Can I share the glossary with my team?

The assistant calls `export_glossary`. The result is a Markdown glossary for the team wiki,
or CSV for a spreadsheet. Undefined terms are marked, and `structured.quality` says how
complete the vocabulary is. Maria does not need a taxonomy or an ontology to use it.

## 2. Metadata profile: connect terms to the data

After the reviewer approves, Maria asks *"what next?"* again. A typical step is *Map the
scanned columns*.

> **Maria:** Which columns still have no term?

The assistant calls `build_map` with the latest scan. It shows, per table, the columns that
carry a term and those that do not. For a column that clearly carries an existing term, the
assistant proposes the link with `propose_realization`. A column that needs a new term goes
back to step 1.

**Deliverable:** `export_data_dictionary` turns the scan into a data dictionary, in Markdown
or CSV. Each column comes with its type, business term and definition, and columns without
a term are marked. Maria sends the CSV to the analysts.

## 3. Taxonomy: from broad to narrow

> **Maria:** Card payments, transfers and vouchers are all kinds of payment.

The assistant calls `propose_add_hierarchy` with parent *Payment* and children *Card
payment*, *Bank transfer* and *Voucher*. If *Payment* already exists, it is reused. Moving a
term that already exists under another parent (`propose_add_relation(relation="broader")`) is
refused on OpenMetadata 2.1.0-rc1: that version fails when it applies a parent change. Create
new terms directly under their parent, as here, or move an existing term in the OpenMetadata
UI.

If most terms still lacked definitions, the proposal would carry a warning that the
vocabulary should be finished first.

> **Maria:** Do we cover everything we care about?

`assess_taxonomy_coverage` compares the topics Maria lists with the taxonomy and reports
the gaps. `get_next_steps` also flags a hierarchy that is too flat or too deep.

**Deliverable:** `export_taxonomy(format="mermaid")` draws the tree for a slide, and
`format="csv"` feeds a BI hierarchy or a portal menu.

## 4. Thesaurus: related terms and notes

> **Maria:** Which terms go together?

`suggest_related_terms` finds terms that appear on the same tables. The assistant shows the
pairs, for example *Invoice ↔ Payment*. For each pair Maria confirms, it calls
`propose_add_relation(relation="related")`. The link goes both ways.

Maria also asks for notes:

- *"Explain that Order excludes quotes"* → `propose_add_scope_note`;
- *"Record that Client was renamed to Customer in 2026"* → `propose_add_history_note`.

`validate_thesaurus` then checks the structure against ANSI Z39.19, for example that every
related link has its reverse and that the hierarchy has no cycles.

**Deliverables:**
- `export_synonyms` writes a synonym file for Elasticsearch or OpenSearch. Searching for
  "client" now finds "customer", and a deprecated term redirects to its replacement.
- `expand_query` turns a question into the terms a search or a RAG retriever should use.
  For example, *"late purchase"* gives *Order* (matched through its synonym) plus its
  synonyms, narrower terms and related terms.

> **Tip.** At any point, ask *"What can I already hand out?"*. `get_next_steps` lists every
> deliverable as ready, partial or unavailable. See
> [What you get at each stage](../concepts/deliverables.md).

## 5. Publish

When the reviewer has approved the changes, `get_next_steps` shows *Release the applied
changes*. A user with the `lakeloom:publish` scope asks for a release. The assistant calls
`create_release` without a token first: this dry run shows the version (`1.0.0` for the
first release), the changelog and any warnings. After the user confirms, it calls it again
with the token.

## What Maria never had to know

She never wrote RDF or picked a SKOS predicate, and never touched OpenMetadata directly. Each
decision was hers, each change was reviewed, and `get_next_steps` told her what mattered
next.

Next: [Maintaining a model](maintaining-a-model.md).
