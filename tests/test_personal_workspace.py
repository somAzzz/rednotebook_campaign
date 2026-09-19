"""Offline recovery, orchestration, discovery and retention acceptance."""

import asyncio
import json
from datetime import timedelta

import pytest
from pydantic import ValidationError
from pydantic_ai.models.function import FunctionModel
from test_research import good_response

from rednotebook import creative, workspace
from rednotebook.browser_service import BrowserService, JobRequest, capture_path
from rednotebook.domain.models import ResearchBrief
from rednotebook.errors import DomainError
from rednotebook.importing import import_file, validation_issues
from rednotebook.research.config import LocalModelConfig, ResearchBudget, research_budget
from rednotebook.research.fixtures import research_fixture
from rednotebook.research.runner import analyse
from rednotebook.research.store import read_run
from rednotebook.storage import Database

NOTE = "https://www.xiaohongshu.com/explore/" + "b" * 24 + "?xsec_token=PRIVATE_SENTINEL"


def allowed(grant):
    return grant.model_copy(
        update={"permissions": grant.permissions.model_copy(update={"automated_access": "allowed"})}
    )


def brief():
    return ResearchBrief(
        id="personal",
        synthetic=True,
        product="合成测试",
        audience="测试",
        research_question="合成问题",
        objective="测试",
        keywords=["测试"],
    )


def config():
    return LocalModelConfig(
        base_url="http://127.0.0.1:30000/v1",
        model="offline",
        api_key="NO_NETWORK",
        timeout_seconds=1,
    )


class CaptureReader:
    def __init__(self, failure=None, block=False):
        self.failure = failure
        self.block = block
        self.saved = asyncio.Event()
        self.calls = 0

    async def close(self):
        pass

    async def search(self, keyword, limit, scrolls, checkpoint):
        checkpoint()
        return {"state": "partial", "candidates": []}

    async def collect(
        self, url, target, comment_limit, max_images, checkpoint, on_progress, capture_images=True
    ):
        self.calls += 1
        checkpoint()
        raw = {
            "title": "合成标题",
            "text": "合成问题，不是真实研究",
            "metrics": {},
            "comments": [],
            "has_images": False,
            "identity": {"state": "verified", "note_id": url.split("/explore/")[1].split("?")[0]},
            "body_check": {"state": "complete"},
        }
        value = {
            "state": "partial",
            "raw": raw,
            "images": [],
            "declared_total": None,
            "errors": [],
        }
        on_progress(value, "text_saved")
        self.saved.set()
        if self.block:
            await asyncio.Event().wait()
        if self.failure:
            raise self.failure
        return {**value, "state": "complete", "errors": [], "declared_total": 0}


def submit_capture(service, grant):
    return service.submit(
        JobRequest(
            operation="collect",
            source_id=grant.id,
            url=NOTE,
            keyword="测试",
            brief_id="personal",
            comment_limit=0,
        )
    )["job_id"]


@pytest.mark.parametrize(
    "failure", [TimeoutError(), RuntimeError("SECRET_SENTINEL"), DomainError("captcha_required")]
)
def test_capture_failure_keeps_checkpoint_and_explicit_partial_import(db, grant, tmp_path, failure):
    async def run():
        db.register_grant(allowed(grant))
        service = BrowserService(
            db, tmp_path / "profile", tmp_path / "config", CaptureReader(failure)
        )
        ident = submit_capture(service, grant)
        await service.tasks[ident]
        job = service.get(ident, True)
        assert job["state"] in {"failed", "paused"}
        assert job["recovery"]["saved_capture_available"]
        assert job["progress"].get("failed_stage", job["progress"]["stage"]) == "text_saved"
        assert "SENTINEL" not in json.dumps(job)
        with pytest.raises(DomainError, match="partial_capture_requires_explicit_import"):
            service.import_capture(ident, False)
        assert service.import_capture(ident, False, True)["accepted_rows"] == 1
        assert service.gate.status()["paused"]
        db.revoke(grant.id)
        assert not capture_path(db, ident).exists()
        row = db.conn.execute(
            "SELECT result_json,progress_json FROM browser_jobs WHERE id=?", (ident,)
        ).fetchone()
        assert list(row) == [None, None]
        await service.close()

    asyncio.run(run())


