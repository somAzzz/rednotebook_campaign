"""Evidence and opt-in local model CLI. JSON results or explicit Markdown views."""

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from pydantic import ValidationError

from rednotebook import __version__
from rednotebook.analysis.metrics import metric_delta, rank_observations
from rednotebook.analysis.quality import quality_report
from rednotebook.domain.models import EvidenceInput, Metric, ResearchBrief, SourceGrant, date_input
from rednotebook.errors import DomainError
from rednotebook.fixtures import write_fixture
from rednotebook.importing import import_file, validation_issues
from rednotebook.research.config import ResearchBudget, load_config
from rednotebook.research.probe import probe
from rednotebook.research.render import markdown
from rednotebook.research.runner import analyse
from rednotebook.research.store import read_run
from rednotebook.storage import Database
from rednotebook.util import now_utc, stamp

IMPLEMENTED = [
    "status",
    "doctor",
    "schema",
    "brief validate",
    "fixtures",
    "import",
    "evidence inspect",
    "quality",
    "metrics rank",
    "metrics delta",
    "source register",
    "playwright search/collect",
    "stdio MCP tools",
    "source revoke",
    "source sweep",
    "job inspect",
    "model probe",
    "analyse",
    "research show",
    "propose",
    "bundle",
    "revise",
    "review",
    "preview",
    "export",
    "outcome import",
    "retrospective",
    "media inspect",
    "adapter fetch",
    "asset add",
    "media-extract",
    "media-analyse",
    "gallery-analyse",
    "enrich",
    "evaluate",
]


