# Local campaign workflow

Use `rednotebook --db <db> campaign create --file <ContentBrief.json>` to save a version.
`campaign generate <id> --version <n> --budget <optional-budget.json>` uses the configured local model. Defaults retain the generous research budget; a budget file may override selected fields.

Use `campaign revise-brief ... --file ...` for intent/material/series changes and `campaign revise-output ... --file ...` for a CampaignOutput (plan, post, claims). Inspect JSON schemas through `rednotebook schema content-brief` and `rednotebook schema campaign-output`. `campaign assess` reviews the current output without regenerating it.

`campaign check <id> --version <n>` separates `program`, `model`, and `author` checks. A model concern can be resolved only by an actual author review; no automatic conversion to verified facts. Unknown facts should be deleted, supported, or qualified in the copy before author confirmation.

The author records confirmation with `campaign confirm <id> --version <n> --hash <hash> --file <confirmation.json>`. The JSON contains `reviewer`, `note`, `facts_and_rights_checked: true`, and `resolved_issue_ids` for the current semantic questions. This creates a new unapproved version. The existing `review` command approves that exact new version/hash, after which `export` works. Neither action counts as independent research evaluation.

If no semantic model review was run, the author still reviews all factual claims and rights. A successful program check alone cannot unlock approval. Ordinary drafting and watermarked preview do not need confirmation.

Counterexamples:

- Device has arrived; projects are planned: describe arrival accurately and keep projects in future tense.
- Several projects share an overview: keep their different progress states; one project's completion changes no others.
- Professional readers know a term: do not force a glossary merely because a beginner once asked about it.
- Title introduces an object, cover supplies the payoff: assess the combination rather than rejecting either alone.
- A course teaser links the lesson: do not demand the full course in the teaser or claim the unread course was verified.
- No CTA or measurement plan: treat those checks as not applicable.
