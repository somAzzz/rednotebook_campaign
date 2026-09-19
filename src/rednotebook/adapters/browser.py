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
from rednotebook.browser_deadline import browser_deadline
from rednotebook.browser_session import BrowserSession
from rednotebook.capture_review import completeness, select_comments
from rednotebook.domain.models import EvidenceInput
from rednotebook.errors import DomainError
from rednotebook.util import atomic_json, digest, stamp

ORIGIN = "https://www.xiaohongshu.com"
NOTE_PATH = re.compile(r"/(?:explore|search_result)/([0-9a-f]{24})$")

SEARCH_DOM = """() => Array.from(document.querySelectorAll('section.note-item')).map(e => {
 const links=Array.from(e.querySelectorAll('a[href*="/explore/"],a[href*="/search_result/"]'))
 .filter(a=>{const r=a.getBoundingClientRect();return r.width>0&&r.height>0;});
 const link=links.find(a=>new URL(a.href).searchParams.has('xsec_token')) || links[0];
 return {url:link?.href || '',title:e.querySelector('.title')?.innerText || '',
 metric_display:e.querySelector('.like-wrapper')?.innerText || '',
 video:!!e.querySelector('video, .play-icon, .video-icon') ||
 Array.from(e.querySelectorAll('svg use')).some(u => /video|play/i.test(u.getAttribute('href') || u.getAttribute('xlink:href') || ''))};
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
 likes:e.querySelector('.like-wrapper .count')?.innerText?.trim() || '',
 replies:e.querySelector('.reply-count')?.innerText?.trim() || '',
 pinned:!!e.querySelector('.top-tag, .pinned-tag'),
 parent_id:e.closest('.reply-container')?.closest('.comment-item')?.id || null,
 reply:e.classList.contains('comment-item-sub') || !!e.closest('.reply-container')
 }))};
}"""
IDENTITY_DOM = """() => {
 const n=document.querySelector('#noteContainer');
 const urls=[...document.querySelectorAll('link[rel="canonical"],meta[property="og:url"]')].map(e=>e.href||e.content);
 const ids=[n?.getAttribute('data-note-id'),n?.getAttribute('data-id')].filter(Boolean);
 const state=window.__INITIAL_STATE__?.note;
 const current=typeof state?.currentNoteId==='string' ? state.currentNoteId : state?.currentNoteId?.value ?? state?.currentNoteId?._value;
 if(typeof current==='string') ids.push(current);
 const note=state?.noteDetailMap?.[current]?.note;
 if(typeof note?.noteId==='string') ids.push(note.noteId);
 if(typeof note?.id==='string') ids.push(note.id);
 return {url:location.href, ids, urls};
}"""
BODY_CHECK_DOM = r"""() => {
 const n=document.querySelector('#noteContainer');
 const d=n?.querySelector('#detail-desc');
 const folded=!!n?.querySelector('#detail-desc .expand, #detail-desc .show-more, #detail-desc [aria-expanded="false"]');
 const clipped=d && (d.scrollHeight>d.clientHeight+2) && ['hidden','clip'].includes(getComputedStyle(d).overflowY);
 const state=window.__INITIAL_STATE__?.note;
 const current=typeof state?.currentNoteId==='string' ? state.currentNoteId : state?.currentNoteId?.value ?? state?.currentNoteId?._value;
 const desc=state?.noteDetailMap?.[current]?.note?.desc;
 const text=d?.innerText?.trim()||'';
 // Metadata encodes visible #topics as #topic[话题]#. Keep saved DOM text intact.
 const renderedDesc=typeof desc==='string' ? desc.replace(/#([^#\n]+)\[话题\]#/g,'#$1').trim() : desc;
 return {present:!!d || desc==='', folded:folded||!!clipped, text, metadata_matches:typeof renderedDesc!=='string' || renderedDesc===text};
}"""

