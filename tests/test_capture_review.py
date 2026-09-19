"""Synthetic DOM tests for capture completeness, identity and optional comments."""

import asyncio
import hashlib
import json
from io import BytesIO

import pytest
from PIL import Image
from test_browser import NOTE, OfflineGate, offline_note_location

from rednotebook.adapters import browser
from rednotebook.capture_review import completeness, select_comments
from rednotebook.errors import DomainError


def test_comment_ranking_dedup_and_unknown_metrics():
    rows = [
        dict(id="a", text="a", replies="0", likes="100"),
        dict(id="b", text="b", replies="3", likes="2"),
        dict(id="b", text="b", replies="3", likes="2"),
    ]
    selected, sampling = select_comments(rows, 1)
    assert selected[0]["id"] == "b" and sampling["observed_unique"] == 2
    assert sampling["selection_basis"] == "visible_sample_replies_descending"
    rows[1]["replies"] = ""
    rows[2]["replies"] = ""
    selected, sampling = select_comments(rows, 1)
    assert (
        selected[0]["id"] == "a"
        and sampling["selection_basis"] == "visible_sample_likes_descending"
    )
    rows[0]["likes"] = ""
    _, sampling = select_comments(rows, 1)
    assert sampling["selection_basis"] == "visible_order_metrics_incomplete"


def test_completeness_requires_known_total_and_independent_body_identity():
    value = dict(
        raw={"text": "", "identity": {"state": "verified"}, "body_check": {"state": "complete"}},
        images=[],
        errors=[],
        declared_total=0,
    )
    assert completeness(value)["capture_gate"] == "passed"
    assert completeness(value)["body_empty"]
    value["declared_total"] = None
    assert completeness(value)["capture_gate"] == "blocked"
    assert completeness(value)["missing_positions"] is None
    value["declared_total"] = 2
    value["images"] = [{"position": 1}, {"position": 1}]
    assert completeness(value)["missing_positions"] == [2]


async def reader_with_html(tmp_path, html):
    reader = browser.BrowserReader(
        tmp_path / "profile", headless=True, gate=OfflineGate(tmp_path / "profile")
    )
    await reader.open()
    offline_note_location(reader, NOTE)

    async def navigate(url):
        await reader.page.set_content(html)

    async def check():
        pass

    reader.navigate, reader.check = navigate, check
    return reader


def test_wrong_note_stops_before_checkpoint(tmp_path):
    async def run():
        reader = await reader_with_html(
            tmp_path,
            '<div id="noteContainer" data-note-id="bbbbbbbbbbbbbbbbbbbbbbbb"><div id="detail-desc">wrong</div></div>',
        )
        saved = []
        try:
            with pytest.raises(DomainError, match="note_identity_mismatch"):
                await reader.collect(NOTE, tmp_path, on_progress=lambda *args: saved.append(args))
            assert not saved
        finally:
            await reader.close()

    asyncio.run(run())


def test_folded_body_unknown_identity_and_disabled_images_never_pass(tmp_path):
    async def run():
        reader = await reader_with_html(
            tmp_path,
            '<div id="noteContainer" data-image-count="0"><div id="detail-desc">body</div></div>',
        )
        try:
            with pytest.raises(DomainError, match="note_identity_unverified"):
                await reader.collect(NOTE, tmp_path)
            await reader.page.locator("#noteContainer").evaluate(
                "e => e.dataset.noteId='aaaaaaaaaaaaaaaaaaaaaaaa'"
            )

            async def no_navigation(url):
                pass

            reader.navigate = no_navigation
            result = await reader.collect(NOTE, tmp_path, comment_limit=0, capture_images=False)
            assert (
                result["state"] == "partial" and result["completeness"]["capture_gate"] == "blocked"
            )
            await reader.page.locator("#detail-desc").evaluate(
                "e => {e.style.height='5px';e.style.overflow='hidden';}"
            )
            result = await reader.collect(NOTE, tmp_path, comment_limit=0)
            assert result["completeness"]["body"] == "unknown" and result["state"] == "partial"
        finally:
            await reader.close()

    asyncio.run(run())


