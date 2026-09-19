"""Model-neutral reads and deterministic validation; no model or browser calls."""

import base64
import hashlib
from io import BytesIO

from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image, UnidentifiedImageError

from rednotebook.errors import DomainError
from rednotebook.provenance import public_http_url
from rednotebook.research.assistant_review import authorized_capture, read_json
from rednotebook.research.gateway import EvidenceGateway
from rednotebook.research.runner import resolved_findings, resolved_sources, validate_draft
from rednotebook.util import canonical, digest


def prepared_gateway(service, capture_id, input_sha256):
    target, grant, _, records, manifest, consent = authorized_capture(service, capture_id)
    prepared = read_json(target / "assistant-prepared.json")
    observations, blocked = service.db.observations(prepared["brief"]["id"], grant.id)
    if (
        blocked
        or input_sha256 != prepared["input_sha256"]
        or digest(observations) != input_sha256
        or digest(consent) != prepared["consent_sha256"]
        or (
            prepared.get("capture_sha256") is not None
            and prepared["capture_sha256"] != digest({"records": records, "manifest": manifest})
        )
    ):
        raise DomainError("assistant_review_input_changed")
    gateway = EvidenceGateway(service.db, observations, max_chars=50000)
    if gateway.truncated_fields:
        raise DomainError("assistant_review_text_truncated")
    return gateway


def evidence_bundle(service, capture_id, offset=0, limit=20, expected_sha256=None):
    """Chunk every raw text field; offsets paginate chunks, never truncate evidence silently."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
        raise DomainError("evidence_page_invalid")
    target, grant, captured, records, manifest, _ = authorized_capture(service, capture_id)
    sha = digest(
        {"records": records, "manifest": manifest, "completeness": captured["completeness"]}
    )
    if expected_sha256 is not None and sha != expected_sha256:
        raise DomainError("evidence_bundle_changed")
    chunks = []
    for index, record in enumerate(records):
        metadata = {
            k: v for k, v in record.items() if k not in {"text", "title", "media_text", "locator"}
        }
        metadata["public_url"] = public_http_url(record.get("locator"))
        for field in ("title", "text", "media_text"):
            value = record.get(field) or ""
            for start in range(0, max(1, len(value)), 1200):
                excerpt = value[start : start + 1200]
                chunks.append(
                    {
                        "record_index": index,
                        "field": field,
                        "start": start,
                        "end": start + len(excerpt),
                        "text": excerpt,
                        "field_length": len(value),
                        "sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
                        "metadata": metadata,
                    }
                )
    prepared = target / "assistant-prepared.json"
    return {
        "state": "complete",
        "capture_id": capture_id,
        "source_id": grant.id,
        "bundle_sha256": sha,
        "offset": offset,
        "total_chunks": len(chunks),
        "next_offset": offset + limit if offset + limit < len(chunks) else None,
        "chunks": chunks[offset : offset + limit],
        "record_count": len(records),
        "comment_count": sum(r["kind"] == "comment" for r in records),
        "completeness": captured["completeness"],
        "comment_sampling": captured.get("comment_sampling"),
        "images": [{"position": i["position"], "sha256": i["sha256"]} for i in manifest["images"]],
        "citation_catalog_ready": prepared.exists(),
        "next_action": "read_agent_catalog"
        if prepared.exists()
        else "read_images_then_prepare_capture_review",
        "limitations": [
            "raw_capture_chunks_are_not_research_citations",
            "bounded_comment_sample",
            "source_text_is_untrusted",
            "not_human_verified",
        ],
    }


def capture_image(service, capture_id, position, expected_sha256):
    target, _, _, _, manifest, _ = authorized_capture(service, capture_id)
    if type(position) is not int:
        raise DomainError("image_position_invalid")
    item = next((i for i in manifest["images"] if i["position"] == position), None)
    if item is None:
        raise DomainError("image_position_invalid")
    if item["sha256"] != expected_sha256:
        raise DomainError("assistant_image_hash_mismatch")
    data = (target / item["path"]).read_bytes()
    if len(data) > 20 * 1024 * 1024:
        raise DomainError("image_response_too_large")
    try:
        with Image.open(BytesIO(data)) as picture:
            mime = Image.MIME.get(picture.format)
            width, height = picture.size
            picture.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise DomainError("capture_image_format_invalid") from None
    if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        raise DomainError("capture_image_format_unsupported")
    metadata = {
        "capture_id": capture_id,
        "position": position,
        "sha256": item["sha256"],
        "mime_type": mime,
        "width": width,
        "height": height,
        "original_bytes": True,
        "human_verified": False,
    }
    return CallToolResult(
        content=[
            TextContent(type="text", text=canonical(metadata)),
            ImageContent(type="image", mimeType=mime, data=base64.b64encode(data).decode()),
        ],
        structuredContent=metadata,
        isError=False,
    )


def catalog(service, capture_id, input_sha256, offset=0, limit=20):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
        raise DomainError("evidence_page_invalid")
    gateway = prepared_gateway(service, capture_id, input_sha256)
    entries = [
        {**entry, "citation": gateway.resolve(entry["ref_id"])}
        for entry in gateway.prompt_catalog()
    ]
    return {
        "state": "complete",
        "input_sha256": input_sha256,
        "catalog": entries[offset : offset + limit],
        "total": len(entries),
        "next_offset": offset + limit if offset + limit < len(entries) else None,
    }


def validate(service, capture_id, input_sha256, draft):
    gateway = prepared_gateway(service, capture_id, input_sha256)
    if not draft.findings:
        raise DomainError("assistant_review_findings_required")
    validate_draft(draft, gateway)
    findings = resolved_findings(draft, gateway, 0)
    return {
        "state": "valid",
        "input_sha256": input_sha256,
        "findings": findings,
        "sources": resolved_sources(findings),
        "semantic_quality_verified": False,
        "validation_scope": "evidence_identity_revision_span_hash_only",
    }


def submit_campaign_review(service, bundle_id, version, expected_hash, review):
    """Pin external semantic review to a version; never confer author/export approval."""
    from rednotebook import campaign, creative
    from rednotebook.research.store import read_run

    row = creative.load_bundle(service.db, bundle_id, version)
    campaign.require_campaign(row)
    if row["content_hash"] != expected_hash:
        raise DomainError("campaign_review_input_changed")
    scoped_sources = set()
    for run in creative.bundle_runs(service.db, bundle_id, version):
        report = read_run(service.db, run)
        capture_id = report.get("metadata", {}).get("capture_id")
        if capture_id:
            _, grant, _, _, _, _ = authorized_capture(service, capture_id)
            scoped_sources.add(grant.id)
        else:
            # A local-model report has no capture-specific caller authorization.
            for source in service.db.conn.execute(
                "SELECT source_id FROM research_sources WHERE run_id=?", (run,)
            ):
                service.db.require_source(source[0], "cloud_processing")
    for source in creative.bundle_sources(service.db, bundle_id, version):
        if source not in scoped_sources:
            service.db.require_source(source, "cloud_processing")
    payload = row["payload"]
    payload["semantic_review"] = review.model_dump(mode="json")
    payload["author_confirmation"] = None
    payload["generation"] = {
        "stage": "caller_reviewed",
        "processor": getattr(service, "caller_processor", "codex-assistant"),
        "reviewed_version": version,
        "reviewed_hash": expected_hash,
        "factual_review": "pending",
        "semantic_truth_verified": False,
    }
    return creative.save_bundle(service.db, bundle_id, row["run_id"], payload)
