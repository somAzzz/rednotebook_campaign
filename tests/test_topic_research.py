"""Offline full topic workflow with synthetic captures and a deterministic test model."""

import asyncio
import json

import pytest
from pydantic import ValidationError
from test_personal_workspace import CaptureReader, allowed, brief, wire_offline_model

from rednotebook import campaign, creative
from rednotebook.browser_service import BrowserService, JobRequest
from rednotebook.campaign_contracts import ContentBrief
from rednotebook.errors import DomainError
from rednotebook.search_intent import SearchIntent
from rednotebook.topic_research import TopicSelection


class TopicReader(CaptureReader):
    def __init__(self, fail_at=None):
        super().__init__()
        self.candidates = 0
        self.fail_at = fail_at
        self.urls = []

    async def search(self, keyword, limit, scrolls, checkpoint):
        checkpoint()
        values = []
        for _ in range(limit):
            self.candidates += 1
            ident = f"{self.candidates:024x}"
            url = "https://www.xiaohongshu.com/explore/" + ident
            values.append(
                dict(
                    note_id=ident,
                    title="合成样本",
                    public_url=url,
                    collect_url=url + "?xsec_token=SECRET_SENTINEL",
                    metric_display=None,
                )
            )
        return {"state": "partial", "candidates": values, "truncated": True}

    async def collect(self, url, *args, **kwargs):
        self.urls.append(url)
        self.failure = DomainError("note_not_found") if len(self.urls) == self.fail_at else None
        return await super().collect(url, *args, **kwargs)


def content(**options):
    return ContentBrief(
        id="synthetic-topic",
        synthetic=True,
        creator_goal="合成策划",
        audience="测试",
        content_role="系列开场",
        reader_takeaway="三个项目",
        **options,
    )


async def setup(service, source_id):
    plan = service.submit(
        JobRequest(
            operation="plan_search",
            source_id=source_id,
            intent=SearchIntent(
                primary_query="NAS",
                category_hint="家庭存储",
                expansions=[
                    {"query": q, "kind": "scenario", "reason": "覆盖合成目标"}
                    for q in ["照片", "文件", "检索"]
                ],
            ),
        )
    )
    await service.tasks[plan["job_id"]]
    job = service.submit(
        JobRequest(operation="search_plan", source_id=source_id, plan_job_id=plan["job_id"])
    )
    await service.tasks[job["job_id"]]
    result = service.get(job["job_id"], True)["result"]
    assert len(result["candidates"]) == 35
    assert result["achieved_depth"] == "search_only" and not result["research_complete"]
    selection = TopicSelection(
        search_job_id=job["job_id"],
        notes=[
            dict(
                note_id=c["note_id"],
                reason="覆盖不同项目的合成候选",
                question=f"问题{i}",
                coverage=[f"项目{i}"],
            )
            for i, c in enumerate(result["candidates"][:3])
        ],
    )
    return JobRequest(
        operation="topic_research",
        source_id=source_id,
        topic_selection=selection,
        brief=brief(),
        analyse_images=False,
    )


def test_scan_details_campaign_and_revocation(db, grant, tmp_path, monkeypatch):
    wire_offline_model(monkeypatch)

    async def run():
        db.register_grant(allowed(grant))
        reader = TopicReader()
        service = BrowserService(db, tmp_path / "profile", tmp_path / "config", reader)
        request = await setup(service, grant.id)
        search_campaign = campaign.create(
            db,
            content(
                research_mode="search_only", search_job_id=request.topic_selection.search_job_id
            ),
        )
        stored = creative.load_bundle(db, search_campaign["id"], search_campaign["version"])
        assert "SECRET_SENTINEL" not in json.dumps(stored)
        assert stored["payload"]["context"]["mode"] == "search_assisted"
        ident = service.submit(request)["job_id"]
        await service.tasks[ident]
        job = service.get(ident, True)
        assert job["state"] == "complete", job
        result = job["result"]
        assert result["completed_count"] == 3 and result["campaign_ready"]
        assert len(reader.urls) == 3 and len(set(result["research_run_ids"])) == 3
        assert "SECRET_SENTINEL" not in json.dumps(result)
        draft = campaign.create(db, content(research_mode="research_then_plan", topic_job_id=ident))
        stored = creative.load_bundle(db, draft["id"], draft["version"])
        assert len(stored["payload"]["context"]["research"]) == 3
        assert (
            db.conn.execute(
                "SELECT count(*) FROM bundle_runs WHERE bundle_id=?", (draft["id"],)
            ).fetchone()[0]
            == 3
        )
        db.revoke(grant.id)
        assert (
            db.conn.execute("SELECT payload FROM bundles WHERE id=?", (draft["id"],)).fetchone()[0]
            is None
        )
        assert all(
            row[0] is None for row in db.conn.execute("SELECT result_json FROM browser_jobs")
        )
        await service.close()

    asyncio.run(run())


