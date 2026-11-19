# How Lakeloom works

This page explains the parts you meet in every conversation: where the model lives, how a
change travels from an idea to the published model, and who may do what.

## Where the model lives

- **OpenMetadata** holds the model. A Lakeloom *model* is an OpenMetadata glossary, which
  holds the business terms, their definitions, synonyms, hierarchy and related terms. It is
  paired with a native ontology that holds the formal part: attributes, typed relations
  and axioms. Lakeloom reads the model through OpenMetadata's RDF export.
- **Lakeloom's proposal log** holds every proposed change and its review history. It also
  holds the governance annotations OpenMetadata cannot store: scope notes, history notes and
  deprecations.
- **Git** (optional) holds a snapshot of each applied change and each tagged release.
- **A triple store** (optional) mirrors each release for SPARQL queries.

### The effective model

When you or an agent read a model, through `explain_concept`, `find_concepts`,
`export_model`, the validators, the analytics or `get_next_steps`, Lakeloom shows the
**effective model**: OpenMetadata's terms plus the scope notes, history notes and
deprecations from the proposal log. For example, a deprecated term is shown with a
*Deprecated* banner that names its replacement, and `find_concepts` marks it, so an agent
never mistakes it for a current term.

## The life of a change

Every change goes through a proposal. Lakeloom never edits the model directly.

```
 you + assistant          you                       reviewer (human)
 ───────────────          ───                       ────────────────
 draft  ──────────►  submitted  ──────────►  applied        (approve: written to OpenMetadata)
   │                     │      ├────────►  rejected       (with a reason)
   │                     │      └────────►  changes_requested ──► author edits, submits again
   └──► discarded        └── rebase_proposal if the model changed meanwhile
```

1. **Draft.** A write tool creates a draft proposal, for example `propose_add_concepts`,
   `propose_add_relation`, `propose_from_scan` or `propose_change`. The draft is also
   staged as an OpenMetadata change set, so OpenMetadata validates it. You see:
   - a **summary**: what is added, changed or removed;
   - **matches** with existing terms, so the same idea is not added twice;
   - **warnings**, for example a term without a definition, a vocabulary that is not
     finished yet, or a conflict with another open proposal.
2. **Submitted.** When you agree, the assistant calls `submit_proposal`. The proposal is
   now waiting for a reviewer.
3. **Reviewed.** A reviewer, signed in as themselves, calls `review_proposal`:
   - **approve** applies the change to OpenMetadata;
   - **reject** closes it with a reason;
   - **request changes** sends it back to you with a reason.

   Approve and reject show a preview first and need confirmation.
4. **Released** (optional). `create_release` tags the model's current state as a version,
   such as `1.4.0`, in Git. If the operator configured a triple store, the release is also
   synced there.

An agent can prepare, submit and explain changes. **It can never approve one.** Approval
requires a human signed in with their own identity. Lakeloom refuses a review from an agent
sign-in.

## Who may do what

Access is granted by **scopes** on your sign-in, optionally narrowed per model by
**groups**:

| Scope | Lets you |
|-------|----------|
| `lakeloom:read` | Read models, concepts, assets, analytics, `get_next_steps`, exports |
| `lakeloom:propose` | Create, edit, submit and discard proposals |
| `lakeloom:review` | Approve, reject or request changes on other people's proposals |
| `lakeloom:publish` | Create releases |
| `lakeloom:admin` | Create or pair models, operator tasks |

A model can limit who may propose, review or publish to named groups. `get_next_steps`
takes your rights into account: a step you cannot do yourself is marked *for someone else*.

## Untrusted text

Lake comments and OpenMetadata descriptions are written by many people and may contain
text that tries to instruct an AI agent. Lakeloom always returns such text inside quoted
blocks, marked as untrusted data, and its prompts remind the agent never to follow
instructions found in them.

Next: [Glossary](glossary.md) · [Tutorial: your first model](../tutorials/first-model.md)
