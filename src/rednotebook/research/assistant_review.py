"""Explicit assistant submissions, never an automatic cloud fallback.

An operator-recorded, capture-bound consent and matching audit event are required.
All sidecars live under the original source-managed capture directory and expire
with the source. Source grants and original captures remain immutable.
"""

import hashlib
import json
from datetime import datetime
from uuid import uuid4

from pydantic import Field

from rednotebook.analysis.quality import quality_report
from rednotebook.domain.models import Contract, ResearchBrief
from rednotebook.enrichment import enrich
from rednotebook.errors import DomainError
from rednotebook.importing import import_file
from rednotebook.research.contracts import BatchDraft
from rednotebook.research.gateway import EvidenceGateway
from rednotebook.research.runner import resolved_findings, resolved_sources, validate_draft
from rednotebook.research.store import checkpoint, read_run, start_run
from rednotebook.topic_research import TopicSelection, resolve_search
from rednotebook.util import canonical, digest, stamp


class PageReading(Contract):
    position: int = Field(ge=1, le=20, strict=True)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(min_length=1, max_length=12000)
    key_content_readable: bool = Field(strict=True)


def read_json(path):
    if not path.is_file() or path.is_symlink():
        raise DomainError("assistant_review_file_missing")
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        raise DomainError("assistant_review_file_invalid") from None


def consent_action(processor, capture_id):
    return (
        "scoped_codex_consent:"
        if processor == "codex-assistant"
        else "scoped_agent_consent:" + processor + ":"
    ) + capture_id


def authorized_capture(service, capture_id):
    from rednotebook.browser_service import capture_path
    from rednotebook.capture_review import completeness

    job = service.get(capture_id, True)
    if job["operation"] not in {"collect", "workflow"} or job["state"] in {"queued", "running"}:
        raise DomainError("capture_not_ready")
    grant = service.require(job["source_id"])
    target = capture_path(service.db, capture_id)
    processor = getattr(service, "caller_processor", "codex-assistant")
    if grant.permissions.cloud_processing == "allowed":
        consent = {
            "source_id": grant.id,
            "processor": processor,
            "permission": "cloud_processing",
            "decision": "allowed",
            "scope": "source_grant",
            "grant_sha256": digest(grant.model_dump(mode="json")),
        }
    else:
        consent = read_json(target / "assistant-consent.json")
        expected = {
            "source_id": grant.id,
            "capture_id": capture_id,
            "processor": processor,
            "permission": "cloud_processing",
            "decision": "allowed",
            "scope": "this_capture_only",
        }
        if (
            any(consent.get(k) != v for k, v in expected.items())
            or not service.db.conn.execute(
                "SELECT 1 FROM audit WHERE source_id=? AND action=?",
                (grant.id, consent_action(processor, capture_id)),
            ).fetchone()
        ):
            raise DomainError("capture_cloud_consent_required")
        try:
            expiry = datetime.fromisoformat(consent["expires_at"])
            valid = service.db.clock() < expiry <= grant.valid_until
        except (KeyError, ValueError, TypeError):
            valid = False
        if not valid:
            raise DomainError("capture_cloud_consent_expired")
        for name in ("evidence", "manifest"):
            path = target / f"{name}.json"
            actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            if path.exists():
                read_json(path)
            if actual != consent.get(f"{name}_sha256"):
                raise DomainError("capture_cloud_consent_hash_mismatch")
    captured = read_json(target / "checkpoint.json")
    if captured.get("state") != "complete" or completeness(captured)["capture_gate"] != "passed":
        raise DomainError("capture_completeness_requires_review")
    records = read_json(target / "evidence.json")
    manifest = (
        read_json(target / "manifest.json")
        if (target / "manifest.json").exists()
        else {
            "source_id": grant.id,
            "note_external_id": next(
                (r["external_id"] for r in records if r["kind"] == "note"), None
            ),
            "declared_total": 0,
            "images": [],
        }
    )
    notes = [r for r in records if r["kind"] == "note"]
    if (
        any(r["source_id"] != grant.id for r in records)
        or len(notes) != 1
        or manifest["source_id"] != grant.id
        or manifest["note_external_id"] != notes[0]["external_id"]
        or manifest["declared_total"] != captured["declared_total"]
    ):
        raise DomainError("capture_review_identity_mismatch")
    if manifest["images"] != captured.get("images", []):
        raise DomainError("capture_review_manifest_mismatch")
    positions = [i["position"] for i in manifest["images"]]
    if sorted(positions) != list(range(1, manifest["declared_total"] + 1)):
        raise DomainError("assistant_pages_incomplete")
    for item in manifest["images"]:
        path = target / item["path"]
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(target.resolve())
            or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]
        ):
            raise DomainError("assistant_image_hash_mismatch")
    return target, grant, captured, records, manifest, consent


