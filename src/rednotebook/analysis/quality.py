from collections import defaultdict


def quality_report(observations, blocked):
    latest = {}
    groups = defaultdict(set)
    note_runs = {}
    comment_runs = defaultdict(dict)
    for obs in observations:
        if obs["id"] not in latest or obs["observed_at"] > latest[obs["id"]]["observed_at"]:
            latest[obs["id"]] = obs
        for sample in obs["samples"]:
            s = sample["sampling"]
            groups[s["group"]].add(obs["id"])
            key = (
                obs["source_id"],
                s["run_id"],
                obs["id"] if obs["kind"] == "note" else obs["parent_id"],
            )
            if obs["kind"] == "note":
                old = note_runs.get(key)
                if old is None or obs["observed_at"] > old[0]["observed_at"]:
                    note_runs[key] = (obs, sample)
            else:
                old = comment_runs[key].get(obs["id"])
                if old is None or obs["observed_at"] > old["observed_at"]:
                    comment_runs[key][obs["id"]] = obs
    records = list(latest.values())
    notes = [o for o in records if o["kind"] == "note"]
    comments = [o for o in records if o["kind"] == "comment"]
    authors = {o["content"]["author_pseudonym"] for o in notes if o["content"]["author_pseudonym"]}
    coverage = []
    for (source, run_id, eid), (obs, sample) in sorted(note_runs.items()):
        imported = list(comment_runs[(source, run_id, eid)].values())
        cov = sample["coverage"] or {}
        total = cov.get("reported_total")
        # Report observed coverage, never claim completeness just because counts match.
        coverage.append(
            {
                "source_id": source,
                "run_id": run_id,
                "evidence_id": eid,
                "reported_total": total,
                "imported_comments": len(imported),
                "top_level_imported": sum(not x["content"]["is_reply"] for x in imported),
                "replies_imported": sum(x["content"]["is_reply"] for x in imported),
                "author_replies": sum(x["content"]["is_author_reply"] for x in imported),
                "mode": cov.get("mode", "unknown"),
                "replies_requested": cov.get("includes_replies", False),
                "truncated": bool(cov.get("truncated") or sample["sampling"]["truncated"]),
                "failure": cov.get("failure"),
                "scope": "imported_only_not_verified_complete",
            }
        )
    return {
        "state": "ready" if notes else "insufficient_data",
        "notes": len(notes),
        "comments": len(comments),
        "independent_note_authors": len(authors),
        "notes_with_unknown_author": sum(o["content"]["author_pseudonym"] is None for o in notes),
        "synthetic_records": sum(o["content"]["synthetic"] for o in records),
        "real_records": sum(not o["content"]["synthetic"] for o in records),
        "weak_identity_records": sum(bool(o["weak_identity"]) for o in records),
        "sample_groups": {
            key: {
                "notes": sum(latest[e]["kind"] == "note" for e in ids),
                "comments": sum(latest[e]["kind"] == "comment" for e in ids),
            }
            for key, ids in sorted(groups.items())
        },
        "missing_metrics": {
            name: sum(
                not any(m["name"] == name and m["value"] is not None for m in o["metrics"])
                for o in notes
            )
            for name in ("likes", "saves", "comments", "shares")
        },
        "excluded_sources": blocked,
        "comment_coverage": coverage,
        "limitations": [
            "覆盖范围仅指已导入资料，不代表全平台样本或完整评论区。",
            "采样组可能重叠；不同来源的作者伪名不能可靠跨源去重。",
            "所有 synthetic 记录仅供工程验证。",
        ],
    }
