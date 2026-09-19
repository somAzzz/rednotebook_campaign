"""SQLite repository. Every user-facing read checks the source grant."""

import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from rednotebook.domain.models import EvidenceInput, SourceGrant
from rednotebook.errors import DomainError
from rednotebook.storage.migrations import (
    MIGRATION_2,
    MIGRATION_3,
    MIGRATION_4,
    MIGRATION_5,
    MIGRATION_6,
    MIGRATION_7,
    MIGRATION_8,
    MIGRATION_9,
    MIGRATION_10,
    MIGRATION_11,
    MIGRATION_12,
    MIGRATION_13,
)
from rednotebook.util import canonical, digest, now_utc, stamp

SCHEMA = """
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sources(
 id TEXT PRIMARY KEY, grant_json TEXT NOT NULL, grant_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('active','revoked','expired')),
 registered_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE evidence(
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
 kind TEXT NOT NULL, identity_hash TEXT NOT NULL, weak_identity INTEGER NOT NULL,
 parent_id TEXT REFERENCES evidence(id) ON DELETE CASCADE,
 UNIQUE(source_id, kind, identity_hash)
);
CREATE TABLE revisions(
 evidence_id TEXT NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL, content_hash TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(evidence_id,revision), UNIQUE(evidence_id,content_hash)
);
CREATE TABLE captures(
 evidence_id TEXT NOT NULL, observed_at TEXT NOT NULL, revision INTEGER NOT NULL,
 PRIMARY KEY(evidence_id,observed_at),
 FOREIGN KEY(evidence_id,revision) REFERENCES revisions(evidence_id,revision) ON DELETE CASCADE
);
CREATE TABLE metrics(
 id TEXT PRIMARY KEY, evidence_id TEXT NOT NULL, observed_at TEXT NOT NULL,
 name TEXT NOT NULL, payload TEXT NOT NULL,
 FOREIGN KEY(evidence_id,observed_at) REFERENCES captures(evidence_id,observed_at) ON DELETE CASCADE,
 UNIQUE(evidence_id,observed_at,name)
);
CREATE TABLE samples(
 id TEXT PRIMARY KEY, evidence_id TEXT NOT NULL, observed_at TEXT NOT NULL,
 brief_id TEXT NOT NULL, run_id TEXT NOT NULL, payload TEXT NOT NULL, coverage TEXT,
 FOREIGN KEY(evidence_id,observed_at) REFERENCES captures(evidence_id,observed_at) ON DELETE CASCADE
);
CREATE INDEX samples_brief ON samples(brief_id,evidence_id);
CREATE TABLE jobs(
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
 input_hash TEXT NOT NULL, state TEXT NOT NULL,
 started_at TEXT NOT NULL, report TEXT
);
CREATE TABLE tombstones(evidence_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE audit(id TEXT PRIMARY KEY, source_id TEXT, action TEXT NOT NULL, at TEXT NOT NULL);
PRAGMA user_version=1;
"""


