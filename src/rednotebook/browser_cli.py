"""Standalone Playwright entry point; same service and contracts as MCP."""

import argparse
import asyncio
import fcntl
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from rednotebook.browser_control import AccessGate
from rednotebook.browser_deadline import browser_deadline
from rednotebook.browser_service import BrowserService, JobRequest
from rednotebook.errors import DomainError
from rednotebook.search_intent import SearchIntent
from rednotebook.storage import Database
from rednotebook.util import canonical


async def run(args, db):
    if args.action in {"status", "close", "recover-ui"}:
        async with browser_deadline(90, "browser_control_timeout"):
            return await _run(args, db)
    return await _run(args, db)


async def _run(args, db):
    service = BrowserService(db, args.profile, args.config)
    try:
        if args.action in {"status", "close", "recover-ui"}:
            try:
                await service.reader.open(attach_only=True)
            except DomainError as exc:
                if exc.code == "browser_session_not_running":
                    return {"state": "closed"}
                raise
            if args.action == "close":
                await service.reader.shutdown()
                return {"state": "closed"}
            if args.action == "recover-ui":
                dismissed = await service.reader.dismiss_note_overlay(paced=False)
                return {
                    "state": "complete",
                    "note_overlay": "dismissed" if dismissed else "not_present",
                    "access": service.gate.status(),
                    "next_action": "explicit_operator_resume_required"
                    if service.gate.status()["paused"]
                    else None,
                }
            return await service.reader.state()
        if args.action == "login":
            await service.reader.open()
            try:
                if urlsplit(service.reader.page.url).hostname != "www.xiaohongshu.com":
                    await service.reader.navigate("https://www.xiaohongshu.com/explore")
                await service.reader.page.bring_to_front()
            except DomainError as exc:
                if exc.code not in {"login_required", "captcha_required"}:
                    raise
            print(
                canonical(
                    {
                        "state": "browser_open",
                        "instruction": "请手动登录；检测到账号入口后命令退出，浏览器会保留。",
                    }
                ),
                flush=True,
            )
            while service.reader.context and any(
                not p.is_closed() for p in service.reader.context.pages
            ):
                auth = await service.reader.authentication_state()
                if auth["authentication"] == "logged_in":
                    return {
                        "state": "complete",
                        "browser_kept_open": True,
                        "automation_paused": service.gate.status()["paused"],
                    } | auth
                await asyncio.sleep(2)
            return {"state": "closed", "profile": "dedicated_persistent_profile"}
        if args.action == "authorize-caller":
            from rednotebook.research.assistant_review import record_consent

            consent = record_consent(
                service, args.capture_id, args.statement_file.read_text(), args.processor
            )
            return {
                "state": "complete",
                "capture_id": consent["capture_id"],
                "processor": consent["processor"],
                "scope": consent["scope"],
            }
        if args.action == "job":
            return service.get(args.id, include_result=True)
        if args.action == "resume-collect":
            job = service.resume_collection(args.capture_id, args.from_stage, args.images)
            await service.tasks[job["job_id"]]
            return service.get(job["job_id"], include_result=True)
        if args.action == "plan-search":
            request = JobRequest(
                operation="plan_search",
                source_id=args.source,
                intent=SearchIntent.model_validate_json(args.file.read_bytes()),
                use_model=args.generate,
            )
        elif args.action == "search-plan":
            request = JobRequest(
                operation="search_plan", source_id=args.source, plan_job_id=args.plan_job
            )
        elif args.action == "search":
            request = JobRequest(
                operation="search",
                source_id=args.source,
                keyword=args.keyword,
                limit=args.limit,
                scrolls=args.scrolls,
            )
        else:
            request = JobRequest(
                operation="collect",
                source_id=args.source,
                keyword=args.keyword,
                url=args.url,
                brief_id=args.brief,
                comment_limit=args.comments,
                max_images=max(1, args.images),
                capture_images=args.images != 0,
            )
        job = service.submit(request)
        await service.tasks[job["job_id"]]
        return service.get(job["job_id"], include_result=True)
    finally:
        await service.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Playwright 小红书检索（独立登录配置）")
    parser.add_argument("--db", type=Path, default=Path("data/rednotebook.sqlite"))
    parser.add_argument("--profile", type=Path, default=Path("private/browser-profile"))
    parser.add_argument("--config", type=Path, default=Path("private/model.toml"))
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("login")
    sub.add_parser("status")
    sub.add_parser("recover-ui")
    sub.add_parser("close")
    sub.add_parser("pause")
    sub.add_parser("resume")
    sub.add_parser("job").add_argument("id")
    search = sub.add_parser("search")
    search.add_argument("--source", required=True)
    search.add_argument("--keyword", required=True)
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--scrolls", type=int, default=3)
    plan = sub.add_parser("plan-search")
    plan.add_argument("--source", required=True)
    plan.add_argument("--file", type=Path, required=True)
    plan.add_argument("--generate", action="store_true")
    planned = sub.add_parser("search-plan")
    planned.add_argument("--source", required=True)
    planned.add_argument("--plan-job", required=True)
    resume_collect = sub.add_parser("resume-collect")
    resume_collect.add_argument("--capture-id", required=True)
    resume_collect.add_argument("--from-stage", choices=["comments", "images"], required=True)
    resume_collect.add_argument("--images", type=int, choices=range(1, 21), default=20)
    collect = sub.add_parser("collect")
    collect.add_argument("--source", required=True)
    collect.add_argument("--url", required=True)
    collect.add_argument("--brief", required=True)
    collect.add_argument("--keyword", required=True)
    collect.add_argument("--comments", type=int, default=5)
    collect.add_argument("--images", type=int, choices=range(0, 21), default=20)
    authorize = sub.add_parser(
        "authorize-caller", help="Record explicit user consent for one capture"
    )
    authorize.add_argument("--capture-id", required=True)
    authorize.add_argument("--processor", required=True)
    authorize.add_argument("--statement-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action in {"pause", "resume"}:
            gate = AccessGate(args.profile)
            if args.action == "pause":
                gate.pause()
                result = {"state": "paused"}
            else:
                result = gate.resume()
            print(canonical(result))
            return 0
        args.db.parent.mkdir(parents=True, exist_ok=True)
        with args.db.with_suffix(".browser.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DomainError("browser_service_already_running") from None
            with Database(args.db) as db:
                result = asyncio.run(run(args, db))
    except KeyboardInterrupt:
        result = {"state": "cancelled"}
    except DomainError as exc:
        result = {"state": "failed", "error_code": exc.code}
    except (ValidationError, OSError):
        result = {"state": "failed", "error_code": "configuration_invalid"}
    except Exception:
        result = {"state": "failed", "error_code": "browser_operation_failed"}
    print(canonical(result))
    return 0 if result["state"] in {"complete", "closed", "ready"} else 2
