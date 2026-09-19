"""Synthetic caller path acceptance. Never accesses real accounts or model providers."""

import asyncio
import base64
import hashlib
import json

import pytest
from PIL import Image
from test_assistant_review import ImageReader, setup
from test_personal_workspace import CaptureReader, allowed, brief, submit_capture

from rednotebook import campaign, creative
from rednotebook.browser_service import BrowserService, JobRequest, capture_path
from rednotebook.campaign_contracts import ContentBrief, SemanticReview
from rednotebook.errors import DomainError
from rednotebook.mcp_server import create_server
from rednotebook.research.assistant_review import prepare, record_consent, submit
from rednotebook.research.caller import catalog, evidence_bundle, validate
from rednotebook.research.contracts import BatchDraft
from rednotebook.search_intent import SearchIntent
from rednotebook.topic_research import TopicSelection


class PngReader(ImageReader):
    async def collect(self, url, target, *args, **kwargs):
        result = await super().collect(url, target, *args, **kwargs)
        Image.new("RGB", (12, 16), "red").save(target / "01.image", format="PNG")
        result["images"][0]["sha256"] = hashlib.sha256(
            (target / "01.image").read_bytes()
        ).hexdigest()
        result["raw"]["text"] = "synthetic long body\n" * 250
        result["raw"]["comments"] = [
            {"id": "synthetic-comment-1", "text": "synthetic sampled comment", "likes": "0"},
            {"id": "synthetic-comment-2", "text": "synthetic missing metric"},
        ]
        return result


def draft(ref):
    return BatchDraft.model_validate(
        {
            "findings": [
                {
                    "claim": "Synthetic hypothesis",
                    "category": "discussion",
                    "support_ids": [ref],
                    "counter_search": "not_assessed",
                    "limitation": "Synthetic bounded fixture",
                }
            ]
        }
    )


def cloud(grant):
    grant = allowed(grant)
    return grant.model_copy(
        update={"permissions": grant.permissions.model_copy(update={"cloud_processing": "allowed"})}
    )


def test_mcp_raw_pages_images_and_pinned_findings(db, grant, tmp_path, monkeypatch):
    async def run():
        db.register_grant(cloud(grant))
        service = BrowserService(db, tmp_path / "profile", tmp_path / "missing-config", PngReader())
        service.caller_processor = "arbitrary-vision-agent"
        # Fail immediately if caller path tries to initialize any local model.
        monkeypatch.setattr(
            "rednotebook.browser_service.load_config", lambda *_: pytest.fail("model called")
        )
        capture = service.submit(
            JobRequest(
                operation="collect",
                source_id=grant.id,
                url="https://www.xiaohongshu.com/explore/" + "b" * 24,
                keyword="synthetic",
                brief_id="personal",
                comment_limit=5,
            )
        )["job_id"]
        await service.tasks[capture]
        server = create_server(service)
        chunks, offset, sha = [], 0, None
        while offset is not None:
            result = await server.call_tool(
                "read_evidence_bundle",
                dict(capture_id=capture, offset=offset, limit=2, expected_sha256=sha),
            )
            assert not result.isError
            page = result.structuredContent
            assert "PRIVATE_SENTINEL" not in result.content[0].text
            sha = page["bundle_sha256"]
            chunks.extend(page["chunks"])
            offset = page["next_offset"]
        assert page["comment_count"] == 2
        raw = json.loads((capture_path(db, capture) / "evidence.json").read_text())
        for index, record in enumerate(raw):
            assert (
                "".join(
                    c["text"] for c in chunks if c["record_index"] == index and c["field"] == "text"
                )
                == record["text"]
            )
        first = page["images"][0]
        result = await server.call_tool(
            "read_capture_image",
            dict(capture_id=capture, position=1, expected_sha256=first["sha256"]),
        )
        assert not result.isError and result.content[1].type == "image"
        assert result.content[1].mimeType == "image/png"
        assert (
            hashlib.sha256(base64.b64decode(result.content[1].data)).hexdigest() == first["sha256"]
        )
        bad = await server.call_tool(
            "read_capture_image", dict(capture_id=capture, position=1, expected_sha256="0" * 64)
        )
        assert (
            bad.isError and bad.structuredContent["error_code"] == "assistant_image_hash_mismatch"
        )
        prepared = await server.call_tool(
            "prepare_capture_review",
            dict(
                capture_id=capture,
                brief=brief().model_dump(mode="json"),
                pages=[
                    dict(
                        position=1,
                        sha256=first["sha256"],
                        text="Synthetic red test image",
                        key_content_readable=True,
                    )
                ],
            ),
        )
        assert not prepared.isError
        pinned = prepared.structuredContent["input_sha256"]
        entries = catalog(service, capture, pinned)
        ref = entries["catalog"][0]["ref_id"]
        assert entries["catalog"][0]["citation"]["sha256"]
        validated = validate(service, capture, pinned, draft(ref))
        assert not validated["semantic_quality_verified"]
        assert db.conn.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 0
        with pytest.raises(DomainError, match="unknown_reference_id"):
            validate(service, capture, pinned, draft("invented"))
        with pytest.raises(DomainError, match="assistant_review_input_changed"):
            validate(service, capture, "0" * 64, draft(ref))
        report = submit(service, capture, pinned, draft(ref))
        assert report["research"]["metadata"]["model"]["provider"] == "arbitrary-vision-agent"
        assert report["research"]["selected_comments"] == 2
        db.revoke(grant.id)
        denied = await server.call_tool("read_evidence_bundle", {"capture_id": capture})
        assert denied.isError
        assert not capture_path(db, capture).exists()
        await service.close()

    asyncio.run(run())