def test_comment_failure_preserves_image_and_resume_uses_checkpoint(tmp_path, monkeypatch):
    async def run():
        import base64

        data = BytesIO()
        Image.new("RGB", (300, 400), "red").save(data, format="PNG")
        image_url = "data:image/png;base64," + base64.b64encode(data.getvalue()).decode()
        html = (
            '<div id="noteContainer" data-note-id="aaaaaaaaaaaaaaaaaaaaaaaa" data-image-count="1">'
            '<div id="detail-title">Synthetic</div><div id="detail-desc">full body</div>'
            f'<div class="note-slider-img"><img src="{image_url}"></div>'
            '<div class="note-scroller">comments</div></div>'
        )
        reader = await reader_with_html(tmp_path, html)
        downloads = []

        async def download(client, url):
            downloads.append(url)
            return data.getvalue()

        monkeypatch.setattr(browser, "download_image", download)
        original_action = reader.gate.action

        async def broken_action():
            raise browser.PlaywrightError("SECRET_SENTINEL")

        reader.gate.action = broken_action
        saved = []
        try:
            value = await reader.collect(
                NOTE,
                tmp_path,
                comment_limit=3,
                on_progress=lambda v, stage: saved.append((stage, json.loads(json.dumps(v)))),
            )
            assert value["state"] == "complete"
            assert value["completeness"]["images"] == "complete"
            assert value["comment_error"] == "comment_dom_failed"
            assert "SECRET_SENTINEL" not in json.dumps(value)
            assert saved[0][1]["errors"] == []
            assert len(downloads) == 1
            reader.gate.action = original_action

            async def forbidden_navigation(url):
                raise AssertionError("resume must reuse matching live page")

            reader.navigate = forbidden_navigation
            resumed = await reader.collect(
                NOTE, tmp_path, comment_limit=0, resume=value, resume_from="comments"
            )
            assert resumed["state"] == "complete" and len(downloads) == 1
            await reader.page.locator("#detail-desc").evaluate("e => e.textContent='changed'")
            with pytest.raises(DomainError, match="capture_snapshot_changed"):
                await reader.collect(
                    NOTE, tmp_path, comment_limit=0, resume=value, resume_from="comments"
                )
            assert (
                hashlib.sha256((tmp_path / "01.image").read_bytes()).hexdigest()
                == value["images"][0]["sha256"]
            )
        finally:
            await reader.close()

    asyncio.run(run())


def test_checkpoint_resume_and_revocation_via_service(db, grant, tmp_path):
    from test_personal_workspace import allowed

    from rednotebook.browser_service import BrowserService, JobRequest, capture_path

    async def run():
        db.register_grant(allowed(grant))
        html = (
            '<div id="noteContainer" data-note-id="aaaaaaaaaaaaaaaaaaaaaaaa" data-image-count="0">'
            '<div id="detail-title">synthetic</div><div id="detail-desc">body</div>'
            '<div class="comment-item" id="c1"><span class="content">first</span>'
            '<span class="like-wrapper"><span class="count">10</span></span><span class="reply-count">0</span></div>'
            '<div class="comment-item" id="c2"><span class="content">second</span>'
            '<span class="like-wrapper"><span class="count">2</span></span><span class="reply-count">3</span></div></div>'
        )
        reader = await reader_with_html(tmp_path, html)
        service = BrowserService(db, tmp_path / "profile", tmp_path / "config", reader)
        job = service.submit(
            JobRequest(
                operation="collect",
                source_id=grant.id,
                url=NOTE,
                keyword="synthetic",
                comment_limit=1,
            )
        )["job_id"]
        await service.tasks[job]
        result = service.get(job, True)
        assert result["state"] == "complete", result
        assert (
            result["result"]["comment_sampling"]["selection_basis"]
            == "visible_sample_replies_descending"
        )
        root = capture_path(db, job)
        evidence = json.loads((root / "evidence.json").read_text())
        assert evidence[1]["external_id"] == "c2"
        assert evidence[1]["metrics"][1]["value"] == 3
        assert service.import_capture(job, enriched=False)["accepted_rows"] == 2
        assert all("at" in event for event in result["progress"]["events"])

        async def forbidden_navigation(url):
            raise AssertionError("must reuse matching page")

        reader.navigate = forbidden_navigation
        resumed = service.resume_collection(job, "comments")["job_id"]
        await service.tasks[resumed]
        assert service.get(resumed)["state"] == "complete"
        assert "xsec_token" not in json.dumps(service.get(resumed, True))
        for i in range(120):
            service.progress(resumed, "test_phase")
        progress = service.get(resumed)["progress"]
        assert len(progress["events"]) == 100 and progress["events_truncated"]
        db.revoke(grant.id)
        assert not root.exists() and not capture_path(db, resumed).exists()
        await service.close()

    asyncio.run(run())


