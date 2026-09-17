import asyncio
import json

import httpx
import pytest

from rednotebook.adapters.browser import download_image, metric, note_url, records_from_dom
from rednotebook.browser_control import AccessGate
from rednotebook.browser_service import BrowserService, JobRequest, capture_path
from rednotebook.errors import DomainError
from rednotebook.util import stamp


class OfflineGate(AccessGate):
    PAGE_INTERVAL = 0
    ACTION_INTERVAL = 0


NOTE = "https://www.xiaohongshu.com/explore/" + "a" * 24 + "?xsec_token=private"


@pytest.mark.parametrize(
    "url",
    [
        "http://www.xiaohongshu.com/explore/" + "a" * 24,
        "https://www.xiaohongshu.com.evil.test/explore/" + "a" * 24,
        "https://www.xiaohongshu.com@evil.test/explore/" + "a" * 24,
        "https://www.xiaohongshu.com/publish",
        "file:///etc/passwd",
    ],
)
def test_browser_only_accepts_note_links(url):
    with pytest.raises(DomainError, match="browser_note_url_invalid"):
        note_url(url)


def test_metrics_and_sampling_preserve_uncertainty(grant, clock):
    at = stamp(clock[0])
    assert metric("likes", "1.2万", at)["precision"] == "abbreviated"
    assert metric("likes", "点赞", at)["value"] is None
    assert metric("likes", "0", at)["value"] == 0
    raw = {
        "title": "标题",
        "text": "正文",
        "metrics": {"comments": "20"},
        "author": "/user/profile/author",
        "comments": [
            {"id": "c1", "text": "评论", "author": "author", "reply": True},
            {"id": "c1", "text": "评论", "author": "author", "reply": True},
            {"id": "c2", "text": "另一条", "author": "other"},
        ],
    }
    result = records_from_dom(raw, grant, "brief", "关键词", "run", NOTE, at, 1)
    assert len(result) == 2
    assert "xsec_token" not in result[0]["locator"]
    assert result[0]["sampling"][0]["sort"] == "unknown"
    assert result[0]["coverage"]["truncated"] is True
    assert result[0]["published_at"] is None
    assert result[1]["is_author_reply"] is True


def allowed(grant):
    return grant.model_copy(
        update={"permissions": grant.permissions.model_copy(update={"automated_access": "allowed"})}
    )


class Reader:
    def __init__(self, error=None):
        self.calls = 0
        self.close_calls = 0
        self.error = error

    async def search(self, keyword, limit, scrolls, checkpoint):
        checkpoint()
        self.calls += 1
        if self.error:
            raise DomainError(self.error)
        return {"state": "partial", "candidates": [], "keyword": keyword}

    async def close(self):
        self.close_calls += 1


class FailingOnceReader(Reader):
    async def search(self, keyword, limit, scrolls, checkpoint):
        checkpoint()
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("synthetic local browser failure")
        return {"state": "partial", "candidates": [], "keyword": keyword}


def test_jobs_pause_retry_and_revoke_files(db, grant, tmp_path):
    async def run():
        db.register_grant(allowed(grant))
        reader = Reader("login_required")
        service = BrowserService(db, tmp_path / "profile", tmp_path / "config", reader)
        job = service.submit(JobRequest(operation="search", source_id=grant.id, keyword="简历"))
        await service.tasks[job["job_id"]]
        assert service.get(job["job_id"])["state"] == "paused"
        with pytest.raises(DomainError, match="browser_access_paused"):
            service.retry(job["job_id"])
        service.gate.resume()
        reader.error = None
        retried = service.retry(job["job_id"])
        await service.tasks[retried["job_id"]]
        assert service.get(retried["job_id"])["state"] == "partial"
        folder = capture_path(db, job["job_id"])
        folder.mkdir(parents=True)
        (folder / "private.json").write_text("source material")
        db.revoke(grant.id)
        assert not folder.exists()
        with pytest.raises(DomainError):
            service.get(retried["job_id"], True)
        assert db.conn.execute("SELECT request_json FROM browser_jobs").fetchone()[0] is None
        await service.close()

    asyncio.run(run())


def test_jobs_require_permission_before_browser(db, grant, tmp_path):
    async def run():
        db.register_grant(grant)
        reader = Reader()
        service = BrowserService(db, tmp_path, tmp_path, reader)
        with pytest.raises(DomainError):
            service.submit(JobRequest(operation="search", source_id=grant.id, keyword="简历"))
        assert reader.calls == 0
        await service.close()

    asyncio.run(run())


