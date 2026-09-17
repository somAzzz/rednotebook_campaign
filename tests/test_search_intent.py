"""Offline intent planning and bounded multi-query execution; no live search findings."""

import asyncio
import json

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from rednotebook.browser_service import BrowserService, JobRequest
from rednotebook.errors import DomainError
from rednotebook.research.config import LocalModelConfig
from rednotebook.search_intent import SearchIntent, generate_plan, make_plan
from rednotebook.workspace import history


def intent(**values):
    return SearchIntent(primary_query="rtx pro 6000", **values)


def test_core_first_expansion_and_budget():
    plan = make_plan(intent())
    assert plan.queries[0].query == "rtx pro 6000"
    assert [q.kind for q in plan.queries] == ["core", "category", "angle", "scenario"]
    assert any(q.query == "高价显卡用途" for q in plan.queries)
    assert all(q.assumption for q in plan.queries[1:])
    assert sum(q.limit for q in plan.queries[1:]) <= plan.queries[0].limit
    assert not any("Blackwell" in q.query or "96GB" in q.query for q in plan.queries)


@pytest.mark.parametrize(
    "options",
    [
        {"exact_only": True},
        {"user_goal": "只看这个型号"},
        {"user_goal": "不要扩展"},
        {"max_queries": 1},
    ],
)
def test_restricted_scope(options):
    assert len(make_plan(intent(**options)).queries) == 1


def test_unknown_subject_no_invented_taxonomy_and_dedup():
    plan = make_plan(SearchIntent(primary_query="不明主体"))
    assert len(plan.queries) == 1 and plan.assumptions
    plan = make_plan(
        intent(
            expansions=[
                {"query": "RTX  PRO 6000", "kind": "angle", "reason": "duplicate"},
                {"query": "高价显卡", "kind": "category", "reason": "reference"},
                {"query": "高价显卡", "kind": "scenario", "reason": "duplicate"},
            ],
            core_limit=2,
        )
    )
    assert len(plan.queries) == 2 and plan.queries[1].limit == 2
    assert any("歧义" in x for x in make_plan(intent(user_goal="高价值显卡")).assumptions)


def test_model_output_cannot_replace_core_or_provide_aliases():
    calls = []

    def response(messages, info):
        calls.append(1)
        assert not info.function_tools
        return ModelResponse(
            [
                ToolCallPart(
                    "submit_search_intent",
                    {
                        "expansions": [
                            {"query": "昂贵设备的用途", "kind": "angle", "reason": "探索表达"}
                        ],
                        "assumptions": ["不代表主体价格已核实"],
                    },
                )
            ]
        )

    config = LocalModelConfig(
        base_url="http://127.0.0.1:30000/v1", model="synthetic", api_key="secret"
    )
    plan = asyncio.run(generate_plan(intent(), config, lambda: None, model=FunctionModel(response)))
    assert plan.queries[0].query == "rtx pro 6000" and len(calls) == 1
    assert plan.generation["budget"]["max_model_calls"] == 60
    assert "secret" not in json.dumps(plan.model_dump())
    asyncio.run(
        generate_plan(intent(exact_only=True), config, lambda: None, model=FunctionModel(response))
    )
    assert len(calls) == 1


class Reader:
    def __init__(self, fail_at=None, on_search=None):
        self.calls = []
        self.fail_at = fail_at
        self.on_search = on_search

    async def search(self, keyword, limit, scrolls, checkpoint):
        checkpoint()
        self.calls.append(keyword)
        if len(self.calls) == self.fail_at:
            raise DomainError("captcha_required")
        if self.on_search:
            self.on_search()
        return {
            "state": "partial",
            "candidates": [
                {
                    "note_id": "a" * 24,
                    "public_url": "https://www.xiaohongshu.com/explore/" + "a" * 24,
                    "collect_url": "signed_input_only",
                    "title": "合成标题",
                    "metric_display": None if len(self.calls) == 1 else "0",
                }
            ],
            "sort": "unknown",
            "truncated": True,
        }

    async def close(self):
        pass


def allowed(grant):
    return grant.model_copy(
        update={"permissions": grant.permissions.model_copy(update={"automated_access": "allowed"})}
    )


async def plan_job(service, source, **options):
    job = service.submit(
        JobRequest(operation="plan_search", source_id=source, intent=intent(**options))
    )
    await service.tasks[job["job_id"]]
    return job["job_id"]


