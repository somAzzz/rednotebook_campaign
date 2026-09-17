"""Dedicated long-lived Chromium process; MCP clients detach without ending the session."""

import asyncio
import fcntl
import json
import os
import re
import subprocess
from pathlib import Path

import httpx

from rednotebook.errors import DomainError
from rednotebook.util import canonical


class BrowserSession:
    def __init__(self, profile: Path):
        self.profile = profile.resolve()
        self.metadata = self.profile / ".rednotebook-browser.json"
        self.process = None

    def read(self):
        if not self.metadata.exists():
            return None
        try:
            data = json.loads(self.metadata.read_text())
            if (
                type(data["pid"]) is not int
                or data["pid"] <= 1
                or type(data["port"]) is not int
                or not 1024 <= data["port"] <= 65535
                or not re.fullmatch(r"/devtools/browser/[a-zA-Z0-9-]+", data["endpoint"])
            ):
                raise ValueError
            os.kill(data["pid"], 0)
            return data
        except ProcessLookupError:
            return None
        except (KeyError, OSError, TypeError, ValueError):
            raise DomainError("browser_session_metadata_invalid") from None

    async def verify(self, data):
        try:
            async with httpx.AsyncClient(
                trust_env=False, timeout=2, follow_redirects=False
            ) as client:
                response = await client.get(f"http://127.0.0.1:{data['port']}/json/version")
                expected = f"ws://127.0.0.1:{data['port']}{data['endpoint']}"
                if (
                    response.status_code != 200
                    or response.json().get("webSocketDebuggerUrl") != expected
                ):
                    raise ValueError
            return expected
        except (httpx.HTTPError, ValueError):
            raise DomainError("browser_session_unreachable") from None

    async def connect(self, playwright, *, create=True, headless=False):
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.profile.chmod(0o700)
        with (self.profile / ".rednotebook-session.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DomainError("browser_session_busy") from None
            data = self.read()
            if not data:
                if not create:
                    raise DomainError("browser_session_not_running")
                # Never kill an existing profile owner or try to share its profile directory.
                singleton = self.profile / "SingletonLock"
                if singleton.is_symlink() or singleton.exists():
                    try:
                        pid = int(os.readlink(singleton).rsplit("-", 1)[-1])
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        pass
                    except (OSError, ValueError):
                        raise DomainError("browser_profile_busy") from None
                    else:
                        raise DomainError("browser_profile_busy")
                active_port = self.profile / "DevToolsActivePort"
                active_port.unlink(missing_ok=True)
                args = [
                    playwright.chromium.executable_path,
                    "--user-data-dir=" + str(self.profile),
                    "--remote-debugging-address=127.0.0.1",
                    "--remote-debugging-port=0",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--password-store=basic",
                    "--use-mock-keychain",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "about:blank",
                ]
                if headless:
                    args.insert(1, "--headless=new")
                self.process = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                try:
                    for _ in range(100):
                        if self.process.poll() is not None:
                            raise DomainError("browser_session_launch_failed")
                        if active_port.is_file():
                            lines = active_port.read_text().splitlines()
                            if len(lines) >= 2:
                                data = {
                                    "pid": self.process.pid,
                                    "port": int(lines[0]),
                                    "endpoint": lines[1],
                                }
                                await self.verify(data)
                                self.metadata.write_text(canonical(data))
                                self.metadata.chmod(0o600)
                                break
                        await asyncio.sleep(0.1)
                    else:
                        raise DomainError("browser_session_launch_timeout")
                except BaseException:
                    self.process.terminate()
                    raise
            endpoint = await self.verify(data)
            return await playwright.chromium.connect_over_cdp(
                endpoint, timeout=15000, no_defaults=True
            )

    async def shutdown(self, browser):
        session = await browser.new_browser_cdp_session()
        try:
            await session.send("Browser.close")
        finally:
            self.metadata.unlink(missing_ok=True)
            if self.process:
                for _ in range(50):
                    if self.process.poll() is not None:
                        break
                    await asyncio.sleep(0.1)