def test_cancel_queued_job_does_not_execute(db, grant, tmp_path):
    async def run():
        db.register_grant(allowed(grant))
        reader = Reader()
        service = BrowserService(db, tmp_path, tmp_path, reader)
        job = service.submit(JobRequest(operation="search", source_id=grant.id, keyword="简历"))
        assert (await service.cancel(job["job_id"]))["state"] == "cancelled"
        assert reader.calls == 0
        await service.close()

    asyncio.run(run())


def test_cdn_redirects_and_non_images_are_not_followed():
    async def run():
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(DomainError, match="image_host_not_allowed"):
                await download_image(client, "https://evil.test/image")
            with pytest.raises(DomainError, match="image_download_failed"):
                await download_image(client, "https://sns-webpic.xhscdn.com/image")
        assert len(seen) == 1

    asyncio.run(run())


def test_capture_rejects_path_traversal(db):
    with pytest.raises(DomainError, match="capture_id_invalid"):
        capture_path(db, "../private")


def test_real_stdio_mcp_initialization(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        import sys

        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "rednotebook.mcp_server",
                "--db",
                str(tmp_path / "test.sqlite"),
                "--profile",
                str(tmp_path / "profile"),
                "--config",
                str(tmp_path / "model.toml"),
            ],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                initialized = await client.initialize()
                assert "public_url" in initialized.instructions
                assert "collect_url" in initialized.instructions
                assert "new_search_reset_available" in initialized.instructions
                assert "Never use retry_job" in initialized.instructions
                listed = (await client.list_tools()).tools
                names = {t.name for t in listed}
                assert {
                    "search_notes",
                    "collect_note",
                    "analyse_gallery",
                    "cancel_job",
                    "research_notes",
                    "research_workflow",
                    "list_history",
                    "read_research",
                    "record_finding_feedback",
                    "workspace_status",
                } <= names
                descriptions = {t.name: t.description for t in listed}
                assert "public_url" in descriptions["search_notes"]
                assert "collect_url" in descriptions["collect_note"]
                collect_schema = next(t.inputSchema for t in listed if t.name == "collect_note")
                assert "collect_url" in collect_schema["properties"]
                assert "url" not in collect_schema["properties"]
                assert not {"publish", "approve", "evaluate_javascript", "get_cookies"} & names
                response = await client.call_tool("workspace_status", {})
                diagnostic = json.loads(response.content[0].text)
                assert diagnostic["network_access"] is False
                assert diagnostic["schema_version"] == 9
                response = await client.call_tool("list_history", {"kind": "notes"})
                assert json.loads(response.content[0].text)["items"] == []
                response = await client.call_tool(
                    "research_notes",
                    {"source_id": "missing", "brief": {"SECRET_SENTINEL": "SECRET_SENTINEL"}},
                )
                assert "SECRET_SENTINEL" not in str(response)
                assert response.isError
                assert json.loads(response.content[0].text)["issues"]
                response = await client.call_tool("browser_status", {})
                assert json.loads(response.content[0].text)["state"] == "closed"
                response = await client.call_tool(
                    "search_notes",
                    {"source_id": "missing", "keyword": "test", "limit": "SECRET_SENTINEL"},
                )
                assert "SECRET_SENTINEL" not in str(response)
                assert (
                    json.loads(response.content[0].text)["error_code"]
                    == "tool_input_or_execution_invalid"
                )
                response = await client.call_tool(
                    "search_notes", {"source_id": "missing", "keyword": "test"}
                )
                assert response.isError
                assert json.loads(response.content[0].text)["state"] == "failed"

    asyncio.run(run())


def test_playwright_dom_search_offline(tmp_path):
    from rednotebook.adapters.browser import BrowserReader

    async def run():
        reader = BrowserReader(
            tmp_path / "profile", headless=True, gate=OfflineGate(tmp_path / "profile")
        )
        await reader.open()

        async def navigate(url):
            await reader.page.set_content(
                '''<section class="note-item"><a href="'''
                + NOTE
                + '''">链接</a>
            <span class="title">第一篇</span></section><section class="note-item"><a href="'''
                + NOTE
                + """">重复</a>
            </section><section class="note-item"><a href="https://evil.test/explore/aaaaaaaaaaaaaaaaaaaaaaaa">外站</a></section>"""
            )

        async def check():
            pass

        reader.navigate = navigate
        reader.check = check
        try:
            result = await reader.search("简历", limit=2, scrolls=0)
            assert len(result["candidates"]) == 1
            assert result["state"] == "partial"
            assert result["candidates"][0]["title"] == "第一篇"
            candidate = result["candidates"][0]
            assert candidate["public_url"] == NOTE.split("?")[0]
            assert candidate["collect_url"] == NOTE
            assert "url" not in candidate
            assert "xsec_token" not in candidate["public_url"]
        finally:
            await reader.close()

    asyncio.run(run())


