# Topic research handoff

Read `rednotebook://schemas/topic-selection` and the ResearchBrief schema. A selection uses:

```json
{
  "search_job_id": "completed-search-plan-job-id",
  "notes": [
    {"note_id": "returned-note-id", "reason": "Why this candidate", "question": "What to examine", "coverage": ["user-project-or-question"]}
  ],
  "gaps": ["uncovered question"],
  "sample_exception": "Required only if fewer than three selected notes"
}
```

Normally supply 3–5 entries, not the single illustrative entry above. URLs are resolved from the saved search result; never copy signed URLs into selection prose. Supply `source_id`, `selection`, and the user's `brief` to `research_topic`. Model budget remains the generous per-note deep budget. Poll the durable job; each note points to its child workflow and captured/researched scope.

`search_complete` and `campaign_ready` are separate. Only complete child workflows count toward `completed_count`. A stopped result names `stopped_job_id`; inspect that child with `get_job`. Resolve access pauses outside MCP when needed. For a saved capture, explicitly continue `research_workflow` using the entry's saved `research_brief` and capture ID; do not create a different brief. `allow_partial=true` produces a limited report and may still remain partial. It cannot certify completion of missing reading.

Once the recovered child is complete, call `resume_topic_research(job_id, replacement_jobs={note_id: completed_child_job_id})`. Already completed children are reused. Resume preserves the selection and research question. To change selection, create a new topic job and explain why; currently it does not reuse children across different topic selections.

Pass the completed topic job into ContentBrief.topic_job_id with research_mode=research_then_plan. Original sources and research runs are resolved by the program and participate in permission checks and revocation cleanup. An execution-complete topic remains a bounded, provisional research sample, never a platform-wide conclusion.
