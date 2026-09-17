"""Synthetic engineering cases; not content-quality or real publishing acceptance."""

import asyncio
import json
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError
from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from rednotebook import campaign, creative, workspace
from rednotebook.campaign_contracts import (
    AuthorConfirmation,
    CampaignOutput,
    ContentBrief,
    PostDraft,
)
from rednotebook.domain.models import ResearchBrief
from rednotebook.errors import DomainError
from rednotebook.research.config import LocalModelConfig, ResearchBudget
from rednotebook.research.store import checkpoint, start_run
from rednotebook.storage import Database


def brief(**updates):
    return ContentBrief.model_validate(
        {
            "id": "synthetic-campaign",
            "synthetic": True,
            "creator_goal": "合成示例：介绍设备并预告两个项目",
            "audience": "本地模型爱好者",
            "content_role": "series_opener",
            "reader_takeaway": "了解两个用途及不同进度",
            "hardware": [{"id": "gpu", "description": "合成设备", "possession": "arrived"}],
            "projects": [
                {"id": "a", "name": "合成项目甲", "purpose": "资料整理", "status": "planned"},
                {"id": "b", "name": "合成项目乙", "purpose": "图片工具", "status": "completed"},
            ],
            **updates,
        }
    )


def config():
    return LocalModelConfig(
        base_url="http://127.0.0.1:30000/v1",
        model="offline-fixture",
        api_key="SECRET_SENTINEL",
        timeout_seconds=2,
    )


def confirmation(ids=()):
    return AuthorConfirmation(
        reviewer="synthetic author",
        note="合成验收记录，不是真实作者批准",
        resolved_issue_ids=list(ids),
        facts_and_rights_checked=True,
    )


def source_run(db, grant):
    db.register_grant(grant)
    r = start_run(
        db,
        ResearchBrief(
            id="synthetic-research",
            synthetic=True,
            product="示例",
            audience="研究者",
            research_question="表达什么",
            objective="观察",
            keywords=["例"],
        ),
        [grant.id],
        {},
    )
    checkpoint(
        db,
        r,
        {
            "run_id": r,
            "state": "complete",
            "findings": [
                {
                    "id": "f1",
                    "claim": "合成观察",
                    "support": [],
                    "counter": [],
                    "status": "hypothesis",
                    "limitation": "合成数据",
                }
            ],
        },
    )
    return r


def test_author_without_research_and_optional_fields(db):
    b = campaign.create(db, brief())
    assert b["run_id"] is None
    assert db.conn.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 0
    assert workspace.history(db, "drafts")["items"][0]["bundle_id"] == b["id"]
    checks = campaign.check(db, b["id"], b["version"])
    assert not checks["ready_for_approval"]
    optional = {c["id"]: c["status"] for c in checks["checks"]}
    assert all(
        optional[k] == "not_applicable"
        for k in ("primary_action", "feedback_question", "measurement")
    )
    assert not any(c["status"] == "warn" for c in checks["checks"])
    with pytest.raises(DomainError, match="campaign_review_unresolved"):
        creative.review(db, b["id"], 1, b["content_hash"], "tester")


@pytest.mark.parametrize("count", [1, 2, 5, 7, 20])
def test_variable_pages_with_exact_approval_and_manifest(db, count):
    b = campaign.create(db, brief())
    draft = PostDraft(
        title="合成", body="合成正文", pages=[{"title": "合成页", "body": "合成内容"}] * count
    )
    b = creative.revise(db, b["id"], b["version"], draft)
    b = campaign.confirm(db, b["id"], b["version"], b["content_hash"], confirmation())
    creative.review(db, b["id"], b["version"], b["content_hash"], "synthetic reviewer")
    result = creative.export(db, b["id"], b["version"])
    assert result["manifest"]["page_count"] == count
    pngs = list(Path(result["path"]).glob("*.png"))
    assert len(pngs) == count
    for file in pngs:
        with Image.open(file) as im:
            assert im.size == (1080, 1620)
    assert creative.export(db, b["id"], b["version"])["reused"]
    draft.body = "合成改稿"
    changed = creative.revise(db, b["id"], b["version"], draft)
    assert changed["state"] == "draft" and changed["payload"]["author_confirmation"] is None
    with pytest.raises(DomainError, match="exact_version_approval_required"):
        creative.export(db, changed["id"], changed["version"])