def test_playwright_collect_orders_images_and_skips_video(tmp_path, monkeypatch):
    from io import BytesIO

    from PIL import Image

    from rednotebook.adapters import browser

    async def run():
        reader = browser.BrowserReader(
            tmp_path / "profile", headless=True, gate=OfflineGate(tmp_path / "profile")
        )
        await reader.open()
        payloads = {}
        import base64

        for color in ("red", "green", "blue"):
            data = BytesIO()
            Image.new("RGB", (300, 400), color).save(data, format="PNG")
            encoded = "data:image/png;base64," + base64.b64encode(data.getvalue()).decode()
            payloads[encoded] = data.getvalue()
        urls = list(payloads)
        html = '<div id="noteContainer"><div id="detail-title">标题</div><div id="detail-desc">正文</div>'
        html += '<div class="note-slider-img"><img src="' + urls[0] + '"></div></div>'
        html += '<div class="pagination-teleport-container">'
        for index, url in enumerate(urls):
            html += (
                '<button class="pagination-item" '
                + ('style="pointer-events:none" ' if index == 0 else "")
                + "onclick=\"document.querySelector('img').src='"
                + url
                + "'\">"
                + str(index)
                + "</button>"
            )
        html += "</div>"

        async def navigate(url):
            await reader.page.set_content(html)

        async def check():
            pass

        async def download(client, url):
            return payloads[url]

        reader.navigate = navigate
        reader.check = check
        monkeypatch.setattr(browser, "download_image", download)
        try:
            result = await reader.collect(NOTE, tmp_path, comment_limit=0, max_images=2)
            assert result["state"] == "partial"
            assert result["declared_total"] == 3
            assert [i["position"] for i in result["images"]] == [1, 2]
            assert result["errors"] == [{"code": "image_limit_reached"}]
            assert (tmp_path / "01.image").read_bytes() == payloads[urls[0]]
            assert (tmp_path / "02.image").read_bytes() == payloads[urls[1]]
            attempts = []

            async def rejected_download(client, url):
                attempts.append(url)
                raise DomainError("image_download_failed")

            monkeypatch.setattr(browser, "download_image", rejected_download)
            failed = await reader.collect(NOTE, tmp_path, comment_limit=0, max_images=3)
            assert len(attempts) == 1
            assert failed["state"] == "partial"
            assert len(failed["errors"]) == 1
            html = '<div id="noteContainer"><video></video></div>'
            result = await reader.collect(NOTE, tmp_path)
            assert result == {"state": "skipped", "reason": "video_deferred"}
        finally:
            await reader.close()

    asyncio.run(run())


def test_restart_marks_active_jobs_interrupted(db, grant, tmp_path):
    from rednotebook.util import canonical

    db.register_grant(allowed(grant))
    request = JobRequest(operation="search", source_id=grant.id, keyword="简历")
    at = stamp(db.clock())
    with db.conn:
        db.conn.execute(
            "INSERT INTO browser_jobs (id,source_id,state,request_json,result_json,error_code,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                "old-job",
                grant.id,
                "running",
                canonical(request.model_dump(mode="json")),
                None,
                None,
                at,
                at,
            ),
        )
    service = BrowserService(db, tmp_path, tmp_path, Reader())
    assert service.get("old-job")["state"] == "interrupted"


def test_navigation_spacing_persists_across_gate_instances(tmp_path):
    async def run():
        now = [1000.0]
        waits = []

        async def sleep(seconds):
            waits.append(seconds)
            now[0] += seconds

        first = AccessGate(tmp_path, clock=lambda: now[0], sleep=sleep)
        await first.navigation()
        second = AccessGate(tmp_path, clock=lambda: now[0], sleep=sleep)
        await second.navigation()
        assert waits == [60]
        first.pause("user_reported_platform_restriction")
        with pytest.raises(DomainError, match="browser_access_paused"):
            await second.navigation()
        assert waits == [60]
        assert len(json.loads(first.history.read_text())) == 2

    asyncio.run(run())