def test_v10_migration_preserves_payload_and_marks_new_contract(tmp_path, grant):
    from test_topic_research import content

    from rednotebook import campaign
    from rednotebook.storage import Database

    path = tmp_path / "old.sqlite"
    with Database(path) as db:
        draft = campaign.create(db, content())
        original = tuple(
            db.conn.execute("SELECT * FROM bundles WHERE id=?", (draft["id"],)).fetchone()
        )
        db.conn.execute("DELETE FROM settings WHERE key='capture_review_contract_version'")
        db.conn.execute("PRAGMA user_version=10")
        db.conn.commit()
    with Database(path) as db:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 13
        assert (
            tuple(db.conn.execute("SELECT * FROM bundles WHERE id=?", (draft["id"],)).fetchone())
            == original
        )
        assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()


def test_unknown_exception_has_safe_location_and_stage(db, grant, tmp_path):
    from test_personal_workspace import CaptureReader, allowed, submit_capture

    from rednotebook.browser_service import BrowserService

    async def run():
        db.register_grant(allowed(grant))
        service = BrowserService(
            db,
            tmp_path / "profile",
            tmp_path / "config",
            CaptureReader(TypeError("SECRET_SENTINEL")),
        )
        job = submit_capture(service, grant)
        await service.tasks[job]
        value = service.get(job, True)
        assert value["progress"]["failure"]["exception_type"] == "TypeError"
        assert value["progress"]["failure"]["locations"]
        assert value["progress"]["failed_stage"] == "text_saved"
        assert "SECRET_SENTINEL" not in json.dumps(value)
        await service.close()

    asyncio.run(run())


def test_image_resume_downloads_only_missing_page(tmp_path, monkeypatch):
    import base64

    async def run():
        payloads = {}
        for color in ("red", "green"):
            f = BytesIO()
            Image.new("RGB", (300, 400), color).save(f, format="PNG")
            payloads["data:image/png;base64," + base64.b64encode(f.getvalue()).decode()] = (
                f.getvalue()
            )
        a, b = payloads
        html = (
            '<div id="noteContainer" data-note-id="aaaaaaaaaaaaaaaaaaaaaaaa">'
            '<div id="detail-title">synthetic</div><div id="detail-desc">body</div>'
            f'<div class="note-slider-img"><img id="a" src="{a}"><img id="b" style="display:none" src="{b}"></div></div>'
            '<div class="pagination-teleport-container"><button class="pagination-item">1</button>'
            "<button class=\"pagination-item\" onclick=\"document.querySelector('#a').style.display='none';document.querySelector('#b').style.display='block'\">2</button></div>"
        )
        reader = await reader_with_html(tmp_path, html)
        downloads = []

        async def download(client, url):
            downloads.append(url)
            return payloads[url]

        monkeypatch.setattr(browser, "download_image", download)
        try:
            saved = await reader.collect(NOTE, tmp_path, comment_limit=0, max_images=1)
            assert saved["state"] == "partial" and saved["completeness"]["missing_positions"] == [2]

            async def no_navigation(url):
                raise AssertionError("should reuse live page")

            reader.navigate = no_navigation
            result = await reader.collect(
                NOTE, tmp_path, comment_limit=0, max_images=2, resume=saved, resume_from="images"
            )
            assert result["state"] == "complete"
            assert downloads == [a, b]
            assert [i["position"] for i in result["images"]] == [1, 2]
        finally:
            await reader.close()

    asyncio.run(run())


def test_image_only_body_and_title_fallback_are_explicit(grant, clock):
    from rednotebook.util import stamp

    at = stamp(clock[0])
    raw = {"text": "", "title": "title only", "has_images": True, "comments": []}
    note = browser.records_from_dom(raw, grant, "b", "synthetic", "r", NOTE, at, 0)[0]
    assert note["text_origin"] == "title_fallback"
    raw["title"] = ""
    note = browser.records_from_dom(raw, grant, "b", "synthetic", "r", NOTE, at, 0)[0]
    assert note["text"] == "" and note["text_origin"] == "empty_body"


