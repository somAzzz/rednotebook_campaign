from copy import deepcopy

import pytest

from rednotebook.analysis.metrics import metric_delta, percentiles, rank_observations, safe_ratio
from rednotebook.domain.models import Metric
from rednotebook.errors import DomainError
from rednotebook.importing import import_file

BRIEF = "demo-cando-001"


def test_average_rank_golden_example():
    assert percentiles([1, 2, 2, 4]) == [0, 0.5, 0.5, 1]
    assert percentiles([3, 3, 3]) == [0.5, 0.5, 0.5]


def test_twenty_compatible_notes_rank(db, grant, fixture_data, write_rows):
    rows = fixture_data[1][10:30]
    import_file(db, write_rows(rows), grant)
    output = rank_observations(db.observations(BRIEF)[0])
    scores = sorted(n["score"] for n in output["notes"])
    assert len(scores) == 20 and scores[0] == 0 and scores[-1] == 100
    assert scores[1] == 5.3
    assert all(n["n"] == 20 for n in output["notes"])


def test_nineteen_no_score(db, grant, fixture_data, write_rows):
    import_file(db, write_rows(fixture_data[1][10:29]), grant)
    rows = rank_observations(db.observations(BRIEF)[0])["notes"]
    assert len(rows) == 19 and all(n["score"] is None for n in rows)
    assert {n["reason"] for n in rows} == {"insufficient_cohort"}


def test_missing_metric_unknown_scale_and_precision_separate(db, grant, fixture_data, write_rows):
    import_file(db, write_rows(fixture_data[1][:30]), grant)
    output = rank_observations(db.observations(BRIEF)[0])
    reasons = [n["reason"] for n in output["notes"]]
    assert reasons.count("incomplete_metrics") == 1
    assert reasons.count("unknown_comparison_fields") == 1
    assert reasons.count("insufficient_cohort") == 1  # abbreviated singleton
    assert reasons.count(None) == 27


def test_different_sampling_and_source_do_not_mix(db, grant, fixture_data, write_rows):
    raw = deepcopy(fixture_data[1][10:30])
    raw[-1]["sampling"][0].update(group="popular", run_id="popular", sort="popular")
    import_file(db, write_rows(raw), grant)
    result = rank_observations(db.observations(BRIEF)[0])
    assert sorted(c["n"] for c in result["cohorts"]) == [1, 19]
    assert all(n["score"] is None for n in result["notes"])


def test_latest_incomplete_snapshot_does_not_fall_back_to_old(db, grant, fixture_data, write_rows):
    raw = deepcopy(fixture_data[1][10:30])
    import_file(db, write_rows(raw), grant)
    later = deepcopy(raw[0])
    later["observed_at"] = "2026-09-15T12:00:00Z"
    later["metrics"] = []
    import_file(db, write_rows([later]), grant)
    result = rank_observations(db.observations(BRIEF)[0])
    assert all(n["score"] is None for n in result["notes"])
    assert len(result["notes"]) == 20


def test_age_and_followers_are_historical_snapshot_metadata(db, grant, fixture_data, write_rows):
    raw = deepcopy(fixture_data[1][10:30])
    import_file(db, write_rows(raw), grant)
    later = deepcopy(raw[0])
    later.update(followers=12000, observed_at="2026-09-15T12:00:00Z")
    for m in later["metrics"]:
        m["observed_at"] = later["observed_at"]
    import_file(db, write_rows([later]), grant)
    observations = db.observations(BRIEF)[0]
    old = [o for o in observations if o["observed_at"].startswith("2026-09-14")]
    assert all(n["score"] is not None for n in rank_observations(old)["notes"])
    assert all(n["score"] is None for n in rank_observations(observations)["notes"])


def test_negative_and_approximate_delta(fixture_data):
    raw = deepcopy(fixture_data[1][10]["metrics"][0])
    first = Metric.model_validate(raw)
    raw.update(value=10, raw_display="10", observed_at="2026-09-15T12:00:00Z")
    second = Metric.model_validate(raw)
    delta = metric_delta(first, second, same_source=True)
    assert delta["value"] == -20 and delta["decrease_anomaly"]
    raw.update(value=None, raw_display="1.2万", precision="abbreviated")
    approx = Metric.model_validate(raw)
    assert metric_delta(first, approx, same_source=True)["precision"] == "approximate"
    with pytest.raises(DomainError, match="incompatible"):
        metric_delta(first, second, same_source=False)
    with pytest.raises(DomainError, match="observation_time"):
        metric_delta(second, first, same_source=True)
    with pytest.raises(DomainError, match="incompatible"):
        metric_delta(first, second.model_copy(update={"definition": "other"}), same_source=True)


def test_safe_ratio_never_invents_denominator():
    assert safe_ratio(3, 0)["value"] is None
    assert safe_ratio(3, None)["reason"] == "missing_value"
    assert safe_ratio(0, 10)["value"] == 0
    with pytest.raises(DomainError):
        safe_ratio(2, float("nan"))
