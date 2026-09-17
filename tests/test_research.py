import asyncio
import hashlib
import json
import sqlite3
from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError
from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from rednotebook.domain.models import ResearchBrief
from rednotebook.errors import DomainError
from rednotebook.importing import import_file
from rednotebook.research.config import LocalModelConfig, ResearchBudget
from rednotebook.research.fixtures import research_fixture
from rednotebook.research.gateway import EvidenceGateway
from rednotebook.research.model import request_guard
from rednotebook.research.render import markdown
from rednotebook.research.runner import analyse
from rednotebook.research.store import read_run, save_brief
from rednotebook.storage import Database
from rednotebook.storage.database import SCHEMA


@pytest.fixture
def local_config():
    return LocalModelConfig(
        base_url="http://127.0.0.1:30000/v1",
        model="test-qwen",
        api_key="TEST_SECRET",
        timeout_seconds=1,
    )


@pytest.fixture
def loaded(db, clock, write_rows):
    grant, brief, rows = research_fixture(clock[0])
    import_file(db, write_rows(rows), grant)
    return grant, brief


def payload(messages):
    for message in messages:
        for part in message.parts:
            if isinstance(part, UserPromptPart):
                return json.loads(part.content)
    raise AssertionError("missing evidence")


def good_response(messages, info):
    data = payload(messages)
    ref = next(r["ref_id"] for r in data["untrusted_evidence"] if r["field"] == "text")
    return ModelResponse(
        [
            ToolCallPart(
                "submit_research",
                {
                    "findings": [
                        {
                            "claim": "这条合成文本提出了一个有限问题。",
                            "category": "discussion",
                            "support_ids": [ref],
                            "counter_ids": [],
                            "counter_search": "not_assessed",
                            "limitation": "单条合成文本，不代表真实用户。",
                        }
                    ],
                    "gaps": [],
                },
            )
        ],
        usage=RequestUsage(input_tokens=100, output_tokens=60),
    )


def run_research(db, brief, config, function=good_response, **limits):
    return asyncio.run(
        analyse(db, brief, config, ResearchBudget(**limits), model=FunctionModel(function))
    )


def test_research_good_result_is_persisted_unreviewed(db, loaded, local_config):
    _, brief = loaded
    report = run_research(db, brief, local_config)
    assert report["state"] == "complete" and len(report["findings"]) == 1
    f = report["findings"][0]
    assert f["status"] == "hypothesis" and f["semantic_review"] == "pending"
    cite = f["support"][0]
    assert hashlib.sha256(cite["excerpt"].encode()).hexdigest() == cite["sha256"]
    assert cite["source"]["public_url"] is None
    assert report["sources"] == [cite["source"]]
    assert read_run(db, report["run_id"]) == report
    assert "TEST_SECRET" not in json.dumps(report)
    assert "原文" not in report["metadata"]
    assert "待验证假设" in markdown(report)


def test_fabricated_reference_has_bounded_repair_and_no_persisted_claim(db, loaded, local_config):
    _, brief = loaded
    calls = []

    def bad(messages, info):
        calls.append(1)
        response = good_response(messages, info)
        response.parts[0].args["findings"][0]["support_ids"] = ["made-up-id"]
        return response

    report = run_research(db, brief, local_config, bad)
    assert report["state"] == "failed" and report["findings"] == []
    assert len(calls) == 3 and report["usage"]["calls"] == 3
    assert report["errors"][0]["code"] == "model_output_invalid_after_retries"
    assert "made-up-id" not in json.dumps(read_run(db, report["run_id"]))


def test_bad_json_then_repair_counts_every_attempt(db, loaded, local_config):
    _, brief = loaded
    calls = []

    def sometimes(messages, info):
        calls.append(1)
        return (
            ModelResponse([ToolCallPart("submit_research", "not-json")])
            if len(calls) == 1
            else good_response(messages, info)
        )

    report = run_research(db, brief, local_config, sometimes)
    assert report["state"] == "complete" and report["usage"]["calls"] == 2


