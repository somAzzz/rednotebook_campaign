"""No live accounts or actual machine sleep: exercise stalled async operations."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from rednotebook.browser_deadline import browser_deadline
from rednotebook.errors import DomainError


def test_timeout_and_cancellation_are_distinct():
    async def run():
        with pytest.raises(DomainError, match="test_timeout"):
            async with browser_deadline(0.01, "test_timeout"):
                await asyncio.Event().wait()
        with pytest.raises(asyncio.CancelledError):
            async with browser_deadline(1, "test_timeout"):
                raise asyncio.CancelledError

    asyncio.run(run())


@pytest.mark.parametrize("fail", [False, True])
def test_clock_jump_is_suspected_not_definitive_sleep(monkeypatch, fail):
    import rednotebook.browser_deadline as module

    wall = iter([100, 200])
    mono = iter([10, 11])
    monkeypatch.setattr(
        module, "time", SimpleNamespace(time=lambda: next(wall), monotonic=lambda: next(mono))
    )

    async def run():
        with pytest.raises(DomainError, match="system_suspend_or_clock_change_suspected"):
            async with browser_deadline(1, "test_timeout"):
                if fail:
                    raise DomainError("another_failure")

    asyncio.run(run())


def test_stalled_open_has_outer_deadline_and_cleanup(tmp_path, monkeypatch):
    from rednotebook.adapters import browser

    async def run():
        reader = browser.BrowserReader(tmp_path)

        async def stalled(**kwargs):
            await asyncio.Event().wait()

        reader._open = stalled
        reader.close = AsyncMock()
        monkeypatch.setattr(
            browser, "browser_deadline", lambda seconds, code: browser_deadline(0.01, code)
        )
        with pytest.raises(DomainError, match="browser_open_timeout"):
            await reader.open(attach_only=True)
        reader.close.assert_awaited_once()

    asyncio.run(run())


def test_recovery_bounds_locator_calls_not_only_navigation(tmp_path, monkeypatch):
    from rednotebook.adapters import browser

    async def run():
        reader = browser.BrowserReader(tmp_path)
        mask = SimpleNamespace(count=AsyncMock(side_effect=lambda: asyncio.Event().wait()))

        # AsyncMock return awaitables are not recursively awaited: use a real stalled method.
        async def count():
            await asyncio.Event().wait()

        mask.count = count
        reader.page = SimpleNamespace(
            is_closed=lambda: False,
            url="https://www.xiaohongshu.com/explore/test",
            locator=lambda _: SimpleNamespace(first=mask),
        )
        monkeypatch.setattr(
            browser, "browser_deadline", lambda seconds, code: browser_deadline(0.01, code)
        )
        with pytest.raises(DomainError, match="note_overlay_recovery_timeout"):
            await reader.dismiss_note_overlay(paced=False)

    asyncio.run(run())


@pytest.mark.parametrize("stalled_tab", [0, 1])
def test_attach_probes_renderer_and_bounds_other_tabs(tmp_path, monkeypatch, stalled_tab):
    from rednotebook.adapters import browser

    async def run():
        async def stalled(*args):
            await asyncio.Event().wait()

        pages = [
            SimpleNamespace(
                url="about:blank",
                set_default_timeout=lambda _: None,
                evaluate=AsyncMock(return_value=1),
            )
            for _ in range(2)
        ]
        pages[stalled_tab].evaluate = stalled
        context = SimpleNamespace(
            pages=pages,
            set_default_timeout=lambda _: None,
            add_init_script=AsyncMock(),
            route=AsyncMock(),
            close=AsyncMock(),
        )
        driver = SimpleNamespace(
            chromium=SimpleNamespace(launch_persistent_context=AsyncMock(return_value=context)),
            stop=AsyncMock(),
        )
        monkeypatch.setattr(
            browser,
            "async_playwright",
            lambda: SimpleNamespace(start=AsyncMock(return_value=driver)),
        )
        monkeypatch.setattr(
            browser,
            "browser_deadline",
            lambda seconds, code: browser_deadline(0.02 if seconds == 5 else 1, code),
        )
        reader = browser.BrowserReader(tmp_path, headless=True)
        expected = (
            "browser_renderer_unresponsive" if stalled_tab == 0 else "browser_video_guard_timeout"
        )
        with pytest.raises(DomainError, match=expected):
            await reader.open()
        assert reader.context is None and reader.playwright is None
        context.close.assert_awaited_once()
        driver.stop.assert_awaited_once()

    asyncio.run(run())


def test_foreground_activation_is_bounded_and_respects_access(tmp_path, monkeypatch):
    from rednotebook.adapters import browser

    async def run():
        reader = browser.BrowserReader(tmp_path)

        async def stalled():
            await asyncio.Event().wait()

        reader.page = SimpleNamespace(bring_to_front=stalled)
        reader.check = AsyncMock()
        monkeypatch.setattr(
            browser, "browser_deadline", lambda seconds, code: browser_deadline(0.01, code)
        )
        with pytest.raises(DomainError, match="browser_interaction_timeout"):
            await reader.prepare_interaction()
        reader.page.bring_to_front = AsyncMock()
        reader.check = AsyncMock(side_effect=DomainError("captcha_required"))
        with pytest.raises(DomainError, match="captcha_required"):
            await reader.prepare_interaction()
        reader.page.bring_to_front.assert_not_awaited()

    asyncio.run(run())
