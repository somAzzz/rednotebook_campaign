import json
import sqlite3
from copy import deepcopy
from datetime import timedelta

import pytest

from rednotebook.analysis.quality import quality_report
from rednotebook.domain.models import SourceGrant
from rednotebook.errors import DomainError
from rednotebook.fixtures import write_fixture
from rednotebook.importing import import_file

BRIEF = "demo-cando-001"


def count(db, table):
    return db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_f0_counts_errors_idempotence_and_coverage(db, grant, fixture_data, write_rows):
    path = write_rows(fixture_data[1])
    first = import_file(db, path, grant)
    assert (first["state"], first["accepted_rows"], first["rejected_rows"]) == ("partial", 702, 3)
    assert (first["new_evidence"], first["new_revisions"], first["new_metrics"]) == (700, 700, 603)
    assert [e["row"] for e in first["errors"]] == [703, 704, 705]
    assert first["errors"][0]["issues"][0]["field"] == "metrics.0.value"
    second = import_file(db, path, grant)
    assert second["new_evidence"] == second["new_revisions"] == second["new_metrics"] == 0
    assert count(db, "evidence") == 700 and count(db, "metrics") == 603
    observations, excluded = db.observations(BRIEF)
    report = quality_report(observations, excluded)
    assert (report["notes"], report["comments"], report["synthetic_records"]) == (200, 500, 700)
    assert report["notes_with_unknown_author"] == 1
    cov = next(c for c in report["comment_coverage"] if c["imported_comments"] == 10)
    assert cov["reported_total"] == 30 and cov["truncated"] and cov["mode"] == "hot"
    assert report["missing_metrics"]["saves"] == 1
    assert "comment-author-0" not in json.dumps(observations)
    assert count(db, "jobs") == 2


def test_csv_matches_json_and_preserves_bad_rows(db, tmp_path, clock, grant):
    write_fixture(tmp_path / "f0", clock[0])
    first = import_file(db, tmp_path / "f0/evidence.csv", grant)
    assert first["accepted_rows"] == 702 and first["rejected_rows"] == 3
    assert [e["row"] for e in first["errors"]] == [704, 705, 706]
    repeat = import_file(db, tmp_path / "f0/evidence.json", grant)
    assert repeat["new_evidence"] == repeat["new_metrics"] == repeat["new_revisions"] == 0


def test_historical_revisions_are_immutable_and_latest_is_observation_time(
    db, grant, fixture_data, write_rows
):
    row = deepcopy(fixture_data[1][10])
    import_file(db, write_rows([row]), grant)
    eid = db.observations(BRIEF)[0][0]["id"]
    old = db.inspect(eid, 1)
    row["text"] = "第二版正文"
    row["observed_at"] = "2026-09-15T12:00:00Z"
    for metric in row["metrics"]:
        metric["observed_at"] = row["observed_at"]
    import_file(db, write_rows([row]), grant)
    assert db.inspect(eid)["revision"] == 2
    import_file(db, write_rows([fixture_data[1][10]]), grant)
    assert db.inspect(eid, 1) == old
    assert db.inspect(eid)["revision"] == 2


def test_conflict_rolls_back_whole_row_not_other_rows(db, grant, fixture_data, write_rows):
    row = deepcopy(fixture_data[1][10])
    import_file(db, write_rows([row]), grant)
    row["metrics"][0]["value"] = 999
    row["metrics"][0]["raw_display"] = "999"
    report = import_file(db, write_rows([row, fixture_data[1][11]]), grant)
    assert report["state"] == "partial" and report["accepted_rows"] == 1
    assert report["errors"][0]["issues"][0]["code"] == "metric_snapshot_conflict"
    assert count(db, "evidence") == 2 and count(db, "metrics") == 6


def test_infrastructure_failure_atomic_rollback_then_retry(
    db, grant, fixture_data, write_rows, monkeypatch
):
    original = db.insert_record
    calls = [0]

    def fail_second(record):
        calls[0] += 1
        if calls[0] == 2:
            raise sqlite3.OperationalError("simulated disk error with SECRET value")
        return original(record)

    monkeypatch.setattr(db, "insert_record", fail_second)
    path = write_rows(fixture_data[1][:5])
    with pytest.raises(sqlite3.OperationalError):
        import_file(db, path, grant)
    assert count(db, "evidence") == count(db, "metrics") == 0
    job = db.conn.execute("SELECT report FROM jobs").fetchone()[0]
    assert "SECRET" not in job and json.loads(job)["state"] == "failed"
    monkeypatch.setattr(db, "insert_record", original)
    assert import_file(db, path, grant)["new_evidence"] == 5


def test_keyboard_interrupt_rolls_back_batch(db, grant, fixture_data, write_rows, monkeypatch):
    original = db.insert_record

    def interrupt(record):
        original(record)
        raise KeyboardInterrupt()

    monkeypatch.setattr(db, "insert_record", interrupt)
    with pytest.raises(KeyboardInterrupt):
        import_file(db, write_rows(fixture_data[1][:1]), grant)
    assert count(db, "evidence") == 0


def test_comments_before_parents_and_missing_parent(db, grant, fixture_data, write_rows):
    notes = fixture_data[1]
    report = import_file(db, write_rows([notes[200], notes[0], notes[699]]), grant)
    assert report["accepted_rows"] == 2
    assert report["errors"][0]["issues"][0]["code"] == "parent_note_not_found"


