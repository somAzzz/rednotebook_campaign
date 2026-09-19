"""Versioned contracts. Uncertain permission and missing metrics fail closed."""

import re
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    model_validator,
)


def utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def date_input(value):
    if not isinstance(value, (str, datetime)):
        raise ValueError("ISO-8601 timestamp with timezone required")
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("ISO-8601 timestamp with timezone required") from None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone required")
    return value


def nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("blank text is not allowed")
    return value


Timestamp = Annotated[datetime, BeforeValidator(date_input), AfterValidator(utc)]
Text = Annotated[str, Field(min_length=1), AfterValidator(nonblank)]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
Count = Annotated[StrictInt, Field(ge=0)]
Permission = Literal["allowed", "denied", "unknown"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class Permissions(Contract):
    storage: Permission = "unknown"
    local_analysis: Permission = "unknown"
    cloud_processing: Permission = "unknown"
    excerpt_export: Permission = "unknown"
    image_reuse: Permission = "unknown"
    automated_access: Permission = "unknown"


class SourceGrant(Contract):
    id: Identifier
    source_name: Text
    basis_ref: Text
    synthetic: StrictBool
    valid_from: Timestamp
    valid_until: Timestamp
    retention_days: Annotated[StrictInt, Field(ge=1, le=3650)] = 30
    permissions: Permissions

    @model_validator(mode="after")
    def period(self):
        if self.valid_until <= self.valid_from:
            raise ValueError("valid_until must follow valid_from")
        return self


class Asset(Contract):
    id: Identifier
    path: Text
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    owner: Text
    rights_ref: Text


class ProductFact(Contract):
    id: Identifier
    claim: Text
    status: Literal["verified", "unverified"] = "unverified"
    evidence_ids: list[Identifier] = Field(default_factory=list)
    product_version: Text | None = None
    verified_at: Timestamp | None = None

    @model_validator(mode="after")
    def proof(self):
        if self.status == "verified" and not (
            self.evidence_ids and self.product_version and self.verified_at
        ):
            raise ValueError("verified fact needs evidence, product_version and verified_at")
        return self


class ResearchBrief(Contract):
    schema_version: Literal["0.1-draft"] = "0.1-draft"
    synthetic: StrictBool
    id: Identifier
    product: Text
    audience: Text
    research_question: Text
    objective: Text
    keywords: list[Text] = Field(min_length=1)
    facts: list[ProductFact] = Field(default_factory=list)
    assets: list[Asset] = Field(default_factory=list)
    forbidden_claims: list[Text] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def links(self):
        for objects in (self.facts, self.assets):
            if len({x.id for x in objects}) != len(objects):
                raise ValueError("duplicate fact or asset ID")
        assets = {x.id for x in self.assets}
        for fact in self.facts:
            if not set(fact.evidence_ids) <= assets:
                raise ValueError("fact evidence must reference declared assets")
        return self


class Metric(Contract):
    name: Literal[
        "likes", "saves", "comments", "shares", "views", "impressions", "clicks", "replies"
    ]
    value: Annotated[float, Field(ge=0, le=2**53 - 1, allow_inf_nan=False, strict=True)] | None
    raw_display: str | None = None
    source_kind: Literal["public", "owner", "estimate"]
    precision: Literal["exact", "abbreviated", "estimated"]
    definition: Text
    unit: Literal["count"] = "count"
    observed_at: Timestamp
    missing_reason: Text | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_display(cls, data):
        if not isinstance(data, dict):
            return data
        data = dict(data)
        raw = data.get("raw_display")
        if isinstance(raw, str) and re.fullmatch(r"\d+(?:\.\d+)?万", raw):
            approx = float(raw[:-1]) * 10000
            if data.get("precision") != "abbreviated":
                raise ValueError("万 display requires abbreviated precision")
            if data.get("value") is None:
                data["value"] = approx
            elif data["value"] != approx:
                raise ValueError("value conflicts with raw_display")
        return data

    @model_validator(mode="after")
    def consistency(self):
        if self.value is None and not self.missing_reason:
            raise ValueError("null metric requires missing_reason")
        if self.value is not None and self.missing_reason:
            raise ValueError("present metric cannot have missing_reason")
        if self.source_kind == "estimate" and self.precision != "estimated":
            raise ValueError("estimate source requires estimated precision")
        if self.precision == "abbreviated" and not self.raw_display:
            raise ValueError("abbreviated metric requires raw_display")
        if self.value is not None and self.precision == "exact" and not self.value.is_integer():
            raise ValueError("exact count must be an integer")
        if (
            self.value is not None
            and self.raw_display is not None
            and self.precision == "exact"
            and re.fullmatch(r"\d+", self.raw_display)
            and int(self.raw_display) != self.value
        ):
            raise ValueError("exact value conflicts with raw_display")
        return self


class Sampling(Contract):
    run_id: Identifier
    brief_id: Identifier
    keyword: Text
    sort: Literal["latest", "popular", "manual", "unknown"]
    group: Literal["recent", "popular", "unknown"]
    truncated: StrictBool = False


class Coverage(Contract):
    reported_total: Count | None = None
    mode: Literal["hot", "latest", "mixed", "unknown"] = "unknown"
    includes_replies: StrictBool = False
    truncated: StrictBool = False
    failure: Text | None = None


class MediaProvenance(Contract):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: Literal["image", "video"]
    processor: Text
    human_verified: Literal[False] = False
    coverage: Literal["single_image", "sampled_frames_only", "whole_image"]


class EvidenceInput(Contract):
    source_id: Identifier
    external_id: Text | None = None
    kind: Literal["note", "comment"]
    parent_external_id: Text | None = None
    title: str = ""
    text: str
    text_origin: Literal["unspecified", "body", "title_fallback", "empty_body", "comment"] = (
        "unspecified"
    )
    locator: Text
    author_id: Text | None = None
    observed_at: Timestamp
    published_at: Timestamp | None = None
    topic: Text | None = None
    format: Literal["text", "image_text", "video"] | None = None
    media_text: str = Field(default="", max_length=50000)
    media_provenance: list[MediaProvenance] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def media_origin(self):
        if self.media_text and not self.media_provenance:
            raise ValueError("media_text_requires_provenance")
        return self

    followers: Count | None = None
    synthetic: StrictBool
    is_reply: StrictBool = False
    is_author_reply: StrictBool = False
    metrics: list[Metric] = Field(default_factory=list)
    sampling: list[Sampling] = Field(min_length=1)
    coverage: Coverage | None = None

    @model_validator(mode="after")
    def consistency(self):
        if not self.text.strip() and not (
            self.kind == "note" and self.format == "image_text" and self.text_origin == "empty_body"
        ):
            raise ValueError("blank_text_requires_explicit_image_only_note")
        if self.kind == "comment" and not self.parent_external_id:
            raise ValueError("comment requires root note parent_external_id")
        if self.kind == "note" and (
            self.parent_external_id or self.is_reply or self.is_author_reply
        ):
            raise ValueError("note cannot be a reply or have a parent")
        if self.kind == "comment" and (
            self.coverage or any(m.name not in {"likes", "replies"} for m in self.metrics)
        ):
            raise ValueError("comments allow only likes and replies metrics, not note coverage")
        if self.published_at and self.published_at > self.observed_at:
            raise ValueError("published_at is later than observation")
        if len({m.name for m in self.metrics}) != len(self.metrics):
            raise ValueError("duplicate metric name within observation")
        if any(m.observed_at != self.observed_at for m in self.metrics):
            raise ValueError("metric timestamps must match the evidence observation")
        return self
