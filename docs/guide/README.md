# Lakeloom User Guide

Lakeloom helps a team turn the tables of a data lake into a shared, governed business
vocabulary (an *ontology*) stored in OpenMetadata. You work with it through an AI assistant
that is connected to Lakeloom over MCP: you describe what you want in plain language, the
assistant calls Lakeloom's tools, and every change waits for a human reviewer before it is
applied.

This guide is organised by what you do with Lakeloom. Start with the path that fits you.

## Pick your path

| You are… | You want to… | Start here |
|----------|--------------|------------|
| **A business user** (analyst, data product owner, domain expert) | Build or improve a model by talking to the assistant, with no ontology background | [The ontology pipeline in plain language](concepts/ontology-pipeline.md), then [Tutorial: your first model](tutorials/first-model.md) |
| **A reviewer or curator** | Approve or reject proposed changes, keep the vocabulary healthy, publish releases | [Reviewing and releasing](reviewers/review-and-release.md) |
| **An operator** | Install, configure and run Lakeloom next to OpenMetadata | [Operator guide](operators/README.md) |
| **An agent developer** | Connect an MCP client, call tools, handle errors and scopes | [MCP reference](reference/README.md) |

## Contents

1. **Concepts**
   - [The ontology pipeline in plain language](concepts/ontology-pipeline.md)
   - [How Lakeloom works: proposals, review and the effective model](concepts/how-lakeloom-works.md)
   - [Glossary](concepts/glossary.md)
2. **Tutorials** (business users)
   - [Your first model, step by step](tutorials/first-model.md)
   - [Maintaining a model: notes, deprecations and lake drift](tutorials/maintaining-a-model.md)
3. **Reviewers and curators**
   - [Reviewing and releasing](reviewers/review-and-release.md)
4. **Operators**
   - [Operator guide](operators/README.md)
5. **Reference** (agent developers)
   - [MCP reference: tools by stage, prompts, resources, scopes and errors](reference/README.md)

## The one-minute version

1. Ask the assistant: *"What should I do next on the `retail` model?"* It calls
   `get_next_steps` and tells you the model's stage and the most useful next step.
2. Agree, or ask for something else. The assistant prepares a **draft proposal**.
3. You read the summary and the warnings, then the assistant submits it for review.
4. A **reviewer** approves it with their own sign-in, and the change lands in OpenMetadata.
5. When the model is in a good state, a release tags it and publishes it.

Nothing you or the assistant do changes the model until a human reviewer approves it.

## Credits

The staged method follows Jessica Talisman's *Ontology Pipeline: A Framework for Building
Knowledge Infrastructures* (Technics Publications, 2026). *The Ontology Pipeline®* is her
registered trademark. Lakeloom is independent and not endorsed by her. Standards, tools and
their authors and licenses are listed in [THIRD_PARTY.md](../../THIRD_PARTY.md).
