import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from rednotebook.domain.models import EvidenceInput, Metric, ResearchBrief, SourceGrant
from rednotebook.errors import DomainError


@pytest.mark.parametrize("field,value", [("audience", ""), ("objective", " "), ("keywords", [])])
def test_brief_missing_fields(field, value):
    brief = json.loads(Path("examples/brief.cando.synthetic.json").read_text())
    brief[field] = value
    with pytest.raises(ValidationError):
        ResearchBrief.model_validate(brief)


def test_verified_fact_requires_proof():
    brief = json.loads(Path("examples/brief.cando.synthetic.json").read_text())
    brief["facts"][0]["status"] = "verified"
    with pytest.raises(ValidationError):
        ResearchBrief.model_validate(brief)


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, "12"])
def test_invalid_metrics(fixture_data, value):
    metric = deepcopy(fixture_data[1][0]["metrics"][0])
    metric["value"] = value
    with pytest.raises(ValidationError):
        Metric.model_validate(metric)


def test_metric_zero_missing_and_abbreviation(fixture_data):
    notes = fixture_data[1]
    assert Metric.model_validate(notes[0]["metrics"][0]).value == 0
    short = Metric.model_validate(notes[1]["metrics"][0])
    assert (
        short.value == 12000 and short.raw_display == "1.2万" and short.precision == "abbreviated"
    )
    assert Metric.model_validate(notes[2]["metrics"][1]).value is None
    bad = deepcopy(notes[1]["metrics"][0])
    bad["precision"] = "exact"
    with pytest.raises(ValidationError):
        Metric.model_validate(bad)


@pytest.mark.parametrize("date", ["2026-09-14T12:00:00", "nonsense", 123, True])
def test_timestamps_require_timezone(fixture_data, date):
    record = deepcopy(fixture_data[1][0])
    record["observed_at"] = date
    with pytest.raises(ValidationError):
        EvidenceInput.model_validate(record)


def test_timezone_normalization(fixture_data):
    record = deepcopy(fixture_data[1][0])
    record["observed_at"] = "2026-09-14T14:00:00+02:00"
    parsed = EvidenceInput.model_validate(record)
    assert parsed.observed_at.hour == 12


def test_grant_unknown_storage_and_expiry(db, fixture_data, clock):
    raw = deepcopy(fixture_data[0])
    raw["permissions"]["storage"] = "unknown"
    with pytest.raises(DomainError, match="storage_permission_required"):
        db.register_grant(SourceGrant.model_validate(raw))
    raw = deepcopy(fixture_data[0])
    raw["valid_until"] = (clock[0] - timedelta(hours=1)).isoformat()
    with pytest.raises(DomainError, match="grant_outside_valid_period"):
        db.register_grant(SourceGrant.model_validate(raw))


def test_exact_display_cannot_conflict_with_value(fixture_data):
    metric = deepcopy(fixture_data[1][0]["metrics"][0])
    metric.update(value=12, raw_display="13")
    with pytest.raises(ValidationError):
        Metric.model_validate(metric)