def parser():
    p = argparse.ArgumentParser(description="RedNotebook：本地证据导入、质量检查与确定性指标")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument(
        "--db", type=Path, default=Path("data/rednotebook.sqlite"), help="SQLite 文件路径"
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--profile", type=Path, default=Path("private/browser-profile"))
    doctor.add_argument("--config", type=Path, default=Path("private/model.toml"))
    schema = sub.add_parser("schema", help="输出输入契约的 JSON Schema")
    schema.add_argument("kind", choices=["brief", "grant", "evidence"])
    brief = (
        sub.add_parser("brief").add_subparsers(dest="action", required=True).add_parser("validate")
    )
    brief.add_argument("--file", required=True, type=Path)
    fixture = sub.add_parser("fixtures", help="生成 200 笔记/500 评论合成夹具")
    fixture.add_argument("--out", type=Path, required=True)
    fixture.add_argument("--at", help="可选固定的带时区基准时间")
    imp = sub.add_parser("import", help="获准 JSON/CSV 导入；坏行使结果为 partial")
    imp.add_argument("--file", type=Path, required=True)
    imp.add_argument("--grant", type=Path, required=True)
    evidence = (
        sub.add_parser("evidence")
        .add_subparsers(dest="action", required=True)
        .add_parser("inspect")
    )
    evidence.add_argument("id")
    evidence.add_argument("--revision", type=int)
    quality = sub.add_parser("quality")
    quality.add_argument("--brief", required=True)
    quality.add_argument("--source")
    metrics = sub.add_parser("metrics").add_subparsers(dest="action", required=True)
    rank = metrics.add_parser("rank")
    rank.add_argument("--brief", required=True)
    rank.add_argument("--source")
    delta = metrics.add_parser("delta")
    delta.add_argument("--brief", required=True)
    delta.add_argument("--evidence", required=True)
    delta.add_argument("--metric", required=True)
    delta.add_argument("--before", required=True)
    delta.add_argument("--after", required=True)
    source = sub.add_parser("source").add_subparsers(dest="action", required=True)
    source.add_parser("revoke", help="撤销来源并清除该来源的本地证据").add_argument("id")
    source.add_parser("register", help="登记来源许可").add_argument(
        "--file", type=Path, required=True
    )
    source.add_parser("sweep", help="清除到期来源的本地证据")
    job = sub.add_parser("job").add_subparsers(dest="action", required=True).add_parser("inspect")
    job.add_argument("id")
    model = sub.add_parser("model").add_subparsers(dest="action", required=True).add_parser("probe")
    model.add_argument("--config", type=Path, default=Path("private/model.toml"))
    research = (
        sub.add_parser("research").add_subparsers(dest="action", required=True).add_parser("show")
    )
    research.add_argument("id")
    research.add_argument("--format", choices=["json", "markdown"], default="json")
    run = sub.add_parser("analyse", help="使用已配置的本地模型进行有引用的分批研究")
    run.add_argument("--brief", type=Path, required=True)
    run.add_argument("--config", type=Path, default=Path("private/model.toml"))
    run.add_argument("--max-notes", type=int, default=200)
    run.add_argument("--max-comments", type=int, default=500)
    run.add_argument("--max-model-calls", type=int, default=60)
    run.add_argument("--batch-notes", type=int, default=10)
    run.add_argument("--max-chars", type=int, default=50000)
    asset = sub.add_parser("asset").add_subparsers(dest="action", required=True).add_parser("add")
    asset.add_argument("id")
    asset.add_argument("--version", type=int, required=True)
    asset.add_argument("--file", type=Path, required=True)
    asset.add_argument("--owner", required=True)
    asset.add_argument("--rights-ref", required=True)
    proposal = sub.add_parser("propose")
    proposal.add_argument("--research", required=True)
    proposal.add_argument("--generate", action="store_true", help="显式调用本地模型生成创意草案")
    proposal.add_argument("--config", type=Path, default=Path("private/model.toml"))
    for command in ("bundle", "review", "preview", "export", "retrospective", "revise"):
        item = sub.add_parser(command)
        item.add_argument("id")
        item.add_argument("--version", type=int, required=True)
        if command == "review":
            item.add_argument("--hash", required=True)
            item.add_argument("--reviewer", required=True)
        if command == "revise":
            item.add_argument("--file", type=Path, required=True)
    outcome = (
        sub.add_parser("outcome").add_subparsers(dest="action", required=True).add_parser("import")
    )
    outcome.add_argument("--file", type=Path, required=True)
    media = (
        sub.add_parser("media").add_subparsers(dest="action", required=True).add_parser("inspect")
    )
    media.add_argument("--file", type=Path, required=True)
    media.add_argument("--kind", choices=["image", "video"], required=True)
    adapter = (
        sub.add_parser("adapter").add_subparsers(dest="action", required=True).add_parser("fetch")
    )
    adapter.add_argument("--source", required=True)
    adapter.add_argument("--path", required=True)
    asr = sub.add_parser("asr").add_subparsers(dest="action", required=True)
    prepare = asr.add_parser("prepare")
    prepare.add_argument("--size", choices=["tiny", "base", "small"], default="small")
    transcript = asr.add_parser("transcribe")
    transcript.add_argument("--file", type=Path, required=True)
    transcript.add_argument("--source", required=True)
    transcript.add_argument(
        "--model", type=Path, default=Path("private/models/faster-whisper-small")
    )
    transcript.add_argument("--language", choices=["zh", "en"])
    gallery = sub.add_parser("gallery-analyse")
    gallery.add_argument("--manifest", type=Path, required=True)
    gallery.add_argument("--config", type=Path, default=Path("private/model.toml"))
    gallery.add_argument("--max-images", type=int, default=20)
    evaluation = sub.add_parser("evaluate")
    evaluation.add_argument("--file", type=Path, required=True)
    enrich = sub.add_parser("enrich")
    enrich.add_argument("--file", type=Path, required=True)
    enrich.add_argument("--gallery", type=Path, required=True)
    enrich.add_argument("--out", type=Path, required=True)
    vision = sub.add_parser("media-analyse")
    vision.add_argument("--source", required=True)
    vision.add_argument("--file", type=Path, required=True)
    vision.add_argument("--kind", choices=["image", "video"], required=True)
    vision.add_argument("--config", type=Path, default=Path("private/model.toml"))
    vision.add_argument("--frames", type=int, default=4)
    extraction = sub.add_parser("media-extract")
    extraction.add_argument("--file", type=Path, required=True)
    extraction.add_argument("--kind", choices=["image", "video"], required=True)
    return p


def load_brief(path):
    brief = ResearchBrief.model_validate_json(path.read_bytes())
    for asset in brief.assets:
        file = path.parent / asset.path
        if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != asset.sha256:
            raise DomainError("asset_missing_or_hash_mismatch")
    return brief


def dispatch(args):
    if args.command == "status":
        return {
            "project": "RedNotebook",
            "version": __version__,
            "stage": "S8-playwright-mcp",
            "implemented": IMPLEMENTED,
            "planned": ["heldout_quality_acceptance", "longitudinal_value_pilot"],
            "deferred_by_user": ["video", "asr"],
            "network_access": "explicit_local_model_and_readonly_adapter",
            "real_data_validation": "3_public_notes_30_comments_8_image_gallery_smoke_test",
        }, 0
    if args.command == "doctor":
        with sqlite3.connect(":memory:") as connection:
            sqlite_ok = connection.execute("SELECT 1").fetchone() == (1,)
        from rednotebook.diagnostics import diagnose

        with Database(args.db) as diagnostic_db:
            details = diagnose(diagnostic_db, args.profile, args.config)
        return {
            **details,
            "python": sys.version.split()[0],
            "python_ok": sys.version_info >= (3, 12),
            "sqlite": sqlite3.sqlite_version,
            "sqlite_ok": sqlite_ok,
        }, 0
    if args.command == "schema":
        return {"brief": ResearchBrief, "grant": SourceGrant, "evidence": EvidenceInput}[
            args.kind
        ].model_json_schema(), 0
    if args.command == "brief":
        brief = load_brief(args.file)
        return {
            "brief_id": brief.id,
            "valid": True,
            "synthetic": brief.synthetic,
            "unverified_facts": [f.id for f in brief.facts if f.status == "unverified"],
            "verified_fact_count": sum(f.status == "verified" for f in brief.facts),
            "note": "结构及素材哈希校验通过，不替代人工验证产品功能。",
        }, 0
    if args.command == "fixtures":
        return write_fixture(args.out, date_input(args.at) if args.at else now_utc()), 0
    if args.command == "model":
        result = asyncio.run(probe(load_config(args.config)))
        return result, 0 if result["structured_output"] and result["tool_calling"] else 1
    if args.command == "enrich":
        from rednotebook.enrichment import enrich

        result = enrich(json.loads(args.file.read_text()), json.loads(args.gallery.read_text()))
        with args.out.open("x", encoding="utf-8") as output:
            json.dump(result, output, ensure_ascii=False, indent=2)
        return {"rows": len(result), "output": str(args.out)}, 0
    if args.command == "media-extract":
        from rednotebook.media import extract_media

        return extract_media(args.file, args.kind), 0
    if args.command == "media":
        from rednotebook.media import inspect_media

        return inspect_media(args.file, args.kind), 0
    if args.command == "asr" and args.action == "prepare":
        from rednotebook.asr import prepare

        return prepare(args.size), 0
    with Database(args.db) as db:
        if args.command == "asr":
            from rednotebook.asr import transcribe

            return transcribe(db, args.source, args.file, args.model, args.language), 0

        if args.command == "evaluate":
            from rednotebook.research.evaluation import Evaluation, evaluate

            return evaluate(db, Evaluation.model_validate_json(args.file.read_bytes())), 0
        if args.command == "gallery-analyse":
            from rednotebook.research.gallery import Gallery, analyse_gallery

            gallery = Gallery.model_validate_json(args.manifest.read_bytes())
            result = asyncio.run(
                analyse_gallery(
                    db, gallery, args.manifest.parent, load_config(args.config), args.max_images
                )
            )
            return result, 0 if result["state"] == "complete" else 2
        if args.command == "media-analyse":
            from rednotebook.research.vision import analyse_media

            result = asyncio.run(
                analyse_media(
                    db,
                    args.source,
                    args.file,
                    args.kind,
                    load_config(args.config),
                    frame_limit=args.frames,
                )
            )
            return result, 0 if result["state"] == "complete" else 2
        from rednotebook import creative

        if args.command == "asset":
            return creative.add_asset(
                db, args.id, args.version, args.file, args.owner, args.rights_ref
            ), 0
        if args.command == "propose":
            if args.generate:
                from rednotebook.research.proposals import generate

                return asyncio.run(generate(db, args.research, load_config(args.config))), 0
            return creative.propose(db, args.research), 0
        if args.command == "bundle":
            return creative.load_bundle(db, args.id, args.version), 0
        if args.command == "review":
            return creative.review(db, args.id, args.version, args.hash, args.reviewer), 0
        if args.command == "revise":
            editorial = creative.Editorial.model_validate_json(args.file.read_bytes())
            return creative.revise(db, args.id, args.version, editorial), 0
        if args.command in ("preview", "export"):
            return creative.export(db, args.id, args.version, args.command == "preview"), 0
        if args.command in ("outcome", "retrospective"):
            from rednotebook.outcomes import Outcome, import_outcome, retrospective

            if args.command == "outcome":
                return import_outcome(db, Outcome.model_validate_json(args.file.read_bytes())), 0
            return retrospective(db, args.id, args.version), 0
        if args.command == "adapter":
            from rednotebook.adapters.readonly import ReadOnlyAdapter

            adapter = ReadOnlyAdapter(db, args.source, "https://www.xiaohongshu.com")
            try:
                return adapter.get_note(args.path), 2
            finally:
                adapter.close()

        if args.command == "analyse":
            brief = load_brief(args.brief)
            budget = ResearchBudget(
                max_notes=args.max_notes,
                max_comments=args.max_comments,
                max_model_calls=args.max_model_calls,
                batch_notes=args.batch_notes,
                max_chars_per_field=args.max_chars,
            )
            result = asyncio.run(analyse(db, brief, load_config(args.config), budget))
            summary = {
                key: result[key]
                for key in (
                    "run_id",
                    "state",
                    "selected_notes",
                    "selected_comments",
                    "completed_batches",
                    "errors",
                    "usage",
                )
            }
            summary["finding_count"] = len(result["findings"])
            summary["semantic_quality_verified"] = False
            return summary, 0 if result["state"] == "complete" else 2
        if args.command == "research":
            report = read_run(db, args.id)
            return markdown(report) if args.format == "markdown" else report, 0
        if args.command == "import":
            grant = SourceGrant.model_validate_json(args.grant.read_bytes())
            result = import_file(db, args.file, grant)
            return result, 0 if result["state"] == "complete" else 2
        if args.command == "evidence":
            return db.inspect(args.id, args.revision), 0
        if args.command == "quality":
            records, blocked = db.observations(args.brief, args.source)
            return {"brief_id": args.brief, **quality_report(records, blocked)}, 0
        if args.command == "metrics":
            if args.action == "rank":
                records, blocked = db.observations(args.brief, args.source)
                return {
                    "brief_id": args.brief,
                    "excluded_sources": blocked,
                    **rank_observations(records),
                }, 0
            # inspect checks tombstones and permission before selecting any metrics.
            detail = db.inspect(args.evidence)
            records, _ = db.observations(args.brief, detail["content"]["source_id"])
            before, after = stamp(date_input(args.before)), stamp(date_input(args.after))
            snapshots = {
                o["observed_at"]: m
                for o in records
                if o["id"] == args.evidence
                for m in o["metrics"]
                if m["name"] == args.metric
            }
            if before not in snapshots or after not in snapshots:
                raise DomainError("snapshot_not_found_in_brief")
            result = metric_delta(
                Metric.model_validate(snapshots[before]),
                Metric.model_validate(snapshots[after]),
                same_source=True,
            )
            return {"evidence_id": args.evidence, "metric": args.metric, **result}, 0
        if args.command == "source":
            if args.action == "register":
                grant = SourceGrant.model_validate_json(args.file.read_bytes())
                db.register_grant(grant)
                return {"source_id": grant.id, "state": "registered"}, 0
            return (db.revoke(args.id) if args.action == "revoke" else db.sweep()), 0
        if args.command == "job":
            return db.job(args.id), 0
    raise DomainError("unimplemented_command")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result, code = dispatch(args)
    except ValidationError as exc:
        result, code = {"error": "validation_failed", "issues": validation_issues(exc)}, 1
    except DomainError as exc:
        result, code = {"error": exc.code}, 1
    except (OSError, ValueError, sqlite3.Error):
        # Error output intentionally omits source values, filesystem paths and SQL text.
        result, code = {"error": "io_parse_or_database_error"}, 1
    except KeyboardInterrupt:
        result, code = {"error": "interrupted_retry_original_file"}, 130
    print(
        result
        if isinstance(result, str)
        else json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
        file=sys.stderr if code in (1, 130) else sys.stdout,
    )
    return code
