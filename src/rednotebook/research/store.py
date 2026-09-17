import json
from uuid import uuid4

from rednotebook.domain.models import ResearchBrief
from rednotebook.errors import DomainError
from rednotebook.util import canonical, digest, stamp


def save_brief(db, brief: ResearchBrief):
    payload = brief.model_dump(mode="json")
    hashed = digest(payload)
    with db.conn:
        row = db.conn.execute(
            "SELECT version FROM briefs WHERE id=? AND content_hash=?", (brief.id, hashed)
        ).fetchone()
        if row:
            return row["version"]
        version = db.conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM briefs WHERE id=?", (brief.id,)
        ).fetchone()[0]
        db.conn.execute(
            "INSERT INTO briefs VALUES (?,?,?,?,?)",
            (brief.id, version, hashed, canonical(payload), stamp(db.clock())),
        )
        return version


def start_run(db, brief, source_ids, metadata):
    version = save_brief(db, brief)
    run_id = str(uuid4())
    with db.conn:
        db.conn.execute(
            "INSERT INTO research_runs VALUES (?,?,?,?,?,?,?,?)",
            (
                run_id,
                brief.id,
                version,
                "running",
                canonical(metadata),
                None,
                stamp(db.clock()),
                stamp(db.clock()),
            ),
        )
        for source_id in source_ids:
            db.require_source(source_id)
            db.conn.execute("INSERT INTO research_sources VALUES (?,?)", (run_id, source_id))
    return run_id


def checkpoint(db, run_id, report):
    with db.conn:
        row = db.conn.execute("SELECT state FROM research_runs WHERE id=?", (run_id,)).fetchone()
        if row["state"] == "revoked":
            raise DomainError("research_revoked")
        for source in db.conn.execute(
            "SELECT source_id FROM research_sources WHERE run_id=?", (run_id,)
        ):
            db.require_source(source["source_id"])
        db.conn.execute(
            "UPDATE research_runs SET state=?,report_json=?,updated_at=? WHERE id=?",
            (report["state"], canonical(report), stamp(db.clock()), run_id),
        )


def invalidate_run(db, run_id):
    with db.conn:
        db.conn.execute(
            "UPDATE research_runs SET state='revoked',report_json=NULL,updated_at=? WHERE id=?",
            (stamp(db.clock()), run_id),
        )


def read_run(db, run_id):
    row = db.conn.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise DomainError("research_not_found")
    if row["state"] == "revoked":
        raise DomainError("research_revoked")
    for source in db.conn.execute(
        "SELECT source_id FROM research_sources WHERE run_id=?", (run_id,)
    ):
        db.require_source(source["source_id"])
    if not row["report_json"]:
        return {"run_id": run_id, "state": row["state"], "metadata": json.loads(row["metadata"])}
    return json.loads(row["report_json"])
