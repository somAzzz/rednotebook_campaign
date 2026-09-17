import json
import subprocess
import sys


def run(*args):
    return subprocess.run(
        [sys.executable, "-m", "rednotebook", *map(str, args)], text=True, capture_output=True
    )


def test_cli_end_to_end(tmp_path):
    db = tmp_path / "demo.sqlite"
    fixture = tmp_path / "f0"
    assert run("fixtures", "--out", fixture).returncode == 0
    assert run("fixtures", "--out", fixture).returncode == 1  # no overwrite
    result = run(
        "--db", db, "import", "--file", fixture / "evidence.json", "--grant", fixture / "grant.json"
    )
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["new_evidence"] == 700 and report["rejected_rows"] == 3
    job = run("--db", db, "job", "inspect", report["job_id"])
    assert json.loads(job.stdout) == report
    quality = run("--db", db, "quality", "--brief", "demo-cando-001")
    assert quality.returncode == 0
    assert json.loads(quality.stdout)["notes"] == 200
    ranked = run("--db", db, "metrics", "rank", "--brief", "demo-cando-001")
    assert len(json.loads(ranked.stdout)["notes"]) == 200
    revoke = run("--db", db, "source", "revoke", "synthetic-f0")
    assert json.loads(revoke.stdout)["deleted_evidence"] == 700
    denied = run("--db", db, "quality", "--brief", "demo-cando-001", "--source", "synthetic-f0")
    assert denied.returncode == 1 and "source_inactive" in denied.stderr


def test_brief_and_schema():
    result = run("brief", "validate", "--file", "examples/brief.cando.synthetic.json")
    assert result.returncode == 0 and json.loads(result.stdout)["verified_fact_count"] == 0
    schema = run("schema", "evidence")
    assert json.loads(schema.stdout)["additionalProperties"] is False


def test_cli_does_not_implement_publish_and_requires_explicit_model_use():
    status = json.loads(run("status").stdout)
    assert status["network_access"] == "explicit_local_model_and_readonly_adapter"
    assert "analyse" in status["implemented"]
    assert run("publish").returncode == 2