def test_cancel_and_restart_keep_capture(db, grant, tmp_path):
    async def run():
        db.register_grant(allowed(grant))
        reader = CaptureReader(block=True)
        service = BrowserService(db, tmp_path / "profile", tmp_path / "config", reader)
        ident = submit_capture(service, grant)
        await reader.saved.wait()
        await service.cancel(ident)
        assert service.get(ident)["state"] == "cancelled"
        await service.close()
        restarted = BrowserService(db, tmp_path / "profile", tmp_path / "config", CaptureReader())
        assert restarted.get(ident)["recovery"]["saved_capture_available"]
        assert restarted.import_capture(ident, False, True)["state"] == "complete"
        await restarted.close()

    asyncio.run(run())


def test_browser_runs_while_model_is_busy(db, grant, tmp_path):
    async def run():
        db.register_grant(allowed(grant))
        service = BrowserService(db, tmp_path / "profile", tmp_path / "config", CaptureReader())
        await service.model_lock.acquire()
        try:
            model = service.submit(
                JobRequest(operation="research", source_id=grant.id, brief=brief())
            )["job_id"]
            search = service.submit(
                JobRequest(operation="search", source_id=grant.id, keyword="测试")
            )["job_id"]
            await asyncio.wait_for(service.tasks[search], 1)
            assert service.get(search)["state"] == "partial"
            assert service.get(model)["state"] == "queued"
            await service.cancel(model)
        finally:
            service.model_lock.release()
            await service.close()

    asyncio.run(run())


def wire_offline_model(monkeypatch):
    import rednotebook.browser_service as module

    monkeypatch.setattr(module, "load_config", lambda _: config())

    async def offline(db, brief, config, budget, progress=None):
        return await analyse(
            db, brief, config, budget, model=FunctionModel(good_response), progress=progress
        )

    monkeypatch.setattr(module, "analyse", offline)


def test_workflow_end_to_end_and_history_without_network(db, grant, tmp_path, monkeypatch):
    wire_offline_model(monkeypatch)

    async def run():
        db.register_grant(allowed(grant))
        reader = CaptureReader()
        service = BrowserService(db, tmp_path / "profile", tmp_path / "config", reader)
        job = service.submit(
            JobRequest(
                operation="workflow",
                source_id=grant.id,
                brief=brief(),
                brief_id="personal",
                url=NOTE,
                keyword="测试",
                analyse_images=False,
            )
        )["job_id"]
        await service.tasks[job]
        result = service.get(job, True)
        assert result["state"] == "complete", result
        assert result["result"]["research"]["findings"]
        assert result["result"]["research"]["usage"]["calls"] == 1
        assert reader.calls == 1
        notes = workspace.history(db, "notes", "合成", limit=1)
        assert len(notes["items"]) == 1
        assert "xsec_token" not in json.dumps(notes)
        assert workspace.history(db, "jobs")["items"][0]["job_id"] == job
        assert workspace.history(db, "research")["items"]
        await service.close()

    asyncio.run(run())


def test_saved_workflow_continues_while_browser_paused(db, grant, tmp_path, monkeypatch):
    wire_offline_model(monkeypatch)

    async def run():
        db.register_grant(allowed(grant))
        reader = CaptureReader(TimeoutError())
        service = BrowserService(db, tmp_path / "profile", tmp_path / "config", reader)
        capture = submit_capture(service, grant)
        await service.tasks[capture]
        assert service.gate.status()["paused"]
        request = JobRequest(
            operation="workflow",
            source_id=grant.id,
            brief=brief(),
            brief_id="personal",
            capture_id=capture,
            allow_partial=True,
            analyse_images=False,
        )
        job = service.submit(request)["job_id"]
        await service.tasks[job]
        result = service.get(job, True)
        assert result["state"] == "partial", result
        assert result["result"]["research"]["state"] == "complete"
        assert reader.calls == 1
        assert service.gate.status()["paused"]
        await service.close()

    asyncio.run(run())


