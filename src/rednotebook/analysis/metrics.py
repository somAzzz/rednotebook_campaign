import math
from collections import defaultdict
from datetime import datetime

from rednotebook.domain.models import Metric
from rednotebook.errors import DomainError
from rednotebook.util import digest

WEIGHTS = {"likes": 0.25, "saves": 0.45, "comments": 0.30}


def percentiles(values: list[float]) -> list[float]:
    if len(values) < 2 or any(not math.isfinite(v) for v in values):
        raise DomainError("percentile_requires_two_finite_values")
    positions = defaultdict(list)
    for rank, value in enumerate(sorted(values), start=1):
        positions[value].append(rank)
    return [(sum(positions[v]) / len(positions[v]) - 1) / (len(values) - 1) for v in values]


def cohort_key(observation):
    content = observation["content"]
    if any(content.get(k) is None for k in ("topic", "format", "published_at", "followers")):
        return None, "unknown_comparison_fields"
    elapsed = (
        datetime.fromisoformat(observation["observed_at"])
        - datetime.fromisoformat(content["published_at"])
    ).total_seconds()
    if elapsed < 0:
        return None, "invalid_publication_time"
    days = math.floor(elapsed / 86400)
    age = "0-7" if days <= 7 else "8-30" if days <= 30 else "31-90" if days <= 90 else "91+"
    followers = content["followers"]
    size = "<1k" if followers < 1000 else "1k-10k" if followers < 10000 else "10k+"
    metrics = {m["name"]: m for m in observation["metrics"]}
    if any(name not in metrics or metrics[name]["value"] is None for name in WEIGHTS):
        return None, "incomplete_metrics"
    signature = tuple(
        (
            metrics[k]["source_kind"],
            metrics[k]["precision"],
            metrics[k]["definition"],
            metrics[k]["unit"],
        )
        for k in WEIGHTS
    )
    sampling_groups = tuple(sorted({s["sampling"]["group"] for s in observation["samples"]}))
    # Never mix synthetic/real records, independently contracted sources, or sampling populations.
    return (
        content["synthetic"],
        observation["source_id"],
        content["topic"],
        content["format"],
        age,
        size,
        sampling_groups,
        signature,
    ), None


def rank_observations(observations, minimum=20):
    if minimum < 20:
        raise DomainError("minimum_cohort_must_be_at_least_20")
    latest = {}
    for obs in observations:
        if obs["kind"] == "note" and (
            obs["id"] not in latest or obs["observed_at"] > latest[obs["id"]]["observed_at"]
        ):
            latest[obs["id"]] = obs
    groups, rows = defaultdict(list), []
    for obs in latest.values():
        key, reason = cohort_key(obs)
        if reason:
            rows.append({"evidence_id": obs["id"], "score": None, "reason": reason})
        else:
            groups[key].append(obs)
    cohorts = []
    for key, group in groups.items():
        cohort_id = digest(key)
        cohorts.append({"id": cohort_id, "n": len(group), "key": key})
        if len(group) < minimum:
            rows.extend(
                {
                    "evidence_id": obs["id"],
                    "cohort_id": cohort_id,
                    "n": len(group),
                    "score": None,
                    "reason": "insufficient_cohort",
                }
                for obs in group
            )
            continue
        ranks = {
            name: percentiles(
                [next(m["value"] for m in obs["metrics"] if m["name"] == name) for obs in group]
            )
            for name in WEIGHTS
        }
        for index, obs in enumerate(group):
            p = {name: ranks[name][index] for name in WEIGHTS}
            rows.append(
                {
                    "evidence_id": obs["id"],
                    "observed_at": obs["observed_at"],
                    "cohort_id": cohort_id,
                    "n": len(group),
                    "percentiles": p,
                    "score": round(100 * sum(WEIGHTS[k] * p[k] for k in WEIGHTS), 1),
                    "reason": None,
                }
            )
    return {
        "algorithm": "research-ranking-v1",
        "weights": WEIGHTS,
        "minimum_cohort": minimum,
        "interpretation": "研究排序分；不是爆火概率或传播效率",
        "cohorts": sorted(cohorts, key=lambda c: c["id"]),
        "notes": sorted(rows, key=lambda r: r["evidence_id"]),
    }


def metric_delta(before: Metric, after: Metric, *, same_source: bool):
    if not same_source or any(
        getattr(before, k) != getattr(after, k)
        for k in ("name", "source_kind", "definition", "unit")
    ):
        raise DomainError("incompatible_snapshots")
    if after.observed_at <= before.observed_at:
        raise DomainError("observation_time_must_increase")
    if before.value is None or after.value is None:
        return {"value": None, "reason": "missing_value"}
    value = after.value - before.value
    return {
        "value": value,
        "decrease_anomaly": value < 0,
        "precision": "exact" if before.precision == after.precision == "exact" else "approximate",
        "interval_seconds": (after.observed_at - before.observed_at).total_seconds(),
    }


def safe_ratio(numerator: float | None, denominator: float | None):
    if numerator is None or denominator is None:
        return {"value": None, "reason": "missing_value"}
    if (
        not math.isfinite(numerator)
        or not math.isfinite(denominator)
        or numerator < 0
        or denominator < 0
    ):
        raise DomainError("invalid_ratio_values")
    if denominator == 0:
        return {"value": None, "reason": "zero_denominator"}
    return {"value": numerator / denominator, "reason": None}