def test_stop_recover_resume_does_not_repeat_completed_notes(db, grant, tmp_path, monkeypatch):
    wire_offline_model(monkeypatch)

    async def run():
        db.register_grant(allowed(grant))
        reader = TopicReader(fail_at=2)
        service = BrowserService(db, tmp_path / "profile", tmp_path / "config", reader)
        request = await setup(service, grant.id)
        ident = service.submit(request)["job_id"]
        await service.tasks[ident]
        result = service.get(ident, True)["result"]
        assert len(reader.urls) == 2 and result["completed_count"] == 1
        assert (
            service.get(ident)["recovery"]["next_action"]
            == result["next_action"]
            == "recover_child_then_resume_topic"
        )
        assert not result["campaign_ready"]
        with pytest.raises(DomainError, match="campaign_detail_research_incomplete"):
            campaign.create(db, content(research_mode="research_then_plan", topic_job_id=ident))
        stopped = result["notes"][-1]
        raw = db.conn.execute(
            "SELECT request_json FROM browser_jobs WHERE id=?", (stopped["job_id"],)
        ).fetchone()[0]
        child = JobRequest.model_validate_json(raw)
        # Explicit saved-material recovery; no repeat browser navigation.
        recovery = service.submit(
            child.model_copy(
                update={"capture_id": stopped["job_id"], "url": "", "allow_partial": True}
            )
        )["job_id"]
        await service.tasks[recovery]
        # This capture was partial, so recovery remains partial: topic may not call it fully read.
        assert service.get(recovery)["state"] == "partial"
        resumed = service.submit(
            request.model_copy(
                update={
                    "resume_topic_job_id": ident,
                    "replacement_jobs": {stopped["note_id"]: recovery},
                }
            )
        )["job_id"]
        await service.tasks[resumed]
        assert service.get(resumed)["error_code"] == "topic_child_requires_explicit_recovery"
        assert len(reader.urls) == 2
        # User explicitly requests a fresh capture of the failed note, with the same research brief.
        recovered = service.submit(child)["job_id"]
        await service.tasks[recovered]
        assert service.get(recovered)["state"] == "complete"
        resumed = service.submit(
            request.model_copy(
                update={
                    "resume_topic_job_id": ident,
                    "replacement_jobs": {stopped["note_id"]: recovered},
                }
            )
        )["job_id"]
        await service.tasks[resumed]
        result = service.get(resumed, True)["result"]
        assert result["campaign_ready"] and result["completed_count"] == 3
        assert len(reader.urls) == 4 and reader.urls.count(reader.urls[0]) == 1
        await service.close()

    asyncio.run(run())


def test_selection_and_requested_depth_are_enforced(db):
    with pytest.raises(ValidationError):
        TopicSelection(
            search_job_id="x", notes=[dict(note_id="x", reason="r", question="q", coverage=["c"])]
        )
    with pytest.raises(DomainError, match="campaign_detail_research_required"):
        campaign.create(db, content(research_mode="research_then_plan"))
    assert campaign.create(db, content(research_mode="author_only"))["version"] == 1


def test_candidate_identity_source_and_pause_checks(db, grant, tmp_path):
    async def run():
        db.register_grant(allowed(grant))
        service = BrowserService(db, tmp_path / "profile", tmp_path / "config", TopicReader())
        request = await setup(service, grant.id)
        bad = request.topic_selection.model_copy(
            update={
                "notes": [
                    n.model_copy(update={"note_id": "f" * 24})
                    for n in request.topic_selection.notes
                ]
            }
        )
        with pytest.raises(DomainError, match="topic_candidate_not_found"):
            service.submit(request.model_copy(update={"topic_selection": bad}))
        other = allowed(grant).model_copy(update={"id": "other-synthetic"})
        db.register_grant(other)
        with pytest.raises(DomainError, match="topic_search_not_ready"):
            service.submit(request.model_copy(update={"source_id": other.id}))
        service.gate.pause("operator_paused")
        with pytest.raises(DomainError, match="browser_access_paused"):
            service.submit(request)
        assert service.gate.status()["paused"]
        assert service.reader.calls == 0
        await service.close()

    asyncio.run(run())


