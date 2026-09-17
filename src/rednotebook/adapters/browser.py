"""Bounded DOM-only Xiaohongshu reader using a dedicated Playwright profile."""

import asyncio
import hashlib
import re
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from rednotebook.browser_control import AccessGate
from rednotebook.browser_session import BrowserSession
from rednotebook.domain.models import EvidenceInput
from rednotebook.errors import DomainError
from rednotebook.util import atomic_json, stamp

ORIGIN = "https://www.xiaohongshu.com"
NOTE_PATH = re.compile(r"/(?:explore|search_result)/([0-9a-f]{24})$")

SEARCH_DOM = """() => Array.from(document.querySelectorAll('section.note-item')).map(e => {
 const links=Array.from(e.querySelectorAll('a[href*="/explore/"],a[href*="/search_result/"]'))
 .filter(a=>{const r=a.getBoundingClientRect();return r.width>0&&r.height>0;});
 const link=links.find(a=>new URL(a.href).searchParams.has('xsec_token')) || links[0];
 return {url:link?.href || '',title:e.querySelector('.title')?.innerText || '',
 metric_display:e.querySelector('.like-wrapper')?.innerText || ''};
})"""
DETAIL_DOM = """() => {
 const n=document.querySelector('#noteContainer'); if(!n) return null;
 const txt=s=>n.querySelector(s)?.innerText?.trim() || '';
 return {title:txt('#detail-title'),text:txt('#detail-desc'),
 author:n.querySelector('.author-wrapper a[href*="/user/profile/"]')?.getAttribute('href') || null,
 video:!!n.querySelector('video'), has_images:!!n.querySelector('.note-slider-img img'),
 metrics:{likes:txt('.interact-container .like-wrapper .count'),
 saves:txt('.interact-container .collect-wrapper .count'),
 comments:txt('.interact-container .chat-wrapper .count')},
 comments:Array.from(n.querySelectorAll('.comment-item')).slice(0,200).map(e=>({
 id:e.id || '',text:e.querySelector('.content')?.innerText?.trim() || '',
 author:e.querySelector('[data-user-id]')?.getAttribute('data-user-id') || null,
 reply:e.classList.contains('comment-item-sub') || !!e.closest('.reply-container')
 }))};
}"""
CURRENT_IMAGE = """() => {
 const root=document.querySelector('#noteContainer'); if(!root) return null;
 const platform=location.hostname==='www.xiaohongshu.com';
 const allowed=src=>{try{const u=new URL(src);return u.protocol==='https:'&&u.hostname.endsWith('.xhscdn.com');}catch{return false;}};
 const candidates=Array.from(root.querySelectorAll('.note-slider-img img')).map(e=>{
 const r=e.getBoundingClientRect(); const p=e.parentElement.getBoundingClientRect();
 const visible=Math.max(0,Math.min(r.right,innerWidth,p.right)-Math.max(r.left,0,p.left));
 const src=platform ? [e.currentSrc,e.src,e.getAttribute('src')].find(allowed) : e.currentSrc;
 return {src:src||'',visible,loaded:e.complete&&e.naturalWidth>0};
 }).filter(e=>{
  if(e.visible<=100) return false;
  return platform ? allowed(e.src) : e.loaded;
 }).sort((a,b)=>b.visible-a.visible);
 return candidates[0]?.src || null;
}"""


def note_url(value):
    try:
        url = urlsplit(urljoin(ORIGIN, value))
        match = NOTE_PATH.fullmatch(url.path)
        if url.scheme != "https" or url.netloc != "www.xiaohongshu.com" or not match:
            raise ValueError
        return match[1], urlunsplit((url.scheme, url.netloc, url.path, url.query, ""))
    except ValueError:
        raise DomainError("browser_note_url_invalid") from None