def test_tool_cannot_escalate_or_read_other_batch(db, loaded):
    _, brief = loaded
    records, _ = db.observations(brief.id)
    gateway = EvidenceGateway(db, [records[0]])
    for name in ("publish", "shell", "read_file", "fetch_url", "approve"):
        with pytest.raises(DomainError, match="tool_not_allowed"):
            gateway.dispatch(name)
    with pytest.raises(DomainError, match="note_outside_batch"):
        gateway.dispatch("get_note_evidence", "unknown")
    ref = next(iter(gateway.catalog.values()))
    with pytest.raises(DomainError, match="reference_span_invalid"):
        gateway.validate_citation(ref.model_copy(update={"end": 99999}))
    with pytest.raises(DomainError, match="reference_excerpt_mismatch"):
        gateway.validate_citation(ref.model_copy(update={"sha256": "0" * 64}))


def test_prompt_injection_cannot_call_unregistered_tool(db, loaded, local_config, monkeypatch):
    import socket

    monkeypatch.setattr(
        socket.socket,
        "connect",
        lambda *a: (_ for _ in ()).throw(AssertionError("network not allowed")),
    )
    _, brief = loaded

    def malicious(messages, info):
        assert "TEST_SECRET" not in str(messages)
        return ModelResponse([ToolCallPart("publish", {"text": "bad"})])

    report = run_research(db, brief, local_config, malicious)
    assert report["state"] == "failed" and report["tool_calls"] == []
    assert report["usage"]["calls"] <= 3


def test_token_budget_rejects_before_request(db, loaded, local_config):
    _, brief = loaded

    def never(*args):
        raise AssertionError("must not call model")

    report = run_research(db, brief, local_config, never, max_input_tokens=1)
    assert report["state"] == "failed" and report["usage"]["calls"] == 0
    assert report["errors"][0]["code"] == "research_budget_exhausted"


def test_partial_batch_results_survive_global_call_limit(db, loaded, local_config):
    _, brief = loaded
    report = run_research(db, brief, local_config, max_model_calls=1, batch_notes=1)
    assert report["state"] == "partial" and report["completed_batches"] == 1
    assert len(report["findings"]) == 1 and report["usage"]["calls"] == 1


def test_timeouts_stop_and_charge_failed_call(db, loaded, local_config):
    _, brief = loaded

    async def slow(messages, info):
        await asyncio.sleep(0.1)
        return good_response(messages, info)

    report = run_research(
        db, brief, local_config.model_copy(update={"timeout_seconds": 0.01}), slow
    )
    assert report["state"] == "failed" and report["usage"]["calls"] == 1
    assert report["errors"][0]["code"] == "model_timeout"


def test_revocation_removes_stored_derived_report(db, loaded, local_config):
    grant, brief = loaded
    report = run_research(db, brief, local_config)
    db.revoke(grant.id)
    with pytest.raises(DomainError, match="research_revoked"):
        read_run(db, report["run_id"])
    assert db.conn.execute("SELECT report_json FROM research_runs").fetchone()[0] is None


def test_expiry_blocks_report_and_sweep_purges(db, loaded, local_config, clock):
    _, brief = loaded
    report = run_research(db, brief, local_config)
    clock[0] += timedelta(days=8)
    with pytest.raises(DomainError, match="source_expired"):
        read_run(db, report["run_id"])
    db.sweep()
    assert db.conn.execute("SELECT report_json FROM research_runs").fetchone()[0] is None


def test_revocation_during_model_request_cannot_commit(db, loaded, local_config):
    grant, brief = loaded

    async def revoked(messages, info):
        db.revoke(grant.id)
        return good_response(messages, info)

    with pytest.raises(DomainError):
        run_research(db, brief, local_config, revoked)
    assert db.conn.execute("SELECT report_json FROM research_runs").fetchone()[0] is None


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/v1",
        "http://169.254.169.254/v1",
        "http://127.0.0.1/v1?token=x",
        "http://user:pass@127.0.0.1/v1",
    ],
)
def test_local_config_rejects_unapproved_destinations(url):
    with pytest.raises(ValidationError):
        LocalModelConfig(base_url=url, model="test", api_key="key")


