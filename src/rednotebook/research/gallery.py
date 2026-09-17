"""Ordered whole-note image analysis, resumable per source/hash/model/prompt."""

import hashlib
import json

from pydantic import Field, model_validator

from rednotebook.domain.models import Contract, Identifier, Text
from rednotebook.errors import DomainError
from rednotebook.research.vision import PROMPT, analyse_media
from rednotebook.util import canonical, digest


class GalleryImage(Contract):
    position: int = Field(ge=1, le=100, strict=True)
    path: Text
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Gallery(Contract):
    source_id: Identifier
    note_external_id: Identifier
    declared_total: int = Field(ge=1, le=100, strict=True)
    images: list[GalleryImage] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def positions(self):
        positions = [i.position for i in self.images]
        if len(positions) != len(set(positions)) or max(positions) > self.declared_total:
            raise ValueError("invalid_image_positions")
        return self


async def analyse_gallery(
    db, manifest, base, config, max_images=20, analyser=analyse_media, progress=None
):
    db.require_source(manifest.source_id)
    if not 1 <= max_images <= 20:
        raise DomainError("gallery_budget_invalid")
    results, errors = [], []
    requests = 0
    by_hash = {}
    for item in sorted(manifest.images, key=lambda x: x.position)[:max_images]:
        db.require_source(manifest.source_id)
        if progress:
            progress(
                current_position=item.position,
                completed_images=len(results),
                declared_total=manifest.declared_total,
            )
        file = (base / item.path).resolve()
        if not file.is_relative_to(base.resolve()) or not file.is_file():
            errors.append(
                {"position": item.position, "code": "gallery_file_missing_or_outside_root"}
            )
            continue
        if file.stat().st_size > 20 * 1024 * 1024:
            errors.append({"position": item.position, "code": "gallery_image_too_large"})
            continue
        if hashlib.sha256(file.read_bytes()).hexdigest() != item.sha256:
            errors.append({"position": item.position, "code": "gallery_hash_mismatch"})
            continue
        key = digest(
            [
                item.sha256,
                config.public_metadata(),
                digest(config.base_url),
                PROMPT,
                "resize1280-jpeg90-v1",
            ]
        )
        cached = db.conn.execute(
            "SELECT payload FROM media_cache WHERE source_id=? AND cache_key=?",
            (manifest.source_id, key),
        ).fetchone()
        reused = bool(cached)
        duplicate_of = by_hash.get(item.sha256)
        if cached:
            result = json.loads(cached[0])
        else:
            try:
                result = await analyser(db, manifest.source_id, file, "image", config)
            except DomainError as exc:
                errors.append({"position": item.position, "code": exc.code})
                continue
            requests += result["requests"]
            if result["state"] == "complete":
                db.require_source(manifest.source_id)
                with db.conn:
                    db.conn.execute(
                        "INSERT OR REPLACE INTO media_cache VALUES (?,?,?)",
                        (manifest.source_id, key, canonical(result)),
                    )
        by_hash.setdefault(item.sha256, item.position)
        results.append(
            {
                "position": item.position,
                "sha256": item.sha256,
                "duplicate_of": duplicate_of,
                "cached": reused,
                "analysis": result,
            }
        )
        if result["state"] != "complete":
            errors.append({"position": item.position, "code": "image_analysis_incomplete"})
    complete = {r["position"] for r in results if r["analysis"]["state"] == "complete"}
    missing = sorted(set(range(1, manifest.declared_total + 1)) - complete)
    db.require_source(manifest.source_id)
    return {
        "note_external_id": manifest.note_external_id,
        "source_id": manifest.source_id,
        "state": "partial" if missing or errors else "complete",
        "declared_total": manifest.declared_total,
        "processed": len(complete),
        "missing_positions": missing,
        "images": results,
        "errors": errors,
        "requests": requests,
        "human_verified": False,
        "summary_method": "ordered_page_evidence_for_research_agent",
        "media_text": "\n\n".join(
            f"[第{r['position']}/{manifest.declared_total}张；机器识别待核验]\n"
            + r["analysis"]["media_text"]
            for r in results
        ),
    }