def test_plan_then_search_dedup_provenance_and_revocation(db, grant, tmp_path):
    async def run():
        db.register_grant(allowed(grant))
        reader = Reader()
        service = BrowserService(db, tmp_path / "profile", tmp_path / "config", reader)
        parent = await plan_job(service, grant.id)
        assert not reader.calls
        assert history(db, "jobs", query="rtx pro 6000")["items"]
        job = service.submit(
            JobRequest(operation="search_plan", source_id=grant.id, plan_job_id=parent)
        )
        await service.tasks[job["job_id"]]
        result = service.get(job["job_id"], True)["result"]
        assert reader.calls[0] == "rtx pro 6000" and len(reader.calls) == 4
        assert result["execution_complete"] and result["state"] == "partial"
        assert len(result["candidates"]) == 1
        c = result["candidates"][0]
        assert len(c["hits"]) == 4 and c["groups"][0] == "core"
        assert c["hits"][0]["metric_display"] is None and c["hits"][1]["metric_display"] == "0"
        assert result["query_results"][0]["sort"] == "unknown"
        db.revoke(grant.id)
        assert all(
            r[0] is None and r[1] is None
            for r in db.conn.execute("SELECT request_json,result_json FROM browser_jobs")
        )
        await service.close()

    asyncio.run(run())


def test_first_failure_stops_expansions_keeps_checkpoint(db, grant, tmp_path):
    async def run():
        db.register_grant(allowed(grant))
        reader = Reader(fail_at=2)
        service = BrowserService(db, tmp_path / "profile", tmp_path / "config", reader)
        parent = await plan_job(service, grant.id)
        job = service.submit(
            JobRequest(operation="search_plan", source_id=grant.id, plan_job_id=parent)
        )
        await service.tasks[job["job_id"]]
        saved = service.get(job["job_id"], True)
        assert saved["state"] == "paused" and len(reader.calls) == 2
        assert saved["result"]["completed_queries"] == 1
        assert len(saved["result"]["candidates"]) == 1
        with pytest.raises(DomainError, match="browser_access_paused"):
            service.retry(job["job_id"])
        await service.close()

    asyncio.run(run())


def test_planning_while_paused_no_automated_access_needed(db, grant, tmp_path):
    async def run():
        db.register_grant(grant)
        reader = Reader()
        service = BrowserService(db, tmp_path / "profile", tmp_path / "missing", reader)
        service.gate.pause()
        parent = await plan_job(service, grant.id, exact_only=True)
        assert service.get(parent)["state"] == "complete" and not reader.calls
        with pytest.raises(DomainError):
            service.submit(
                JobRequest(operation="search_plan", source_id=grant.id, plan_job_id=parent)
            )
        await service.close()

    asyncio.run(run())


def test_revoke_during_search_cannot_retain_result(db, grant, tmp_path):
    async def run():
        db.register_grant(allowed(grant))
        reader = Reader(on_search=lambda: db.revoke(grant.id))
        service = BrowserService(db, tmp_path / "profile", tmp_path / "config", reader)
        parent = await plan_job(service, grant.id)
        job = service.submit(
            JobRequest(operation="search_plan", source_id=grant.id, plan_job_id=parent)
        )
        await service.tasks[job["job_id"]]
        assert len(reader.calls) == 1
        assert (
            db.conn.execute(
                "SELECT result_json FROM browser_jobs WHERE id=?", (job["job_id"],)
            ).fetchone()[0]
            is None
        )
        await service.close()

    asyncio.run(run())


def test_migration_v8_keeps_legacy_jobs_and_enables_contract(tmp_path, grant):
    from rednotebook.storage import Database

    path = tmp_path / "upgrade.sqlite"
    with Database(path) as db:
        db.register_grant(grant)
        db.conn.execute(
            "INSERT INTO browser_jobs VALUES ('legacy',?,'complete',?,NULL,NULL,'then','then',NULL)",
            (grant.id, '{"operation":"search","source_id":"synthetic-f0","keyword":"old"}'),
        )
        original = tuple(db.conn.execute("SELECT * FROM browser_jobs").fetchone())
        db.conn.execute("DELETE FROM settings WHERE key='search_plan_contract_version'")
        db.conn.execute("PRAGMA user_version=8")
        db.conn.commit()
    with Database(path) as db:
        assert tuple(db.conn.execute("SELECT * FROM browser_jobs").fetchone()) == original
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 9
        assert (
            db.conn.execute(
                "SELECT value FROM settings WHERE key='search_plan_contract_version'"
            ).fetchone()[0]
            == "1"
        )
        assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()


def test_mcp_plan_tools_executable_and_schema(db, grant, tmp_path):
    from rednotebook.mcp_server import create_server

    async def run():
        db.register_grant(allowed(grant))
        reader = Reader()
        service = BrowserService(db, tmp_path / "profile", tmp_path / "missing", reader)
        server = create_server(service)
        response = await server.call_tool(
            "plan_search",
            {
                "source_id": grant.id,
                "intent": intent(exact_only=True).model_dump(),
                "use_model": True,
            },
        )
        assert not response.isError
        parent = response.structuredContent["job_id"]
        await service.tasks[parent]
        assert (
            service.get(parent)["state"] == "complete"
        )  # exact-only never loads missing model config
        response = await server.call_tool(
            "search_with_plan", {"source_id": grant.id, "plan_job_id": parent}
        )
        job = response.structuredContent["job_id"]
        await service.tasks[job]
        assert reader.calls == ["rtx pro 6000"]
        assert service.get(job, True)["result"]["execution_complete"]
        await service.close()

    asyncio.run(run())