def test_scoped_codex_consent_does_not_authorize_other_agents(db, grant, tmp_path):
    async def run():
        service, capture, _ = await setup(db, grant, tmp_path)
        record_consent(service, capture, "Synthetic consent")
        service.caller_processor = "other-agent"
        with pytest.raises(DomainError, match="capture_cloud_consent_required"):
            evidence_bundle(service, capture)
        service.caller_processor = "codex-assistant"
        assert evidence_bundle(service, capture)["record_count"] == 1
        with pytest.raises(DomainError, match="evidence_bundle_changed"):
            evidence_bundle(service, capture, expected_sha256="0" * 64)
        await service.close()

    asyncio.run(run())


def test_text_only_caller_path_and_campaign_review(db, grant, tmp_path):
    async def run():
        db.register_grant(cloud(grant))
        service = BrowserService(db, tmp_path / "profile", tmp_path / "missing", CaptureReader())
        service.caller_processor = "text-agent"
        capture = submit_capture(service, grant)
        await service.tasks[capture]
        assert evidence_bundle(service, capture)["images"] == []
        prepared = prepare(service, capture, brief(), [])
        saved = submit(
            service, capture, prepared["input_sha256"], draft(prepared["catalog"][0]["ref_id"])
        )
        bundle = campaign.create(
            db,
            ContentBrief(
                id="synthetic-caller",
                synthetic=True,
                creator_goal="Synthetic",
                audience="Synthetic",
                content_role="Synthetic",
                reader_takeaway="Synthetic",
                research_run_ids=[saved["run_id"]],
            ),
        )
        server = create_server(service)
        args = dict(
            bundle_id=bundle["id"],
            version=bundle["version"],
            expected_hash=bundle["content_hash"],
            review=SemanticReview(issues=[]).model_dump(),
        )
        wrong = await server.call_tool("submit_campaign_review", args | {"expected_hash": "0" * 64})
        assert wrong.isError
        result = await server.call_tool("submit_campaign_review", args)
        assert not result.isError
        reviewed = result.structuredContent
        assert reviewed["version"] == bundle["version"] + 1
        assert reviewed["state"] == "draft" and reviewed["payload"]["author_confirmation"] is None
        with pytest.raises(DomainError):
            creative.export(db, reviewed["id"], reviewed["version"])
        await service.close()

    asyncio.run(run())


def test_prepare_topic_is_not_research_and_never_calls_model(db, grant, tmp_path, monkeypatch):
    async def run():
        db.register_grant(cloud(grant))
        service = BrowserService(db, tmp_path / "profile", tmp_path / "missing", PngReader())
        monkeypatch.setattr(
            "rednotebook.browser_service.load_config", lambda *_: pytest.fail("model called")
        )
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
            sample_exception="Synthetic one note",
            notes=[
                dict(
                    note_id="b" * 24,
                    reason="Synthetic",
                    question="Synthetic focus",
                    coverage=["Synthetic"],
                )
            ],
        )
        server = create_server(service)
        result = await server.call_tool(
            "prepare_topic_evidence",
            dict(
                source_id=grant.id,
                selection=selection.model_dump(),
                brief=brief().model_dump(mode="json"),
            ),
        )
        await service.tasks[result.structuredContent["job_id"]]
        result = service.get(result.structuredContent["job_id"], True)["result"]
        assert result["evidence_prepared"] and result["completed_count"] == 1
        assert not result["campaign_ready"] and not result["research_complete"]
        assert result["achieved_depth"] == "collected_evidence"
        assert db.conn.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 0
        child = result["notes"][0]
        assert child["research_brief"]["research_question"].endswith("Synthetic focus")
        capture_id = child["result"]["capture_id"]
        image = evidence_bundle(service, capture_id)["images"][0]
        from rednotebook.domain.models import ResearchBrief
        from rednotebook.research.assistant_review import PageReading, assemble_topic

        prepared = prepare(
            service,
            capture_id,
            ResearchBrief.model_validate(child["research_brief"]),
            [
                PageReading(
                    position=1,
                    sha256=image["sha256"],
                    text="Synthetic red image",
                    key_content_readable=True,
                )
            ],
        )
        reviewed = submit(
            service, capture_id, prepared["input_sha256"], draft(prepared["catalog"][0]["ref_id"])
        )
        completed = assemble_topic(
            service, grant.id, selection, brief(), {"b" * 24: reviewed["job_id"]}
        )
        assert completed["research_complete"] and completed["campaign_ready"]
        bundle = campaign.create(
            db,
            ContentBrief(
                id="synthetic-complete-caller",
                synthetic=True,
                creator_goal="Synthetic",
                audience="Synthetic",
                content_role="Synthetic",
                reader_takeaway="Synthetic",
                research_mode="research_then_plan",
                topic_job_id=completed["job_id"],
            ),
        )
        assert bundle["state"] == "draft"
        await service.close()

    asyncio.run(run())