def metric(name, raw, at):
    raw = raw.strip()
    value, precision = None, "exact"
    if re.fullmatch(r"\d+", raw):
        value = float(raw)
    elif re.fullmatch(r"\d+(?:\.\d+)?万", raw):
        value, precision = float(raw[:-1]) * 10000, "abbreviated"
    return {
        "name": name,
        "value": value,
        "raw_display": raw or None,
        "source_kind": "public",
        "precision": precision,
        "definition": "public DOM displayed count; platform definition unverified",
        "observed_at": at,
        "missing_reason": "not_readable" if value is None else None,
    }


def records_from_dom(raw, source, brief_id, keyword, run_id, url, at, comment_limit):
    note_id, _ = note_url(url)
    text = raw.get("text", "").strip() or raw.get("title", "").strip()
    if not text:
        raise DomainError("browser_note_text_missing")
    comments = [c for c in raw.get("comments", []) if c.get("id") and c.get("text")]
    comments = list({c["id"]: c for c in comments}.values())[:comment_limit]
    sample = {
        "run_id": run_id,
        "brief_id": brief_id,
        "keyword": keyword,
        "sort": "unknown",
        "group": "unknown",
        "truncated": True,
    }
    shared = {
        "source_id": source.id,
        "observed_at": at,
        "synthetic": source.synthetic,
        "sampling": [sample],
    }
    metrics = [
        metric(k, raw.get("metrics", {}).get(k, ""), at) for k in ("likes", "saves", "comments")
    ]
    total = metrics[2]["value"] if metrics[2]["precision"] == "exact" else None
    author = raw.get("author")
    author = author.split("/user/profile/")[-1].split("?")[0] if author else None
    note = shared | {
        "kind": "note",
        "external_id": note_id,
        "title": raw.get("title", ""),
        "text": text,
        "locator": f"{ORIGIN}/explore/{note_id}",
        "author_id": author,
        "format": "image_text" if raw.get("has_images") else None,
        "metrics": metrics,
        "coverage": {
            "reported_total": int(total) if total is not None else None,
            "mode": "unknown",
            "includes_replies": any(c.get("reply") for c in comments),
            "truncated": True,
            "failure": "bounded_visible_comments_only",
        },
    }
    result = [note]
    for c in comments:
        result.append(
            shared
            | {
                "kind": "comment",
                "external_id": c["id"],
                "parent_external_id": note_id,
                "text": c["text"],
                "locator": note["locator"] + "#" + quote(c["id"], safe=""),
                "author_id": c.get("author"),
                "is_reply": bool(c.get("reply")),
                "is_author_reply": bool(author and c.get("author") == author),
            }
        )
    return [EvidenceInput.model_validate(r).model_dump(mode="json") for r in result]