def test_cli_zero_images_explicitly_disables_capture(db, grant, tmp_path, monkeypatch):
    from argparse import Namespace

    from test_personal_workspace import allowed

    from rednotebook import browser_cli
    from rednotebook.browser_service import BrowserService

    async def run():
        db.register_grant(allowed(grant))
        reader = await reader_with_html(
            tmp_path,
            '<div id="noteContainer" data-note-id="aaaaaaaaaaaaaaaaaaaaaaaa" data-image-count="0"><div id="detail-title">synthetic</div><div id="detail-desc">body</div></div>',
        )
        service = BrowserService(db, tmp_path / "profile", tmp_path / "config", reader)
        monkeypatch.setattr(browser_cli, "BrowserService", lambda *args: service)
        args = Namespace(
            action="collect",
            profile=tmp_path / "profile",
            config=tmp_path / "config",
            source=grant.id,
            url=NOTE,
            brief="synthetic",
            keyword="test",
            comments=0,
            images=0,
        )
        result = await browser_cli.run(args, db)
        assert result["result"]["image_capture"] == "not_requested"
        assert result["state"] == "partial"
        assert result["result"]["completeness"]["capture_gate"] == "blocked"

    asyncio.run(run())


def test_stale_image_after_page_click_is_not_counted(tmp_path, monkeypatch):
    import base64

    async def run():
        f = BytesIO()
        Image.new("RGB", (300, 400), "red").save(f, format="PNG")
        url = "data:image/png;base64," + base64.b64encode(f.getvalue()).decode()
        html = (
            '<div id="noteContainer" data-note-id="aaaaaaaaaaaaaaaaaaaaaaaa">'
            '<div id="detail-title">synthetic</div><div id="detail-desc">body</div>'
            f'<div class="note-slider-img"><img src="{url}"></div></div>'
            '<div class="pagination-teleport-container"><button class="pagination-item">1</button><button class="pagination-item">2</button></div>'
        )
        reader = await reader_with_html(tmp_path, html)
        original_wait = reader.page.wait_for_function

        async def short_wait(script, **kwargs):
            return await original_wait(script, **(kwargs | {"timeout": 100}))

        monkeypatch.setattr(reader.page, "wait_for_function", short_wait)

        async def download(client, src):
            return f.getvalue()

        monkeypatch.setattr(browser, "download_image", download)
        try:
            value = await reader.collect(NOTE, tmp_path, comment_limit=0)
            assert value["state"] == "partial"
            assert value["completeness"]["missing_positions"] == [2]
            assert value["errors"] == [{"position": 2, "code": "image_page_unverified"}]
            assert not (tmp_path / "02.image").exists()
        finally:
            await reader.close()

    asyncio.run(run())