CURRENT_IMAGE = """() => {
 const root=document.querySelector('#noteContainer'); if(!root) return null;
 const platform=location.hostname==='www.xiaohongshu.com';
 const allowed=src=>{try{const u=new URL(src);return u.protocol==='https:'&&u.hostname.endsWith('.xhscdn.com');}catch{return false;}};
 const candidates=Array.from(root.querySelectorAll('.note-slider-img img')).map(e=>{
 const r=e.getBoundingClientRect(); const p=(e.closest('.note-slider')||e.parentElement).getBoundingClientRect();
 const visible=Math.max(0,Math.min(r.right,innerWidth,p.right)-Math.max(r.left,0,p.left));
 const src=platform ? [e.currentSrc,e.src,e.getAttribute('src')].find(allowed) : e.currentSrc;
 return {src:src||'',visible,loaded:e.complete&&e.naturalWidth>0};
 }).filter(e=>{
  if(e.visible<=100) return false;
  return platform ? allowed(e.src) : e.loaded;
 }).sort((a,b)=>b.visible-a.visible);
 return candidates[0]?.src || null;
}"""


# Runs before site scripts and on existing tabs. Keep the video element so detail
# classification still works; block playback even when bytes are cached or blob-backed.
VIDEO_GUARD = """(() => {
 if (window.__rednotebookVideoGuard) return;
 window.__rednotebookVideoGuard = true;
 const stop = v => {v.pause(); v.muted=true; v.autoplay=false;
   v.removeAttribute('autoplay'); v.preload='none';};
 const originalPlay = HTMLMediaElement.prototype.play;
 HTMLMediaElement.prototype.play = function(...args) {
   if (this instanceof HTMLVideoElement) {stop(this); return Promise.resolve();}
   return originalPlay.apply(this,args);
 };
 const sweep = root => {
   if (root instanceof HTMLVideoElement) stop(root);
   if (root.querySelectorAll) root.querySelectorAll('video').forEach(stop);
 };
 document.addEventListener('play', e => {
   if (e.target instanceof HTMLVideoElement) stop(e.target);
 }, true);
 new MutationObserver(records => {
   for (const r of records) {
     if (r.type==='attributes' && r.target instanceof HTMLVideoElement) stop(r.target);
     for (const n of r.addedNodes) sweep(n);
   }
 }).observe(document, {subtree:true, childList:true, attributes:true, attributeFilter:['autoplay']});
 sweep(document);
})();"""