def test_detail_failure_does_not_lock_following_jobs(db, grant, tmp_path):
    async def run():
        db.register_grant(allowed(grant))
        reader = Reader("note_detail_unavailable")
        service = BrowserService(db, tmp_path / "profile", tmp_path / "config", reader)
        job = service.submit(JobRequest(operation="search", source_id=grant.id, keyword="test"))
        await service.tasks[job["job_id"]]
        assert service.get(job["job_id"])["state"] == "failed"
        assert service.get(job["job_id"])["error_code"] == "note_detail_unavailable"
        assert AccessGate(tmp_path / "profile").status()["paused"] is False
        reader.error = None
        following = service.submit(
            JobRequest(operation="search", source_id=grant.id, keyword="next")
        )
        await service.tasks[following["job_id"]]
        assert service.get(following["job_id"])["state"] == "partial"
        assert reader.calls == 2
        await service.close()

    asyncio.run(run())


def test_partial_capture_review_does_not_lock_browser(db, grant, tmp_path, monkeypatch):
    async def run():
        db.register_grant(allowed(grant))
        reader = Reader()
        service = BrowserService(db, tmp_path / "profile", tmp_path / "config", reader)

        async def partial_result(ident, request):
            return {"state": "partial", "errors": [{"code": "image_limit_reached"}]}

        monkeypatch.setattr(service, "execute", partial_result)
        job = service.submit(
            JobRequest(
                operation="collect",
                source_id=grant.id,
                url=NOTE,
                keyword="test",
            )
        )
        await service.tasks[job["job_id"]]
        assert service.get(job["job_id"])["state"] == "partial"
        assert AccessGate(tmp_path / "profile").status()["paused"] is False
        await service.close()

    asyncio.run(run())


def test_hourly_budget_waits_without_persistent_pause(tmp_path):
    async def run():
        now = [1000.0]

        async def sleep(seconds):
            now[0] += seconds

        gate = AccessGate(tmp_path, clock=lambda: now[0], sleep=sleep)
        for _ in range(AccessGate.HOURLY_PAGE_LIMIT):
            await gate.navigation()
        status = gate.status()
        assert status["remaining_hourly_navigations"] == 0
        assert status["next_navigation_in_seconds"] > 0
        with pytest.raises(DomainError, match="browser_hourly_budget_wait"):
            await gate.navigation()
        assert gate.status()["paused"] is False
        now[0] += 3601
        await gate.navigation()

    asyncio.run(run())


def test_service_rejects_exhausted_hourly_budget_without_job_or_pause(db, grant, tmp_path):
    async def run():
        db.register_grant(allowed(grant))
        profile = tmp_path / "profile"
        gate = AccessGate(profile, clock=lambda: 1000.0)
        profile.mkdir()
        gate.history.write_text(json.dumps([1000.0] * gate.HOURLY_PAGE_LIMIT))
        reader = Reader()
        reader.gate = gate
        service = BrowserService(db, profile, tmp_path / "config", reader)
        with pytest.raises(DomainError, match="browser_hourly_budget_wait"):
            service.submit(JobRequest(operation="search", source_id=grant.id, keyword="test"))
        assert db.conn.execute("SELECT count(*) FROM browser_jobs").fetchone()[0] == 0
        assert gate.status()["paused"] is False
        await service.close()

    asyncio.run(run())


def test_fresh_search_resets_failure_pause_and_preserves_history(db, grant, tmp_path):
    async def run():
        db.register_grant(allowed(grant))
        profile = tmp_path / "profile"
        gate = AccessGate(profile, clock=lambda: 1000.0)
        profile.mkdir()
        gate.history.write_text(json.dumps([900.0]))
        gate.pause("browser_operation_failed")
        reader = Reader()
        reader.gate = gate
        service = BrowserService(db, profile, tmp_path / "config", reader)

        assert gate.status()["new_search_reset_available"] is True
        assert gate.status()["resume"] == "fresh_search_or_explicit_operator_cli"
        job = service.submit(JobRequest(operation="search", source_id=grant.id, keyword="新的检索"))
        assert job["browser_reset"] == {
            "reason": "browser_operation_failed",
            "history_preserved": True,
        }
        await service.tasks[job["job_id"]]
        assert service.get(job["job_id"])["state"] == "partial"
        assert gate.status()["paused"] is False
        assert json.loads(gate.history.read_text()) == [900.0]
        assert reader.calls == 1
        await service.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "reason",
    [
        "access_blocked",
        "captcha_required",
        "login_required",
        "operator_paused",
        "rate_limited",
        "unexpected_page",
        "user_reported_platform_restriction",
    ],
)
def test_fresh_search_does_not_reset_safety_pause(db, grant, tmp_path, reason):
    async def run():
        db.register_grant(allowed(grant))
        profile = tmp_path / "profile"
        gate = AccessGate(profile)
        gate.pause(reason)
        reader = Reader()
        reader.gate = gate
        service = BrowserService(db, profile, tmp_path / "config", reader)

        assert gate.status()["new_search_reset_available"] is False
        with pytest.raises(DomainError, match="browser_access_paused"):
            service.submit(JobRequest(operation="search", source_id=grant.id, keyword="新检索"))
        assert gate.status()["reason"] == reason
        assert reader.calls == 0
        await service.close()

    asyncio.run(run())


