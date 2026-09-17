"""Author-led content contracts, independent of research and optional measurement."""

import json
from typing import Literal

from pydantic import Field, StrictBool, field_validator, model_validator

from rednotebook.creative import Editorial, Page
from rednotebook.domain.models import Contract, Identifier, Text, Timestamp


class AuthorStatement(Contract):
    id: Identifier
    text: Text = Field(max_length=5000)
    provider: Text = "author"
    status: Literal["unverified", "author_confirmed", "unknown"] = "unverified"
    support: list[str] = Field(default_factory=list, max_length=30)


class Hardware(Contract):
    id: Identifier
    description: Text = Field(max_length=2000)
    possession: Literal["unknown", "planned", "arrived", "borrowed", "owned"] = "unknown"


class Project(Contract):
    id: Identifier
    name: Text = Field(max_length=200)
    purpose: Text = Field(max_length=2000)
    status: Literal["unknown", "planned", "in_progress", "completed"] = "unknown"


class TestResult(Contract):
    id: Identifier
    project_id: Identifier
    description: Text = Field(max_length=3000)
    executed: Literal["unknown", "no", "yes"] = "unknown"
    support: list[str] = Field(default_factory=list, max_length=30)
    conditions: str = Field(default="", max_length=3000)


class Promise(Contract):
    id: Identifier
    text: Text = Field(max_length=2000)
    scope: Literal["in_post", "resource", "future", "result"]
    delivery_location: str | None = Field(default=None, max_length=2000)
    dependency_ids: list[str] = Field(default_factory=list, max_length=30)
    status: Literal["unknown", "planned", "delivered", "withdrawn"] = "unknown"


class Series(Contract):
    id: Identifier
    published_context: list[str] = Field(default_factory=list, max_length=30)
    candidate_topics: list[str] = Field(default_factory=list, max_length=30)
    promises: list[Promise] = Field(default_factory=list, max_length=30)


class MeasurementPlan(Contract):
    hypothesis: Text = Field(max_length=2000)
    primary_metric: Text = Field(max_length=200)
    guardrail_metrics: list[str] = Field(default_factory=list, max_length=10)
    baseline_status: Literal["unknown", "unavailable", "available"] = "unknown"
    baseline_ref: str | None = None
    planned_at: Timestamp | None = None
    observation_window: Literal["24h", "72h", "7d"] | None = None


class ContentBrief(Contract):
    schema_version: Literal[1] = 1
    id: Identifier
    synthetic: StrictBool = False
    creator_goal: Text = Field(max_length=5000)
    audience: Text = Field(max_length=2000)
    content_role: Text = Field(max_length=200)
    reader_takeaway: Text = Field(max_length=3000)
    statements: list[AuthorStatement] = Field(default_factory=list, max_length=100)
    hardware: list[Hardware] = Field(default_factory=list, max_length=30)
    projects: list[Project] = Field(default_factory=list, max_length=30)
    tests: list[TestResult] = Field(default_factory=list, max_length=100)
    must_include: list[str] = Field(default_factory=list, max_length=30)
    forbidden_claims: list[str] = Field(default_factory=list, max_length=30)
    reveal_now: list[str] = Field(default_factory=list, max_length=30)
    defer: list[str] = Field(default_factory=list, max_length=30)
    reader_entry_state: str | None = None
    reader_task: str | None = None
    hook: str | None = None
    term_glossary: dict[str, str] = Field(default_factory=dict)
    primary_action: str | None = None
    feedback_question: str | None = None
    # An absent key, unknown, and an explicit lack of need are distinct.
    optional_status: dict[str, Literal["unknown", "not_needed", "provided"]] = Field(
        default_factory=dict
    )
    assumptions: list[str] = Field(default_factory=list, max_length=30)
    research_run_ids: list[str] = Field(default_factory=list, max_length=20)
    selected_finding_ids: list[str] = Field(default_factory=list, max_length=100)
    source_ids: list[Identifier] = Field(default_factory=list, max_length=30)
    promises: list[Promise] = Field(default_factory=list, max_length=30)
    series: Series | None = None
    measurement: MeasurementPlan | None = None
    visual_preferences: str | None = None
    max_pages: int = Field(default=20, ge=1, le=20, strict=True)

    @model_validator(mode="after")
    def references(self):
        for group in (self.statements, self.hardware, self.projects, self.tests, self.promises):
            if len({x.id for x in group}) != len(group):
                raise ValueError("duplicate_content_identifier")
        if any(t.project_id not in {p.id for p in self.projects} for t in self.tests):
            raise ValueError("test_project_not_found")
        if self.series and len({p.id for p in self.series.promises}) != len(self.series.promises):
            raise ValueError("duplicate_series_promise_identifier")
        return self


class PostPage(Page):
    role: str | None = Field(default=None, max_length=200)
    visual_direction: str | None = Field(default=None, max_length=2000)


class PostDraft(Editorial):
    pages: list[PostPage] = Field(min_length=1, max_length=20)
    disclosure: str = Field(default="", max_length=2000)


class CampaignPlan(Contract):
    positioning: Text = Field(max_length=5000)
    reader_value: Text = Field(max_length=3000)
    structure: list[str] = Field(min_length=1, max_length=20)
    next_topics: list[str] = Field(default_factory=list, max_length=30)
    material_gaps: list[str] = Field(default_factory=list, max_length=30)


class ClaimUse(Contract):
    id: Identifier
    text: Text = Field(max_length=3000)
    dependency_ids: list[str] = Field(default_factory=list, max_length=30)
    # Mapping a claim to a reference never certifies semantic support.


class SemanticIssue(Contract):
    id: Identifier
    field: Text = Field(max_length=300)
    concern: Text = Field(
        max_length=3000, description="具体疑点或必要改进，不填写通过项、称赞或无需修改项"
    )
    status: Literal["needs_review", "warn"] = "needs_review"
    factual: StrictBool = True


class CampaignOutput(Contract):
    plan: CampaignPlan
    post: PostDraft

    @field_validator("plan", "post", mode="before")
    @classmethod
    def decode_nested_object(cls, value):
        # Local OpenAI-compatible providers can JSON-encode nested objects again.
        # Decode JSON only; all original field/length/extra-key checks still apply.
        return json.loads(value) if isinstance(value, str) else value

    claims: list[ClaimUse] = Field(default_factory=list, max_length=100)


class SemanticReview(Contract):
    issues: list[SemanticIssue] = Field(default_factory=list, max_length=50)
    limitation: Text = "模型辅助审阅，不是事实验证或人工批准。"


class AuthorConfirmation(Contract):
    reviewer: Text = Field(max_length=200)
    note: Text = Field(max_length=3000)
    resolved_issue_ids: list[str] = Field(default_factory=list, max_length=50)
    facts_and_rights_checked: Literal[True]