def test_page_limits_and_separate_legacy_schema(db):
    with pytest.raises(ValidationError):
        PostDraft(title="合成", body="合成", pages=[])
    with pytest.raises(ValidationError):
        creative.Editorial(title="合成", body="合成", pages=[{"title": "一", "body": "二"}])
    b = campaign.create(db, brief(max_pages=1))
    with pytest.raises(DomainError, match="campaign_page_budget_exceeded"):
        creative.revise(
            db,
            b["id"],
            1,
            PostDraft(title="合成", body="合成", pages=[{"title": "一", "body": "二"}] * 2),
        )


def test_state_axes_remain_independent_in_model_request(db):
    b = campaign.create(
        db,
        brief(
            tests=[{"id": "t", "project_id": "b", "description": "性能测试", "executed": "no"}],
            assumptions=["目标受众为推断，待作者确认"],
            forbidden_claims=["不声称已购买"],
            primary_action=None,
        ),
    )
    seen = []

    def respond(messages, info):
        data = next(
            json.loads(p.content)
            for m in messages
            for p in m.parts
            if isinstance(p, UserPromptPart)
        )
        seen.append(data)
        ctx = data["creative_context"]
        assert ctx["brief"]["hardware"][0]["possession"] == "arrived"
        assert [p["status"] for p in ctx["brief"]["projects"]] == ["planned", "completed"]
        assert ctx["brief"]["tests"][0]["executed"] == "no"
        assert ctx["brief"]["forbidden_claims"] == ["不声称已购买"]
        assert ctx["brief"]["assumptions"]
        assert info.function_tools == []
        if len(seen) == 1:
            out = {
                "plan": b["payload"]["campaign_plan"],
                "post": b["payload"]["editorial"],
                "claims": [{"id": "c", "text": "合成", "dependency_ids": ["projects:a"]}],
            }
            return ModelResponse([ToolCallPart("submit_campaign", out)])
        return ModelResponse(
            [
                ToolCallPart(
                    "submit_review",
                    {
                        "issues": [
                            {
                                "id": "question",
                                "field": "post.body",
                                "concern": "确认表述范围",
                                "status": "needs_review",
                                "factual": True,
                            }
                        ]
                    },
                )
            ]
        )

    result = asyncio.run(campaign.generate(db, b["id"], 1, config(), model=FunctionModel(respond)))
    assert len(seen) == 2 and result["version"] == 3
    trace = result["payload"]["generation"]
    assert (
        trace["budget"]["max_model_calls"] == 60
        and trace["budget"]["max_input_tokens"] == 4_000_000
    )
    assert trace["context_hash"] == result["payload"]["context_hash"]
    assert trace["request_hash"] and trace["review_request_hash"]
    assert "SECRET_SENTINEL" not in json.dumps(result)
    with pytest.raises(DomainError, match="campaign_review_unresolved"):
        campaign.confirm(db, result["id"], 3, result["content_hash"], confirmation())
    confirmed = campaign.confirm(
        db, result["id"], 3, result["content_hash"], confirmation(["question"])
    )
    check = campaign.check(db, confirmed["id"], confirmed["version"])
    assert check["ready_for_approval"] and not check["semantic_truth_verified"]
    assert any(c["layer"] == "model" and c["status"] == "needs_review" for c in check["checks"])


def test_failed_review_retains_generated_draft_and_budget(db):
    b = campaign.create(db, brief())

    def generate_only(messages, info):
        return ModelResponse(
            [
                ToolCallPart(
                    "submit_campaign",
                    {
                        "plan": b["payload"]["campaign_plan"],
                        "post": {**b["payload"]["editorial"], "body": "保存合成生成稿"},
                        "claims": [],
                    },
                )
            ]
        )

    result = asyncio.run(
        campaign.generate(
            db,
            b["id"],
            1,
            config(),
            ResearchBudget(max_model_calls=1),
            model=FunctionModel(generate_only),
        )
    )
    assert result["state"] == "failed"
    saved = creative.load_bundle(db, result["saved_bundle_id"], result["saved_version"])
    assert saved["payload"]["editorial"]["body"] == "保存合成生成稿"
    assert saved["payload"]["generation"]["usage"]["calls"] == 1
    assert saved["payload"]["author_confirmation"] is None


