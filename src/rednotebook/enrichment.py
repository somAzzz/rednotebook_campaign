"""Join a gallery result to its matching note before first evidence import."""

from rednotebook.domain.models import EvidenceInput
from rednotebook.errors import DomainError


def _enrich(records, gallery):
    if gallery.get("state") not in {"complete", "partial"} or not gallery.get("images"):
        raise DomainError("gallery_has_no_usable_images")
    result = []
    matched = 0
    for raw in records:
        record = EvidenceInput.model_validate(raw)
        if record.kind == "note" and record.external_id == gallery.get("note_external_id"):
            if record.source_id != gallery.get("source_id"):
                raise DomainError("gallery_source_mismatch")
            matched += 1
            payload = record.model_dump(mode="json")
            payload["media_text"] = (
                f"[图片覆盖：{gallery['processed']}/{gallery['declared_total']}；"
                f"缺失页码：{gallery['missing_positions']}]\n" + gallery["media_text"]
            )
            payload["media_provenance"] = [
                {
                    "sha256": image["sha256"],
                    "kind": "image",
                    "processor": image["analysis"]["model"],
                    "coverage": "single_image",
                    "human_verified": False,
                }
                for image in gallery["images"]
            ]
            result.append(EvidenceInput.model_validate(payload).model_dump(mode="json"))
        else:
            result.append(record.model_dump(mode="json"))
    if matched != 1:
        raise DomainError("gallery_requires_one_matching_note")
    return result


def enrich(records, gallery):
    try:
        return _enrich(records, gallery)
    except (KeyError, TypeError, AttributeError, IndexError):
        raise DomainError("gallery_result_invalid") from None
