import asyncio
import hashlib

import pytest
from PIL import Image
from pydantic import ValidationError

from rednotebook.domain.models import EvidenceInput
from rednotebook.research.config import LocalModelConfig
from rednotebook.research.gallery import Gallery, analyse_gallery


def config():
    return LocalModelConfig(base_url="http://127.0.0.1:1234/v1", model="test", api_key="dummy")


def test_gallery_keeps_order_and_duplicate_positions_but_reuses_image(db, grant, tmp_path):
    db.register_grant(grant)
    file = tmp_path / "one.png"
    Image.new("RGB", (10, 10), "red").save(file)
    hashed = hashlib.sha256(file.read_bytes()).hexdigest()
    gallery = Gallery(
        source_id=grant.id,
        note_external_id="note1",
        declared_total=3,
        images=[{"position": p, "path": file.name, "sha256": hashed} for p in [3, 1, 2]],
    )
    calls = []

    async def fake(*args):
        calls.append(1)
        return {"requests": 1, "state": "complete", "media_text": "红色正方形"}

    result = asyncio.run(analyse_gallery(db, gallery, tmp_path, config(), analyser=fake))
    assert result["state"] == "complete"
    assert [r["position"] for r in result["images"]] == [1, 2, 3]
    assert [r["duplicate_of"] for r in result["images"]] == [None, 1, 1]
    assert len(calls) == 1 and result["requests"] == 1
    again = asyncio.run(analyse_gallery(db, gallery, tmp_path, config(), analyser=fake))
    assert again["requests"] == 0
    db.revoke(grant.id)
    assert db.conn.execute("SELECT COUNT(*) FROM media_cache").fetchone()[0] == 0


def test_gallery_missing_and_budget_never_report_complete(db, grant, tmp_path):
    db.register_grant(grant)
    gallery = Gallery(
        source_id=grant.id,
        note_external_id="note1",
        declared_total=8,
        images=[{"position": 1, "path": "missing.png", "sha256": "a" * 64}],
    )
    result = asyncio.run(analyse_gallery(db, gallery, tmp_path, config()))
    assert result["state"] == "partial" and result["processed"] == 0
    assert result["missing_positions"] == list(range(1, 9))
    assert result["requests"] == 0


def test_gallery_rejects_duplicate_position():
    with pytest.raises(ValidationError):
        Gallery(
            source_id="s",
            note_external_id="n",
            declared_total=2,
            images=[{"position": 1, "path": "a", "sha256": "a" * 64}] * 2,
        )


def test_media_text_cannot_lose_provenance(fixture_data):
    raw = fixture_data[1][0] | {"media_text": "机器描述"}
    with pytest.raises(ValidationError):
        EvidenceInput.model_validate(raw)


def test_nested_editorial_json_is_validated_strictly():
    import json

    from rednotebook.research.proposals import ProposalDraft

    editorial = {
        "title": "测试",
        "body": "研究草稿",
        "pages": [{"title": "标题", "body": "文字"} for _ in range(6)],
    }
    draft = {
        "directions": [{"angle": "问题", "validation_target": "扩大样本", "finding_ids": ["F1"]}],
        "editorial": json.dumps(editorial),
        "limitations": ["小样本"],
    }
    assert len(ProposalDraft.model_validate(draft).editorial.pages) == 6
    editorial["unexpected"] = "reject"
    draft["editorial"] = json.dumps(editorial)
    with pytest.raises(ValidationError):
        ProposalDraft.model_validate(draft)


def test_text_note_contract_and_gallery_source_boundary(fixture_data):
    from rednotebook.enrichment import enrich
    from rednotebook.errors import DomainError

    raw = fixture_data[1][0] | {"format": "text"}
    note = EvidenceInput.model_validate(raw)
    assert note.format == "text"
    gallery = {
        "state": "complete",
        "note_external_id": note.external_id,
        "source_id": note.source_id,
        "processed": 1,
        "declared_total": 1,
        "missing_positions": [],
        "media_text": "图中文字",
        "images": [{"sha256": "a" * 64, "analysis": {"model": "test"}}],
    }
    enriched = enrich([raw], gallery)
    assert "图中文字" in enriched[0]["media_text"]
    assert enriched[0]["media_provenance"][0]["human_verified"] is False
    assert "media_text" not in raw or not raw["media_text"]
    with pytest.raises(DomainError, match="gallery_source_mismatch"):
        enrich([raw], gallery | {"source_id": "different-source"})
    with pytest.raises(DomainError, match="gallery_requires_one_matching_note"):
        enrich([raw, raw], gallery)


@pytest.mark.parametrize("failure", ["connection", "timeout", "provider"])
def test_provider_failure_stops_gallery_and_progress_counts_success(db, grant, tmp_path, failure):
    import httpx

    from rednotebook.research.vision import analyse_media

    db.register_grant(grant)
    file = tmp_path / "synthetic.png"
    Image.new("RGB", (20, 20), "blue").save(file)
    hashed = hashlib.sha256(file.read_bytes()).hexdigest()
    gallery = Gallery(
        source_id=grant.id,
        note_external_id="synthetic",
        declared_total=17,
        images=[{"position": p, "path": file.name, "sha256": hashed} for p in range(1, 18)],
    )
    calls = []

    def transport(request):
        calls.append(1)
        if failure == "connection":
            raise httpx.ConnectError("SECRET_SENTINEL", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("SECRET_SENTINEL", request=request)
        return httpx.Response(503, json={"error": "SECRET_SENTINEL"})

    async def analyser(db, source, path, kind, config):
        return await analyse_media(
            db, source, path, kind, config, transport=httpx.MockTransport(transport)
        )

    progress = []
    result = asyncio.run(
        analyse_gallery(
            db,
            gallery,
            tmp_path,
            config(),
            analyser=analyser,
            progress=lambda **value: progress.append(value),
        )
    )
    assert len(calls) == result["requests"] == 1
    assert result["processed"] == progress[-1]["completed_images"] == 0
    assert result["missing_positions"] == list(range(1, 18))
    assert result["state"] == "partial"
    assert (
        result["errors"][0]["code"]
        == {
            "connection": "vision_connection_failed",
            "timeout": "vision_request_timeout",
            "provider": "vision_provider_rejected",
        }[failure]
    )
    assert "SECRET_SENTINEL" not in str(result)