def test_nonexistent_claim_dependency_retries_then_stops(db):
    b = campaign.create(db, brief())

    def invalid(messages, info):
        return ModelResponse(
            [
                ToolCallPart(
                    "submit_campaign",
                    {
                        "plan": b["payload"]["campaign_plan"],
                        "post": b["payload"]["editorial"],
                        "claims": [{"id": "x", "text": "伪造依赖", "dependency_ids": ["unknown"]}],
                    },
                )
            ]
        )

    result = asyncio.run(campaign.generate(db, b["id"], 1, config(), model=FunctionModel(invalid)))
    assert result["state"] == "failed"
    saved = creative.load_bundle(db, result["saved_bundle_id"], result["saved_version"])
    assert saved["payload"]["claims"] == []
    assert saved["payload"]["generation"]["usage"]["calls"] == 3


def test_source_dependency_cannot_be_removed_by_brief_edit(db, grant):
    grant = grant.model_copy(
        update={"permissions": grant.permissions.model_copy(update={"excerpt_export": "allowed"})}
    )
    r = source_run(db, grant)
    b = campaign.create(db, brief(research_run_ids=[r]))
    assert b["payload"]["context"]["research"][0]["findings"][0]["status"] == "hypothesis"
    changed = campaign.revise_brief(db, b["id"], 1, brief())
    assert creative.bundle_sources(db, b["id"], changed["version"]) == [grant.id]
    result = creative.export(db, b["id"], changed["version"], True)
    db.revoke(grant.id)
    assert not Path(result["path"]).exists()
    assert all(
        r[0] is None for r in db.conn.execute("SELECT payload FROM bundles WHERE id=?", (b["id"],))
    )
    with pytest.raises(DomainError, match="bundle_revoked"):
        creative.load_bundle(db, b["id"], changed["version"])
    assert workspace.history(db, "drafts")["items"] == []


def test_author_source_permissions_and_expiry(db, grant, clock):
    db.register_grant(grant)
    b = campaign.create(db, brief(source_ids=[grant.id]))
    checks = campaign.check(db, b["id"], 1)
    assert any(c["status"] == "block" for c in checks["checks"])
    with pytest.raises(DomainError):
        creative.export(db, b["id"], 1, True)
    clock[0] = grant.valid_until
    with pytest.raises(DomainError):
        creative.load_bundle(db, b["id"], 1)
    db.sweep()
    assert (
        db.conn.execute("SELECT payload FROM bundles WHERE id=?", (b["id"],)).fetchone()[0] is None
    )


def test_asset_and_brief_edits_invalidate_confirmation(db, tmp_path):
    b = campaign.create(db, brief())
    b = campaign.confirm(db, b["id"], 1, b["content_hash"], confirmation())
    file = tmp_path / "synthetic.png"
    Image.new("RGB", (20, 20), "blue").save(file)
    asset = creative.add_asset(db, b["id"], b["version"], file, "synthetic", "fixture")
    assert asset["payload"]["author_confirmation"] is None
    changed = campaign.revise_brief(db, b["id"], asset["version"], brief(creator_goal="合成教程"))
    assert changed["payload"]["context"]["available_assets"]
    assert changed["state"] == "draft"
    with pytest.raises(DomainError, match="review_hash"):
        campaign.confirm(db, b["id"], changed["version"], b["content_hash"], confirmation())


def test_promise_scope_and_series_candidates_are_not_facts(db):
    b = campaign.create(
        db,
        brief(
            series={"id": "s", "candidate_topics": ["可能做教程"], "promises": []},
            promises=[
                {"id": "next", "text": "下一篇分享Prompt", "scope": "future", "status": "planned"}
            ],
        ),
    )
    assert not any(c["status"] == "block" for c in campaign.check(db, b["id"], 1)["checks"])
    assert b["payload"]["context"]["brief"]["series"]["promises"] == []
    assert workspace.history(db, "drafts", query="s")["items"][0]["series_id"] == "s"
    with pytest.raises(DomainError, match="campaign_promise_dependency_missing"):
        campaign.create(
            db,
            brief(
                promises=[
                    {"id": "bad", "text": "结果", "scope": "result", "dependency_ids": ["absent"]}
                ]
            ),
        )


