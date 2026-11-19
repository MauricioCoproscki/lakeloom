# Tutorial: maintaining a model

A model is never finished. Terms change meaning, tables change shape, and reviews pile up.
This tutorial covers the routine work. Start each session the same way:

> **You:** What needs attention on the retail model?

The assistant calls `get_next_steps(model="retail", goal="maintain")`. With
`goal="maintain"` it ranks only blockers, reviews, releases and lake changes.

## Proposals that wait for you

If a reviewer **requested changes** on your proposal, it comes first in the list. Ask the
assistant to show it:
- `get_proposal` shows the reviewer's reason;
- `diff_proposal` shows what the proposal changes.

Fix it with `propose_change`, then `submit_proposal` again.

If the model changed in OpenMetadata after you proposed, approval fails with a `conflict`.
Then `rebase_proposal` rebuilds your proposal against the current state, and you submit
it again.

Old drafts nobody submitted show up as *stale drafts*. Submit them, or remove them with
`discard_proposal`, which asks for confirmation.

## Replacing a term

> **You:** We no longer say *Client*; it's *Customer* now.

The assistant calls `propose_deprecate_concept`:

```json
{"model": "retail", "concept": "Client", "reason": "Merged into Customer in 2026",
 "successor": "Customer"}
```

Once approved:
- `explain_concept("Client")` opens with **Deprecated. Use `Customer` instead.**;
- `find_concepts` marks *Client* as deprecated;
- the SKOS export records `owl:deprecated` and `dct:isReplacedBy`, and keeps "Client" as a
  hidden label of *Customer*, so searches for the old name still find the new term.

`list_deprecated_concepts` lists every deprecated term, and `get_annotation_stats` shows how
many terms have scope notes, history notes and definitions.

## When the lake changes

Tables gain columns, lose columns, get renamed.

> **You:** Did the tables behind the retail model change?

The `drift_check` prompt, or `detect_drift`, scans the model's tables again and compares
them with the scans the model was built from:
- added columns become new attributes;
- removed columns become removals;
- possible renames are shown as a removal plus an addition, and the assistant asks you
  about each one.

You get a draft proposal to review and submit. An operator can also run this check on a
schedule. `get_next_steps` then shows *Review lake changes* when the scheduled run found
something.

## Keeping releases current

`get_next_steps` reports applied changes that are not in a release yet. A user with
`lakeloom:publish` calls `create_release`. The version is computed from what changed:
- a removed term or structural link is a **major** version;
- an added term is a **minor** version;
- only labels, definitions or notes is a **patch**.

`list_releases` and `diff_releases` show the history.

## Health checks worth running now and then

| Question | Tool |
|----------|------|
| Is the model structurally sound? | `validate_model` |
| Is the thesaurus well formed? | `validate_thesaurus` |
| How much of the model is backed by data? | `assess_realization_coverage` |
| Are there orphan terms, and is the hierarchy too deep or too flat? | `get_model_stats` |
| Which columns have no term? | `build_map`, `validate_map` |
| Do our terms match public vocabularies? | `suggest_external_mappings` |
