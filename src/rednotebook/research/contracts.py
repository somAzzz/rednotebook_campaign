from typing import Literal

from pydantic import Field

from rednotebook.domain.models import Contract, Text


class FindingDraft(Contract):
    claim: str = Field(min_length=1, max_length=700)
    category: Literal["discussion", "expression"]
    support_ids: list[str] = Field(min_length=1, max_length=8)
    counter_ids: list[str] = Field(default_factory=list, max_length=8)
    counter_search: Literal["provided", "not_found_in_batch", "not_assessed"]
    limitation: str = Field(min_length=1, max_length=700)


class BatchDraft(Contract):
    findings: list[FindingDraft] = Field(default_factory=list, max_length=5)
    gaps: list[str] = Field(default_factory=list, max_length=5)


class Citation(Contract):
    evidence_id: Text
    revision: int = Field(ge=1, strict=True)
    source_id: Text
    field: Literal["title", "text", "media_text"]
    start: int = Field(ge=0, strict=True)
    end: int = Field(ge=1, strict=True)
    excerpt: Text
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