def test_shared_http_clients_keep_service_alive(db, grant, tmp_path):
    """Real MCP wire protocol over in-process HTTP; both sessions share one DB/service."""
    from contextlib import asynccontextmanager

    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def run():
        db.register_grant(cloud(grant))
        service = BrowserService(db, tmp_path / "profile", tmp_path / "missing", PngReader())
        capture = submit_capture(service, grant)
        await service.tasks[capture]
        image = evidence_bundle(service, capture)["images"][0]
        closed = []
        original_close = service.close

        async def close():
            closed.append(True)
            await original_close()

        service.close = close
        server = create_server(service)
        # JSON responses avoid buffering an open SSE response in ASGITransport.
        server.settings.json_response = True
        app = server.streamable_http_app()

        @asynccontextmanager
        async def connect():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
                async with streamable_http_client(
                    "http://127.0.0.1:8000/mcp", http_client=client
                ) as streams:
                    yield streams

        async with app.router.lifespan_context(app):
            async with connect() as first:
                async with ClientSession(first[0], first[1]) as a:
                    await a.initialize()
                    async with connect() as second:
                        async with ClientSession(second[0], second[1]) as b:
                            await b.initialize()
                            image_result = await b.call_tool(
                                "read_capture_image",
                                {
                                    "capture_id": capture,
                                    "position": 1,
                                    "expected_sha256": image["sha256"],
                                },
                            )
                            assert not image_result.isError
                            assert image_result.content[1].type == "image"
                            assert (
                                hashlib.sha256(
                                    base64.b64decode(image_result.content[1].data)
                                ).hexdigest()
                                == image["sha256"]
                            )
                            tools = await b.list_tools()
                            assert "read_capture_image" in {t.name for t in tools.tools}
                            planned = await a.call_tool(
                                "plan_search",
                                dict(
                                    source_id=grant.id,
                                    intent={"primary_query": "synthetic"},
                                    use_model=False,
                                ),
                            )
                            ident = planned.structuredContent["job_id"]
                            await service.tasks[ident]
                            read = await b.call_tool(
                                "get_job", dict(job_id=ident, include_result=True)
                            )
                            assert read.structuredContent["state"] == "complete"
                    assert not closed, "disconnecting one client must not close shared service"
                    assert not (await a.call_tool("analysis_capabilities", {})).isError
            assert not closed
        assert closed == [True]

    asyncio.run(asyncio.wait_for(run(), timeout=15))


def test_v13_migration_keeps_legacy_job_and_grant_bytes(tmp_path, grant, clock):
    from rednotebook.storage import Database
    from rednotebook.util import canonical, stamp

    path = tmp_path / "migration.sqlite"
    request = {"operation": "collect", "source_id": grant.id}
    with Database(path, clock=lambda: clock[0]) as old:
        old.register_grant(grant)
        before = tuple(old.conn.execute("SELECT * FROM sources").fetchone())
        with old.conn:
            old.conn.execute(
                "INSERT INTO browser_jobs (id,source_id,state,request_json,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (
                    "legacy",
                    grant.id,
                    "complete",
                    canonical(request),
                    stamp(clock[0]),
                    stamp(clock[0]),
                ),
            )
            old.conn.execute("DELETE FROM settings WHERE key='caller_analysis_contract_version'")
            old.conn.execute("PRAGMA user_version=12")
    with Database(path, clock=lambda: clock[0]) as migrated:
        assert migrated.conn.execute("PRAGMA user_version").fetchone()[0] == 13
        assert tuple(migrated.conn.execute("SELECT * FROM sources").fetchone()) == before
        payload = migrated.conn.execute("SELECT request_json FROM browser_jobs").fetchone()[0]
        assert payload == canonical(request)
        assert JobRequest.model_validate_json(payload).analysis_mode == "local_model_analysis"
