"""Source-bound durable jobs; one browser operation at a time, bounded execution."""

import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, ValidationError

from rednotebook.adapters.browser import BrowserReader, note_url, save_capture
from rednotebook.browser_control import AccessGate
from rednotebook.domain.models import Contract, Identifier, ResearchBrief
from rednotebook.enrichment import enrich
from rednotebook.errors import DomainError
from rednotebook.importing import import_file
from rednotebook.research.config import ResearchBudget, load_config
from rednotebook.research.gallery import Gallery, analyse_gallery
from rednotebook.research.runner import analyse
from rednotebook.search_intent import (
    SearchIntent,
    SearchPlan,
    generate_plan,
    make_plan,
    merge_candidates,
)
from rednotebook.topic_research import TopicSelection, execute_topic, resolve_search
from rednotebook.util import atomic_json, canonical, digest, stamp

PAUSED = {
    "browser_access_paused",
    "login_required",
    "captcha_required",
    "rate_limited",
    "access_blocked",
    "unexpected_page",
}


class JobRequest(Contract):
    operation: Literal[
        "search",
        "collect",
        "analyse_gallery",
        "research",
        "workflow",
        "plan_search",
        "search_plan",
        "topic_research",
    ]
    source_id: Identifier
    topic_selection: TopicSelection | None = None
    resume_topic_job_id: str | None = None
    replacement_jobs: dict[str, str] = Field(default_factory=dict)
    intent: SearchIntent | None = None
    search_plan: SearchPlan | None = None
    plan_job_id: str | None = None
    analysis_mode: Literal["local_model_analysis", "caller_analysis"] = "local_model_analysis"
    use_model: bool = False
    keyword: str = Field(default="", max_length=100)
    limit: int = Field(default=10, ge=1, le=20, strict=True)
    scrolls: int = Field(default=3, ge=0, le=5, strict=True)
    url: str = Field(default="", max_length=4000)
    brief_id: Identifier = "public-research"
    comment_limit: int = Field(default=5, ge=0, le=100, strict=True)
    max_images: int = Field(default=20, ge=1, le=20, strict=True)
    capture_id: str = ""
    resume_from: Literal["comments", "images"] | None = None
    brief: ResearchBrief | None = None
    budget: ResearchBudget = Field(default_factory=ResearchBudget)
    model_timeout_seconds: int = Field(default=7200, ge=60, le=28800, strict=True)
    capture_images: bool = True
    analyse_images: bool = True
    allow_partial: bool = False

    @property
    def browser_work(self):
        return self.operation in {"search", "collect", "search_plan", "topic_research"} or (
            self.operation == "workflow" and not self.capture_id
        )


def capture_root(db):
    root = db.path.resolve().parent / "managed-captures"
    if root.is_symlink():
        raise DomainError("capture_root_invalid")
    return root


def capture_path(db, job_id):
    # Accept only generated UUID names, never arbitrary filesystem paths from MCP.
    from uuid import UUID

    try:
        if str(UUID(job_id)) != job_id:
            raise ValueError
    except ValueError:
        raise DomainError("capture_id_invalid") from None
    path = capture_root(db) / job_id
    if path.is_symlink():
        raise DomainError("capture_path_invalid")
    return path


def purge_captures(db, source_id):
    ids = db.conn.execute("SELECT id FROM browser_jobs WHERE source_id=?", (source_id,)).fetchall()
    for row in ids:
        target = capture_path(db, row[0])
        if target.exists():
            shutil.rmtree(target)
    with db.conn:
        db.conn.execute(
            "UPDATE browser_jobs SET state='revoked',request_json=NULL,result_json=NULL,"
            "error_code=NULL,progress_json=NULL,updated_at=? WHERE source_id=?",
            (stamp(db.clock()), source_id),
        )