class BrowserReader:
    def __init__(self, profile: Path, headless=False, gate=None):
        self.profile = profile.resolve()
        self.headless = headless
        self.gate = gate or AccessGate(self.profile)
        self.playwright = self.context = self.page = None
        self.browser = None
        self.session = BrowserSession(self.profile)

    async def open(self, *, attach_only=False):
        if not attach_only:
            self.gate.require_active()
        if self.context:
            return
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.profile.chmod(0o700)
        self.playwright = await async_playwright().start()
        try:
            if self.headless:
                self.context = await self.playwright.chromium.launch_persistent_context(
                    str(self.profile),
                    headless=True,
                    accept_downloads=False,
                    viewport={"width": 1280, "height": 900},
                    timeout=30000,
                )
            else:
                self.browser = await self.session.connect(self.playwright, create=not attach_only)
                self.context = self.browser.contexts[0]
            await self.context.route("**/*", self._route)
            self.page = next(
                (
                    p
                    for p in self.context.pages
                    if urlsplit(p.url).hostname == "www.xiaohongshu.com"
                ),
                self.context.pages[0] if self.context.pages else None,
            )
            if self.page is None:
                self.page = await self.context.new_page()
            self.page.set_default_timeout(10000)
        except BaseException:
            await self.close()
            raise

    async def _route(self, route):
        if route.request.resource_type == "media":
            await route.abort()
        else:
            await route.continue_()

    async def close(self):
        """Disconnect automation. Keep the visible dedicated browser and its live login session."""
        try:
            if self.context and self.browser:
                await self.context.unroute("**/*", self._route)
                await self.browser.close()
            elif self.context:
                await self.context.close()
        except PlaywrightError:
            pass
        finally:
            self.context = self.page = self.browser = None
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None

    async def shutdown(self):
        """Explicit operator request to close the dedicated browser, preserving its profile."""
        try:
            if self.browser:
                await self.session.shutdown(self.browser)
        finally:
            await self.close()

    async def authentication_state(self):
        if not self.page or self.page.is_closed():
            return {"authentication": "unknown", "evidence": "no_open_page"}
        if urlsplit(self.page.url).hostname != "www.xiaohongshu.com":
            return {"authentication": "unknown", "evidence": "not_on_platform"}
        if await self.page.locator(".login-container").first.is_visible():
            return {"authentication": "login_required", "evidence": "visible_login_dialog"}
        account = self.page.get_by_role("link", name="我", exact=True)
        if await account.count() == 1 and await account.is_visible():
            return {"authentication": "logged_in", "evidence": "visible_account_navigation"}
        return {"authentication": "unknown", "evidence": "account_navigation_not_detected"}

    async def note_overlay_state(self):
        """Inspect only the known note-detail shell, never the note's source text."""
        if not self.page or self.page.is_closed():
            return "unknown"
        try:
            visible = self.page.locator(".note-detail-mask:visible #noteContainer:visible")
            return "visible" if await visible.count() else "not_present"
        except PlaywrightError:
            return "unknown"

    async def dismiss_note_overlay(self, *, paced=True):
        """Operator recovery: leave one stale note route without clearing a pause."""
        if not self.page or self.page.is_closed():
            raise DomainError("browser_session_not_running")
        if urlsplit(self.page.url).hostname != "www.xiaohongshu.com":
            return False
        mask = self.page.locator(".note-detail-mask:visible").first
        if not await mask.count() or not await mask.locator("#noteContainer:visible").count():
            return False
        if paced:
            await self.gate.action()
        try:
            # Closing controls/backdrop clicks can leave the direct detail route
            # mounted. Operator recovery returns to the prior route explicitly.
            await self.page.go_back(wait_until="commit", timeout=10000)
        except (PlaywrightError, PlaywrightTimeoutError):
            # SPA history can change the route without producing a navigation
            # lifecycle event. The observed UI state is authoritative here.
            if await self.note_overlay_state() == "not_present":
                return True
            raise DomainError("note_overlay_recovery_failed") from None
        try:
            await mask.wait_for(state="hidden", timeout=5000)
        except (PlaywrightError, PlaywrightTimeoutError):
            if await self.note_overlay_state() == "not_present":
                return True
            raise DomainError("note_overlay_recovery_failed") from None
        return True

    async def state(self):
        overlay = await self.note_overlay_state()
        if self.gate.status()["paused"]:
            return {
                "state": "paused",
                "reason": "browser_access_paused",
                "access": self.gate.status(),
                "note_overlay": overlay,
                "operator_recovery": "recover_ui_then_explicit_resume"
                if overlay == "visible"
                else None,
            } | await self.authentication_state()
        if not self.page or self.page.is_closed():
            return {"state": "closed"}
        # Inspect dedicated visible dialogs, never all note text (which is untrusted data).
        for selector in (".captcha-container", "#captcha_container", ".verify-dialog"):
            if await self.page.locator(selector).first.is_visible():
                self.gate.pause("captcha_required")
                return {"state": "paused", "reason": "captcha_required"}
        if await self.page.locator(".login-container").first.is_visible():
            self.gate.pause("login_required")
            return {"state": "paused", "reason": "login_required"}
        if urlsplit(self.page.url).hostname != "www.xiaohongshu.com":
            return {"state": "paused", "reason": "unexpected_page"}
        return {
            "state": "ready",
            "note_overlay": overlay,
            "access": self.gate.status(),
        } | await self.authentication_state()

    async def check(self):
        state = await self.state()
        if state["state"] != "ready":
            raise DomainError(state.get("reason", "browser_closed"))

    async def navigate(self, url):
        await self.gate.navigation()
        await self.open()
        # page.goto replaces the current route directly. A note overlay is page
        # state, not an access-risk signal and not a prerequisite to navigation.
        try:
            response = await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except PlaywrightTimeoutError:
            await self.check()
            raise DomainError("browser_navigation_timeout") from None
        except PlaywrightError:
            await self.check()
            raise DomainError("browser_navigation_failed") from None
        if response and response.status in {401, 403, 429}:
            code = {401: "login_required", 403: "access_blocked", 429: "rate_limited"}[
                response.status
            ]
            self.gate.pause(code)
            raise DomainError(code)
        await self.check()

    async def search(self, keyword, limit=10, scrolls=3, checkpoint=lambda: None):
        if (
            not keyword.strip()
            or len(keyword) > 100
            or not 1 <= limit <= 20
            or not 0 <= scrolls <= 5
        ):
            raise DomainError("browser_search_budget_invalid")
        checkpoint()
        await self.navigate(
            f"{ORIGIN}/search_result?keyword={quote(keyword, safe='')}&source=web_explore_feed"
        )
        try:
            await self.page.locator("section.note-item").first.wait_for(state="visible")
        except Exception:
            await self.check()
            raise DomainError("search_results_unavailable") from None
        found = {}
        for turn in range(scrolls + 1):
            checkpoint()
            await self.check()
            for raw in await self.page.evaluate(SEARCH_DOM):
                try:
                    ident, url = note_url(raw["url"])
                except DomainError:
                    continue
                found.setdefault(
                    ident,
                    {
                        "note_id": ident,
                        # Display the stable URL. The signed URL is only for collect_note.
                        "public_url": f"{ORIGIN}/explore/{ident}",
                        "collect_url": url,
                        "title": raw["title"][:500],
                        "metric_display": raw["metric_display"][:100],
                    },
                )
                if len(found) >= limit:
                    break
            if len(found) >= limit or turn == scrolls:
                break
            await self.gate.action()
            await self.page.mouse.wheel(0, 700)
            await asyncio.sleep(0.8)
        checkpoint()
        return {
            "state": "partial",
            "keyword": keyword,
            "sort": "unknown",
            "truncated": True,
            "candidates": list(found.values()),
            "scrolls": turn,
            "limitations": ["bounded_search_not_representative", "ranking_unverified"],
        }

    async def image_pagination(self):
        # The desktop page can retain a hidden teleport clone alongside the visible slider.
        visible = self.page.locator(
            "#noteContainer .pagination-media-container .pagination-item:visible"
        )
        if await visible.count():
            return visible
        return self.page.locator(".pagination-teleport-container .pagination-item:visible")

    async def collect(
        self,
        url,
        target,
        comment_limit=20,
        max_images=20,
        checkpoint=lambda: None,
        on_progress=None,
        capture_images=True,
    ):
        _, url = note_url(url)
        if not 0 <= comment_limit <= 100 or not 1 <= max_images <= 20:
            raise DomainError("browser_collect_budget_invalid")
        checkpoint()
        await self.navigate(url)
        try:
            await self.page.locator("#noteContainer").wait_for(state="visible")
        except Exception:
            await self.check()
            raise DomainError("note_detail_unavailable") from None
        raw = await self.page.evaluate(DETAIL_DOM)
        if raw["video"]:
            return {"state": "skipped", "reason": "video_deferred"}
        images, errors, total = [], [], None

        def persist(stage):
            checkpoint()
            if on_progress:
                on_progress(
                    {
                        "state": "partial",
                        "raw": {**raw, "comments": raw["comments"][:comment_limit]},
                        "images": list(images),
                        "declared_total": total,
                        "errors": list(errors) or [{"code": "capture_in_progress"}],
                    },
                    stage,
                )

        persist("text_saved")
        for _ in range(3 if comment_limit else 0):
            if len(raw["comments"]) >= comment_limit:
                break
            checkpoint()
            panel = self.page.locator("#noteContainer .note-scroller").first
            if not await panel.count():
                break
            await self.gate.action()
            await panel.hover()
            await self.page.mouse.wheel(0, 650)
            await asyncio.sleep(0.5)
            await self.check()
            raw = await self.page.evaluate(DETAIL_DOM)
            persist("comments_saved")
        if not capture_images:
            return {
                "state": "complete",
                "raw": raw,
                "images": [],
                "declared_total": None,
                "errors": [],
                "image_capture": "not_requested",
            }
        dots = await self.image_pagination()
        total = await dots.count()
        if not total:
            # Absence of pagination is not proof of a single image.
            return {
                "state": "partial",
                "raw": raw,
                "images": [],
                "declared_total": None,
                "errors": [{"code": "image_count_unavailable"}],
            }
        persist("images")
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            for position in range(1, min(total, max_images) + 1):
                checkpoint()
                await self.check()
                # The first image is already selected. Its active pagination
                # marker may intentionally be non-interactive.
                if position > 1:
                    await self.gate.action()
                    try:
                        await dots.nth(position - 1).click()
                    except PlaywrightTimeoutError:
                        errors.append(
                            {"position": position, "code": "image_pagination_not_interactive"}
                        )
                        break
                    await asyncio.sleep(0.4)
                try:
                    await self.page.wait_for_function(CURRENT_IMAGE, timeout=30000)
                except PlaywrightTimeoutError:
                    errors.append({"position": position, "code": "image_not_ready"})
                    break
                src = await self.page.evaluate(CURRENT_IMAGE)
                try:
                    data = await download_image(client, src)
                    checkpoint()
                    file = target / f"{position:02d}.image"
                    file.write_bytes(data)
                    file.chmod(0o600)
                    images.append(
                        {
                            "position": position,
                            "path": file.name,
                            "sha256": hashlib.sha256(data).hexdigest(),
                        }
                    )
                    persist("images")
                except (DomainError, httpx.HTTPError) as exc:
                    errors.append(
                        {
                            "position": position,
                            "code": exc.code
                            if isinstance(exc, DomainError)
                            else "image_network_error",
                        }
                    )
                    break
        if total > max_images and not errors:
            errors.append({"code": "image_limit_reached"})
        checkpoint()
        return {
            "state": "partial" if errors or len(images) != total else "complete",
            "raw": raw,
            "images": images,
            "declared_total": total,
            "errors": errors,
        }