def test_invalid_search_does_not_clear_failure_pause(db, grant, tmp_path):
    async def run():
        db.register_grant(allowed(grant))
        profile = tmp_path / "profile"
        gate = AccessGate(profile)
        gate.pause("browser_operation_failed")
        reader = Reader()
        reader.gate = gate
        service = BrowserService(db, profile, tmp_path / "config", reader)

        with pytest.raises(DomainError, match="search_keyword_required"):
            service.submit(JobRequest(operation="search", source_id=grant.id))
        assert gate.status()["reason"] == "browser_operation_failed"
        assert reader.calls == 0
        await service.close()

    asyncio.run(run())


def test_failed_search_requires_a_fresh_search_instead_of_retry(db, grant, tmp_path):
    async def run():
        db.register_grant(allowed(grant))
        profile = tmp_path / "profile"
        reader = FailingOnceReader()
        service = BrowserService(db, profile, tmp_path / "config", reader)

        failed = service.submit(
            JobRequest(operation="search", source_id=grant.id, keyword="第一次")
        )
        await service.tasks[failed["job_id"]]
        assert service.get(failed["job_id"])["state"] == "failed"
        assert service.gate.status()["reason"] == "browser_operation_failed"
        with pytest.raises(DomainError, match="browser_access_paused"):
            service.retry(failed["job_id"])

        fresh = service.submit(JobRequest(operation="search", source_id=grant.id, keyword="下一次"))
        assert fresh["browser_reset"]["reason"] == "browser_operation_failed"
        await service.tasks[fresh["job_id"]]
        assert service.get(fresh["job_id"])["state"] == "partial"
        assert service.gate.status()["paused"] is False
        assert reader.calls == 2
        assert reader.close_calls == 1
        await service.close()

    asyncio.run(run())


def test_search_prefers_visible_signed_link(tmp_path):
    from rednotebook.adapters.browser import SEARCH_DOM, BrowserReader

    async def run():
        reader = BrowserReader(
            tmp_path / "profile", headless=True, gate=OfflineGate(tmp_path / "profile")
        )
        try:
            await reader.open()
            await reader.page.set_content(
                '<section class="note-item"><a style="display:none" href="'
                + NOTE.split("?")[0]
                + '"></a>'
                '<a href="' + NOTE + '">实际打开笔记</a><span class="title">标题</span></section>'
            )
            result = await reader.page.evaluate(SEARCH_DOM)
            assert result[0]["url"] == NOTE
        finally:
            await reader.close()

    asyncio.run(run())


def test_pagination_ignores_hidden_teleport_clone(tmp_path):
    from rednotebook.adapters.browser import BrowserReader

    async def run():
        reader = BrowserReader(
            tmp_path / "profile", headless=True, gate=OfflineGate(tmp_path / "profile")
        )
        try:
            await reader.open()
            await reader.page.set_content("""
                <div id="noteContainer"><div class="pagination-media-container">
                <button class="pagination-item">1</button><button class="pagination-item">2</button>
                </div></div><div class="pagination-teleport-container" style="display:none">
                <button class="pagination-item">hidden1</button><button class="pagination-item">hidden2</button>
                </div>""")
            dots = await reader.image_pagination()
            assert await dots.count() == 2
            assert await dots.first.inner_text() == "1"
            await dots.nth(1).click()
        finally:
            await reader.close()

    asyncio.run(run())