def test_feedback_retention_and_draft_immutability(db, clock, write_rows):
    grant, research_brief, rows = research_fixture(clock[0])
    import_file(db, write_rows(rows), grant)
    report = asyncio.run(analyse(db, research_brief, config(), model=FunctionModel(good_response)))
    ident = report["run_id"]
    original = read_run(db, ident)
    bundle = creative.propose(db, ident)
    finding = report["findings"][0]["id"]
    workspace.record_feedback(
        db,
        ident,
        finding,
        workspace.FindingFeedback(
            decision="correct",
            corrected_claim="用户修正的合成观察",
            user_instruction="把这条改为用户修正的合成观察",
        ),
    )
    assert read_run(db, ident) == original
    assert (
        workspace.reviewed_report(db, ident)["findings"][0]["user_feedback"]["decision"]
        == "correct"
    )
    newer = creative.propose(db, ident)
    assert newer["payload"]["editorial"]["pages"][1]["body"] == "用户修正的合成观察"
    assert creative.load_bundle(db, bundle["id"], 1)["content_hash"] == bundle["content_hash"]
    workspace.record_feedback(
        db,
        ident,
        finding,
        workspace.FindingFeedback(decision="exclude", user_instruction="排除这条"),
    )
    with pytest.raises(DomainError, match="research_has_no_findings"):
        creative.propose(db, ident)
    clock[0] += timedelta(days=31)
    assert workspace.history(db, "notes")["items"] == []
    with pytest.raises(DomainError):
        workspace.reviewed_report(db, ident)
    db.sweep()
    assert db.conn.execute("SELECT COUNT(*) FROM finding_reviews").fetchone()[0] == 0


def test_schema_six_migration_preserves_existing_jobs(tmp_path, grant, clock):
    path = tmp_path / "upgrade.sqlite"
    with Database(path, clock=lambda: clock[0]) as db:
        db.register_grant(grant)
        db.conn.execute("DROP TABLE diagnostic_events")
        db.conn.execute("DROP TABLE bundle_sources")
        db.conn.execute("DROP TABLE bundle_runs")
        db.conn.execute("DROP TABLE finding_reviews")
        db.conn.execute("ALTER TABLE browser_jobs DROP COLUMN progress_json")
        db.conn.execute("PRAGMA user_version=6")
        db.conn.execute(
            "INSERT INTO browser_jobs VALUES ('old',?,'complete','{}',NULL,NULL,'then','then')",
            (grant.id,),
        )
        db.conn.commit()
    with Database(path, clock=lambda: clock[0]) as db:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 13
        assert db.conn.execute("SELECT id,progress_json FROM browser_jobs").fetchone()[0] == "old"
        assert db.require_source(grant.id).id == grant.id


def test_budget_generous_and_override_not_silent():
    budget = research_budget()
    assert budget.max_model_calls == 60
    assert budget.max_input_tokens == 4_000_000
    assert budget.max_chars_per_field == 50_000
    assert research_budget("quick").max_chars_per_field == 50_000
    assert research_budget("deep", {"max_model_calls": 100}).max_model_calls == 100
    with pytest.raises(ValidationError):
        research_budget("deep", {"max_model_calls": 99999})
    with pytest.raises(ValidationError) as caught:
        ResearchBudget.model_validate({"SECRET_SENTINEL": "SECRET_SENTINEL"})
    assert "SECRET_SENTINEL" not in json.dumps(validation_issues(caught.value))


