"""Reproducible S1–S3 synthetic integration/performance verification."""

import hashlib
import json
import platform
import resource
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from rednotebook.analysis.metrics import rank_observations
from rednotebook.analysis.quality import quality_report
from rednotebook.domain.models import SourceGrant
from rednotebook.fixtures import write_fixture
from rednotebook.importing import import_file
from rednotebook.storage import Database
from rednotebook.util import digest, now_utc

ROOT = Path(__file__).resolve().parents[1]


def main():
    started = perf_counter()
    now = now_utc()
    with TemporaryDirectory(prefix="rednotebook-verification-") as temporary:
        directory = Path(temporary)
        write_fixture(directory / "f0", now)
        grant = SourceGrant.model_validate_json((directory / "f0/grant.json").read_bytes())
        with Database(directory / "test.sqlite", clock=lambda: now) as db:
            first = import_file(db, directory / "f0/evidence.json", grant)
            second = import_file(db, directory / "f0/evidence.csv", grant)
            assert first["new_evidence"] == 700 and first["new_metrics"] == 603
            assert first["rejected_rows"] == 3
            assert second["new_evidence"] == second["new_revisions"] == second["new_metrics"] == 0
            records, blocked = db.observations("demo-cando-001")
            quality = quality_report(records, blocked)
            ranking = rank_observations(records)
            assert quality["notes"] == 200 and quality["comments"] == 500
            score_count = sum(x["score"] is not None for x in ranking["notes"])
            assert score_count == 197
            assert db.revoke(grant.id)["deleted_evidence"] == 700
            assert db.observations("demo-cando-001")[0] == []
            assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    duration = perf_counter() - started
    memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_mb = memory / (1024 * 1024) if sys.platform == "darwin" else memory / 1024
    assert duration <= 60 and peak_mb < 1024
    code_paths = sorted((ROOT / "src").rglob("*.py"))
    code_hash = digest(
        {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in code_paths}
    )
    print(
        json.dumps(
            {
                "result": "pass",
                "scope": "S1-S3 synthetic engineering only",
                "at": now.isoformat(),
                "platform": platform.platform(),
                "python": platform.python_version(),
                "code_sha256": code_hash,
                "lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
                "fixture_sha256": first["input_sha256"],
                "elapsed_seconds": round(duration, 3),
                "peak_rss_mb": round(peak_mb, 1),
                "unique_notes": 200,
                "unique_comments": 500,
                "metric_snapshots": 603,
                "valid_input_rows": 702,
                "rejected_rows": 3,
                "scored_notes": score_count,
                "repeat_import_added_records": 0,
                "revocation_purged_records": 700,
                "real_data_research_validated": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
