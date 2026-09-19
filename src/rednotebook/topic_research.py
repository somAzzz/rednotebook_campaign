"""Bounded topic orchestration; selection judgments are supplied, not invented as facts."""

from pydantic import Field, model_validator

from rednotebook.adapters.browser import note_url
from rednotebook.domain.models import Contract, Text
from rednotebook.errors import DomainError


class SelectedNote(Contract):
    note_id: Text
    reason: Text = Field(max_length=2000)
    question: Text = Field(max_length=2000)
    coverage: list[Text] = Field(min_length=1, max_length=10)


class TopicSelection(Contract):
    search_job_id: Text
    notes: list[SelectedNote] = Field(min_length=1, max_length=5)
    gaps: list[Text] = Field(default_factory=list, max_length=20)
    sample_exception: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_sample(self):
        if len({n.note_id for n in self.notes}) != len(self.notes):
            raise ValueError("duplicate_selected_note")
        if len(self.notes) < 3 and not self.sample_exception.strip():
            raise ValueError("small_sample_reason_required")
        return self


def resolve_search(service, source_id, selection):
    job = service.get(selection.search_job_id, True)
    result = job.get("result") or {}
    if (
        job["source_id"] != source_id
        or job["operation"] != "search_plan"
        or job["state"] not in {"complete", "partial"}
        or not result.get("execution_complete")
    ):
        raise DomainError("topic_search_not_ready")
    candidates = {c["note_id"]: c for c in result["candidates"]}
    if any(n.note_id not in candidates for n in selection.notes):
        raise DomainError("topic_candidate_not_found")
    if any(not candidates[n.note_id].get("collect_url") for n in selection.notes):
        raise DomainError("topic_candidate_not_collectable")
    if any(note_url(candidates[n.note_id]["collect_url"])[0] != n.note_id for n in selection.notes):
        raise DomainError("topic_candidate_identity_mismatch")
    return result, candidates


async def execute_topic(service, ident, request):
    from rednotebook.browser_service import JobRequest

    search, candidates = resolve_search(service, request.source_id, request.topic_selection)
    entries = []
    caller = request.analysis_mode == "caller_analysis"
    if request.resume_topic_job_id:
        previous = service.get(request.resume_topic_job_id, True)
        saved = previous.get("result") or {}
        service.update(ident, "running", saved)
        entries = list(saved.get("notes", []))
        # Reconcile a child that completed just before its parent was interrupted.
        for entry in entries:
            child_id = request.replacement_jobs.get(entry["note_id"], entry["job_id"])
            child = service.get(child_id, True)
            raw = service.db.conn.execute(
                "SELECT request_json FROM browser_jobs WHERE id=?", (child_id,)
            ).fetchone()[0]
            parsed = JobRequest.model_validate_json(raw)
            captured = parsed
            if parsed.capture_id:
                capture_job = service.get(parsed.capture_id)
                if capture_job["source_id"] != request.source_id:
                    raise DomainError("topic_child_requires_explicit_recovery")
                capture_raw = service.db.conn.execute(
                    "SELECT request_json FROM browser_jobs WHERE id=?", (parsed.capture_id,)
                ).fetchone()[0]
                captured = JobRequest.model_validate_json(capture_raw)
            expected_brief = f"topic-{entry['original_job_id']}"
            if (
                child["source_id"] != request.source_id
                or child["operation"] != "workflow"
                or parsed.brief_id != expected_brief
                or not captured.url
                or note_url(captured.url)[0] != entry["note_id"]
                or child["state"] != "complete"
                or (not caller and not (child.get("result") or {}).get("run_id"))
            ):
                raise DomainError("topic_child_requires_explicit_recovery")
            entry.update(job_id=child_id, state="complete", result=child["result"])

    def snapshot():
        finished = [
            e
            for e in entries
            if e["state"] == "complete"
            and (e.get("result") or {}).get("completeness", {}).get("capture_gate") == "passed"
            and (
                caller
                or (e.get("result") or {}).get("completeness", {}).get("image_reading")
                in {"complete", "not_applicable"}
            )
        ]
        return {
            "state": "partial",
            "requested_depth": "research_then_plan",
            "achieved_depth": ("collected_evidence" if caller else "detail_research")
            if finished
            else "search_only",
            "analysis_mode": request.analysis_mode,
            "evidence_prepared": caller and len(finished) == len(request.topic_selection.notes),
            "search_complete": True,
            "candidate_count": len(candidates),
            "scan_target": [30, 40],
            "scan_target_met": 30 <= len(candidates) <= 40,
            "selection": request.topic_selection.model_dump(),
            "selected_count": len(request.topic_selection.notes),
            "completed_count": len(finished),
            "skipped_video_count": sum(
                e["state"] == "skipped"
                and (e.get("result") or {}).get("reason") == "video_deferred"
                for e in entries
            ),
            "notes": entries,
            "research_run_ids": [
                e["result"]["run_id"] for e in finished if e["result"].get("run_id")
            ],
            "source_ids": [request.source_id],
            "research_complete": not caller and len(finished) == len(request.topic_selection.notes),
            "campaign_ready": not caller and len(finished) == len(request.topic_selection.notes),
            "limitations": ["bounded_sample_not_representative", "research_is_hypothesis"],
        }

    service.update(ident, "running", snapshot())
    completed = {e["note_id"] for e in entries}
    for chosen in request.topic_selection.notes:
        if chosen.note_id in completed:
            continue
        candidate = candidates[chosen.note_id]
        # Each note gets a separate brief so later research cannot silently mix prior samples.
        from uuid import uuid4

        child_id = str(uuid4())
        brief = request.brief.model_copy(
            update={
                "id": f"topic-{child_id}",
                "research_question": request.brief.research_question
                + "\n本篇重点："
                + chosen.question,
            }
        )
        child = request.model_copy(
            update={
                "operation": "workflow",
                "brief": brief,
                "brief_id": brief.id,
                "url": candidate["collect_url"],
                "keyword": search["plan"]["intent"]["primary_query"],
                "topic_selection": None,
                "resume_topic_job_id": None,
                "replacement_jobs": {},
                "capture_id": "",
                "allow_partial": False,
            }
        )
        service.submit(child, reset_failure_pause=False, parent_job_id=ident, job_id=child_id)
        entry = {
            **chosen.model_dump(),
            "public_url": candidate.get("public_url"),
            "job_id": child_id,
            "original_job_id": child_id,
            "research_brief": brief.model_dump(mode="json"),
            "state": "running",
            "result": None,
        }
        entries.append(entry)
        service.update(ident, "running", snapshot())
        service.progress(
            ident,
            "detail_research",
            selected_count=len(request.topic_selection.notes),
            completed_count=len(completed),
        )
        await service.tasks[child_id]
        service.require(request.source_id)
        outcome = service.get(child_id, True)
        entry.update(
            state=outcome["state"], result=outcome.get("result"), error_code=outcome["error_code"]
        )
        service.update(ident, "running", snapshot())
        if (
            outcome["state"] == "skipped"
            and (outcome.get("result") or {}).get("reason") == "video_deferred"
        ):
            continue
        if outcome["state"] != "complete" or (
            not caller and not (outcome.get("result") or {}).get("run_id")
        ):
            return snapshot() | {
                "next_action": "recover_child_then_resume_topic",
                "stopped_job_id": child_id,
            }
        completed.add(chosen.note_id)
    result = snapshot()
    return result | {
        "state": "complete"
        if result["campaign_ready"] or result["evidence_prepared"]
        else "partial",
        "next_action": "read_evidence_bundle" if caller else "read_research",
        "execution_complete": True,
    }