def test_cancel_parent_cancels_child_and_preserves_saved_scope(db, grant, tmp_path, monkeypatch):
    wire_offline_model(monkeypatch)

    async def run():
        db.register_grant(allowed(grant))
        reader = TopicReader()
        reader.block = True
        service = BrowserService(db, tmp_path / "profile", tmp_path / "config", reader)
        request = await setup(service, grant.id)
        ident = service.submit(request)["job_id"]
        await reader.saved.wait()
        with pytest.raises(DomainError, match="browser_job_already_active"):
            service.submit(JobRequest(operation="search", source_id=grant.id, keyword="overlap"))
        await service.cancel(ident)
        result = service.get(ident, True)["result"]
        entry = result["notes"][0]
        child = service.get(entry["job_id"], True)
        assert child["state"] == "cancelled" and child["recovery"]["saved_capture_available"]
        assert entry["research_brief"]["id"] == "topic-" + entry["original_job_id"]
        assert not result["campaign_ready"]
        await service.close()
        restarted = BrowserService(db, tmp_path / "profile", tmp_path / "config", TopicReader())
        assert restarted.get(ident, True)["result"]["notes"][0]["job_id"] == entry["job_id"]
        await restarted.close()

    asyncio.run(run())


def test_v9_migration_keeps_existing_payload_bytes(tmp_path, grant):
    from rednotebook.storage import Database

    path = tmp_path / "migration.sqlite"
    with Database(path) as database:
        database.register_grant(grant)
        draft = campaign.create(database, content())
        original = tuple(
            database.conn.execute("SELECT * FROM bundles WHERE id=?", (draft["id"],)).fetchone()
        )
        database.conn.execute("DELETE FROM settings WHERE key='topic_research_contract_version'")
        database.conn.execute("PRAGMA user_version=9")
        database.conn.commit()
    with Database(path) as database:
        assert database.conn.execute("PRAGMA user_version").fetchone()[0] == 13
        assert (
            database.conn.execute(
                "SELECT value FROM settings WHERE key='topic_research_contract_version'"
            ).fetchone()[0]
            == "1"
        )
        assert (
            tuple(
                database.conn.execute("SELECT * FROM bundles WHERE id=?", (draft["id"],)).fetchone()
            )
            == original
        )
        assert not database.conn.execute("PRAGMA foreign_key_check").fetchall()


def test_mcp_topic_tools_execute_and_expose_contract(db, grant, tmp_path, monkeypatch):
    from rednotebook.mcp_server import create_server

    wire_offline_model(monkeypatch)

    async def run():
        db.register_grant(allowed(grant))
        service = BrowserService(db, tmp_path / "profile", tmp_path / "config", TopicReader())
        request = await setup(service, grant.id)
        server = create_server(service)
        assert {"research_topic", "resume_topic_research"} <= {
            t.name for t in await server.list_tools()
        }
        assert "rednotebook://schemas/topic-selection" in {
            str(r.uri) for r in await server.list_resources()
        }
        result = await server.call_tool(
            "research_topic",
            {
                "source_id": grant.id,
                "selection": request.topic_selection.model_dump(),
                "brief": brief().model_dump(mode="json"),
                "analyse_images": False,
            },
        )
        assert not result.isError
        job_id = result.structuredContent["job_id"]
        await service.tasks[job_id]
        assert service.get(job_id, True)["result"]["campaign_ready"]
        await service.close()

    asyncio.run(run())


def test_topic_skips_video_and_continues_without_researching_it(db, grant, tmp_path, monkeypatch):
    wire_offline_model(monkeypatch)

    class VideoReader(TopicReader):
        async def collect(self, url, *args, **kwargs):
            if url.split("/explore/")[1].split("?")[0] == f"{2:024x}":
                return {"state": "skipped", "reason": "video_deferred"}
            return await super().collect(url, *args, **kwargs)

    async def run():
        db.register_grant(allowed(grant))
        service = BrowserService(db, tmp_path / "profile", tmp_path / "config", VideoReader())
        request = await setup(service, grant.id)
        job = service.submit(request)["job_id"]
        await service.tasks[job]
        result = service.get(job, True)["result"]
        assert result["skipped_video_count"] == 1
        assert result["completed_count"] == 2
        assert len(result["research_run_ids"]) == 2
        assert result["state"] == "partial" and not result["campaign_ready"]
        assert result["notes"][1]["result"] == {"state": "skipped", "reason": "video_deferred"}
        await service.close()

    asyncio.run(run())
