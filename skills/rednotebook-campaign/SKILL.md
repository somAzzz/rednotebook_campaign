---
name: rednotebook-campaign
description: Plan and revise author-led posts, series openers, tutorials and follow-up content with RedNotebook. Use for content positioning or turning available research/materials into a reader-facing draft; public-note collection belongs to the research workflow.
---

# RedNotebook Campaign

Preserve the author's chosen goal, audience and topic. One reader task can include several project previews; do not substitute the best-documented project for the requested series. A tutorial request calls for useful detail, not an opener template.

## Working with RedNotebook

1. Recover relevant drafts with `list_history(kind="drafts")` and `read_draft`. Assemble a `ContentBrief` from the conversation. Read the `rednotebook://schemas/content-brief` resource, or use `rednotebook schema content-brief`; never send this contract to `research_notes`.
2. Keep author statements, hardware possession, each project's progress, and specific test results separate. Mark assumptions and missing facts. Research is optional; declare source IDs for source-derived material and research run IDs when using stored research. Source permissions remain enforced.
3. Call `create_campaign` to save a scaffold. This is not finished copy. When local-model generation is requested, call `generate_campaign`; it transmits the stored context, saves the draft, then runs semantic review. On failure, read the returned saved version instead of starting again. Browser pauses never prohibit author-only planning and never authorize resuming collection.
4. Inspect plan, post, claim dependencies and gaps with `read_draft`. Revise the brief with `revise_campaign_brief`, or plan/post together with `revise_campaign_output`. Use `revise_draft` for copy only. Add original assets through the existing CLI, then regenerate or reference the returned asset IDs. Never label public images as author-owned.
5. Use `check_campaign`. For caller_analysis, write plan/post with `revise_campaign_output` and submit your semantic review through `submit_campaign_review`, binding the exact version/content_hash from `read_draft`; use the campaign-output and semantic-review schema resources. For explicitly requested local_model_analysis, optionally use `generate_campaign(assess_only=true)`. Program checks prove structure and dependency availability only. Model concerns remain suggestions or questions. Resolve factual concerns by reviewing the material, correcting the draft, or explicitly qualifying claims.
6. Preview through `preview_draft`. Author confirmation and exact-version approval remain separate CLI actions; do not invent either. Changes to brief, draft or assets require fresh confirmation and approval. Export is local delivery, never permission to publish or interact with accounts.

For a research-then-planning request, complete the research Skill’s 30–40 candidate scan and 3–5 selected detail reads before creating a campaign. Set `research_mode="research_then_plan"` and the completed `topic_job_id`; the program attaches the stored research and source dependencies. For explicitly search-only assistance use `research_mode="search_only"` and `search_job_id`. Use `research_mode="author_only"` for author materials alone. Do not downgrade a requested research task to title-based drafting because generation is available.

When author-approved research would help, pass the author's goal and reader task to the research workflow's `plan_search` user_goal, keeping the device/topic as primary_query. Related-category results are contextual references, not evidence of the exact product's capabilities. Do not turn an author-only drafting request into unsolicited browsing.

Before research-based drafting, inspect per-note completeness and actual image reading; search cards, title fallback and partial captures cannot support a claim of full-note research. Sparse sampled comments only support qualified sample observations.

## Editorial judgment

Title, cover and body can complement one another. Choose page count and structure for the available material, within the returned schema's limits. No mandatory CTA, feedback question, baseline or publication time for ordinary drafting. A series opener can deliver an understandable overview, a story, or current progress; it need not contain all later tutorials.

Distinguish current-post delivery, linked resources, future promises and result claims. Missing images are unknown, not proof of absent delivery. Internal candidate topics are not promises. Research hypotheses and machine-extracted text remain labeled as such; reference existence is not factual verification.

Keep operational checks out of reader-facing copy: state a relevant plan or factual boundary naturally where needed, without repeating “not tested / not paid / no promise” on every page. Review repetition and useful detail as well as factual consistency.

For commands and review transitions, read [references/workflow.md](references/workflow.md). Contract schemas and application prompts are authoritative for field names; avoid maintaining another competing rule set here.