class BrowserService:
    def __init__(self, db, profile: Path, config: Path, reader=None):
        self.db, self.config = db, config
        self.reader = reader or BrowserReader(profile)
        self.gate = getattr(self.reader, "gate", None) or AccessGate(profile)
        self.lock = asyncio.Lock()
        self.model_lock = asyncio.Lock()
        self.tasks = {}
        # MCP service owns one DB/profile; overlapping services are disallowed by host lock.
        with db.conn:
            db.conn.execute(
                "UPDATE browser_jobs SET state='interrupted',error_code='service_restarted' "
                "WHERE state IN ('queued','running')"
            )

    def require(self, source_id, browser=False):
        source = self.db.require_source(source_id, "storage")
        self.db.require_source(source_id, "local_analysis")
        if browser:
            self.gate.require_active()
            self.db.require_source(source_id, "automated_access")
        return source

    def submit(
        self, request: JobRequest, *, reset_failure_pause=True, parent_job_id=None, job_id=None
    ):
        self.tasks = {key: task for key, task in self.tasks.items() if not task.done()}
        self.require(request.source_id)
        if request.browser_work:
            self.db.require_source(request.source_id, "automated_access")
        if request.operation == "plan_search" and request.intent is None:
            raise DomainError("search_intent_required")
        if request.operation == "plan_search":
            request = request.model_copy(update={"keyword": request.intent.primary_query})
        if request.operation == "search_plan":
            if not request.plan_job_id:
                raise DomainError("search_plan_job_required")
            parent = self.get(request.plan_job_id, True)
            if (
                parent["source_id"] != request.source_id
                or parent["operation"] != "plan_search"
                or parent["state"] != "complete"
            ):
                raise DomainError("search_plan_job_invalid")
            plan = SearchPlan.model_validate(parent["result"]["plan"])
            request = request.model_copy(
                update={"search_plan": plan, "keyword": plan.intent.primary_query}
            )
        if request.browser_work and self.gate.status().get("remaining_hourly_navigations") == 0:
            raise DomainError("browser_hourly_budget_wait")
        if request.browser_work and any(
            key != parent_job_id and not task.done() and self.is_browser_job(key)
            for key, task in self.tasks.items()
        ):
            raise DomainError("browser_job_already_active")
        if sum(not t.done() for t in self.tasks.values()) >= 4:
            raise DomainError("job_queue_full")
        if request.operation == "search" and not request.keyword.strip():
            raise DomainError("search_keyword_required")
        if request.operation == "collect" or (
            request.operation == "workflow" and not request.capture_id
        ):
            from rednotebook.adapters.browser import note_url

            note_url(request.url)
            if not request.keyword.strip():
                raise DomainError("sampling_keyword_required")
        if request.operation in {"research", "workflow"} and request.brief is None:
            raise DomainError("research_brief_required")
        if request.operation == "workflow":
            if request.brief.id != request.brief_id:
                raise DomainError("workflow_brief_mismatch")
            if request.brief.synthetic != self.require(request.source_id).synthetic:
                raise DomainError("synthetic_flag_mismatch")
        if request.operation == "topic_research":
            if request.topic_selection is None or request.brief is None:
                raise DomainError("topic_selection_and_brief_required")
            search, _ = resolve_search(self, request.source_id, request.topic_selection)
            request = request.model_copy(
                update={"keyword": search["plan"]["intent"]["primary_query"]}
            )
            if request.brief.synthetic != self.require(request.source_id).synthetic:
                raise DomainError("synthetic_flag_mismatch")
            if request.resume_topic_job_id:
                previous = self.get(request.resume_topic_job_id, True)
                if (
                    previous["source_id"] != request.source_id
                    or previous["operation"] != "topic_research"
                    or previous["state"] in {"queued", "running", "complete"}
                ):
                    raise DomainError("topic_resume_invalid")
                original = self.db.conn.execute(
                    "SELECT request_json FROM browser_jobs WHERE id=?",
                    (request.resume_topic_job_id,),
                ).fetchone()[0]
                original = JobRequest.model_validate_json(original)
                if (
                    original.topic_selection != request.topic_selection
                    or original.brief != request.brief
                    or original.analysis_mode != request.analysis_mode
                ):
                    raise DomainError("topic_resume_scope_changed")
        reset_reason = None
        if reset_failure_pause and request.operation in {"search", "search_plan"}:
            reset_reason = self.gate.reset_for_new_search()
        if request.browser_work:
            self.gate.require_active()
        ident = job_id or str(uuid4())
        at = stamp(self.db.clock())
        with self.db.conn:
            self.db.conn.execute(
                "INSERT INTO browser_jobs (id,source_id,state,request_json,result_json,error_code,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    ident,
                    request.source_id,
                    "queued",
                    canonical(request.model_dump(mode="json")),
                    None,
                    None,
                    at,
                    at,
                ),
            )
        self.tasks[ident] = asyncio.create_task(
            self._run(ident, request, reset_browser=bool(reset_reason))
        )
        result = {"job_id": ident, "state": "queued"}
        if reset_reason:
            result["browser_reset"] = {
                "reason": reset_reason,
                "history_preserved": True,
            }
        return result

    def is_browser_job(self, ident):
        row = self.db.conn.execute(
            "SELECT request_json FROM browser_jobs WHERE id=?", (ident,)
        ).fetchone()
        if not (row and row[0]):
            return False
        request = JobRequest.model_validate_json(row[0])
        if request.operation == "workflow":
            progress = self.db.conn.execute(
                "SELECT progress_json FROM browser_jobs WHERE id=?", (ident,)
            ).fetchone()[0]
            if progress and json.loads(progress).get("stage") in {
                "waiting_for_model",
                "analyse_gallery",
                "import",
                "research",
                "finished",
            }:
                return False
        return request.browser_work

    @asynccontextmanager
    async def execution_lock(self, request):
        # A workflow takes each lock only for its stage. SQLite operations stay on
        # this event-loop thread and never span an await inside a transaction.
        if request.operation in {"workflow", "topic_research"}:
            yield
        else:
            async with self.lock if request.browser_work else self.model_lock:
                yield

    def _saved_result(self, ident):
        row = self.db.conn.execute(
            "SELECT result_json FROM browser_jobs WHERE id=?", (ident,)
        ).fetchone()
        return row[0] if row else None

    def progress(self, ident, stage, **details):
        row = self.db.conn.execute(
            "SELECT progress_json FROM browser_jobs WHERE id=?", (ident,)
        ).fetchone()
        previous = json.loads(row[0]) if row and row[0] else {}
        events = previous.get("events", [])
        events.append({"stage": stage, "at": stamp(self.db.clock())})
        if stage == "execution_failed":
            previous["failed_stage"] = previous.get("stage", "unknown")
        previous["events"] = events[-100:]
        previous["events_truncated"] = previous.get("events_truncated", False) or len(events) > 100
        with self.db.conn:
            self.db.conn.execute(
                "UPDATE browser_jobs SET progress_json=?,updated_at=? WHERE id=? AND state!='revoked'",
                (canonical({**previous, "stage": stage, **details}), stamp(self.db.clock()), ident),
            )

    def update(self, ident, state, result=None, code=None):
        with self.db.conn:
            self.db.conn.execute(
                "UPDATE browser_jobs SET state=?,result_json=?,error_code=?,updated_at=? "
                "WHERE id=? AND state!='revoked'",
                (
                    state,
                    canonical(result) if result is not None else self._saved_result(ident),
                    code,
                    stamp(self.db.clock()),
                    ident,
                ),
            )

    async def _run(self, ident, request, *, reset_browser=False):
        try:
            async with self.execution_lock(request):
                if reset_browser:
                    # Drop stale locators/controller state, but keep the dedicated
                    # Chromium process, profile, login, and access history.
                    await self.reader.close()
                self.require(request.source_id, request.browser_work)
                self.update(ident, "running")
                self.progress(ident, request.operation)
                async with asyncio.timeout(
                    request.model_timeout_seconds * len(request.topic_selection.notes)
                    if request.operation == "topic_research"
                    else request.model_timeout_seconds
                    if request.operation
                    in {"analyse_gallery", "research", "workflow", "plan_search"}
                    else (
                        180 * len(request.search_plan.queries)
                        if request.operation == "search_plan"
                        else 180
                    )
                ):
                    result = await self.execute(ident, request)
                self.progress(ident, "finished")
                self.require(request.source_id)
                self.update(
                    ident,
                    "complete"
                    if result.get("state") not in {"partial", "skipped", "failed"}
                    else result["state"],
                    result,
                )
        except asyncio.CancelledError:
            self.update(ident, "cancelled", code="cancelled_by_client")
        except TimeoutError:
            self.progress(ident, "execution_failed", error_code="job_timeout")
            if request.operation != "topic_research" and self.is_browser_job(ident):
                self.gate.pause("browser_job_timeout")
            self.update(ident, "failed", code="job_timeout")
        except DomainError as exc:
            self.progress(ident, "execution_failed", error_code=exc.code)
            if (
                self.is_browser_job(ident)
                and exc.code in PAUSED
                and exc.code != "browser_access_paused"
            ):
                self.gate.pause(exc.code)
            self.update(ident, "paused" if exc.code in PAUSED else "failed", code=exc.code)
        except ValidationError as exc:
            self.record_failure(ident, exc)
            if request.operation != "topic_research" and self.is_browser_job(ident):
                self.gate.pause("browser_contract_invalid")
            self.update(ident, "failed", code="job_contract_invalid")
        except Exception as exc:
            self.record_failure(ident, exc)
            if request.operation != "topic_research" and self.is_browser_job(ident):
                self.gate.pause("browser_operation_failed")
            # Browser exception strings can contain signed URLs, page text and credentials.
            self.update(ident, "failed", code="browser_or_model_operation_failed")

    def record_failure(self, ident, exc):
        # No exception messages, locals, source lines or arbitrary class names.
        known = {
            "TypeError",
            "ValidationError",
            "ValueError",
            "KeyError",
            "AttributeError",
            "RuntimeError",
            "TimeoutError",
            "Error",
            "OSError",
        }
        kind = type(exc).__name__ if type(exc).__name__ in known else "OtherError"
        frames = []
        tb = exc.__traceback__
        root = Path(__file__).resolve().parent
        while tb:
            path = Path(tb.tb_frame.f_code.co_filename).resolve()
            if path.is_relative_to(root):
                frames.append({"module": str(path.relative_to(root)), "line": tb.tb_lineno})
            tb = tb.tb_next
        self.progress(
            ident, "execution_failed", failure={"exception_type": kind, "locations": frames[-5:]}
        )

    def resume_collection(self, capture_id, resume_from, max_images=20):
        parent = self.get(capture_id)
        if parent["operation"] not in {"collect", "workflow"} or parent["state"] in {
            "running",
            "queued",
        }:
            raise DomainError("capture_not_ready")
        row = self.db.conn.execute(
            "SELECT request_json FROM browser_jobs WHERE id=?", (capture_id,)
        ).fetchone()
        request = JobRequest.model_validate_json(row[0])
        checkpoint_file = capture_path(self.db, capture_id) / "checkpoint.json"
        if checkpoint_file.is_symlink() or not checkpoint_file.is_file():
            raise DomainError("capture_checkpoint_unavailable")
        updates = {"operation": "collect", "capture_id": capture_id, "resume_from": resume_from}
        if resume_from == "images":
            updates.update(capture_images=True, max_images=max_images)
        return self.submit(
            JobRequest.model_validate(request.model_dump() | updates), reset_failure_pause=False
        )

    async def execute(self, ident, request):
        def check():
            return self.require(request.source_id, request.browser_work)

        source = check()
        if request.operation == "plan_search":
            plan = make_plan(request.intent)
            # Save a usable deterministic plan before optional model work.
            result = {
                "plan": plan.model_dump(mode="json"),
                "plan_hash": digest(plan.model_dump(mode="json")),
            }
            self.update(ident, "running", result)
            if request.use_model and not plan.intent.exact_only and plan.intent.max_queries > 1:
                plan = await generate_plan(
                    request.intent, load_config(self.config), check, budget=request.budget
                )
            return {
                "state": "complete",
                "plan": plan.model_dump(mode="json"),
                "plan_hash": digest(plan.model_dump(mode="json")),
                "source_id": source.id,
            }
        if request.operation == "search_plan":
            plan = request.search_plan
            results = []

            def snapshot():
                return {
                    "state": "partial",
                    "plan_job_id": request.plan_job_id,
                    "plan": plan.model_dump(mode="json"),
                    "plan_hash": digest(plan.model_dump(mode="json")),
                    "source_id": source.id,
                    "query_results": results,
                    "completed_queries": len(results),
                    "planned_queries": len(plan.queries),
                    "candidates": merge_candidates(results),
                    "limitations": [
                        "bounded_search_not_representative",
                        "ranking_unverified",
                        "expansion_not_subject_evidence",
                        "groups_are_query_origin_only",
                    ],
                }

            self.update(ident, "running", snapshot())
            for query in plan.queries:
                check()
                self.progress(
                    ident,
                    "search_plan",
                    query_id=query.id,
                    completed_queries=len(results),
                    planned_queries=len(plan.queries),
                )
                async with asyncio.timeout(180):
                    result = await self.reader.search(
                        query.query, query.limit, plan.intent.scrolls, check
                    )
                check()
                if result.get("state") in {"failed", "paused", "skipped"} or result.get(
                    "error_code"
                ):
                    raise DomainError("planned_query_failed")
                results.append(
                    {**result, "query": query.model_dump(), "observed_at": stamp(self.db.clock())}
                )
                self.update(ident, "running", snapshot())
                self.progress(
                    ident,
                    "search_plan",
                    query_id=query.id,
                    completed_queries=len(results),
                    planned_queries=len(plan.queries),
                )
            return snapshot() | {
                "execution_complete": True,
                "achieved_depth": "search_only",
                "research_complete": False,
                "next_action": "select_notes_then_research_topic",
            }
        if request.operation == "topic_research":
            return await execute_topic(self, ident, request)
        if request.operation == "workflow":
            return await self.workflow(ident, request)
        if request.operation == "search":
            result = await self.reader.search(
                request.keyword, request.limit, request.scrolls, check
            )
            return result | {
                "observed_at": stamp(self.db.clock()),
                "source_id": source.id,
                "achieved_depth": "search_only",
                "research_complete": False,
                "candidate_count": len(result.get("candidates", [])),
            }
        if request.operation == "collect":
            target = capture_path(self.db, ident)
            target.mkdir(parents=True, mode=0o700)
            target.parent.chmod(0o700)
            observed_at = stamp(self.db.clock())

            def saved(collected, stage):
                check()
                result = save_capture(
                    target,
                    collected,
                    source,
                    request.brief_id,
                    request.keyword,
                    ident,
                    request.url,
                    self.db.clock,
                    observed_at,
                ) | {"capture_id": ident, "imported": False}
                self.update(ident, "running", result)
                self.progress(
                    ident,
                    stage,
                    images=result["images"],
                    declared_total=result["declared_total"],
                    records=result["records"],
                )
                return result

            self.progress(
                ident,
                "collect_start",
                access={
                    k: self.gate.status().get(k)
                    for k in (
                        "paused",
                        "remaining_hourly_navigations",
                        "next_navigation_in_seconds",
                    )
                },
            )
            resume_options = {}
            if request.resume_from:
                parent = self.get(request.capture_id)
                if parent["source_id"] != request.source_id or parent["state"] in {
                    "running",
                    "queued",
                }:
                    raise DomainError("capture_source_or_type_mismatch")
                previous = capture_path(self.db, request.capture_id)
                if (previous / "checkpoint.json").is_symlink():
                    raise DomainError("capture_checkpoint_unavailable")
                resume = json.loads((previous / "checkpoint.json").read_text())
                if resume["raw"].get("identity", {}).get("note_id") != note_url(request.url)[0]:
                    raise DomainError("capture_identity_unverified")
                import hashlib

                for item in resume["images"]:
                    name = f"{item['position']:02d}.image"
                    if item["path"] != name or (previous / name).is_symlink():
                        raise DomainError("capture_image_invalid")
                    data = (previous / name).read_bytes()
                    if hashlib.sha256(data).hexdigest() != item["sha256"]:
                        raise DomainError("capture_image_invalid")
                    (target / name).write_bytes(data)
                    (target / name).chmod(0o600)
                resume_options = {"resume": resume, "resume_from": request.resume_from}
            collected = await self.reader.collect(
                request.url,
                target,
                request.comment_limit,
                request.max_images,
                check,
                on_progress=saved,
                capture_images=request.capture_images,
                **resume_options,
            )
            if collected["state"] == "skipped":
                return collected
            collected["raw"]["comments"] = collected["raw"]["comments"][: request.comment_limit]
            return saved(collected, "capture_saved")
        if request.operation == "analyse_gallery":
            parent = self.get(request.capture_id)
            if parent["source_id"] != source.id or parent["operation"] not in {
                "collect",
                "workflow",
            }:
                raise DomainError("capture_source_or_type_mismatch")
            if request.capture_id != ident and parent["state"] in {"queued", "running"}:
                raise DomainError("capture_not_ready")
            target = capture_path(self.db, request.capture_id)
            path = target / "manifest.json"
            if not path.is_file() or path.is_symlink():
                raise DomainError("capture_manifest_missing")
            manifest = Gallery.model_validate_json(path.read_bytes())
            if manifest.source_id != source.id:
                raise DomainError("capture_source_or_type_mismatch")
            result = await analyse_gallery(
                self.db,
                manifest,
                target,
                load_config(self.config),
                request.max_images,
                progress=lambda **counts: self.progress(ident, "analyse_gallery", **counts),
            )
            check()
            atomic_json(target / "gallery.json", result)
            # Enriched evidence is imported explicitly via import_capture after analysis.
            evidence = json.loads((target / "evidence.json").read_text())
            atomic_json(target / "enriched.json", enrich(evidence, result))
            from rednotebook.research.gallery import PROVIDER_FAILURES

            fatal = next(
                (e["code"] for e in result["errors"] if e["code"] in PROVIDER_FAILURES), None
            )
            if fatal:
                raise DomainError(fatal)
            return {
                k: result[k]
                for k in ("state", "processed", "declared_total", "missing_positions", "requests")
            }
        if request.brief.synthetic != source.synthetic:
            raise DomainError("synthetic_flag_mismatch")
        records, _ = self.db.observations(request.brief.id)
        if any(r["source_id"] != source.id for r in records):
            raise DomainError("research_job_requires_single_source_brief")
        return await analyse(
            self.db,
            request.brief,
            load_config(self.config),
            request.budget,
            progress=lambda **counts: self.progress(ident, "research", **counts),
        )

    def get(self, ident, include_result=False):
        row = self.db.conn.execute("SELECT * FROM browser_jobs WHERE id=?", (ident,)).fetchone()
        if not row:
            raise DomainError("browser_job_not_found")
        self.require(row["source_id"])
        request = json.loads(row["request_json"])
        result = {
            "job_id": ident,
            "source_id": row["source_id"],
            "state": row["state"],
            "operation": request["operation"],
            "error_code": row["error_code"],
            "updated_at": row["updated_at"],
            "created_at": row["created_at"],
            "progress": json.loads(row["progress_json"])
            if row["progress_json"]
            else {"stage": row["state"]},
            "browser_access": self.gate.status(),
            "recovery": self.recovery(row),
        }
        if include_result:
            result["result"] = json.loads(row["result_json"]) if row["result_json"] else None
            if result["result"] and "next_action" in result["result"]:
                result["result"]["next_action"] = result["recovery"]["next_action"]
        return result

    async def cancel(self, ident):
        self.get(ident)
        task = self.tasks.get(ident)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # A task cancelled before its coroutine starts has no finally/except execution.
            self.update(ident, "cancelled", code="cancelled_by_client")
        return self.get(ident)

    def retry(self, ident):
        job = self.get(ident)
        if job["state"] not in {"failed", "paused", "interrupted", "cancelled"}:
            raise DomainError("job_not_retryable")
        request = self.db.conn.execute(
            "SELECT request_json FROM browser_jobs WHERE id=?", (ident,)
        ).fetchone()[0]
        # New job ID preserves original attempt; gallery retry reuses successful image cache.
        parsed = JobRequest.model_validate_json(request)
        if parsed.operation == "topic_research":
            raise DomainError("use_resume_topic_research")
        saved = self.get(ident, True).get("result") or {}
        if parsed.operation == "workflow" and saved.get("capture_id"):
            parsed = parsed.model_copy(update={"capture_id": saved["capture_id"], "url": ""})
        # Retrying the failed attempt is not a fresh user search and may not clear its pause.
        return self.submit(parsed, reset_failure_pause=False)

    def import_capture(self, ident, enriched=True, allow_partial=False):
        if self.model_lock.locked():
            raise DomainError("model_stage_busy")
        job = self.get(ident)
        if job["operation"] not in {"collect", "workflow"} or job["state"] in {
            "queued",
            "running",
            "revoked",
        }:
            raise DomainError("capture_not_ready")
        if job["state"] != "complete" and not allow_partial:
            raise DomainError("partial_capture_requires_explicit_import")
        if job["state"] not in {
            "complete",
            "partial",
            "failed",
            "paused",
            "interrupted",
            "cancelled",
        }:
            raise DomainError("capture_not_ready")
        path = capture_path(self.db, ident) / ("enriched.json" if enriched else "evidence.json")
        if path.is_symlink() or not path.is_file():
            raise DomainError("capture_evidence_missing")
        return import_file(self.db, path, self.require(job["source_id"]))

    def recovery(self, row):
        saved = json.loads(row["result_json"]) if row["result_json"] else {}
        request = json.loads(row["request_json"])
        capture_id = saved.get("capture_id") or request.get("capture_id") or None
        available = bool(
            capture_id and (capture_path(self.db, capture_id) / "evidence.json").is_file()
        )
        access = self.gate.status()
        if access["paused"]:
            action = (
                "fresh_search_or_explicit_operator_cli"
                if access.get("new_search_reset_available")
                else "resolve_pause_then_explicit_operator_resume"
            )
        elif row["error_code"] in {
            "vision_connection_failed",
            "vision_request_timeout",
            "vision_provider_rejected",
        }:
            action = "restore_local_model_then_resume_workflow"
        elif row["error_code"] in {
            "capture_image_snapshot_unverified",
            "capture_snapshot_changed",
            "note_content_mismatch",
            "note_identity_mismatch",
        }:
            action = "review_capture_then_explicit_recollect"
        elif row["state"] in {"queued", "running"}:
            action = "wait_or_read_result"
        elif saved.get("next_action"):
            action = saved["next_action"]
            # Interpret older saved responses without modifying historical payloads.
            if "allow_partial" in action:
                action = (
                    "retry_gallery_then_continue_workflow"
                    if saved.get("gallery")
                    else self.capture_recovery_action(saved.get("capture", saved))
                )
        elif available:
            action = self.capture_recovery_action(saved.get("capture", saved))
        elif row["state"] in {"failed", "interrupted", "cancelled"}:
            action = "inspect_error_then_explicit_recovery"
        else:
            action = "wait_or_read_result"
        return {
            "capture_id": capture_id,
            "saved_capture_available": available,
            "next_action": action,
            "automatic_browser_retry": False,
        }

    @staticmethod
    def capture_recovery_action(capture):
        review = capture.get("completeness", {})
        if review.get("capture_gate") == "passed":
            return "continue_workflow_with_capture_id"
        if review.get("missing_positions") or review.get("images") in {"partial", "unknown"}:
            return "resume_collect_images"
        return "review_capture_then_explicit_recollect"

    async def workflow(self, ident, request):
        capture_id = request.capture_id or ident
        capture = None
        if not request.capture_id:
            self.progress(ident, "waiting_for_browser")
            async with self.lock:
                async with asyncio.timeout(180):
                    capture = await self.execute(
                        ident, request.model_copy(update={"operation": "collect"})
                    )
            if capture.get("state") == "skipped":
                return capture
            if (
                capture["state"] != "complete"
                or capture.get("completeness", {}).get("capture_gate") != "passed"
            ):
                return capture | {"next_action": self.capture_recovery_action(capture)}
        else:
            parent = self.get(capture_id, True)
            if parent["source_id"] != request.source_id or parent["operation"] not in {
                "collect",
                "workflow",
            }:
                raise DomainError("capture_source_or_type_mismatch")
            if parent["state"] in {"queued", "running"}:
                raise DomainError("capture_not_ready")
            saved_result = parent["result"] or {}
            capture = saved_result.get("capture", saved_result)
            checkpoint_file = capture_path(self.db, capture_id) / "checkpoint.json"
            if checkpoint_file.is_file() and not checkpoint_file.is_symlink():
                saved_capture = json.loads(checkpoint_file.read_text())
                capture = {
                    **saved_capture,
                    "images": len(saved_capture.get("images", [])),
                }
            if capture.get("state") != "complete" and not request.allow_partial:
                raise DomainError("partial_capture_requires_explicit_import")
            if (
                capture.get("completeness", {}).get("capture_gate") != "passed"
                and not request.allow_partial
            ):
                raise DomainError("capture_completeness_requires_review")
        target = capture_path(self.db, capture_id)
        evidence_path = target / "evidence.json"
        if not evidence_path.is_file() or evidence_path.is_symlink():
            raise DomainError("capture_evidence_missing")
        evidence = json.loads(evidence_path.read_text())
        if any(
            r["source_id"] != request.source_id
            or any(s["brief_id"] != request.brief.id for s in r["sampling"])
            for r in evidence
        ):
            raise DomainError("workflow_brief_mismatch")
        if request.analysis_mode == "caller_analysis":
            return {
                "state": "complete",
                "capture_id": capture_id,
                "analysis_mode": "caller_analysis",
                "research_complete": False,
                "campaign_ready": False,
                "completeness": capture["completeness"],
                "brief": request.brief.model_dump(mode="json"),
                "next_action": "read_evidence_bundle",
            }
        self.update(ident, "running", {"capture_id": capture_id, "capture": capture})
        self.progress(ident, "waiting_for_model", capture_id=capture_id)
        async with self.model_lock:
            enriched = False
            gallery = None
            if request.analyse_images and (target / "manifest.json").is_file():
                gallery = await self.execute(
                    ident,
                    request.model_copy(
                        update={"operation": "analyse_gallery", "capture_id": capture_id}
                    ),
                )
                if gallery["state"] != "complete" and not request.allow_partial:
                    return {
                        "state": "partial",
                        "capture_id": capture_id,
                        "gallery": gallery,
                        "next_action": "retry_gallery_then_continue_workflow",
                    }
                enriched = True
            self.progress(ident, "import", capture_id=capture_id)
            self.require(request.source_id)
            imported = import_file(
                self.db,
                target / ("enriched.json" if enriched else "evidence.json"),
                self.require(request.source_id),
            )
            if imported.get("state") not in {"complete", "completed"} and imported.get("errors"):
                return {"state": "partial", "capture_id": capture_id, "import": imported}
            result = await self.execute(ident, request.model_copy(update={"operation": "research"}))
        return {
            "state": "failed"
            if result["state"] == "failed"
            else "partial"
            if capture.get("state") == "partial" or (gallery and gallery["state"] != "complete")
            else result["state"],
            "capture_id": capture_id,
            "completeness": capture.get("completeness", {})
            | {
                "image_reading": "not_applicable"
                if capture.get("declared_total") == 0
                else "complete"
                if gallery and gallery["state"] == "complete"
                else "not_run",
                "human_accuracy_review": "not_run",
            },
            "reading_scope": {
                "capture_state": capture.get("state", "unknown"),
                "records": capture.get("records"),
                "saved_images": capture.get("images"),
                "declared_images": capture.get("declared_total"),
                "capture_images_requested": request.capture_images,
                "analyse_images_requested": request.analyse_images,
                "comment_limit": request.comment_limit,
                "max_images": request.max_images,
                "limitations": capture.get("errors", []),
            },
            "gallery": gallery,
            "import": imported,
            "research": result,
            "run_id": result["run_id"],
        }

    async def pause(self):
        self.gate.pause("operator_paused")
        for key in list(self.tasks):
            if not self.tasks[key].done() and self.is_browser_job(key):
                await self.cancel(key)
        return {"state": "paused", "resume": "explicit_operator_cli_only"}

    async def close(self):
        active = [key for key, task in self.tasks.items() if not task.done()]
        for key in active:
            self.tasks[key].cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for key in active:
            self.update(key, "interrupted", code="service_stopped")
        await self.reader.close()