def prepare(service, capture_id, brief: ResearchBrief, pages: list[PageReading]):
    target, grant, captured, records, manifest, consent = authorized_capture(service, capture_id)
    if brief.synthetic != grant.synthetic or any(
        not r["sampling"] or any(s["brief_id"] != brief.id for s in r["sampling"]) for r in records
    ):
        raise DomainError("workflow_brief_mismatch")
    expected = {i["position"]: i["sha256"] for i in manifest["images"]}
    if len(pages) != len(expected) or {p.position: p.sha256 for p in pages} != expected:
        raise DomainError("assistant_pages_incomplete")
    if any(not p.key_content_readable for p in pages):
        raise DomainError("assistant_key_content_unreadable")
    model = consent["processor"] + ":explicit_external_submission"
    gallery = {
        "state": "complete",
        "source_id": grant.id,
        "note_external_id": manifest["note_external_id"],
        "processed": len(pages),
        "declared_total": len(pages),
        "missing_positions": [],
        "human_verified": False,
        "reading_kind": "page_notes_not_verbatim_ocr",
        "media_text": "[调用助手逐页阅读摘记；非逐字OCR；未人工核验]\n"
        + "\n".join(f"第{p.position}页：{p.text}" for p in sorted(pages, key=lambda p: p.position)),
        "images": [
            {
                "position": p.position,
                "sha256": p.sha256,
                "analysis": {"model": model, "media_text": p.text},
            }
            for p in sorted(pages, key=lambda p: p.position)
        ],
    }
    enriched = enrich(records, gallery) if pages else records
    destination = target / "assistant-enriched.json"
    if destination.exists() and read_json(destination) != enriched:
        raise DomainError("assistant_review_immutable")
    destination.write_text(canonical(enriched))
    imported = import_file(service.db, destination, grant)
    if imported["state"] != "complete":
        raise DomainError("assistant_review_import_failed")
    observations, blocked = service.db.observations(brief.id, grant.id)
    expected_ids = {
        service.db.identity(grant.id, r["kind"], r["external_id"], r["locator"], r["text"])[0]
        for r in records
    }
    if (
        blocked
        or len(observations) != len(records)
        or {o["id"] for o in observations} != expected_ids
        or {datetime.fromisoformat(o["observed_at"]) for o in observations}
        != {datetime.fromisoformat(r["observed_at"]) for r in records}
    ):
        raise DomainError("assistant_review_scope_mismatch")
    gateway = EvidenceGateway(service.db, observations, max_chars=50000)
    if gateway.truncated_fields:
        raise DomainError("assistant_review_text_truncated")
    prepared = {
        "capture_id": capture_id,
        "brief": brief.model_dump(mode="json"),
        "input_sha256": digest(observations),
        "consent_sha256": digest(consent),
        "capture_sha256": digest({"records": records, "manifest": manifest}),
        "gallery": gallery,
        "import": imported,
    }
    path = target / "assistant-prepared.json"
    if path.exists():
        previous = read_json(path)
        if any(
            previous[k] != prepared[k]
            for k in ("brief", "input_sha256", "consent_sha256", "gallery")
        ):
            raise DomainError("assistant_review_immutable")
        if (
            previous.get("capture_sha256") is not None
            and previous["capture_sha256"] != prepared["capture_sha256"]
        ):
            raise DomainError("assistant_review_input_changed")
        prepared = previous
    else:
        path.write_text(canonical(prepared))
    return {
        "state": "prepared",
        "capture_id": capture_id,
        "input_sha256": prepared["input_sha256"],
        "catalog": gateway.prompt_catalog(),
        "reading_kind": gallery["reading_kind"],
    }


