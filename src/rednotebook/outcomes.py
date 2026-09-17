"""Observed outcomes with explicit ownership and comparison windows; no causal scoring."""

from typing import Literal

from pydantic import Field, model_validator

from rednotebook.creative import load_bundle
from rednotebook.domain.models import Contract, Identifier, Metric, Text, Timestamp
from rednotebook.errors import DomainError
from rednotebook.util import canonical, digest


class Outcome(Contract):
    bundle_id: Text
    bundle_version: int = Field(ge=1, strict=True)
    source_id: Identifier
    note_url: Text
    ownership: Literal["own", "public"]
    published_at: Timestamp
    observed_at: Timestamp
    window: Literal["24h", "72h", "7d"]
    metrics: list[Metric] = Field(min_length=1)
    production_minutes: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    baseline_minutes: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    quality_rating: int | None = Field(default=None, ge=1, le=5, strict=True)

    @model_validator(mode="after")
    def valid(self):
        if self.observed_at < self.published_at:
            raise ValueError("observation_before_publication")
        if len({m.name for m in self.metrics}) != len(self.metrics):
            raise ValueError("duplicate_metric")
        for m in self.metrics:
            if m.observed_at != self.observed_at:
                raise ValueError("metric_time_mismatch")
            if self.ownership == "public" and m.source_kind == "owner":
                raise ValueError("public_cannot_be_owner_metric")
        return self


def import_outcome(db, outcome):
    load_bundle(db, outcome.bundle_id, outcome.bundle_version)
    db.require_source(outcome.source_id, "storage")
    payload = outcome.model_dump(mode="json")
    key = digest([outcome.source_id, outcome.note_url, payload["observed_at"], outcome.window])
    existing = db.conn.execute("SELECT payload FROM outcomes WHERE id=?", (key,)).fetchone()
    if existing and existing[0] != canonical(payload):
        raise DomainError("outcome_snapshot_conflict")
    with db.conn:
        db.conn.execute(
            "INSERT OR IGNORE INTO outcomes VALUES (?,?,?,?,?)",
            (key, outcome.bundle_id, outcome.bundle_version, outcome.source_id, canonical(payload)),
        )
    return {"id": key, "duplicate": bool(existing)}


def retrospective(db, bundle_id, version):
    load_bundle(db, bundle_id, version)
    items = []
    for row in db.conn.execute(
        "SELECT * FROM outcomes WHERE bundle_id=? AND bundle_version=?", (bundle_id, version)
    ):
        db.require_source(row["source_id"])
        o = Outcome.model_validate_json(row["payload"])
        age = (o.observed_at - o.published_at).total_seconds() / 3600
        center, tolerance = {"24h": (24, 2), "72h": (72, 6), "7d": (168, 24)}[o.window]
        items.append(
            o.model_dump(mode="json")
            | {
                "actual_age_hours": age,
                "within_window": abs(age - center) <= tolerance,
                "eligible_for_owner_comparison": o.ownership == "own"
                and abs(age - center) <= tolerance,
                "time_saved_fraction": None
                if o.production_minutes is None or o.baseline_minutes is None
                else 1 - o.production_minutes / o.baseline_minutes,
            }
        )
    return {
        "bundle_id": bundle_id,
        "version": version,
        "observations": items,
        "missing_windows": sorted(
            {"24h", "72h", "7d"} - {i["window"] for i in items if i["within_window"]}
        ),
        "interpretation": "descriptive_only_no_causal_attribution",
        "pilot_acceptance": "pending_real_baselines_and_author_evaluation",
    }
