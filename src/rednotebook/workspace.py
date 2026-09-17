"""Permission-checked discovery and append-only user feedback for a personal workspace."""

import json
from typing import Literal
from uuid import uuid4

from pydantic import Field

from rednotebook import creative
from rednotebook.domain.models import Contract, Text
from rednotebook.errors import DomainError
from rednotebook.provenance import public_http_url
from rednotebook.research.store import read_run
from rednotebook.util import stamp


class FindingFeedback(Contract):
    decision: Literal["accept", "correct", "exclude"]
    corrected_claim: str = Field(default="", max_length=2000)
    note: str = Field(default="", max_length=2000)
    user_instruction: Text = Field(max_length=2000)


def record_feedback(db, run_id, finding_id, feedback):
    report = read_run(db, run_id)
    if finding_id not in {f["id"] for f in report.get("findings", [])}:
        raise DomainError("finding_not_found")
    if (feedback.decision == "correct") != bool(feedback.corrected_claim.strip()):
        raise DomainError("corrected_claim_required_only_for_correction")
    ident = str(uuid4())
    # User feedback is not an independent human evaluation or formal export approval.
    note = json.dumps(
        {"note": feedback.note, "user_instruction": feedback.user_instruction}, ensure_ascii=False
    )
    with db.conn:
        db.conn.execute(
            "INSERT INTO finding_reviews VALUES (?,?,?,?,?,?,?)",
            (
                ident,
                run_id,
                finding_id,
                feedback.decision,
                feedback.corrected_claim,
                note,
                stamp(db.clock()),
            ),
        )
    return {"feedback_id": ident, "decision": feedback.decision, "formal_approval": False}


def reviewed_report(db, run_id):
    report = read_run(db, run_id)
    latest = {}
    for row in db.conn.execute(
        "SELECT * FROM finding_reviews WHERE run_id=? ORDER BY created_at,rowid", (run_id,)
    ):
        latest[row["finding_id"]] = {
            "feedback_id": row["id"],
            "decision": row["decision"],
            "corrected_claim": row["corrected_claim"],
            **json.loads(row["note"]),
            "created_at": row["created_at"],
            "kind": "user_feedback_not_independent_verification",
        }
    for finding in report.get("findings", []):
        finding["user_feedback"] = latest.get(finding.get("id"))
    return report


def history(db, kind, query="", limit=20, offset=0, source_id=None):
    if kind not in {"jobs", "notes", "research", "drafts"}:
        raise DomainError("history_kind_invalid")
    if not 1 <= limit <= 100 or not 0 <= offset <= 100000 or len(query) > 200:
        raise DomainError("history_window_invalid")
    allowed, _ = db.allowed_sources(source_id)
    items = []
    if not allowed and kind != "drafts":
        return {"items": [], "next_offset": None}
    # Iterate rows; access checks precede all returned payloads. SQL search is literal,
    # and older observations are not duplicated into the personal library.
    if kind == "notes":
        marks = ",".join("?" for _ in allowed)
        rows = db.conn.execute(
            f"SELECT e.id,e.source_id,c.observed_at,r.revision,r.payload FROM evidence e "
            "JOIN captures c ON c.evidence_id=e.id JOIN revisions r ON r.evidence_id=c.evidence_id AND r.revision=c.revision "
            f"WHERE e.kind='note' AND e.source_id IN ({marks}) "
            "AND c.observed_at=(SELECT MAX(observed_at) FROM captures WHERE evidence_id=e.id) "
            "AND instr(lower(r.payload),lower(?))>0 ORDER BY c.observed_at DESC,e.id LIMIT ? OFFSET ?",
            (*allowed, query, limit + 1, offset),
        ).fetchall()
        for row in rows:
            db.require_source(row["source_id"])
            payload = json.loads(row["payload"])
            items.append(
                {
                    "evidence_id": row["id"],
                    "source_id": row["source_id"],
                    "revision": row["revision"],
                    "title": payload["title"],
                    "excerpt": payload["text"][:500],
                    "public_url": public_http_url(payload["locator"]),
                    "observed_at": row["observed_at"],
                    "brief_ids": [
                        r[0]
                        for r in db.conn.execute(
                            "SELECT DISTINCT brief_id FROM samples WHERE evidence_id=?",
                            (row["id"],),
                        )
                    ],
                }
            )
    else:
        # Metadata-first discovery avoids copying full reports and signed URLs to lists.
        tables = {"jobs": "browser_jobs", "research": "research_runs", "drafts": "bundles"}
        rows = db.conn.execute(
            f"SELECT * FROM {tables[kind]} ORDER BY created_at DESC,rowid DESC"
        ).fetchall()
        skipped = 0
        for row in rows:
            try:
                if kind == "jobs":
                    if row["source_id"] not in allowed or not row["request_json"]:
                        continue
                    db.require_source(row["source_id"])
                    request = json.loads(row["request_json"])
                    item = {
                        "job_id": row["id"],
                        "operation": request["operation"],
                        "source_id": row["source_id"],
                        "keyword": request.get("keyword", ""),
                        "state": row["state"],
                        "created_at": row["created_at"],
                    }
                else:
                    run_id = row["id"] if kind == "research" else row["run_id"]
                    sources = (
                        set(creative.bundle_sources(db, row["id"], row["version"]))
                        if kind == "drafts"
                        else {
                            r[0]
                            for r in db.conn.execute(
                                "SELECT source_id FROM research_sources WHERE run_id=?", (run_id,)
                            )
                        }
                    )
                    if not sources <= set(allowed) or (source_id and source_id not in sources):
                        continue
                    if kind == "research":
                        read_run(db, run_id)
                    if kind == "research":
                        item = {
                            "run_id": run_id,
                            "brief_id": row["brief_id"],
                            "state": row["state"],
                            "created_at": row["created_at"],
                        }
                    else:
                        bundle = creative.load_bundle(db, row["id"], row["version"])
                        item = {
                            "bundle_id": row["id"],
                            "version": row["version"],
                            "run_id": run_id,
                            "kind": bundle["payload"].get("kind", "research"),
                            "series_id": (
                                bundle["payload"].get("context", {}).get("brief", {}).get("series")
                                or {}
                            ).get("id"),
                            "title": bundle["payload"]["editorial"]["title"],
                            "state": row["state"],
                            "created_at": row["created_at"],
                        }
            except DomainError:
                continue
            if query.casefold() not in json.dumps(item, ensure_ascii=False).casefold():
                continue
            if skipped < offset:
                skipped += 1
                continue
            items.append(item)
            if len(items) > limit:
                break
    return {"items": items[:limit], "next_offset": offset + limit if len(items) > limit else None}


def findings_for_draft(db, run_id):
    """Apply feedback to new drafts only; old reports and version hashes stay intact."""
    report = reviewed_report(db, run_id)
    chosen = []
    for finding in report.get("findings", []):
        feedback = finding.get("user_feedback") or {}
        if feedback.get("decision") == "exclude":
            continue
        if feedback.get("decision") == "correct":
            finding["original_claim"] = finding["claim"]
            finding["claim"] = feedback["corrected_claim"]
            finding["semantic_review"] = "correction_support_pending"
        chosen.append(finding)
    report["findings"] = chosen
    return report