def test_real_playwright_local_gallery_workflow(db, grant, tmp_path, monkeypatch):
    import base64
    from io import BytesIO

    from PIL import Image
    from test_browser import OfflineGate

    import rednotebook.browser_service as module
    from rednotebook.adapters import browser
    from rednotebook.research.gallery import analyse_gallery

    wire_offline_model(monkeypatch)

    async def fake_vision(*args):
        return {
            "requests": 1,
            "state": "complete",
            "media_text": "合成测试图片文字",
            "model": "offline",
        }

    async def offline_gallery(db, manifest, target, config, max_images, progress=None):
        return await analyse_gallery(
            db, manifest, target, config, max_images, analyser=fake_vision, progress=progress
        )

    monkeypatch.setattr(module, "analyse_gallery", offline_gallery)

    async def run():
        db.register_grant(allowed(grant))
        reader = browser.BrowserReader(
            tmp_path / "profile", headless=True, gate=OfflineGate(tmp_path / "profile")
        )
        await reader.open()
        from test_browser import offline_note_location

        offline_note_location(reader, NOTE)
        payloads = {}
        for color in ("red", "blue"):
            output = BytesIO()
            Image.new("RGB", (300, 400), color).save(output, format="PNG")
            url = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()
            payloads[url] = output.getvalue()
        urls = list(payloads)
        html = (
            '<div id="noteContainer" data-note-id="bbbbbbbbbbbbbbbbbbbbbbbb"><div id="detail-title">合成标题</div><div id="detail-desc">合成问题</div><div class="note-slider-img"><img src="'
            + urls[0]
            + '"></div></div><div class="pagination-teleport-container">'
        )
        for index, url in enumerate(urls):
            html += (
                "<button class=\"pagination-item\" onclick=\"document.querySelector('img').src='"
                + url
                + "'\">"
                + str(index)
                + "</button>"
            )
        html += "</div>"

        async def navigate(_):
            await reader.page.set_content(html)

        async def check():
            pass

        async def download(_, url):
            return payloads[url]

        reader.navigate = navigate
        reader.check = check
        monkeypatch.setattr(browser, "download_image", download)
        service = BrowserService(db, tmp_path / "profile", tmp_path / "config", reader)
        try:
            ident = service.submit(
                JobRequest(
                    operation="workflow",
                    source_id=grant.id,
                    brief=brief(),
                    brief_id="personal",
                    url=NOTE,
                    keyword="测试",
                    comment_limit=0,
                )
            )["job_id"]
            await asyncio.wait_for(service.tasks[ident], 10)
            result = service.get(ident, True)
            assert result["state"] == "complete", result
            assert result["result"]["gallery"]["processed"] == 2
            rows, _ = db.observations("personal")
            note = rows[0]["content"]
            assert len(note["media_provenance"]) == 2
            assert "第1/2张" in note["media_text"] and "第2/2张" in note["media_text"]
            assert not any(p["human_verified"] for p in note["media_provenance"])
            assert result["result"]["research"]["visual_analysis"] == "imported_machine_extractions"
            # Text-only mode must not guess an image count or click any pagination.
            text_capture = await reader.collect(NOTE, tmp_path, 0, 20, capture_images=False)
            assert text_capture["image_capture"] == "not_requested"
            assert text_capture["declared_total"] is None
        finally:
            await service.close()

    asyncio.run(run())


def test_long_evidence_is_fully_preserved_in_narrow_reference_spans(db, clock, write_rows):
    from rednotebook.research.gateway import EvidenceGateway

    grant, research_brief, rows = research_fixture(clock[0])
    rows[0]["text"] = "这是明确标注的合成材料。" * 500
    import_file(db, write_rows(rows), grant)
    records, _ = db.observations(research_brief.id)
    gateway = EvidenceGateway(db, records, 50000)
    note = next(r for r in records if r["content"]["text"] == rows[0]["text"])
    refs = [
        r for r in gateway.catalog.values() if r.evidence_id == note["id"] and r.field == "text"
    ]
    assert len(refs) > 1
    assert "".join(r.excerpt for r in refs) == rows[0]["text"]
    assert all(len(r.excerpt) <= 1200 for r in refs)
    assert all(gateway.validate_citation(r)["origin_label"] == "正文" for r in refs)
    assert not any(r["text_truncated"] for r in gateway.prompt_catalog())