def completed_job(service, request, result):
    ident = str(uuid4())
    at = stamp(service.db.clock())
    with service.db.conn:
        service.require(request.source_id)
        service.db.conn.execute(
            "INSERT INTO browser_jobs (id,source_id,state,request_json,result_json,error_code,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                ident,
                request.source_id,
                "complete",
                canonical(request.model_dump(mode="json")),
                canonical(result),
                None,
                at,
                at,
            ),
        )
        service.db.audit(request.source_id, "assistant_submission:" + ident)
    return {"job_id": ident, **result}


def submit(service, capture_id, input_sha256, draft: BatchDraft):
    from rednotebook.browser_service import JobRequest

    target, grant, captured, records, manifest, consent = authorized_capture(service, capture_id)
    prepared = read_json(target / "assistant-prepared.json")
    brief = ResearchBrief.model_validate(prepared["brief"])
    observations, blocked = service.db.observations(brief.id, grant.id)
    if (
        input_sha256 != prepared["input_sha256"]
        or digest(observations) != input_sha256
        or digest(consent) != prepared["consent_sha256"]
        or (
            prepared.get("capture_sha256") is not None
            and prepared["capture_sha256"] != digest({"records": records, "manifest": manifest})
        )
    ):
        raise DomainError("assistant_review_input_changed")
    if blocked or not draft.findings:
        raise DomainError("assistant_review_findings_required")
    gateway = EvidenceGateway(service.db, observations, max_chars=50000)
    validate_draft(draft, gateway)
    findings = resolved_findings(draft, gateway, 0)
    metadata = {
        "model": {"provider": consent["processor"], "execution": "explicit_external_submission"},
        "input_sha256": input_sha256,
        "capture_id": capture_id,
        "consent_sha256": digest(consent),
        "synthetic": brief.synthetic,
    }
    run_id = start_run(service.db, brief, [grant.id], metadata)
    report = {
        "run_id": run_id,
        "brief_id": brief.id,
        "state": "complete",
        "metadata": metadata,
        "quality": quality_report(observations, blocked),
        "selected_notes": 1,
        "selected_comments": len(records) - 1,
        "completed_batches": 1,
        "findings": findings,
        "sources": resolved_sources(findings),
        "gaps": draft.gaps,
        "errors": [],
        "tool_calls": [],
        "selection_truncated": False,
        "truncated_fields": gateway.truncated_fields,
        "usage": None,
        "semantic_quality_verified": False,
        "visual_analysis": "assistant_page_notes_not_verbatim_ocr",
    }
    checkpoint(service.db, run_id, report)
    result = {
        "state": "complete",
        "capture_id": capture_id,
        "run_id": run_id,
        "execution_mode": "assistant_submission",
        "research": report,
        "completeness": captured["completeness"]
        | {"image_reading": "complete", "human_accuracy_review": "not_run"},
        "gallery": prepared["gallery"],
        "import": prepared["import"],
        "reading_scope": {
            "saved_images": len(manifest["images"]),
            "declared_images": manifest["declared_total"],
            "comments": len(records) - 1,
            "limitations": ["bounded_comments", "assistant_reading_not_human_verification"],
        },
    }
    return completed_job(
        service,
        JobRequest(
            operation="workflow",
            analysis_mode="caller_analysis",
            source_id=grant.id,
            capture_id=capture_id,
            brief=brief,
            brief_id=brief.id,
        ),
        result,
    )


