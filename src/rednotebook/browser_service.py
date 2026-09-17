"""Source-bound durable jobs; one browser operation at a time, bounded execution."""

import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, ValidationError

from rednotebook.adapters.browser import BrowserReader, save_capture
from rednotebook.browser_control import AccessGate
from rednotebook.domain.models import Contract, Identifier, ResearchBrief
from rednotebook.enrichment import enrich
from rednotebook.errors import DomainError
from rednotebook.importing import import_file
from rednotebook.research.config import ResearchBudget, load_config
from rednotebook.research.gallery import Gallery, analyse_gallery
from rednotebook.research.runner import analyse
from rednotebook.util import atomic_json, canonical, stamp

PAUSED = {
    "browser_access_paused",
    "login_required",
    "captcha_required",
    "rate_limited",
    "access_blocked",
    "unexpected_page",
}


class JobRequest(Contract):
    operation: Literal["search", "collect", "analyse_gallery", "research", "workflow"]
    source_id: Identifier
    keyword: str = Field(default="", max_length=100)
    limit: int = Field(default=10, ge=1, le=20, strict=True)
    scrolls: int = Field(default=3, ge=0, le=5, strict=True)
    url: str = Field(default="", max_length=4000)
    brief_id: Identifier = "public-research"
    comment_limit: int = Field(default=20, ge=0, le=100, strict=True)
    max_images: int = Field(default=20, ge=1, le=20, strict=True)
    capture_id: str = ""
    brief: ResearchBrief | None = None
    budget: ResearchBudget = Field(default_factory=ResearchBudget)
    model_timeout_seconds: int = Field(default=7200, ge=60, le=28800, strict=True)
    capture_images: bool = True
    analyse_images: bool = True
    allow_partial: bool = False

    @property
    def browser_work(self):
        return self.operation in {"search", "collect"} or (
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

    def submit(self, request: JobRequest):
        self.tasks = {key: task for key, task in self.tasks.items() if not task.done()}
        self.require(request.source_id, request.browser_work)
        if request.browser_work and any(
            not task.done() and self.is_browser_job(key) for key, task in self.tasks.items()
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
        ident = str(uuid4())
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
        self.tasks[ident] = asyncio.create_task(self._run(ident, request))
        return {"job_id": ident, "state": "queued"}

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
        if request.operation == "workflow":
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

    async def _run(self, ident, request):
        try:
            async with self.execution_lock(request):
                self.require(request.source_id, request.browser_work)
                self.update(ident, "running")
                self.progress(ident, request.operation)
                async with asyncio.timeout(
                    request.model_timeout_seconds
                    if request.operation in {"analyse_gallery", "research", "workflow"}
                    else 180
                ):
                    result = await self.execute(ident, request)
                self.progress(ident, "finished")
                self.require(request.source_id)
                if request.operation == "collect" and result.get("errors"):
                    self.gate.pause("capture_incomplete_requires_review")
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
            if self.is_browser_job(ident):
                self.gate.pause("browser_job_timeout")
            self.update(ident, "failed", code="job_timeout")
        except DomainError as exc:
            if self.is_browser_job(ident):
                self.gate.pause(exc.code)
            self.update(ident, "paused" if exc.code in PAUSED else "failed", code=exc.code)
        except ValidationError:
            if self.is_browser_job(ident):
                self.gate.pause("browser_contract_invalid")
            self.update(ident, "failed", code="job_contract_invalid")
        except Exception:
            if self.is_browser_job(ident):
                self.gate.pause("browser_operation_failed")
            # Browser exception strings can contain signed URLs, page text and credentials.
            self.update(ident, "failed", code="browser_or_model_operation_failed")

    async def execute(self, ident, request):
        def check():
            return self.require(request.source_id, request.browser_work)

        source = check()
        if request.operation == "workflow":
            return await self.workflow(ident, request)
        if request.operation == "search":
            result = await self.reader.search(
                request.keyword, request.limit, request.scrolls, check
            )
            return result | {"observed_at": stamp(self.db.clock()), "source_id": source.id}
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

            collected = await self.reader.collect(
                request.url,
                target,
                request.comment_limit,
                request.max_images,
                check,
                on_progress=saved,
                capture_images=request.capture_images,
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
        saved = self.get(ident, True).get("result") or {}
        if parsed.operation == "workflow" and saved.get("capture_id"):
            parsed = parsed.model_copy(update={"capture_id": saved["capture_id"], "url": ""})
        return self.submit(parsed)

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
        capture_id = saved.get("capture_id")
        available = bool(
            capture_id and (capture_path(self.db, capture_id) / "evidence.json").is_file()
        )
        return {
            "capture_id": capture_id,
            "saved_capture_available": available,
            "next_action": "import_saved_capture_with_allow_partial_or_continue_workflow"
            if available
            and row["state"] in {"failed", "paused", "interrupted", "cancelled", "partial"}
            else "resolve_pause_then_explicit_operator_resume"
            if self.gate.status()["paused"]
            else "wait_or_read_result",
            "automatic_browser_retry": False,
        }

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
            if capture.get("errors"):
                self.gate.pause("capture_incomplete_requires_review")
            if capture["state"] != "complete":
                return capture | {
                    "next_action": "continue_workflow_with_capture_id_and_allow_partial"
                }
        else:
            parent = self.get(capture_id, True)
            if parent["source_id"] != request.source_id or parent["operation"] not in {
                "collect",
                "workflow",
            }:
                raise DomainError("capture_source_or_type_mismatch")
            if parent["state"] in {"queued", "running"}:
                raise DomainError("capture_not_ready")
            capture = parent["result"] or {}
            if parent["state"] != "complete" and not request.allow_partial:
                raise DomainError("partial_capture_requires_explicit_import")
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
                        "next_action": "continue_workflow_with_allow_partial_or_retry_gallery",
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