class Database:
    def __init__(self, path: Path | str, clock=now_utc):
        self.clock = clock
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        self.conn = sqlite3.connect(self.path, timeout=15)
        if new:
            self.path.chmod(0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA secure_delete=ON")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            try:
                self.conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
                self.conn.execute(
                    "INSERT INTO settings VALUES (?,?)", ("author_salt", secrets.token_hex(32))
                )
                self.conn.commit()
                version = 1
            except BaseException:
                self.conn.rollback()
                self.conn.close()
                raise
        if version == 1:
            try:
                self.conn.executescript(MIGRATION_2)
                version = 2
            except BaseException:
                self.conn.rollback()
                self.conn.close()
                raise
        if version == 2:
            self.conn.executescript(MIGRATION_3)
            version = 3
        if version == 3:
            self.conn.executescript(MIGRATION_4)
            version = 4
        if version == 4:
            self.conn.executescript(MIGRATION_5)
            version = 5
        if version == 5:
            self.conn.executescript(MIGRATION_6)
            version = 6
        if version == 6:
            self.conn.executescript(MIGRATION_7)
            version = 7
        if version == 7:
            self.conn.execute("PRAGMA foreign_keys=OFF")
            try:
                self.conn.executescript(MIGRATION_8)
                if self.conn.execute("PRAGMA foreign_key_check").fetchone():
                    raise DomainError("migration_foreign_key_check_failed")
                self.conn.commit()
                version = 8
            except BaseException:
                self.conn.rollback()
                self.conn.close()
                raise
            finally:
                if version == 8:
                    self.conn.execute("PRAGMA foreign_keys=ON")
        if version == 8:
            self.conn.executescript(MIGRATION_9)
            version = 9
        if version == 9:
            self.conn.executescript(MIGRATION_10)
            version = 10
        if version == 10:
            self.conn.executescript(MIGRATION_11)
            version = 11
        if version == 11:
            self.conn.executescript(MIGRATION_12)
            version = 12
        if version == 12:
            self.conn.executescript(MIGRATION_13)
            version = 13
        if version != 13:
            self.conn.close()
            raise DomainError("unsupported_database_version")
        self.salt = self.conn.execute(
            "SELECT value FROM settings WHERE key='author_salt'"
        ).fetchone()[0]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.conn.close()

    def audit(self, source_id, action):
        self.conn.execute(
            "INSERT INTO audit VALUES (?,?,?,?)",
            (str(uuid4()), source_id, action, stamp(self.clock())),
        )

    def register_grant(self, grant: SourceGrant):
        now = self.clock()
        if not grant.valid_from <= now < grant.valid_until:
            raise DomainError("grant_outside_valid_period")
        if grant.permissions.storage != "allowed":
            raise DomainError("storage_permission_required")
        payload = grant.model_dump(mode="json")
        with self.conn:
            existing = self.conn.execute("SELECT * FROM sources WHERE id=?", (grant.id,)).fetchone()
            if existing:
                if existing["status"] != "active":
                    raise DomainError("source_inactive")
                if existing["grant_hash"] != digest(payload):
                    raise DomainError("grant_immutable_use_new_source_id")
                self.require_source(grant.id, "storage")
                return
            expiry = min(grant.valid_until, now + timedelta(days=grant.retention_days))
            self.conn.execute(
                "INSERT INTO sources VALUES (?,?,?,?,?,?)",
                (
                    grant.id,
                    canonical(payload),
                    digest(payload),
                    "active",
                    stamp(now),
                    stamp(expiry),
                ),
            )
            self.audit(grant.id, "grant_registered")

    def require_source(self, source_id, permission="local_analysis") -> SourceGrant:
        row = self.conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise DomainError("source_not_found")
        grant = SourceGrant.model_validate_json(row["grant_json"])
        now = self.clock()
        if row["status"] != "active":
            raise DomainError("source_inactive")
        if not grant.valid_from <= now < grant.valid_until or stamp(now) >= row["expires_at"]:
            raise DomainError("source_expired")
        if getattr(grant.permissions, permission, "unknown") != "allowed":
            raise DomainError(f"{permission}_permission_required")
        return grant

    def allowed_sources(self, source_id=None):
        if source_id:
            self.require_source(source_id)
            return [source_id], []
        allowed, blocked = [], []
        for row in self.conn.execute("SELECT id FROM sources ORDER BY id").fetchall():
            try:
                self.require_source(row["id"])
                allowed.append(row["id"])
            except DomainError as exc:
                blocked.append({"source_id": row["id"], "reason": exc.code})
        return allowed, blocked

    @staticmethod
    def identity(source_id, kind, external_id, locator="", text=""):
        key = ["external", external_id] if external_id else ["weak", locator.strip(), text]
        identity = digest(key)
        return digest([source_id, kind, identity]), identity

    def insert_record(self, record: EvidenceInput):
        """Caller owns the transaction and per-row savepoint."""
        grant = self.require_source(record.source_id, "storage")
        if record.synthetic != grant.synthetic:
            raise DomainError("synthetic_source_mismatch")
        if record.observed_at > self.clock():
            raise DomainError("future_observation")
        eid, identity = self.identity(
            record.source_id, record.kind, record.external_id, record.locator, record.text
        )
        parent_id = None
        if record.kind == "comment":
            parent_id, _ = self.identity(record.source_id, "note", record.parent_external_id)
            if not self.conn.execute("SELECT 1 FROM evidence WHERE id=?", (parent_id,)).fetchone():
                raise DomainError("parent_note_not_found")
        stored = self.conn.execute("SELECT parent_id FROM evidence WHERE id=?", (eid,)).fetchone()
        if stored and stored["parent_id"] != parent_id:
            raise DomainError("parent_identity_conflict")
        new = stored is None
        self.conn.execute(
            "INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?,?)",
            (eid, record.source_id, record.kind, identity, not record.external_id, parent_id),
        )
        payload = record.model_dump(
            mode="json", exclude={"metrics", "sampling", "coverage", "observed_at", "author_id"}
        )
        payload["author_pseudonym"] = (
            hmac.new(
                bytes.fromhex(self.salt),
                canonical([record.source_id, record.author_id]).encode(),
                hashlib.sha256,
            ).hexdigest()
            if record.author_id
            else None
        )
        content_hash = digest(payload)
        revision_row = self.conn.execute(
            "SELECT revision FROM revisions WHERE evidence_id=? AND content_hash=?",
            (eid, content_hash),
        ).fetchone()
        revised = revision_row is None
        if revised:
            revision = self.conn.execute(
                "SELECT COALESCE(MAX(revision),0)+1 FROM revisions WHERE evidence_id=?", (eid,)
            ).fetchone()[0]
            self.conn.execute(
                "INSERT INTO revisions VALUES (?,?,?,?)",
                (eid, revision, content_hash, canonical(payload)),
            )
        else:
            revision = revision_row["revision"]
        observed = stamp(record.observed_at)
        capture = self.conn.execute(
            "SELECT revision FROM captures WHERE evidence_id=? AND observed_at=?", (eid, observed)
        ).fetchone()
        if capture and capture["revision"] != revision:
            raise DomainError("observation_content_conflict")
        self.conn.execute(
            "INSERT OR IGNORE INTO captures VALUES (?,?,?)", (eid, observed, revision)
        )
        added_metrics = 0
        for metric in record.metrics:
            metric_payload = canonical(metric.model_dump(mode="json"))
            old = self.conn.execute(
                "SELECT payload FROM metrics WHERE evidence_id=? AND observed_at=? AND name=?",
                (eid, observed, metric.name),
            ).fetchone()
            if old and old["payload"] != metric_payload:
                raise DomainError("metric_snapshot_conflict")
            if not old:
                self.conn.execute(
                    "INSERT INTO metrics VALUES (?,?,?,?,?)",
                    (
                        digest([eid, observed, metric.name]),
                        eid,
                        observed,
                        metric.name,
                        metric_payload,
                    ),
                )
                added_metrics += 1
        for sample in record.sampling:
            sample_payload = sample.model_dump(mode="json")
            run = self.conn.execute(
                "SELECT s.payload FROM samples s JOIN evidence e ON e.id=s.evidence_id "
                "WHERE e.source_id=? AND s.run_id=? LIMIT 1",
                (record.source_id, sample.run_id),
            ).fetchone()
            if run:
                previous = json.loads(run["payload"])
                if any(
                    previous[k] != sample_payload[k]
                    for k in ("brief_id", "keyword", "sort", "group")
                ):
                    raise DomainError("sampling_run_definition_conflict")
            sample_id = digest([eid, observed, sample_payload])
            coverage = (
                canonical(record.coverage.model_dump(mode="json")) if record.coverage else None
            )
            existing = self.conn.execute(
                "SELECT payload,coverage FROM samples WHERE evidence_id=? AND observed_at=? AND run_id=?",
                (eid, observed, sample.run_id),
            ).fetchone()
            if existing and existing["payload"] != canonical(sample_payload):
                raise DomainError("sampling_observation_conflict")
            if existing and existing["coverage"] != coverage:
                raise DomainError("coverage_snapshot_conflict")
            self.conn.execute(
                "INSERT OR IGNORE INTO samples VALUES (?,?,?,?,?,?,?)",
                (
                    sample_id,
                    eid,
                    observed,
                    sample.brief_id,
                    sample.run_id,
                    canonical(sample_payload),
                    coverage,
                ),
            )
        return {
            "evidence_id": eid,
            "new_evidence": int(new),
            "new_revisions": int(revised),
            "new_metrics": added_metrics,
        }

    def observations(self, brief_id, source_id=None):
        allowed, blocked = self.allowed_sources(source_id)
        records = []
        # Source filters stay at the SQL boundary; revoked payloads cannot reach analysis.
        for source in allowed:
            rows = self.conn.execute(
                """
              SELECT e.id,e.source_id,e.kind,e.parent_id,e.weak_identity,c.observed_at,c.revision,r.payload
              FROM evidence e JOIN captures c ON c.evidence_id=e.id
              JOIN revisions r ON r.evidence_id=c.evidence_id AND r.revision=c.revision
              WHERE e.source_id=? AND EXISTS(
                SELECT 1 FROM samples s WHERE s.evidence_id=e.id AND s.observed_at=c.observed_at AND s.brief_id=?)
              ORDER BY c.observed_at,e.id
            """,
                (source, brief_id),
            ).fetchall()
            for row in rows:
                item = dict(row)
                item["content"] = json.loads(item.pop("payload"))
                item["metrics"] = [
                    json.loads(x["payload"])
                    for x in self.conn.execute(
                        "SELECT payload FROM metrics WHERE evidence_id=? AND observed_at=? ORDER BY name",
                        (row["id"], row["observed_at"]),
                    )
                ]
                item["samples"] = [
                    {
                        "sampling": json.loads(x["payload"]),
                        "coverage": json.loads(x["coverage"]) if x["coverage"] else None,
                    }
                    for x in self.conn.execute(
                        "SELECT payload,coverage FROM samples WHERE evidence_id=? AND observed_at=? AND brief_id=?",
                        (row["id"], row["observed_at"], brief_id),
                    )
                ]
                records.append(item)
        return records, blocked

    def inspect(self, evidence_id, revision=None):
        row = self.conn.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        if not row:
            tombstone = self.conn.execute(
                "SELECT reason FROM tombstones WHERE evidence_id=?", (evidence_id,)
            ).fetchone()
            raise DomainError("evidence_revoked" if tombstone else "evidence_not_found")
        self.require_source(row["source_id"])
        if revision is None:
            revision = self.conn.execute(
                "SELECT revision FROM captures WHERE evidence_id=? ORDER BY observed_at DESC LIMIT 1",
                (evidence_id,),
            ).fetchone()[0]
        rev = self.conn.execute(
            "SELECT * FROM revisions WHERE evidence_id=? AND revision=?", (evidence_id, revision)
        ).fetchone()
        if not rev:
            raise DomainError("revision_not_found")
        return {
            "evidence_id": evidence_id,
            "revision": revision,
            "content_hash": rev["content_hash"],
            "content": json.loads(rev["payload"]),
            "weak_identity": bool(row["weak_identity"]),
        }

    def revoke(self, source_id, reason="revoked"):
        if reason not in {"revoked", "expired"}:
            raise DomainError("invalid_revocation_reason")
        if not self.conn.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone():
            raise DomainError("source_not_found")
        from rednotebook.creative import purge_source

        purge_source(self, source_id)
        from rednotebook.browser_service import purge_captures

        purge_captures(self, source_id)
        with self.conn:
            self.conn.execute(
                "DELETE FROM finding_reviews WHERE run_id IN "
                "(SELECT run_id FROM research_sources WHERE source_id=?)",
                (source_id,),
            )
            count = self.conn.execute(
                "SELECT COUNT(*) FROM evidence WHERE source_id=?", (source_id,)
            ).fetchone()[0]
            self.conn.execute(
                "UPDATE research_runs SET state='revoked',report_json=NULL,updated_at=? "
                "WHERE id IN (SELECT run_id FROM research_sources WHERE source_id=?)",
                (stamp(self.clock()), source_id),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO tombstones SELECT id,source_id,? FROM evidence WHERE source_id=?",
                (reason, source_id),
            )
            self.conn.execute("DELETE FROM evidence WHERE source_id=?", (source_id,))
            self.conn.execute("UPDATE sources SET status=? WHERE id=?", (reason, source_id))
            self.audit(source_id, reason)
        return {"source_id": source_id, "status": reason, "deleted_evidence": count}

    def sweep(self):
        ids = [
            r["id"]
            for r in self.conn.execute(
                "SELECT id FROM sources WHERE status='active' AND expires_at<=?",
                (stamp(self.clock()),),
            )
        ]
        return {"purged": [self.revoke(x, "expired") for x in ids]}

    def job(self, job_id):
        row = self.conn.execute("SELECT report FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise DomainError("job_not_found")
        return (
            json.loads(row["report"]) if row["report"] else {"job_id": job_id, "state": "importing"}
        )