def test_v7_migration_preserves_approved_payload_exports_and_foreign_keys(tmp_path, clock, grant):
    path = tmp_path / "migration.sqlite"
    with Database(path, clock=lambda: clock[0]) as db:
        grant = grant.model_copy(
            update={
                "permissions": grant.permissions.model_copy(update={"excerpt_export": "allowed"})
            }
        )
        r = source_run(db, grant)
        old = creative.propose(db, r)
        creative.review(db, old["id"], 1, old["content_hash"], "synthetic reviewer")
        out = creative.export(db, old["id"], 1)
        db.conn.execute(
            "INSERT INTO outcomes VALUES ('fixture',?,?,?,'{}')", (old["id"], 1, grant.id)
        )
        db.conn.commit()
        before = tuple(db.conn.execute("SELECT * FROM bundles").fetchone())
        db.conn.execute("PRAGMA foreign_keys=OFF")
        db.conn.executescript("""
            BEGIN;
            DROP TABLE diagnostic_events;
            DROP TABLE bundle_sources;
            DROP TABLE bundle_runs;
            CREATE TABLE legacy_bundles(
                id TEXT NOT NULL, version INTEGER NOT NULL, run_id TEXT NOT NULL REFERENCES research_runs(id),
                content_hash TEXT NOT NULL, payload TEXT, state TEXT NOT NULL,
                reviewer TEXT, approved_hash TEXT, created_at TEXT NOT NULL, PRIMARY KEY(id,version));
            INSERT INTO legacy_bundles SELECT * FROM bundles;
            DROP TABLE bundles;
            ALTER TABLE legacy_bundles RENAME TO bundles;
            PRAGMA user_version=7;
            COMMIT;
        """)
    with Database(path, clock=lambda: clock[0]) as db:
        assert tuple(db.conn.execute("SELECT * FROM bundles").fetchone()) == before
        assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()
        assert db.conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 1
        assert creative.export(db, old["id"], 1)["reused"]
        db.revoke(grant.id)
        assert not Path(out["path"]).exists()
        assert db.conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0


def test_model_cannot_keep_source_after_revocation(db, grant):
    run = source_run(db, grant)
    b = campaign.create(db, brief(research_run_ids=[run]))

    async def revoke_during_request(messages, info):
        db.revoke(grant.id)
        return ModelResponse(
            [
                ToolCallPart(
                    "submit_campaign",
                    {
                        "plan": b["payload"]["campaign_plan"],
                        "post": b["payload"]["editorial"],
                        "claims": [],
                    },
                )
            ]
        )

    with pytest.raises(DomainError):
        asyncio.run(
            campaign.generate(db, b["id"], 1, config(), model=FunctionModel(revoke_during_request))
        )
    assert (
        db.conn.execute("SELECT payload FROM bundles WHERE id=?", (b["id"],)).fetchone()[0] is None
    )


def test_mcp_failed_job_read_is_success_and_direct_failure_is_error(db, tmp_path, grant):
    from rednotebook.browser_service import BrowserService
    from rednotebook.mcp_server import create_server

    db.register_grant(grant)
    db.conn.execute(
        "INSERT INTO browser_jobs VALUES ('fixture',?,'failed',?,NULL,'browser_operation_failed',?,?,NULL)",
        (grant.id, json.dumps({"operation": "search", "source_id": grant.id}), "then", "then"),
    )
    db.conn.commit()

    async def run():
        service = BrowserService(db, tmp_path / "profile", tmp_path / "missing.toml")
        server = create_server(service)
        result = await server.call_tool("get_job", {"job_id": "fixture"})
        assert not result.isError and result.structuredContent["state"] == "failed"
        bad = await server.call_tool("get_job", {"job_id": "missing"})
        assert bad.isError and bad.structuredContent["diagnostic_id"]
        diagnostic = await server.call_tool(
            "read_diagnostic", {"diagnostic_id": bad.structuredContent["diagnostic_id"]}
        )
        assert not diagnostic.isError and diagnostic.structuredContent["error_stage"] == "get_job"
        unknown = await server.call_tool("SECRET_SENTINEL", {})
        assert unknown.isError and "SECRET_SENTINEL" not in str(unknown)
        assert unknown.structuredContent["error_stage"] == "unknown_tool"
        bad = await server.call_tool("create_campaign", {"brief": {"secret": "SECRET_SENTINEL"}})
        assert bad.isError and "SECRET_SENTINEL" not in str(bad)
        created = await server.call_tool(
            "create_campaign", {"brief": brief().model_dump(mode="json")}
        )
        assert not created.isError and created.structuredContent["run_id"] is None
        await service.close()

    asyncio.run(run())