def test_egress_guard_blocks_redirect_target_and_non_model_path():
    guard = request_guard("http://127.0.0.1:30000/v1")
    asyncio.run(guard(httpx.Request("POST", "http://127.0.0.1:30000/v1/chat/completions")))
    for url in (
        "https://example.com/v1/chat/completions",
        "http://127.0.0.1:30000/files",
        "http://127.0.0.1:30000/v1/models?a=1",
    ):
        with pytest.raises(DomainError, match="egress"):
            asyncio.run(guard(httpx.Request("POST", url)))


def test_schema_v1_upgrade_preserves_source_and_settings(tmp_path):
    path = tmp_path / "v1.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.execute("INSERT INTO settings VALUES (?,?)", ("author_salt", "a" * 64))
    with Database(path) as upgraded:
        assert upgraded.salt == "a" * 64
        assert upgraded.conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert upgraded.conn.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 0


def test_brief_versions_are_immutable(db, loaded):
    _, brief = loaded
    assert save_brief(db, brief) == 1
    changed = brief.model_copy(update={"objective": "new objective"})
    assert save_brief(db, changed) == 2
    assert save_brief(db, brief) == 1


def test_markdown_does_not_render_untrusted_images(db, loaded, local_config):
    _, brief = loaded
    report = run_research(db, brief, local_config)
    report["findings"][0]["claim"] = "![tracking](https://outside.test) <script>x</script>"
    output = markdown(report)
    assert "<script>" not in output and "![tracking]" not in output


def test_research_resolves_public_note_links_without_signed_parameters(
    db, clock, write_rows, local_config
):
    grant, brief, rows = research_fixture(clock[0])
    for row in rows:
        if row["kind"] == "note":
            row["locator"] = (
                "https://www.xiaohongshu.com/explore/aaaaaaaaaaaaaaaaaaaaaaaa?xsec_token=private"
            )
    import_file(db, write_rows(rows), grant)
    report = run_research(db, brief, local_config)
    citation = report["findings"][0]["support"][0]
    assert citation["source"]["public_url"] == (
        "https://www.xiaohongshu.com/explore/aaaaaaaaaaaaaaaaaaaaaaaa"
    )
    assert report["sources"] == [citation["source"]]
    rendered = markdown(report)
    assert (
        f"[{citation['source']['title']}]"
        "(<https://www.xiaohongshu.com/explore/aaaaaaaaaaaaaaaaaaaaaaaa>)" in rendered
    )
    assert "xsec_token" not in rendered


def test_empty_evidence_does_not_call_model(db, local_config):
    brief = ResearchBrief(
        id="empty",
        synthetic=True,
        product="test",
        audience="test",
        research_question="test",
        objective="test",
        keywords=["test"],
    )

    def never(*args):
        raise AssertionError("should not call")

    report = run_research(db, brief, local_config, never)
    assert report["state"] == "partial" and report["usage"]["calls"] == 0


def test_model_metrics_use_study_cohort_but_hide_other_note_rows(
    db, grant, fixture_data, write_rows
):
    from rednotebook.analysis.metrics import rank_observations

    import_file(db, write_rows(fixture_data[1][10:30]), grant)
    records, _ = db.observations("demo-cando-001")
    gateway = EvidenceGateway(db, records[:3], metric_context=rank_observations(records))
    metrics = gateway.dispatch("compare_metrics")
    assert len(metrics["notes"]) == 3
    assert all(row["score"] is not None and row["n"] == 20 for row in metrics["notes"])


def test_output_reservation_exhaustion_stops_invalid_retries(db, loaded, local_config):
    _, brief = loaded

    def bad(messages, info):
        return ModelResponse([ToolCallPart("submit_research", "invalid")])

    report = run_research(db, brief, local_config, bad, max_output_tokens=2048)
    assert report["state"] == "failed" and report["usage"]["calls"] == 1
    assert report["errors"][0]["code"] == "research_budget_exhausted"


def test_excerpt_truncation_is_visible_to_model_and_report(db, loaded, local_config):
    _, brief = loaded
    records, _ = db.observations(brief.id)
    gateway = EvidenceGateway(db, records, max_chars=5)
    assert gateway.truncated_fields > 0
    assert all(row["text_truncated"] for row in gateway.prompt_catalog())
    for ref in gateway.catalog:
        assert gateway.resolve(ref)["end"] == 5
