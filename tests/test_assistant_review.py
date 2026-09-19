"""Offline synthetic consent, integrity, research and topic/campaign integration."""

import asyncio
import hashlib
import json
from datetime import timedelta

import pytest
from test_personal_workspace import CaptureReader, allowed, brief, submit_capture

from rednotebook import campaign
from rednotebook.browser_service import BrowserService, JobRequest, capture_path
from rednotebook.campaign_contracts import ContentBrief
from rednotebook.errors import DomainError
from rednotebook.research.assistant_review import (
    PageReading,
    assemble_topic,
    authorized_capture,
    prepare,
    record_consent,
    submit,
)
from rednotebook.research.contracts import BatchDraft
from rednotebook.research.store import read_run
from rednotebook.search_intent import SearchIntent
from rednotebook.topic_research import TopicSelection


class ImageReader(CaptureReader):
    async def collect(self, url, target, *args, **kwargs):
        result = await super().collect(url, target, *args, **kwargs)
        # Explicitly synthetic image bytes; no real account or model access.
        path = target / "01.image"
        path.write_bytes(b"synthetic image fixture")
        result.update(
            declared_total=1,
            images=[
                dict(
                    position=1, path=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest()
                )
            ],
        )
        return result

    async def search(self, keyword, limit, scrolls, checkpoint):
        ident = "b" * 24
        url = "https://www.xiaohongshu.com/explore/" + ident
        return {
            "state": "partial",
            "candidates": [dict(note_id=ident, title="synthetic", public_url=url, collect_url=url)],
        }


async def setup(db, grant, tmp_path):
    db.register_grant(allowed(grant))
    service = BrowserService(db, tmp_path / "profile", tmp_path / "config", ImageReader())
    ident = submit_capture(service, grant)
    await service.tasks[ident]
    path = capture_path(db, ident)
    manifest = json.loads((path / "manifest.json").read_text())
    page = PageReading(
        position=1,
        sha256=manifest["images"][0]["sha256"],
        text="合成图片摘记",
        key_content_readable=True,
    )
    return service, ident, page


def test_scoped_consent_integrity_and_no_widening(db, grant, tmp_path, clock):
    async def run():
        service, ident, page = await setup(db, grant, tmp_path)
        with pytest.raises(DomainError):
            prepare(service, ident, brief(), [page])
        record_consent(service, ident, "Synthetic explicit approval")
        before = db.conn.execute("SELECT grant_hash FROM sources").fetchone()[0]
        with pytest.raises(DomainError, match="assistant_pages_incomplete"):
            prepare(service, ident, brief(), [])
        with pytest.raises(DomainError, match="assistant_pages_incomplete"):
            prepare(service, ident, brief(), [page, page])
        image = capture_path(db, ident) / "01.image"
        old = image.read_bytes()
        image.write_bytes(b"changed")
        with pytest.raises(DomainError, match="assistant_image_hash_mismatch"):
            authorized_capture(service, ident)
        image.write_bytes(old)
        unreadable = page.model_copy(update={"key_content_readable": False})
        with pytest.raises(DomainError, match="assistant_key_content_unreadable"):
            prepare(service, ident, brief(), [unreadable])
        prepared = prepare(service, ident, brief(), [page])
        assert prepared["state"] == "prepared"
        assert db.require_source(grant.id).permissions.cloud_processing != "allowed"
        assert db.conn.execute("SELECT grant_hash FROM sources").fetchone()[0] == before
        clock[0] += timedelta(days=4000)
        with pytest.raises(DomainError, match="source_expired"):
            authorized_capture(service, ident)
        await service.close()

    asyncio.run(run())