def test_author_lineage_explicit_delete(db):
    b = campaign.create(db, brief())
    second = campaign.revise_brief(db, b["id"], 1, brief(content_role="tutorial"))
    preview = creative.export(db, b["id"], 1, True)
    with pytest.raises(DomainError, match="delete_latest"):
        campaign.delete(db, b["id"], 1, b["content_hash"])
    assert (
        campaign.delete(db, b["id"], second["version"], second["content_hash"])["purged_versions"]
        == 2
    )
    assert not Path(preview["path"]).exists()
    with pytest.raises(DomainError, match="bundle_revoked"):
        creative.save_bundle(db, b["id"], None, b["payload"])


def test_feedback_only_outcome_has_no_fabricated_metrics(db, grant):
    from rednotebook.outcomes import Outcome, import_outcome, retrospective

    db.register_grant(grant)
    b = campaign.create(db, brief())
    item = Outcome(
        bundle_id=b["id"],
        bundle_version=1,
        source_id=grant.id,
        note_url="https://example.test/synthetic",
        ownership="own",
        published_at=db.clock(),
        observed_at=db.clock(),
        window="24h",
        reader_feedback=[
            {
                "id": "synthetic-comment",
                "category": "topic_preference",
                "text": "合成：先介绍甲",
                "source_ref": "synthetic-fixture",
                "classification_definition": "明确选择候选主题",
                "classified_by": "synthetic-reviewer",
            }
        ],
    )
    import_outcome(db, item)
    result = retrospective(db, b["id"], 1)
    assert result["observations"][0]["metrics"] == []
    assert result["observations"][0]["time_saved_fraction"] is None
    assert result["measurement_plan"] is None
    db.revoke(grant.id)
    assert not retrospective(db, b["id"], 1)["observations"]


def test_cli_campaign_create_revise_and_check(tmp_path, capsys):
    from rednotebook.cli import main

    path = tmp_path / "brief.json"
    path.write_text(brief().model_dump_json())
    args = ["--db", str(tmp_path / "cli.sqlite")]
    assert main(args + ["campaign", "create", "--file", str(path)]) == 0
    b = json.loads(capsys.readouterr().out)
    assert b["run_id"] is None
    assert main(args + ["campaign", "check", b["id"], "--version", "1"]) == 0
    assert not json.loads(capsys.readouterr().out)["ready_for_approval"]
    assert (
        main(args + ["campaign", "delete", b["id"], "--version", "1", "--hash", b["content_hash"]])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["state"] == "revoked"


def test_local_provider_double_encoded_objects_still_validate_strictly(db):
    b = campaign.create(db, brief())
    calls = []

    def respond(messages, info):
        calls.append(1)
        if len(calls) == 1:
            return ModelResponse(
                [
                    ToolCallPart(
                        "submit_campaign",
                        {
                            "plan": json.dumps(b["payload"]["campaign_plan"]),
                            "post": json.dumps(b["payload"]["editorial"]),
                            "claims": [],
                        },
                    )
                ]
            )
        return ModelResponse([ToolCallPart("submit_review", {"issues": []})])

    result = asyncio.run(campaign.generate(db, b["id"], 1, config(), model=FunctionModel(respond)))
    assert result["payload"]["generation"]["stage"] == "reviewed"
    with pytest.raises(ValidationError):
        CampaignOutput.model_validate(
            {
                "plan": json.dumps({**b["payload"]["campaign_plan"], "unexpected": "value"}),
                "post": json.dumps(b["payload"]["editorial"]),
                "claims": [],
            }
        )


def test_old_outcome_defaults_do_not_create_false_snapshot_conflict(db, grant):
    from rednotebook.outcomes import Outcome, import_outcome
    from rednotebook.util import canonical

    db.register_grant(grant)
    b = campaign.create(db, brief())
    data = Outcome(
        bundle_id=b["id"],
        bundle_version=1,
        source_id=grant.id,
        note_url="https://example.test/fixture",
        ownership="own",
        published_at=db.clock(),
        observed_at=db.clock(),
        window="24h",
        metrics=[
            {
                "name": "likes",
                "value": 0,
                "raw_display": "0",
                "source_kind": "owner",
                "precision": "exact",
                "definition": "synthetic likes",
                "observed_at": db.clock(),
            }
        ],
    )
    first = import_outcome(db, data)
    old = data.model_dump(mode="json")
    del old["reader_feedback"]
    del old["author_notes"]
    db.conn.execute("UPDATE outcomes SET payload=? WHERE id=?", (canonical(old), first["id"]))
    db.conn.commit()
    assert import_outcome(db, data)["duplicate"]
    assert db.conn.execute("SELECT payload FROM outcomes WHERE id=?", (first["id"],)).fetchone()[
        0
    ] == canonical(old)
