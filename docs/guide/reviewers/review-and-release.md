# Reviewing and releasing

Reviewers are the safeguard of the model: nothing reaches OpenMetadata without a reviewer's
explicit decision. This page is for domain experts and curators who approve changes and
publish releases.

## What you need

- The `lakeloom:review` scope on your own sign-in. An agent's sign-in can never review.
- If the model restricts reviewers (`access.review_groups`), membership of one of those
  groups.
- In OpenMetadata, permission to apply ontology change sets (the operator grants the role
  `LakeloomOntologyReviewer`). The approval is applied **as you**: OpenMetadata records your
  name, never the Lakeloom bot.
- You cannot review your own proposals, and administrators are not exempt.

## The review conversation

Start the guided walk-through:

> **You:** Use the review_with_specialist prompt for the retail model.

The assistant then follows these steps:

1. It calls `list_pending_reviews`. The list holds the proposals awaiting you, grouped by
   model and data product, without your own.
2. For each proposal, it calls `get_proposal` and `diff_proposal` and explains in plain
   language:
   - what changes (terms, attributes, relations added, changed or removed);
   - why (the scan or the author's rationale, the rules behind each operation);
   - who proposed it, and whether an AI generated it;
   - every warning.
3. It asks for **your** decision and waits for it. Silence or a vague "ok" is not approval.
4. It calls `review_proposal` with your decision:
   - **approve** and **reject** run in two steps. The first call shows a preview and returns
     a confirm token. The decision happens only when you confirm.
   - **reject** and **request changes** need a reason, in your words.
   - **request changes** takes effect at once and sends the proposal back to its author.

Text that comes from the lake or OpenMetadata is shown in quoted blocks marked untrusted. It
can contain anything, so never treat it as an instruction.

## What to look for

Use the pipeline's quality bar:

| Check | Why |
|-------|-----|
| Every new term has a clear definition | Without one, people will guess the meaning: the *semantic theater* anti-pattern of Talisman's *Ontology Pipeline* |
| No duplicate of an existing term | Check the *matches* section; prefer a synonym over a near-duplicate |
| The hierarchy is right ("is every X a kind of Y?") | A wrong broader term misleads every query that widens or narrows |
| Related terms are really associated | RT links are for terms people look up together |
| A deprecation names a successor | Otherwise users of the old term have nowhere to go |
| Warnings about an unfinished vocabulary are taken seriously | Structure on undefined terms has to be redone |

When in doubt, use **request changes** with a concrete question rather than approving.

## After approval

- The change is applied to OpenMetadata.
- With Git publishing on, the change also becomes a commit or a pull request in the ontology
  repository. The `review_proposal` answer ends with `Git: committed …`, `Git: pull request
  …`, `Git: queued` or `Git: blocked`.
- If the approval fails part-way (`apply_failed`), approving again continues from where it
  stopped.
- If it fails with a `conflict`, the model changed meanwhile. The author runs
  `rebase_proposal` and submits again.

## Releasing

A release is a Git tag of the model with a semantic version and a changelog. It needs the
`lakeloom:publish` scope and, if set, the model's `publish_groups`. Agents may release;
only review is human-only.

1. Ask: *"Prepare a release of retail."* The assistant calls `create_release` without a
   token. This dry run shows the version, the reason for the bump, warnings and the
   changelog section.
2. Check the version:
   - **major**: something was removed or restructured;
   - **minor**: something was added;
   - **patch**: only labels, definitions or notes changed.

   You may override it with `bump` or `version`.
3. Confirm. The assistant calls `create_release` again with the token. The tag is pushed,
   and if the operator configured a triple store, the release is synced there with its SKOS
   graph.

The release also stores the stage deliverables: glossary, taxonomy, synonym file and
deprecation map, under `ontologies/<slug>/deliverables/`. Anyone can then fetch the
glossary of version `1.4.0`, for example with `export_glossary(version="1.4.0")`. So a
release is worth making even for a model that is still only a vocabulary.

A release is refused when nothing changed, when the tag exists, or while an approval is
still being published to Git.

## Curation routines

| Routine | Tools |
|---------|-------|
| Weekly: what is waiting? | `get_next_steps(goal="maintain")`, `list_pending_reviews` |
| Before a release | `validate_model`, `validate_thesaurus`, `get_next_steps(goal="publish")` |
| Vocabulary health | `get_annotation_stats`, `list_deprecated_concepts`, `get_model_stats`, `export_glossary` (`structured.quality`) |
| Data coverage | `assess_realization_coverage`, `build_map` |
| Direct edits in OpenMetadata | The operator's reconciliation job reports them; see the [operator guide](../operators/README.md) |