def assemble_topic(
    service, source_id, selection: TopicSelection, brief: ResearchBrief, review_jobs
):
    from rednotebook.browser_service import JobRequest

    search, candidates = resolve_search(service, source_id, selection)
    if set(review_jobs) != {n.note_id for n in selection.notes}:
        raise DomainError("assistant_topic_jobs_incomplete")
    entries = []
    for chosen in selection.notes:
        job = service.get(review_jobs[chosen.note_id], True)
        result = job.get("result") or {}
        if (
            job["source_id"] != source_id
            or job["operation"] != "workflow"
            or job["state"] != "complete"
            or result.get("execution_mode") != "assistant_submission"
        ):
            raise DomainError("assistant_topic_child_invalid")
        _, _, _, records, _, _ = authorized_capture(service, result["capture_id"])
        report = read_run(service.db, result["run_id"])
        row = service.db.conn.execute(
            "SELECT request_json FROM browser_jobs WHERE id=?", (job["job_id"],)
        ).fetchone()
        child = JobRequest.model_validate_json(row[0])
        note = next(r for r in records if r["kind"] == "note")
        if (
            note["external_id"] != chosen.note_id
            or chosen.question not in child.brief.research_question
            or report["state"] != "complete"
            or result.get("completeness", {}).get("capture_gate") != "passed"
            or result["completeness"].get("image_reading") != "complete"
        ):
            raise DomainError("assistant_topic_child_scope_mismatch")
        entries.append(
            {
                **chosen.model_dump(),
                "public_url": candidates[chosen.note_id].get("public_url"),
                "job_id": job["job_id"],
                "original_job_id": job["job_id"],
                "state": "complete",
                "research_brief": child.brief.model_dump(mode="json"),
                "result": result,
            }
        )
    result = {
        "state": "complete",
        "execution_mode": "assistant_submission",
        "execution_complete": True,
        "requested_depth": "research_then_plan",
        "achieved_depth": "detail_research",
        "search_complete": True,
        "candidate_count": len(candidates),
        "scan_target": [30, 40],
        "scan_target_met": 30 <= len(candidates) <= 40,
        "selection": selection.model_dump(),
        "selected_count": len(entries),
        "completed_count": len(entries),
        "skipped_video_count": 0,
        "notes": entries,
        "research_run_ids": [e["result"]["run_id"] for e in entries],
        "source_ids": [source_id],
        "research_complete": True,
        "campaign_ready": True,
        "limitations": [
            "bounded_sample_not_representative",
            "research_is_hypothesis",
            "assistant_reading_not_human_verification",
        ],
    }
    if brief.synthetic != service.require(source_id).synthetic:
        raise DomainError("synthetic_flag_mismatch")
    return completed_job(
        service,
        JobRequest(
            operation="topic_research",
            analysis_mode="caller_analysis",
            source_id=source_id,
            topic_selection=selection,
            brief=brief,
        ),
        result,
    )


def record_consent(service, capture_id, user_statement, processor="codex-assistant"):
    """Operator-only entry point after explicit user approval; never exposed in MCP."""
    from pydantic import TypeAdapter

    from rednotebook.browser_service import capture_path
    from rednotebook.domain.models import Identifier

    processor = TypeAdapter(Identifier).validate_python(processor)
    job = service.get(capture_id)
    grant = service.require(job["source_id"])
    if job["operation"] not in {"collect", "workflow"} or job["state"] in {"queued", "running"}:
        raise DomainError("capture_not_ready")
    if not isinstance(user_statement, str) or not user_statement.strip():
        raise DomainError("explicit_capture_consent_required")
    target = capture_path(service.db, capture_id)
    expiry = service.db.conn.execute(
        "SELECT expires_at FROM sources WHERE id=?", (grant.id,)
    ).fetchone()[0]
    consent = {
        "version": 1,
        "source_id": grant.id,
        "capture_id": capture_id,
        "processor": processor,
        "permission": "cloud_processing",
        "decision": "allowed",
        "scope": "this_capture_only",
        "user_statement": user_statement,
        "authorized_at": stamp(service.db.clock()),
        "expires_at": expiry,
        "other_permissions": "unchanged",
        "human_verified": False,
    }
    for name in ("evidence", "manifest"):
        path = target / f"{name}.json"
        if path.exists():
            read_json(path)
        elif name != "manifest":
            raise DomainError("assistant_review_file_missing")
        consent[f"{name}_sha256"] = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        )
    path = target / "assistant-consent.json"
    if path.exists():
        previous = read_json(path)
        if any(
            previous.get(k) != consent[k]
            for k in (
                "source_id",
                "processor",
                "capture_id",
                "evidence_sha256",
                "manifest_sha256",
                "user_statement",
            )
        ):
            raise DomainError("capture_consent_immutable")
        consent = previous
    else:
        path.write_text(canonical(consent))
    with service.db.conn:
        service.db.audit(grant.id, consent_action(processor, capture_id))
    return consent
