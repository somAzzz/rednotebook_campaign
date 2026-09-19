"""Deterministic capture checks and bounded comment selection, never popularity claims."""

import hashlib
import re


def displayed_count(value):
    value = (value or "").strip()
    if re.fullmatch(r"\d+", value):
        return float(value)
    if re.fullmatch(r"\d+(?:\.\d+)?万", value):
        return float(value[:-1]) * 10000
    return None


def select_comments(comments, limit):
    unique = {c["id"]: c for c in comments if c.get("id") and c.get("text")}
    values = list(unique.values())
    # Rank only when every candidate has a comparable displayed metric; missing is not zero.
    rank_field = next(
        (
            f
            for f in ("replies", "likes")
            if values and all(displayed_count(c.get(f)) is not None for c in values)
        ),
        None,
    )
    if rank_field:
        values.sort(key=lambda c: displayed_count(c[rank_field]), reverse=True)
    return values[:limit], {
        "observed_unique": len(values),
        "selected": min(limit, len(values)),
        "selection_basis": f"visible_sample_{rank_field}_descending"
        if rank_field
        else "visible_order_metrics_incomplete",
        "truncated": True,
        "platform_ranking_verified": False,
        "selected_metadata": [
            {k: c.get(k) for k in ("id", "parent_id", "pinned", "likes", "replies")}
            for c in values[:limit]
        ],
    }


def completeness(collected):
    raw = collected["raw"]
    total = collected.get("declared_total")
    positions = {i["position"] for i in collected.get("images", [])}
    images_complete = (
        total is not None
        and positions == set(range(1, total + 1))
        and len(collected.get("images", [])) == total
        and not collected.get("errors")
    )
    body = raw.get("body_check", {})
    body_complete = body.get("state") == "complete"
    identity = raw.get("identity", {}).get("state", "unknown")
    return {
        "identity": identity,
        "body": body.get("state", "unknown"),
        "body_empty": not bool(raw.get("text", "").strip()),
        "body_length": len(raw.get("text", "")),
        "body_sha256": hashlib.sha256(raw.get("text", "").encode()).hexdigest(),
        "images": "complete" if images_complete else "unknown" if total is None else "partial",
        "declared_total": total,
        "captured_count": len(positions),
        "missing_positions": sorted(set(range(1, total + 1)) - positions)
        if total is not None
        else None,
        "comments": collected.get("comment_status", "not_requested"),
        "capture_gate": "passed"
        if identity == "verified" and body_complete and images_complete
        else "blocked",
        "image_reading": "not_applicable" if total == 0 else "not_run",
        "human_accuracy_review": "not_run",
    }
