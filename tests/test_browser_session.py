import asyncio

import pytest
from playwright.async_api import async_playwright

from rednotebook.browser_session import BrowserSession
from rednotebook.errors import DomainError


def test_dedicated_browser_survives_client_disconnect(tmp_path):
    async def run():
        owner = BrowserSession(tmp_path / "profile")
        async with async_playwright() as playwright:
            browser = await owner.connect(playwright, headless=True)
            try:
                page = browser.contexts[0].pages[0]
                await page.set_content("<title>session-sentinel</title><p>offline</p>")
                await browser.close()  # Disconnect client only.
                next_client = BrowserSession(owner.profile)
                browser = await next_client.connect(playwright, create=False)
                assert len(browser.contexts[0].pages) == 1
                assert await browser.contexts[0].pages[0].title() == "session-sentinel"
                assert next_client.read()["pid"] == owner.read()["pid"]
            finally:
                await owner.shutdown(browser)
                await browser.close()
        assert not owner.metadata.exists()

    asyncio.run(run())


def test_attach_only_does_not_launch_browser(tmp_path):
    async def run():
        async with async_playwright() as playwright:
            session = BrowserSession(tmp_path)
            with pytest.raises(DomainError, match="browser_session_not_running"):
                await session.connect(playwright, create=False)
            assert session.process is None

    asyncio.run(run())


def test_session_metadata_rejects_arbitrary_endpoint(tmp_path):
    session = BrowserSession(tmp_path)
    session.metadata.write_text('{"pid": 123, "port": 80, "endpoint": "https://evil.test"}')
    with pytest.raises(DomainError, match="browser_session_metadata_invalid"):
        session.read()


def test_login_status_requires_positive_visible_evidence(tmp_path):
    from rednotebook.adapters.browser import BrowserReader

    async def run():
        reader = BrowserReader(tmp_path / "profile", headless=True)
        try:
            await reader.open()
            # Offline route makes the page origin real-looking without contacting the platform.
            await reader.page.route(
                "https://www.xiaohongshu.com/**",
                lambda route: route.fulfill(body="<main></main>", content_type="text/html"),
            )
            await reader.page.goto("https://www.xiaohongshu.com/explore")
            assert (await reader.authentication_state())["authentication"] == "unknown"
            await reader.page.set_content('<a href="/user/profile/synthetic">我</a>')
            assert (await reader.authentication_state())["authentication"] == "logged_in"
            reader.gate.pause()
            result = await reader.state()
            assert result["state"] == "paused" and result["authentication"] == "logged_in"
            await reader.page.set_content(
                '<div class="login-container">登录</div><a href="/user/profile/synthetic">我</a>'
            )
            assert (await reader.authentication_state())["authentication"] == "login_required"
        finally:
            await reader.close()

    asyncio.run(run())