async def download_image(client, url):
    from io import BytesIO

    from PIL import Image

    parsed = urlsplit(url or "")
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or not parsed.hostname.endswith(".xhscdn.com")
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 80, 443}
    ):
        raise DomainError("image_host_not_allowed")
    async with client.stream("GET", url) as response:
        if response.status_code != 200:
            raise DomainError("image_download_failed")
        data = bytearray()
        async for block in response.aiter_bytes():
            data.extend(block)
            if len(data) > 20 * 1024 * 1024:
                raise DomainError("image_too_large")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > 32_000_000:
                raise ValueError
            image.verify()
    except Exception:
        raise DomainError("image_invalid") from None
    return bytes(data)


def save_capture(
    target, collected, source, brief_id, keyword, run_id, url, clock, observed_at=None
):
    records = records_from_dom(
        collected["raw"], source, brief_id, keyword, run_id, url, observed_at or stamp(clock()), 100
    )
    atomic_json(target / "evidence.json", records)
    manifest = None
    if collected["images"]:
        manifest = {
            "source_id": source.id,
            "note_external_id": note_url(url)[0],
            "declared_total": collected["declared_total"],
            "images": collected["images"],
        }
        atomic_json(target / "manifest.json", manifest)
    return {
        "records": len(records),
        "manifest": bool(manifest),
        "images": len(collected["images"]),
        "declared_total": collected["declared_total"],
        "errors": collected["errors"],
        "state": collected["state"],
        "missing_positions": sorted(
            set(range(1, (collected["declared_total"] or 0) + 1))
            - {i["position"] for i in collected["images"]}
        ),
        "image_total_known": collected["declared_total"] is not None,
        "image_capture": collected.get("image_capture", "requested"),
    }