def test_model_failure_reuses_complete_capture_without_partial_override(
    db, grant, tmp_path, monkeypatch
):
    from test_personal_workspace import CaptureReader, allowed, brief, wire_offline_model

    from rednotebook import browser_service
    from rednotebook.browser_service import BrowserService, JobRequest

    wire_offline_model(monkeypatch)
    original = browser_service.analyse
    attempts = []

    async def fail_once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise DomainError("synthetic_model_failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(browser_service, "analyse", fail_once)

    async def run():
        db.register_grant(allowed(grant))
        reader = CaptureReader()
        service = BrowserService(db, tmp_path / "profile", tmp_path / "config", reader)
        request = JobRequest(
            operation="workflow",
            source_id=grant.id,
            brief=brief(),
            brief_id=brief().id,
            url=NOTE,
            keyword="synthetic",
            analyse_images=False,
        )
        failed = service.submit(request)["job_id"]
        await service.tasks[failed]
        assert service.get(failed)["state"] == "failed"
        recovered = service.submit(request.model_copy(update={"capture_id": failed, "url": ""}))[
            "job_id"
        ]
        await service.tasks[recovered]
        result = service.get(recovered, True)
        assert result["state"] == "complete", result
        assert result["result"]["completeness"]["capture_gate"] == "passed"
        assert reader.calls == 1
        await service.close()

    asyncio.run(run())


@pytest.mark.parametrize("controls", ["hover", "arrow", "wrong_page"])
def test_synthetic_17_page_gallery_controls(tmp_path, monkeypatch, controls):
    import base64

    async def run():
        payloads = {}
        for i in range(17):
            f = BytesIO()
            Image.new("RGB", (300, 400), (i * 14, 20, 30)).save(f, format="PNG")
            payloads["data:image/png;base64," + base64.b64encode(f.getvalue()).decode()] = (
                f.getvalue()
            )
        urls = list(payloads)
        html = (
            "<style>.pagination-media-container {opacity:0;pointer-events:none}"
            "#noteContainer:hover .pagination-media-container {opacity:1;pointer-events:auto}</style>"
            '<div id="noteContainer" data-note-id="aaaaaaaaaaaaaaaaaaaaaaaa">'
            '<div id="detail-title">synthetic</div><div id="detail-desc">body</div>'
            f'<div class="note-slider-img"><img src="{urls[0]}"></div>'
            '<span class="fraction">1 / 17</span>'
            f"<script>const urls={json.dumps(urls)}; let page=1;"
            'function show(n) {page=n; document.querySelector("img").src=urls[n-1];'
            'document.querySelector(".fraction").textContent=n+" / 17";}</script>'
        )
        if controls in {"hover", "wrong_page"}:
            html += (
                '<div class="pagination-media-container">'
                + "".join(
                    f'<span class="pagination-item" onclick="show({n + 1 if controls == "wrong_page" else n})">{n}</span>'
                    for n in range(1, 18)
                )
                + "</div>"
            )
        else:
            # Decorative, non-interactive dots must fall back to the visible arrow.
            html += (
                '<div class="pagination-media-container" style="pointer-events:none">'
                + "".join('<span class="pagination-item">•</span>' for _ in range(17))
                + '</div><button class="arrow-controller right" onclick="show(page+1)">Next</button>'
            )
        reader = await reader_with_html(tmp_path, html + "</div>")
        downloads = []

        async def download(client, url):
            downloads.append(url)
            return payloads[url]

        monkeypatch.setattr(browser, "download_image", download)
        try:
            result = await reader.collect(NOTE, tmp_path, comment_limit=0)
            if controls == "wrong_page":
                assert result["state"] == "partial"
                assert result["errors"] == [{"position": 2, "code": "image_page_unverified"}]
                assert downloads == urls[:1]
                assert result["completeness"]["capture_gate"] == "blocked"
                return
            assert result["state"] == "complete"
            assert result["completeness"]["missing_positions"] == []
            assert downloads == urls
            assert [i["position"] for i in result["images"]] == list(range(1, 18))
        finally:
            await reader.close()

    asyncio.run(run())


def test_recovery_defaults_to_completing_capture():
    from rednotebook.browser_service import BrowserService

    assert (
        BrowserService.capture_recovery_action(
            {"completeness": {"missing_positions": list(range(2, 18)), "capture_gate": "blocked"}}
        )
        == "resume_collect_images"
    )
    assert (
        BrowserService.capture_recovery_action({"completeness": {"capture_gate": "passed"}})
        == "continue_workflow_with_capture_id"
    )


def test_vue_ref_identity_and_body_are_checked(tmp_path):
    async def run():
        reader = await reader_with_html(
            tmp_path,
            """
        <div id="noteContainer" data-note-id="aaaaaaaaaaaaaaaaaaaaaaaa" data-image-count="0">
        <div id="detail-desc">visible body</div></div>
        <script>window.__INITIAL_STATE__={note:{currentNoteId:{__v_isRef:true,_value:'aaaaaaaaaaaaaaaaaaaaaaaa'},
        noteDetailMap:{aaaaaaaaaaaaaaaaaaaaaaaa:{note:{desc:'different body'}}}}};</script>""",
        )
        try:
            with pytest.raises(DomainError, match="note_content_mismatch"):
                await reader.collect(NOTE, tmp_path, comment_limit=0)
        finally:
            await reader.close()

    asyncio.run(run())


def test_loop_clones_do_not_invalidate_image_snapshot(tmp_path, monkeypatch):
    import base64

    async def run():
        payloads = {}
        for color in ("red", "green"):
            f = BytesIO()
            Image.new("RGB", (300, 400), color).save(f, format="PNG")
            payloads["data:image/png;base64," + base64.b64encode(f.getvalue()).decode()] = (
                f.getvalue()
            )
        a, b = payloads
        html = (
            '<div id="noteContainer" data-note-id="aaaaaaaaaaaaaaaaaaaaaaaa">'
            '<div id="detail-title">synthetic loop</div><div id="detail-desc">body</div>'
            '<div class="note-slider">'
            f'<div class="swiper-slide swiper-slide-duplicate" style="display:none"><div class="note-slider-img"><img src="{b}"></div></div>'
            f'<div class="swiper-slide"><div class="note-slider-img"><img id="a" src="{a}"></div></div>'
            f'<div class="swiper-slide"><div class="note-slider-img"><img id="b" style="display:none" src="{b}"></div></div>'
            f'<div class="swiper-slide swiper-slide-duplicate" style="display:none"><div class="note-slider-img"><img src="{a}"></div></div>'
            '</div><span class="fraction">1/2</span>'
            "<button class=\"arrow-controller right\" onclick=\"document.querySelector('#a').style.display='none';document.querySelector('#b').style.display='block';document.querySelector('.fraction').textContent='2/2'\">next</button></div>"
        )
        reader = await reader_with_html(tmp_path, html)
        downloads = []

        async def download(client, url):
            downloads.append(url)
            return payloads[url]

        monkeypatch.setattr(browser, "download_image", download)
        try:
            saved = await reader.collect(NOTE, tmp_path, comment_limit=0, max_images=1)
            assert saved["image_set_signature"]
            assert saved["declared_total"] == 2
            result = await reader.collect(
                NOTE, tmp_path, comment_limit=0, resume=saved, resume_from="images"
            )
            assert result["state"] == "complete"
            assert downloads == [a, b]
        finally:
            await reader.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "metadata,expected", [("body #AI[话题]#", True), ("changed #AI[话题]#", False)]
)
def test_topic_markup_normalization_preserves_substantive_body(tmp_path, metadata, expected):
    async def run():
        html = '<div id="noteContainer"><div id="detail-desc">body #AI</div></div>'
        reader = await reader_with_html(tmp_path, html)
        try:
            await reader.page.set_content(html)
            await reader.page.evaluate(
                "desc => {window.__INITIAL_STATE__={note:{currentNoteId:{value:'id'},noteDetailMap:{id:{note:{desc}}}}}}",
                metadata,
            )
            check = await reader.page.evaluate(browser.BODY_CHECK_DOM)
            assert check["metadata_matches"] is expected
            assert check["text"] == "body #AI"
        finally:
            await reader.close()

    asyncio.run(run())


