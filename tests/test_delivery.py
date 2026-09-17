from pathlib import Path

import httpx
import pytest
from PIL import Image

from rednotebook import creative
from rednotebook.adapters.readonly import ReadOnlyAdapter
from rednotebook.domain.models import ResearchBrief
from rednotebook.errors import DomainError
from rednotebook.research.store import checkpoint, start_run


def bundle(db, grant):
    grant = grant.model_copy(
        update={"permissions": grant.permissions.model_copy(update={"excerpt_export": "allowed"})}
    )
    db.register_grant(grant)
    brief = ResearchBrief(
        id="editorial-test",
        synthetic=True,
        product="研究",
        audience="研究者",
        research_question="样本表达什么？",
        objective="验证",
        keywords=["表达"],
    )
    run_id = start_run(db, brief, [grant.id], {})
    checkpoint(
        db,
        run_id,
        {
            "run_id": run_id,
            "state": "complete",
            "findings": [{"claim": "样本包含问题", "support": []}],
        },
    )
    return creative.propose(db, run_id)


def test_revision_does_not_inherit_approval(db, grant):
    b = bundle(db, grant)
    with pytest.raises(DomainError, match="exact_version_approval_required"):
        creative.export(db, b["id"], 1)
    with pytest.raises(DomainError, match="review_hash"):
        creative.review(db, b["id"], 1, "wrong", "reviewer")
    creative.review(db, b["id"], 1, b["content_hash"], "reviewer")
    e = creative.Editorial.model_validate(b["payload"]["editorial"])
    e.body = "修改后的正文"
    changed = creative.revise(db, b["id"], 1, e)
    assert changed["version"] == 2 and changed["state"] == "draft"
    with pytest.raises(DomainError, match="exact_version_approval_required"):
        creative.export(db, b["id"], 2)


def test_export_dimensions_idempotence_tamper_and_revocation(db, grant):
    b = bundle(db, grant)
    result = creative.export(db, b["id"], 1, preview=True)
    path = Path(result["path"])
    assert len(list(path.glob("*.png"))) == 6
    for file in path.glob("*.png"):
        with Image.open(file) as im:
            assert im.size == (1080, 1620)
    assert creative.export(db, b["id"], 1, True)["reused"]
    (path / "01.png").write_bytes(b"tampered")
    with pytest.raises(DomainError, match="export_tampered"):
        creative.export(db, b["id"], 1, True)
    db.revoke(grant.id)
    assert not path.exists()
    with pytest.raises(DomainError, match="bundle_revoked"):
        creative.load_bundle(db, b["id"], 1)


def test_asset_copy_survives_original_changes(db, grant, tmp_path):
    b = bundle(db, grant)
    file = tmp_path / "own.png"
    Image.new("RGB", (30, 30), "red").save(file)
    added = creative.add_asset(db, b["id"], 1, file, "test author", "test original")
    asset_id = next(iter(added["payload"]["assets"]))
    file.write_bytes(b"changed")
    e = creative.Editorial.model_validate(added["payload"]["editorial"])
    e.pages[0].asset_id = asset_id
    revised = creative.revise(db, b["id"], 2, e)
    result = creative.export(db, b["id"], revised["version"], True)
    assert (Path(result["path"]) / f"asset-{asset_id}.bin").read_bytes().startswith(b"\x89PNG")


@pytest.mark.parametrize(
    "status,code", [(401, "adapter_auth_required"), (403, "adapter_access_blocked")]
)
def test_adapter_pauses_on_auth(db, grant, status, code):
    grant = grant.model_copy(
        update={"permissions": grant.permissions.model_copy(update={"automated_access": "allowed"})}
    )
    db.register_grant(grant)
    adapter = ReadOnlyAdapter(
        db,
        grant.id,
        "https://www.xiaohongshu.com",
        transport=httpx.MockTransport(lambda r: httpx.Response(status)),
    )
    with pytest.raises(DomainError, match=code):
        adapter.get_note("/explore/" + "a" * 24)
    with pytest.raises(DomainError, match="adapter_paused"):
        adapter.get_note("/explore/" + "b" * 24)
    adapter.close()


def test_adapter_rate_limit_and_write_path(db, grant):
    grant = grant.model_copy(
        update={"permissions": grant.permissions.model_copy(update={"automated_access": "allowed"})}
    )
    db.register_grant(grant)
    sleeps = []
    adapter = ReadOnlyAdapter(
        db,
        grant.id,
        "https://www.xiaohongshu.com",
        transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "2"})),
        sleep=sleeps.append,
    )
    with pytest.raises(DomainError, match="adapter_read_path_invalid"):
        adapter.get_note("/publish")
    with pytest.raises(DomainError, match="adapter_rate_limited"):
        adapter.get_note("/explore/" + "a" * 24)
    assert adapter.requests == 3 and sleeps == [2, 2]
    adapter.close()


def test_outcome_preserves_late_and_public(db, grant):
    from datetime import timedelta

    from rednotebook.outcomes import Outcome, import_outcome, retrospective

    b = bundle(db, grant)
    at = db.clock()
    o = Outcome(
        bundle_id=b["id"],
        bundle_version=1,
        source_id=grant.id,
        note_url="https://example.test/note",
        ownership="public",
        published_at=at - timedelta(hours=30),
        observed_at=at,
        window="24h",
        metrics=[
            {
                "name": "likes",
                "value": None,
                "raw_display": None,
                "source_kind": "public",
                "precision": "exact",
                "definition": "public likes",
                "observed_at": at,
                "missing_reason": "not_visible",
            }
        ],
    )
    assert not import_outcome(db, o)["duplicate"]
    assert import_outcome(db, o)["duplicate"]
    result = retrospective(db, b["id"], 1)
    assert not result["observations"][0]["within_window"]
    assert not result["observations"][0]["eligible_for_owner_comparison"]
    assert result["observations"][0]["metrics"][0]["value"] is None
    assert "24h" in result["missing_windows"]


def test_formal_export_and_reverting_text_requires_fresh_review(db, grant):
    b = bundle(db, grant)
    creative.review(db, b["id"], 1, b["content_hash"], "fixture reviewer")
    exported = creative.export(db, b["id"], 1)
    assert exported["manifest"]["preview"] is False
    original = creative.Editorial.model_validate(b["payload"]["editorial"])
    changed = original.model_copy(update={"body": "新正文"})
    second = creative.revise(db, b["id"], 1, changed)
    third = creative.revise(db, b["id"], second["version"], original)
    assert third["version"] == 3 and third["state"] == "draft"


def test_manifest_tamper_cannot_hide_file_tamper(db, grant):
    b = bundle(db, grant)
    result = creative.export(db, b["id"], 1, True)
    path = Path(result["path"])
    (path / "manifest.json").write_text('{"files":{}}')
    with pytest.raises(DomainError, match="export_tampered"):
        creative.export(db, b["id"], 1, True)


@pytest.mark.parametrize(
    "html,code",
    [
        ("captcha", "adapter_verification_required"),
        ("<html>changed</html>", "adapter_schema_changed_or_login_required"),
    ],
)
def test_adapter_captcha_and_schema(db, grant, html, code):
    grant = grant.model_copy(
        update={"permissions": grant.permissions.model_copy(update={"automated_access": "allowed"})}
    )
    db.register_grant(grant)
    adapter = ReadOnlyAdapter(
        db,
        grant.id,
        "https://www.xiaohongshu.com",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)),
    )
    with pytest.raises(DomainError, match=code):
        adapter.get_note("/explore/" + "a" * 24)
    adapter.close()
