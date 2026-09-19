"""Cooperative deadlines and conservative suspend/clock-change detection.

No userspace watchdog runs while the host sleeps. Clock divergence is evidence
of a timing discontinuity, not proof of sleep (wall time can be adjusted).
"""

import asyncio
import time
from contextlib import asynccontextmanager

from rednotebook.errors import DomainError


def clock_gap(wall_start, monotonic_start):
    return time.time() - wall_start - (time.monotonic() - monotonic_start)


@asynccontextmanager
async def browser_deadline(seconds, error_code):
    wall_start, mono_start = time.time(), time.monotonic()
    try:
        async with asyncio.timeout(seconds):
            yield
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if abs(clock_gap(wall_start, mono_start)) > 10:
            raise DomainError("system_suspend_or_clock_change_suspected") from None
        if isinstance(exc, TimeoutError):
            raise DomainError(error_code) from None
        raise
    else:
        if abs(clock_gap(wall_start, mono_start)) > 10:
            raise DomainError("system_suspend_or_clock_change_suspected")