def test_revoke_deletes_and_blocks_reads_and_reimport(db, grant, fixture_data, write_rows):
    path = write_rows([fixture_data[1][0], fixture_data[1][200]])
    import_file(db, path, grant)
    eid = db.observations(BRIEF)[0][0]["id"]
    assert db.revoke(grant.id)["deleted_evidence"] == 2
    for table in ("evidence", "revisions", "captures", "metrics", "samples"):
        assert count(db, table) == 0
    with pytest.raises(DomainError, match="evidence_revoked"):
        db.inspect(eid)
    with pytest.raises(DomainError, match="source_inactive"):
        import_file(db, path, grant)
    assert db.observations(BRIEF)[0] == []
    assert db.observations(BRIEF)[1][0]["reason"] == "source_inactive"


def test_expired_retention_denies_then_sweeps(db, grant, fixture_data, write_rows, clock):
    short = grant.model_copy(update={"retention_days": 1})
    import_file(db, write_rows([fixture_data[1][0]]), short)
    eid = db.observations(BRIEF)[0][0]["id"]
    clock[0] += timedelta(days=2)
    with pytest.raises(DomainError, match="source_expired"):
        db.inspect(eid)
    assert not db.observations(BRIEF)[0]
    assert db.sweep()["purged"][0]["deleted_evidence"] == 1
    assert count(db, "revisions") == 0


def test_permissions_unknown_no_analysis_and_no_cloud(db, grant, fixture_data, write_rows):
    raw = grant.model_dump(mode="json")
    raw["permissions"]["local_analysis"] = "unknown"
    limited = SourceGrant.model_validate(raw)
    import_file(db, write_rows([fixture_data[1][0]]), limited)
    assert not db.observations(BRIEF)[0]
    eid = db.conn.execute("SELECT id FROM evidence").fetchone()[0]
    with pytest.raises(DomainError, match="local_analysis_permission_required"):
        db.inspect(eid)
    with pytest.raises(DomainError, match="cloud_processing_permission_required"):
        db.require_source(grant.id, "cloud_processing")


def test_different_brief_and_source_cannot_leak(db, grant, fixture_data, write_rows):
    import_file(db, write_rows([fixture_data[1][0]]), grant)
    assert db.observations("other-brief")[0] == []
    raw = deepcopy(fixture_data[1][1])
    raw["source_id"] = "other-source"
    report = import_file(db, write_rows([raw]), grant)
    assert report["state"] == "failed" and count(db, "evidence") == 1


def test_grant_cannot_be_silently_extended_or_replaced(db, grant):
    db.register_grant(grant)
    with pytest.raises(DomainError, match="grant_immutable"):
        db.register_grant(grant.model_copy(update={"retention_days": 31}))


def test_errors_do_not_echo_untrusted_input(db, grant, fixture_data, write_rows):
    row = deepcopy(fixture_data[1][0])
    row["followers"] = "SECRET_TOKEN"
    report = import_file(db, write_rows([row]), grant)
    assert "SECRET_TOKEN" not in json.dumps(report)


def test_multiple_keywords_deduplicate_and_weak_identity_is_explicit(
    db, grant, fixture_data, write_rows
):
    row = deepcopy(fixture_data[1][10])
    row["external_id"] = None
    second = deepcopy(row["sampling"][0])
    second["keyword"] = "收藏夹吃灰"
    second["run_id"] = "other-keyword"
    row["sampling"].append(second)
    import_file(db, write_rows([row, row]), grant)
    records, blocked = db.observations(BRIEF)
    report = quality_report(records, blocked)
    assert report["notes"] == 1 and report["weak_identity_records"] == 1
    assert len(records[0]["samples"]) == 2


def test_hot_comments_and_author_replies_count_as_imported_not_popularity(
    db, grant, fixture_data, write_rows
):
    note = deepcopy(fixture_data[1][0])
    comments = deepcopy(fixture_data[1][200:210])
    comments[-1].update(is_reply=True, is_author_reply=True)
    import_file(db, write_rows([note, *comments]), grant)
    cov = quality_report(*db.observations(BRIEF))["comment_coverage"][0]
    assert cov["top_level_imported"] == 9 and cov["replies_imported"] == 1
    assert cov["author_replies"] == 1
    assert cov["scope"] == "imported_only_not_verified_complete"


def test_local_pipeline_never_opens_network(db, grant, fixture_data, write_rows, monkeypatch):
    import socket

    from rednotebook.analysis.metrics import rank_observations

    def deny(*args, **kwargs):
        raise AssertionError("unexpected network call")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    import_file(db, write_rows([fixture_data[1][0], fixture_data[1][200]]), grant)
    records, blocked = db.observations(BRIEF)
    assert quality_report(records, blocked)["comments"] == 1
    assert rank_observations(records)["notes"][0]["score"] is None


def test_schema_version_newer_than_supported_is_not_modified(tmp_path):
    from rednotebook.storage import Database

    path = tmp_path / "newer.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(DomainError, match="unsupported_database_version"):
        Database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99


def test_sampling_run_cannot_change_population(db, grant, fixture_data, write_rows):
    import_file(db, write_rows([fixture_data[1][10]]), grant)
    other = deepcopy(fixture_data[1][11])
    other["sampling"][0]["group"] = "popular"
    result = import_file(db, write_rows([other]), grant)
    assert result["state"] == "failed"
    assert result["errors"][0]["issues"][0]["code"] == "sampling_run_definition_conflict"
    assert count(db, "evidence") == 1


def test_sampling_same_observation_cannot_hide_truncation(db, grant, fixture_data, write_rows):
    row = deepcopy(fixture_data[1][10])
    import_file(db, write_rows([row]), grant)
    row["sampling"][0]["truncated"] = False
    result = import_file(db, write_rows([row]), grant)
    assert result["state"] == "failed"
    assert result["errors"][0]["issues"][0]["code"] == "sampling_observation_conflict"
