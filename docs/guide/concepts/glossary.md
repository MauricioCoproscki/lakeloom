# Glossary

Plain-language meanings of the words used across Lakeloom and this guide.

| Term | Meaning |
|------|---------|
| **Concept** (term) | One business idea with a preferred name, such as *Customer*. In OpenMetadata it is a glossary term; in RDF it is a `skos:Concept`. |
| **FQN** | Fully qualified name of a concept: the model slug and the path through its parents, as in `retail.Order.OrderStatus`. It is the safest way to name a concept. |
| **IRI** | The web identifier of a concept, as in `https://…/ontology/retail/Order`. It is used in exports and links. |
| **Model** | A semantic model: one OpenMetadata glossary paired with a native ontology of the same name. It is identified by its **slug**, such as `retail`. |
| **Controlled vocabulary** | The agreed list of terms, with definitions and synonyms. Stage 1 of the pipeline. |
| **Definition** | One or two sentences saying what a term means (`skos:definition`). |
| **Synonym** (altLabel) | Another name people use for the same term. |
| **Folksonomy** | The informal tags people already apply, such as OpenMetadata classification tags. It is the raw material of a vocabulary. |
| **MAP** (Metadata Application Profile) | Which tables and columns carry which terms. Stage 2. Lakeloom builds its implementation layer. |
| **Realization** | A table or column that carries a concept: in OpenMetadata, the asset is tagged with the term. |
| **Taxonomy** | Terms arranged from broad to narrow. Stage 3. |
| **BT / NT** | Broader term and narrower term: the two directions of a parent–child link. |
| **RT** (related term) | Two terms that belong together without one being a kind of the other. The link goes both ways. |
| **Thesaurus** | A taxonomy plus related terms, scope notes, history notes and deprecations. Stage 4. |
| **Scope note** | When to use a term and when not to. |
| **History note** | How and why a term changed over time. |
| **Deprecation** | A term that should no longer be used, and the term that replaces it (its **successor**). |
| **Ontology** | The formal model: attributes with data types, typed relations, axioms. Stage 5. |
| **Knowledge graph** | The published, versioned model linked with other vocabularies and with the data. Stage 6. |
| **Effective model** | What Lakeloom shows when you read a model: OpenMetadata's terms plus the scope notes, history notes and deprecations from the proposal log. |
| **Proposal** | A set of changes waiting for review. It moves through draft, submitted, then applied, rejected or changes requested. |
| **Change set** | OpenMetadata's container for a proposal's operations. Lakeloom creates and submits it, and a reviewer applies it. |
| **Review** | A human decision on a submitted proposal: approve, reject or request changes. |
| **Release** | A Git tag of the model's state with a semantic version, such as `1.4.0`, and a changelog. |
| **Scan** | A recorded read of lake tables: their columns, versions, OpenMetadata matches and, optionally, profiles. |
| **Drift** | The difference between the lake tables now and the scans a model was built from. |
| **Stage** | Where `get_next_steps` places a model: bootstrap, vocabulary, taxonomy, thesaurus, ontology or maintenance. |
| **Scope** (access) | A permission on your sign-in: `lakeloom:read`, `propose`, `review`, `publish` or `admin`. |
| **Confirm token** | The token a dry run returns. You must pass it back to really perform a risky action, such as creating a model, discarding a proposal, approving, rejecting, releasing or importing. |
| **SKOS** | The W3C standard for vocabularies, taxonomies and thesauri. `export_model(format="skos")` produces it. |
| **SHACL shapes** | Rules generated from the model that check whether data matches it. |