def test_failed_model_recovery_does_not_suggest_browser_recollection(db, tmp_path):
    from rednotebook.browser_service import BrowserService

    service = BrowserService(db, tmp_path / "profile", tmp_path / "config")
    row = {
        "request_json": "{}",
        "result_json": None,
        "state": "failed",
        "error_code": "vision_connection_failed",
    }
    assert service.recovery(row)["next_action"] == "restore_local_model_then_resume_workflow"
    service.gate.pause("captcha_required")
    assert service.recovery(row)["next_action"] == "resolve_pause_then_explicit_operator_resume"


def test_first_image_wait_does_not_depend_on_animation_frames(tmp_path, monkeypatch):
    import base64

    async def run():
        f = BytesIO()
        Image.new("RGB", (300, 400), "red").save(f, format="PNG")
        url = "data:image/png;base64," + base64.b64encode(f.getvalue()).decode()
        reader = await reader_with_html(
            tmp_path,
            (
                '<div id="noteContainer" data-note-id="aaaaaaaaaaaaaaaaaaaaaaaa" data-image-count="1">'
                '<div id="detail-desc">synthetic</div>'
                f'<div class="note-slider-img"><img src="{url}"></div></div>'
                "<script>window.requestAnimationFrame=()=>0;</script>"
            ),
        )

        async def download(client, src):
            assert src == url
            return f.getvalue()

        monkeypatch.setattr(browser, "download_image", download)
        try:
            result = await asyncio.wait_for(reader.collect(NOTE, tmp_path, comment_limit=0), 3)
            assert result["state"] == "complete"
        finally:
            await reader.close()

    asyncio.run(run())