def is_video_request(request):
    url = urlsplit(request.url)
    host = (url.hostname or "").lower()
    return request.resource_type == "media" or (
        request.resource_type in {"fetch", "xhr", "other"}
        and (
            bool(re.search(r"\.(mp4|m3u8|m4s|webm|flv|ts)$", url.path, re.I))
            or host.endswith(".xhscdn.com")
            and "video" in host
        )
    )


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
    if not text and not raw.get("has_images"):
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
        "text_origin": "body"
        if raw.get("text", "").strip()
        else "title_fallback"
        if text
        else "empty_body",
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
                "text_origin": "comment",
                "metrics": [metric(k, c.get(k, ""), at) for k in ("likes", "replies")],
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
        try:
            async with browser_deadline(60, "browser_open_timeout"):
                await self._open(attach_only=attach_only)
        except BaseException:
            await self.close()
            raise

    async def _open(self, *, attach_only=False):
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
            self.context.set_default_timeout(10000)
            await self.context.add_init_script(VIDEO_GUARD)
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
            async with browser_deadline(5, "browser_renderer_unresponsive"):
                await self.page.evaluate("1")
                await self.page.evaluate(VIDEO_GUARD)

            # Concurrent and bounded: tab count must not multiply attach latency.
            async def protect(existing):
                async with browser_deadline(5, "browser_video_guard_timeout"):
                    await existing.evaluate(VIDEO_GUARD)

            pending = [
                asyncio.create_task(protect(p)) for p in self.context.pages if p != self.page
            ]
            try:
                if pending:
                    await asyncio.gather(*pending)
            finally:
                for task in pending:
                    if not task.done():
                        task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
        except BaseException:
            raise

    async def _route(self, route):
        if is_video_request(route.request):
            await route.abort()
        else:
            await route.continue_()

    async def close(self):
        """Disconnect automation. Keep the visible dedicated browser and its live login session."""
        context, browser, driver = self.context, self.browser, self.playwright
        self.context = self.page = self.browser = self.playwright = None
        try:
            async with asyncio.timeout(5):
                if context and browser:
                    await context.unroute("**/*", self._route)
                    await browser.close()
                elif context:
                    await context.close()
        except (PlaywrightError, TimeoutError):
            pass
        finally:
            if driver:
                try:
                    async with asyncio.timeout(5):
                        await driver.stop()
                except (PlaywrightError, TimeoutError):
                    pass

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
        # Pacing belongs outside the recovery deadline, and is skipped by recover-ui.
        if paced:
            async with browser_deadline(5, "browser_renderer_unresponsive"):
                if await self.note_overlay_state() != "visible":
                    return False
            await self.gate.action()
        async with browser_deadline(20, "note_overlay_recovery_timeout"):
            return await self._dismiss_note_overlay()

    async def _dismiss_note_overlay(self):
        """Operator recovery: leave one stale note route without clearing a pause."""
        if not self.page or self.page.is_closed():
            raise DomainError("browser_session_not_running")
        if urlsplit(self.page.url).hostname != "www.xiaohongshu.com":
            return False
        mask = self.page.locator(".note-detail-mask:visible").first
        if not await mask.count() or not await mask.locator("#noteContainer:visible").count():
            return False
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
        async with browser_deadline(15, "browser_status_timeout"):
            return await self._state()

    async def _state(self):
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
        skipped_videos = set()
        for turn in range(scrolls + 1):
            checkpoint()
            await self.check()
            for raw in await self.page.evaluate(SEARCH_DOM):
                try:
                    ident, url = note_url(raw["url"])
                except DomainError:
                    continue
                if raw.get("video"):
                    skipped_videos.add(ident)
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
            "skipped_video_count": len(skipped_videos),
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

    async def image_position(self):
        return await self.page.evaluate(r"""() => {
          const root=document.querySelector('#noteContainer');
          const fraction=root?.querySelector('.fraction, .swiper-pagination-fraction');
          const match=fraction?.textContent.match(/(\d+)\s*\/\s*\d+/);
          if(match) return Number(match[1]);
          const containers=document.querySelectorAll(
            '#noteContainer .pagination-media-container, .pagination-teleport-container');
          for(const c of containers) {
            if(!c.getClientRects().length || getComputedStyle(c).display==='none') continue;
            const dots=Array.from(c.querySelectorAll('.pagination-item'));
            const active=dots.findIndex(d=>d.matches(
              '.active, .selected, .pagination-item-active, [aria-current="true"]') || d.querySelector('.dot.active'));
            if(active>=0) return active+1;
          }
          return null;
        }""")

    async def prepare_interaction(self):
        # Background Chromium can suspend rAF: Playwright actionability then waits
        # forever for stability even when hit testing finds the correct control.
        # Activate only our dedicated page; retain all normal actionability checks.
        await self.check()
        async with browser_deadline(5, "browser_interaction_timeout"):
            await self.page.bring_to_front()

    async def select_image_page(self, position):
        # Reveal hover-only navigation before resolving controls. Never force a click
        # or dispatch synthetic DOM events through overlays/access challenges.
        await self.prepare_interaction()
        slider = self.page.locator("#noteContainer .note-slider").first
        if not await slider.count():
            slider = self.page.locator("#noteContainer .note-slider-img").first
        if await slider.count():
            await slider.hover(timeout=2000)
        dots = await self.image_pagination()
        if await dots.count() >= position:
            dot = dots.nth(position - 1)
            controls = [
                dot,
                dot.locator(
                    "button:visible, [role=button]:visible, img:visible, .dot:visible"
                ).first,
            ]
            for control in controls:
                if not await control.count():
                    continue
                try:
                    await control.click(trial=True, timeout=1000)
                except PlaywrightTimeoutError:
                    continue
                await self.gate.action()
                await control.click(timeout=2000)
                return
        # A next button is only safe when an independent counter proves the
        # starting page. This also supports resuming after a browser restart.
        current = await self.image_position()
        if current is None or current >= position:
            raise DomainError("image_pagination_not_interactive")
        for expected in range(current + 1, position + 1):
            await self.check()
            next_button = self.page.locator(
                "#noteContainer .arrow-controller.right:visible, "
                "#noteContainer .swiper-button-next:visible, "
                '#noteContainer button[aria-label="下一张"]:visible'
            )
            if await next_button.count() != 1:
                raise DomainError("image_pagination_unavailable")
            await self.gate.action()
            await next_button.click(timeout=2000)
            for _ in range(20):
                if await self.image_position() == expected:
                    break
                await asyncio.sleep(0.1)
            else:
                raise DomainError("image_page_unverified")

    async def verify_note_identity(self, expected):
        observed = await self.page.evaluate(IDENTITY_DOM)
        ids = {v for v in observed["ids"] if re.fullmatch(r"[0-9a-f]{24}", v)}
        for link in observed["urls"]:
            try:
                ids.add(note_url(link)[0])
            except DomainError:
                continue
        try:
            landed = note_url(observed["url"])[0]
        except DomainError:
            landed = None
        if landed is not None and landed != expected or any(i != expected for i in ids):
            raise DomainError("note_identity_mismatch")
        if landed is None or expected not in ids:
            raise DomainError("note_identity_unverified")
        return {"state": "verified", "note_id": expected, "basis": "detail_metadata_and_route"}

    async def collect(
        self,
        url,
        target,
        comment_limit=5,
        max_images=20,
        checkpoint=lambda: None,
        on_progress=None,
        capture_images=True,
        resume=None,
        resume_from=None,
    ):
        expected, url = note_url(url)
        if not 0 <= comment_limit <= 100 or not 1 <= max_images <= 20:
            raise DomainError("browser_collect_budget_invalid")
        checkpoint()
        same_page = False
        if resume:
            await self.open()
            try:
                await self.verify_note_identity(expected)
                same_page = True
            except DomainError:
                pass
        if not same_page:
            await self.navigate(url)
        try:
            await self.page.locator("#noteContainer").wait_for(state="visible")
        except Exception:
            await self.check()
            raise DomainError("note_detail_unavailable") from None
        identity = await self.verify_note_identity(expected)
        raw = await self.page.evaluate(DETAIL_DOM)
        if raw["video"]:
            return {"state": "skipped", "reason": "video_deferred"}
        # Only explicitly named expansion controls, never arbitrary text from the note.
        expand = self.page.locator("#detail-desc").get_by_text(
            re.compile(r"^(展开|展开全文|更多)$"), exact=True
        )
        if await expand.count() == 1 and await expand.is_visible():
            await self.gate.action()
            await self.prepare_interaction()
            await expand.click(timeout=5000)
        first = await self.page.evaluate(BODY_CHECK_DOM)
        await asyncio.sleep(0.1)
        second = await self.page.evaluate(BODY_CHECK_DOM)
        raw = await self.page.evaluate(DETAIL_DOM)
        if not second["metadata_matches"] and not second["folded"]:
            raise DomainError("note_content_mismatch")
        raw["identity"] = identity
        raw["body_check"] = {
            "state": "complete"
            if second["present"]
            and not second["folded"]
            and first["text"] == second["text"]
            and raw["text"] == second["text"]
            and second["metadata_matches"]
            else "unknown",
            "basis": "expanded_dom_stability_not_human_review",
        }
        if resume and (
            resume["raw"].get("text") != raw.get("text")
            or resume["raw"].get("title") != raw.get("title")
        ):
            raise DomainError("capture_snapshot_changed")
        images = list(resume.get("images", [])) if resume else []
        errors = list(resume.get("errors", [])) if resume_from == "comments" else []
        total = resume.get("declared_total") if resume else None
        value = {
            "state": "partial",
            "raw": raw,
            "images": images,
            "errors": errors,
            "image_checks": list(resume.get("image_checks", [])) if resume else [],
            "declared_total": total,
            "comment_status": "not_requested",
            "image_capture": "requested" if capture_images else "not_requested",
        }

        def persist(stage):
            checkpoint()
            value["declared_total"] = total
            value["completeness"] = completeness(value)
            if on_progress:
                on_progress(value, stage)

        async def image_signature(expected_total):
            resources = await self.page.evaluate("""() => {
              const s=window.__INITIAL_STATE__?.note;
              const current=typeof s?.currentNoteId==='string' ? s.currentNoteId : s?.currentNoteId?.value ?? s?.currentNoteId?._value;
              const list=s?.noteDetailMap?.[current]?.note?.imageList;
              if(Array.isArray(list)) return list.map(i=>i.urlDefault||i.url_default||i.url||'');
              const slides=Array.from(document.querySelectorAll('#noteContainer .swiper-slide:not(.swiper-slide-duplicate)'));
              if(slides.length) return slides.map(e=>e.querySelector('.note-slider-img img')).map(i=>i?.currentSrc||i?.src||'');
              return Array.from(document.querySelectorAll('#noteContainer .note-slider-img img')).map(i=>i.currentSrc||i.src);
            }""")
            return (
                digest(resources)
                if expected_total is not None
                and len(resources) == expected_total
                and all(resources)
                else None
            )

        if resume and images:
            signature = await image_signature(resume.get("declared_total"))
            if not signature or signature != resume.get("image_set_signature"):
                raise DomainError("capture_image_snapshot_unverified")
        persist("text_saved")
        # Optional comments must never prevent the core image stage from running.
        if capture_images and resume_from != "comments":
            dots = await self.image_pagination()
            total = await dots.count() or None
            if total is None:
                # Explicit slide counters and image containers provide an independent fallback.
                image_info = await self.page.evaluate(r"""() => {
                  const n=document.querySelector('#noteContainer');
                  const slider=n.querySelector('.note-slider-img');
                  const count=n.querySelector('.fraction, .swiper-pagination-fraction')?.innerText || '';
                  const m=count.match(/\/(\d+)/);
                  const state=window.__INITIAL_STATE__?.note;
                  const current=typeof state?.currentNoteId==='string' ? state.currentNoteId : state?.currentNoteId?.value ?? state?.currentNoteId?._value;
                  const list=state?.noteDetailMap?.[current]?.note?.imageList;
                  return {count:m?Number(m[1]):Array.isArray(list)?list.length:null,
                    declared:n.getAttribute('data-image-count')};
                }""")
                total = image_info["count"]
                if total is None and image_info["declared"] and image_info["declared"].isdigit():
                    total = int(image_info["declared"])
            if total is not None and (type(total) is not int or not 0 <= total <= 1000):
                raise DomainError("image_count_invalid")
            if total is None:
                errors.append({"code": "image_count_unavailable"})
            else:
                value["image_set_signature"] = await image_signature(total)
                persist("images_start")
                if total:
                    await self.prepare_interaction()
                async with httpx.AsyncClient(
                    timeout=30, follow_redirects=False, trust_env=False
                ) as client:
                    if resume and resume.get("declared_total") not in {None, total}:
                        raise DomainError("capture_snapshot_changed")
                    previous_src = await self.page.evaluate(CURRENT_IMAGE) if images else None
                    for position in range(len(images) + 1, min(total, max_images) + 1):
                        checkpoint()
                        await self.check()
                        await self.verify_note_identity(expected)
                        if position > 1:
                            try:
                                await self.select_image_page(position)
                            except (PlaywrightTimeoutError, DomainError) as exc:
                                if isinstance(exc, DomainError) and exc.code not in {
                                    "image_pagination_not_interactive",
                                    "image_pagination_unavailable",
                                    "image_page_unverified",
                                }:
                                    raise
                                errors.append(
                                    {
                                        "position": position,
                                        "code": exc.code
                                        if isinstance(exc, DomainError)
                                        else "image_pagination_not_interactive",
                                    }
                                )
                                break
                        try:
                            # A changed resource proves we did not immediately save the old page.
                            # Repeated identical resources require further manual page verification.
                            script = (
                                "prev => {const current=("
                                + CURRENT_IMAGE
                                + ")(); return current && current!==prev;}"
                            )
                            await self.page.wait_for_function(
                                script, arg=previous_src, timeout=10000, polling=100
                            )
                            actual_position = await self.image_position()
                            if actual_position is not None and actual_position != position:
                                raise DomainError("image_page_unverified")
                            src = await self.page.evaluate(CURRENT_IMAGE)
                            data = await download_image(client, src)
                            checkpoint()
                            file = target / f"{position:02d}.image"
                            file.write_bytes(data)
                            file.chmod(0o600)
                            from io import BytesIO

                            from PIL import Image

                            with Image.open(BytesIO(data)) as decoded:
                                value.setdefault("image_checks", []).append(
                                    {
                                        "position": position,
                                        "width": decoded.width,
                                        "height": decoded.height,
                                        "decoded": True,
                                    }
                                )
                            images.append(
                                {
                                    "position": position,
                                    "path": file.name,
                                    "sha256": hashlib.sha256(data).hexdigest(),
                                }
                            )
                            previous_src = src
                            persist("images_saved")
                        except PlaywrightTimeoutError:
                            errors.append({"position": position, "code": "image_page_unverified"})
                            break
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
        value["image_set_signature"] = await image_signature(total)
        persist("images_finished")
        persist("comments_start")
        if comment_limit:
            observed = list(raw["comments"])
            try:
                async with browser_deadline(30, "comment_sample_timeout"):
                    for _ in range(3):
                        if len({c.get("id") for c in observed}) >= max(20, comment_limit):
                            break
                        panel = self.page.locator("#noteContainer .note-scroller").first
                        if not await panel.count():
                            break
                        await self.gate.action()
                        await self.prepare_interaction()
                        await panel.hover(timeout=5000)
                        await self.page.mouse.wheel(0, 650)
                        await asyncio.sleep(0.5)
                        await self.check()
                        await self.verify_note_identity(expected)
                        observed.extend((await self.page.evaluate(DETAIL_DOM))["comments"])
                    raw["comments"], value["comment_sampling"] = select_comments(
                        observed, comment_limit
                    )
                    value["comment_status"] = (
                        "sampled" if raw["comments"] else "unavailable_or_empty"
                    )
            except DomainError as exc:
                if exc.code not in {"comment_sample_timeout", "browser_interaction_timeout"}:
                    raise
                raw["comments"], value["comment_sampling"] = select_comments(
                    observed, comment_limit
                )
                value["comment_status"] = "partial" if raw["comments"] else "unavailable"
                value["comment_error"] = exc.code
            except PlaywrightError:
                raw["comments"], value["comment_sampling"] = select_comments(
                    observed, comment_limit
                )
                value["comment_status"] = "partial" if raw["comments"] else "unavailable"
                value["comment_error"] = "comment_dom_failed"
        else:
            raw["comments"] = []
        await self.verify_note_identity(expected)
        final_body = await self.page.evaluate(BODY_CHECK_DOM)
        if final_body["text"] != raw["text"]:
            raw["body_check"]["state"] = "unknown"
            errors.append({"code": "body_changed_during_capture"})
        value["completeness"] = completeness(value)
        value["state"] = (
            "complete" if value["completeness"]["capture_gate"] == "passed" else "partial"
        )
        persist("capture_finished")
        return value


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
    review = completeness(collected)
    state = collected["state"]
    if state == "complete" and review["capture_gate"] != "passed":
        state = "partial"
    atomic_json(target / "checkpoint.json", collected | {"completeness": review, "state": state})
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
        "completeness": review,
        "comment_sampling": collected.get("comment_sampling"),
        "comment_error": collected.get("comment_error"),
        "image_checks": collected.get("image_checks", []),
        "records": len(records),
        "manifest": bool(manifest),
        "images": len(collected["images"]),
        "declared_total": collected["declared_total"],
        "errors": collected["errors"],
        "state": state,
        "missing_positions": sorted(
            set(range(1, (collected["declared_total"] or 0) + 1))
            - {i["position"] for i in collected["images"]}
        ),
        "image_total_known": collected["declared_total"] is not None,
        "image_capture": collected.get("image_capture", "requested"),
    }
