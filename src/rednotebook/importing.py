"""Batch-atomic importer with per-row savepoints and content-free error reports."""

import hashlib
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from rednotebook.adapters.files import read_rows
from rednotebook.domain.models import EvidenceInput, SourceGrant
from rednotebook.errors import DomainError
from rednotebook.storage import Database
from rednotebook.util import canonical, stamp


def validation_issues(error: ValidationError):
    # Never persist input values, arbitrary model messages, or raw source content.
    return [
        {
            "field": ".".join(
                map(str, entry["loc"][:-1] if entry["type"] == "extra_forbidden" else entry["loc"])
            )
            or "row",
            "code": entry["type"],
        }
        for entry in error.errors(include_input=False, include_context=False, include_url=False)
    ]


def import_file(db: Database, path: Path, grant: SourceGrant):
    db.register_grant(grant)
    raw, rows = read_rows(path)
    job_id = str(uuid4())
    report = {
        "job_id": job_id,
        "source_id": grant.id,
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "state": "importing",
        "total_rows": len(rows),
        "accepted_rows": 0,
        "rejected_rows": 0,
        "new_evidence": 0,
        "new_revisions": 0,
        "new_metrics": 0,
        "errors": [],
    }
    with db.conn:
        db.conn.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?)",
            (job_id, grant.id, report["input_sha256"], "importing", stamp(db.clock()), None),
        )
    validated = []
    for line, data, parse_error in rows:
        try:
            if parse_error:
                report["errors"].append({"row": line, "issues": parse_error})
                continue
            record = EvidenceInput.model_validate(data)
            if record.source_id != grant.id:
                raise DomainError("source_id_mismatch")
            validated.append((line, record))
        except ValidationError as exc:
            report["errors"].append({"row": line, "issues": validation_issues(exc)})
        except DomainError as exc:
            report["errors"].append({"row": line, "issues": [{"field": "row", "code": exc.code}]})
    # Notes before comments permits arbitrary row ordering while retaining original line IDs.
    validated.sort(key=lambda item: item[1].kind != "note")
    try:
        with db.conn:
            db.conn.execute("BEGIN IMMEDIATE")
            for line, record in validated:
                db.conn.execute("SAVEPOINT import_row")
                try:
                    changes = db.insert_record(record)
                except DomainError as exc:
                    db.conn.execute("ROLLBACK TO import_row")
                    report["errors"].append(
                        {"row": line, "issues": [{"field": "row", "code": exc.code}]}
                    )
                else:
                    report["accepted_rows"] += 1
                    for key in ("new_evidence", "new_revisions", "new_metrics"):
                        report[key] += changes[key]
                finally:
                    db.conn.execute("RELEASE import_row")
            # Recheck before commit: expiry during a batch cannot authorize its final write.
            db.require_source(grant.id, "storage")
            report["rejected_rows"] = len(report["errors"])
            report["errors"].sort(key=lambda item: item["row"])
            report["state"] = (
                ("partial" if report["accepted_rows"] else "failed")
                if report["errors"]
                else "complete"
            )
            db.conn.execute(
                "UPDATE jobs SET state=?,report=? WHERE id=?",
                (report["state"], canonical(report), job_id),
            )
            db.audit(grant.id, "import_" + report["state"])
    except BaseException:
        # Infrastructure failure/interruption rolls back the whole evidence batch.
        # Re-run the original file safely; there is no deceptive partial checkpoint.
        report.update(
            state="failed",
            accepted_rows=0,
            rejected_rows=len(report["errors"]),
            uncommitted_rows=len(rows) - len(report["errors"]),
            new_evidence=0,
            new_revisions=0,
            new_metrics=0,
            failure="batch_rolled_back_retry_original_file",
        )
        with db.conn:
            db.conn.execute(
                "UPDATE jobs SET state=?,report=? WHERE id=?", ("failed", canonical(report), job_id)
            )
        raise
    return report