def test_assistant_to_topic_campaign_and_revocation(db, grant, tmp_path):
    async def run():
        service, ident, page = await setup(db, grant, tmp_path)
        record_consent(service, ident, "Synthetic explicit approval")
        prepared = prepare(service, ident, brief(), [page])
        ref = prepared["catalog"][0]["ref_id"]
        draft = BatchDraft.model_validate(
            {
                "findings": [
                    dict(
                        claim="合成观察",
                        category="discussion",
                        support_ids=[ref],
                        counter_search="not_assessed",
                        limitation="合成夹具",
                    )
                ]
            }
        )
        with pytest.raises(DomainError, match="assistant_review_input_changed"):
            submit(service, ident, "bad", draft)
        wrong = draft.model_copy(deep=True)
        wrong.findings[0].support_ids = ["not-a-reference"]
        with pytest.raises(DomainError, match="unknown_reference_id"):
            submit(service, ident, prepared["input_sha256"], wrong)
        job = submit(service, ident, prepared["input_sha256"], draft)
        report = read_run(db, job["run_id"])
        assert report["metadata"]["model"]["provider"] == "codex-assistant"
        assert report["usage"] is None and not report["semantic_quality_verified"]
        plan = service.submit(
            JobRequest(
                operation="plan_search",
                source_id=grant.id,
                intent=SearchIntent(primary_query="synthetic"),
            )
        )
        await service.tasks[plan["job_id"]]
        search = service.submit(
            JobRequest(operation="search_plan", source_id=grant.id, plan_job_id=plan["job_id"])
        )
        await service.tasks[search["job_id"]]
        selection = TopicSelection(
            search_job_id=search["job_id"],
            sample_exception="synthetic single note",
            notes=[
                dict(
                    note_id="b" * 24,
                    reason="synthetic",
                    question=brief().research_question,
                    coverage=["synthetic"],
                )
            ],
        )
        with pytest.raises(DomainError, match="assistant_topic_jobs_incomplete"):
            assemble_topic(service, grant.id, selection, brief(), {})
        changed = selection.model_copy(deep=True)
        changed.notes[0].question = "unreviewed question"
        with pytest.raises(DomainError, match="assistant_topic_child_scope_mismatch"):
            assemble_topic(service, grant.id, changed, brief(), {"b" * 24: job["job_id"]})
        topic = assemble_topic(service, grant.id, selection, brief(), {"b" * 24: job["job_id"]})
        assert topic["campaign_ready"] and topic["completed_count"] == 1
        bundle = campaign.create(
            db,
            ContentBrief(
                id="synthetic-assistant-campaign",
                synthetic=True,
                creator_goal="合成",
                audience="合成",
                content_role="合成",
                reader_takeaway="合成",
                research_mode="research_then_plan",
                topic_job_id=topic["job_id"],
            ),
        )
        assert bundle["version"] == 1
        db.revoke(grant.id)
        assert not capture_path(db, ident).exists()
        with pytest.raises(DomainError, match="research_revoked"):
            read_run(db, job["run_id"])
        await service.close()

    asyncio.run(run())


def test_assistant_consent_cannot_be_replayed_or_forged(db, grant, tmp_path):
    async def run():
        service, ident, page = await setup(db, grant, tmp_path)
        record_consent(service, ident, "Synthetic explicit approval")
        target = capture_path(db, ident)
        with db.conn:
            db.conn.execute("DELETE FROM audit WHERE action=?", ("scoped_codex_consent:" + ident,))
        with pytest.raises(DomainError, match="capture_cloud_consent_required"):
            prepare(service, ident, brief(), [page])
        record_consent(service, ident, "Synthetic explicit approval")
        path = target / "evidence.json"
        path.write_text(path.read_text() + " ")
        with pytest.raises(DomainError, match="capture_cloud_consent_hash_mismatch"):
            prepare(service, ident, brief(), [page])
        await service.close()

    asyncio.run(run())


def test_assistant_migration_preserves_existing_grants(tmp_path, grant, clock):
    from rednotebook.storage import Database

    path = tmp_path / "migration.sqlite"
    with Database(path, clock=lambda: clock[0]) as original:
        original.register_grant(grant)
        old = tuple(original.conn.execute("SELECT * FROM sources").fetchone())
        with original.conn:
            original.conn.execute(
                "DELETE FROM settings WHERE key='assistant_review_contract_version'"
            )
            original.conn.execute("PRAGMA user_version=11")
    with Database(path, clock=lambda: clock[0]) as upgraded:
        assert upgraded.conn.execute("PRAGMA user_version").fetchone()[0] == 13
        assert tuple(upgraded.conn.execute("SELECT * FROM sources").fetchone()) == old
        assert (
            upgraded.conn.execute(
                "SELECT value FROM settings WHERE key='assistant_review_contract_version'"
            ).fetchone()[0]
            == "1"
        )
