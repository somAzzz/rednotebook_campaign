"""Persistent operator pause and conservative pacing; never a claim of platform-safe rates."""

import asyncio
import json
import re
import time
from pathlib import Path

from rednotebook.errors import DomainError
from rednotebook.util import canonical


class AccessGate:
    PAGE_INTERVAL = 60
    ACTION_INTERVAL = 3
    HOURLY_PAGE_LIMIT = 60
    # These pauses describe a failed local browser task, not a platform safety signal.
    # A fresh search may clear them, but retries and collection jobs may not.
    RESETTABLE_FAILURE_PAUSES = frozenset(
        {
            "browser_contract_invalid",
            "browser_job_timeout",
            "browser_navigation_failed",
            "browser_navigation_timeout",
            "browser_operation_failed",
            "browser_profile_busy",
            "browser_session_busy",
            "browser_session_launch_failed",
            "browser_session_launch_timeout",
            "browser_session_metadata_invalid",
            "browser_session_not_running",
            "browser_session_unreachable",
            "capture_incomplete_requires_review",
            "note_detail_unavailable",
            "note_overlay_close_failed",
            "note_overlay_dismiss_failed",
            "note_overlay_recovery_failed",
            "search_results_unavailable",
        }
    )

    def __init__(self, profile: Path, clock=time.time, sleep=asyncio.sleep):
        self.profile = profile.resolve()
        self.clock, self.sleep = clock, sleep
        self.marker = self.profile / ".rednotebook-paused.json"
        self.history = self.profile / ".rednotebook-access.json"
        self.last_action = None

    def pause(self, code="operator_paused"):
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.marker.write_text(canonical({"code": code, "at": self.clock()}))
        self.marker.chmod(0o600)

    def _pause_reason(self):
        if not self.marker.exists():
            return None
        try:
            marker = json.loads(self.marker.read_text())
            # Persisted error codes are operational metadata, never exception text.
            code = marker.get("code", "")
            return code if re.fullmatch(r"[a-z_]{1,80}", code) else "pause_reason_unavailable"
        except (OSError, ValueError, TypeError, AttributeError):
            return "pause_reason_unavailable"

    def status(self):
        result = {"paused": self.marker.exists(), "resume": "explicit_operator_cli_only"}
        reason = self._pause_reason()
        if reason is not None:
            result["reason"] = reason
        result["new_search_reset_available"] = reason in self.RESETTABLE_FAILURE_PAUSES
        try:
            history = json.loads(self.history.read_text()) if self.history.exists() else []
            now = self.clock()
            history = [t for t in history if type(t) in (int, float) and t > now - 3600]
            result["remaining_hourly_navigations"] = max(0, self.HOURLY_PAGE_LIMIT - len(history))
            if reason == "hourly_page_budget_exhausted":
                result["new_search_reset_available"] = result["remaining_hourly_navigations"] > 0
            if len(history) >= self.HOURLY_PAGE_LIMIT:
                result["next_navigation_in_seconds"] = max(0, 3600 - (now - history[0]))
            else:
                result["next_navigation_in_seconds"] = (
                    max(0, self.PAGE_INTERVAL - (now - history[-1])) if history else 0
                )
        except (OSError, ValueError, TypeError):
            result["history_state"] = "invalid"
        if result["paused"] and result["new_search_reset_available"]:
            result["resume"] = "fresh_search_or_explicit_operator_cli"
        return result

    def reset_for_new_search(self):
        """Clear only a task-failure pause before an explicit fresh search."""
        reason = self._pause_reason()
        resettable = reason in self.RESETTABLE_FAILURE_PAUSES
        # Compatibility for a marker written by versions that persisted hourly exhaustion.
        if reason == "hourly_page_budget_exhausted":
            resettable = self.status().get("remaining_hourly_navigations", 0) > 0
        if not resettable:
            return None
        self.marker.unlink(missing_ok=True)
        return reason

    def require_active(self):
        if self.marker.exists():
            raise DomainError("browser_access_paused")

    def resume(self):
        # Only exposed as a local operator command, never as an agent/MCP tool.
        self.marker.unlink(missing_ok=True)
        return {"state": "resumed", "history_preserved": True}

    async def navigation(self):
        self.require_active()
        try:
            history = json.loads(self.history.read_text()) if self.history.exists() else []
            if not isinstance(history, list) or any(type(x) not in (int, float) for x in history):
                raise ValueError
        except (OSError, ValueError):
            self.pause("access_history_invalid")
            raise DomainError("browser_access_paused") from None
        now = self.clock()
        history = [t for t in history if t > now - 3600]
        if len(history) >= self.HOURLY_PAGE_LIMIT:
            raise DomainError("browser_hourly_budget_wait")
        delay = max(0, self.PAGE_INTERVAL - (now - history[-1])) if history else 0
        if delay:
            await self.sleep(delay)
        self.require_active()
        history.append(self.clock())
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.history.write_text(canonical(history))
        self.history.chmod(0o600)

    async def action(self):
        self.require_active()
        if self.last_action is not None:
            await self.sleep(max(0, self.ACTION_INTERVAL - (self.clock() - self.last_action)))
        self.require_active()
        self.last_action = self.clock()