def test_current_platform_image_uses_allowed_src_before_decode(tmp_path):
    from rednotebook.adapters.browser import CURRENT_IMAGE, BrowserReader

    async def run():
        reader = BrowserReader(
            tmp_path / "profile", headless=True, gate=OfflineGate(tmp_path / "profile")
        )
        try:
            await reader.open()
            await reader.page.route(
                "https://www.xiaohongshu.com/**",
                lambda route: route.fulfill(
                    status=200,
                    content_type="text/html",
                    body=(
                        '<div id="noteContainer"><div class="note-slider-img">'
                        '<img style="width:400px;height:500px" '
                        'src="https://sns-webpic-qc.xhscdn.com/synthetic"></div></div>'
                    ),
                ),
            )
            await reader.page.route("https://*.xhscdn.com/**", lambda route: route.abort())
            await reader.page.goto("https://www.xiaohongshu.com/explore")
            assert await reader.page.evaluate(CURRENT_IMAGE) == (
                "https://sns-webpic-qc.xhscdn.com/synthetic"
            )
        finally:
            await reader.close()

    asyncio.run(run())


def test_navigation_replaces_stale_note_route_without_preclick(tmp_path, monkeypatch):
    from rednotebook.adapters.browser import BrowserReader

    async def run():
        reader = BrowserReader(
            tmp_path / "profile", headless=True, gate=OfflineGate(tmp_path / "profile")
        )
        try:
            await reader.open()
            html = """
                <div class="note-detail-mask" style="position:fixed;inset:0"
                  onclick="if(event.target===this)this.remove()">
                  <button class="close-circle"
                    onclick="event.stopPropagation()">关闭</button>
                  <div id="noteContainer" style="position:absolute;inset:24px 190px">
                    合成笔记弹层
                  </div>
                </div>
            """
            await reader.page.route(
                "https://www.xiaohongshu.com/**",
                lambda route: route.fulfill(status=200, content_type="text/html", body=html),
            )
            await reader.page.goto("https://www.xiaohongshu.com/explore")
            navigated = []

            async def goto(url, **kwargs):
                assert await reader.page.locator(".note-detail-mask").count() == 1
                await reader.page.locator(".note-detail-mask").evaluate("e=>e.remove()")
                navigated.append((url, kwargs))

            async def check():
                pass

            monkeypatch.setattr(reader.page, "goto", goto)
            monkeypatch.setattr(reader, "check", check)
            assert await reader.note_overlay_state() == "visible"
            await reader.navigate(NOTE)
            assert navigated[0][0] == NOTE
            assert navigated[0][1]["wait_until"] == "domcontentloaded"
            assert await reader.note_overlay_state() == "not_present"
        finally:
            await reader.close()

    asyncio.run(run())


def test_operator_overlay_recovery_does_not_resume_access(tmp_path):
    from rednotebook.adapters.browser import BrowserReader

    async def run():
        gate = OfflineGate(tmp_path / "profile")
        reader = BrowserReader(tmp_path / "profile", headless=True, gate=gate)
        try:
            await reader.open()
            html = """
                <div class="note-detail-mask" style="position:fixed;inset:0"
                  onclick="if(event.target===this)this.remove()">
                  <button class="close-circle"
                    onclick="event.stopPropagation()">关闭</button>
                  <div id="noteContainer" style="position:absolute;inset:24px 190px">
                    合成笔记弹层
                  </div>
                </div>
            """

            def route_page(route):
                body = html if route.request.url.endswith("/" + "a" * 24) else "<main>列表页</main>"
                return route.fulfill(status=200, content_type="text/html", body=body)

            await reader.page.route(
                "https://www.xiaohongshu.com/**",
                route_page,
            )
            await reader.page.goto("https://www.xiaohongshu.com/explore")
            await reader.page.goto(NOTE.split("?")[0])
            gate.pause("browser_operation_failed")
            assert await reader.dismiss_note_overlay(paced=False) is True
            assert gate.status()["paused"] is True
            state = await reader.state()
            assert state["note_overlay"] == "not_present"
        finally:
            await reader.close()

    asyncio.run(run())


def test_overlay_preflight_ignores_initial_blank_page(tmp_path):
    from rednotebook.adapters.browser import BrowserReader

    async def run():
        reader = BrowserReader(
            tmp_path / "profile", headless=True, gate=OfflineGate(tmp_path / "profile")
        )
        try:
            await reader.open()
            assert reader.page.url == "about:blank"
            assert await reader.dismiss_note_overlay() is False
        finally:
            await reader.close()

    asyncio.run(run())
