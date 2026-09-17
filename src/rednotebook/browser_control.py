"""Persistent operator pause and conservative pacing; never a claim of platform-safe rates."""

import asyncio
import json
import time
from pathlib import Path

from rednotebook.errors import DomainError
from rednotebook.util import canonical


class AccessGate:
    PAGE_INTERVAL = 60
    ACTION_INTERVAL = 3
    HOURLY_PAGE_LIMIT = 10

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

    def status(self):
        result = {"paused": self.marker.exists(), "resume": "explicit_operator_cli_only"}
        if self.marker.exists():
            try:
                marker = json.loads(self.marker.read_text())
                # Persisted error codes are operational metadata, never exception text.
                code = marker.get("code", "")
                import re

                result["reason"] = (
                    code if re.fullmatch(r"[a-z_]{1,80}", code) else "pause_reason_unavailable"
                )
            except (OSError, ValueError, TypeError, AttributeError):
                result["reason"] = "pause_reason_unavailable"
        try:
            history = json.loads(self.history.read_text()) if self.history.exists() else []
            now = self.clock()
            history = [t for t in history if type(t) in (int, float) and t > now - 3600]
            result["remaining_hourly_navigations"] = max(0, self.HOURLY_PAGE_LIMIT - len(history))
            result["next_navigation_in_seconds"] = (
                max(0, self.PAGE_INTERVAL - (now - history[-1])) if history else 0
            )
        except (OSError, ValueError, TypeError):
            result["history_state"] = "invalid"
        return result

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
            self.pause("hourly_page_budget_exhausted")
            raise DomainError("browser_access_paused")
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
