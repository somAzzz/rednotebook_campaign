"""Score explicit reviewer labels without generating labels or inventing a holdout set."""

from typing import Literal

from pydantic import Field

from rednotebook.domain.models import Contract, Text
from rednotebook.errors import DomainError
from rednotebook.research.store import read_run


class Label(Contract):
    finding_id: Text
    support: Literal["supported", "partial", "unsupported"]
    severe_fabrication: bool
    reviewer: Text
    note: Text


class Evaluation(Contract):
    run_id: Text
    split: Literal["development", "holdout"]
    labels: list[Label] = Field(min_length=1)


def evaluate(db, evaluation):
    report = read_run(db, evaluation.run_id)
    ids = {f["id"] for f in report.get("findings", [])}
    labels = evaluation.labels
    if len({label.finding_id for label in labels}) != len(labels) or any(
        label.finding_id not in ids for label in labels
    ):
        raise DomainError("evaluation_labels_invalid")
    covered = {label.finding_id for label in labels}
    precision = sum(label.support == "supported" for label in labels) / len(labels)
    severe = sum(label.severe_fabrication for label in labels)
    eligible = len(labels) >= 30 and evaluation.split == "holdout" and covered == ids
    return {
        "run_id": evaluation.run_id,
        "labelled": len(labels),
        "unlabelled": len(ids - covered),
        "support_precision": precision,
        "severe_fabrications": severe,
        "threshold_passed": precision >= 0.9 and severe == 0,
        "benchmark_sample_eligible": eligible,
        "a11_status": "pending_independent_review_and_three_runs",
        "labels_are_reviewer_supplied": True,
    }
