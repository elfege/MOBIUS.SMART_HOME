"""
Event-loop liveness watchdog.

A background coroutine ticks every TICK_INTERVAL_SECS, recording the
monotonic time of each tick. The `/api/health` endpoint consults this
to verify the event loop is actually scheduling tasks, not just that
the HTTP server's socket accepts connections.

WHY THIS EXISTS
---------------
The pre-existing `/api/health` returns a static `{"status": "ok"}`. That
already fails if the loop is fully wedged — nothing can build and send
the response — and Phase A's Docker healthcheck + autoheal sidecar will
restart the container when it does. But "fully wedged" is the late
stage; the loop is usually visibly degraded for tens of seconds before
that, holding open connections, dropping events, and rendering the UI
unusable.

This module narrows the detection window: the loop must prove it's
processing tasks every TICK_INTERVAL_SECS. If it doesn't tick within
ALIVE_THRESHOLD_SECS, the next health probe returns 503, Docker reports
the container unhealthy, and autoheal restarts it — well before the
loop is in the late-stage "no HTTP at all" state.

DESIGN
------
- One background asyncio.Task per process. There is exactly one event
  loop per process, so module-level state is fine (no per-loop registry).
- The task records `time.monotonic()` and `asyncio.sleep(N)` in a loop.
  If the loop is starved (sync syscall holding the thread, deadlock,
  etc.), the sleep doesn't return on schedule and `_last_tick_monotonic`
  goes stale.
- `start_watchdog()` is idempotent. It attaches a done-callback that
  logs any unexpected exception — the watchdog must not silently die.
  (Phase D will generalize the supervised-task pattern across the
  codebase; the watchdog is the first user.)

USAGE
-----
    from services.loop_watchdog import start_watchdog, stop_watchdog, is_loop_alive

    # In FastAPI lifespan startup:
    start_watchdog()

    # In FastAPI lifespan shutdown:
    stop_watchdog()

    # In /api/health:
    if not is_loop_alive():
        raise HTTPException(503, "event loop degraded")
"""

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# How often the watchdog ticks. Short enough that a stall is detected
# within one autoheal poll cycle (~10s); long enough that the tick
# itself imposes negligible overhead on a busy loop.
TICK_INTERVAL_SECS: float = 2.0

# `/api/health` returns 503 when no tick has happened within this
# window. Picked so that one missed tick is noise (network blip, brief
# CPU spike during scheduler fan-out) but a genuine stall surfaces fast.
ALIVE_THRESHOLD_SECS: float = 10.0

# Module-level state. One event loop per Python process, so a singleton
# without a registry is correct here.
_last_tick_monotonic: Optional[float] = None
_watchdog_task: Optional[asyncio.Task] = None


async def _watchdog_loop() -> None:
    """
    Tick forever. Each iteration records the current monotonic time and
    yields control back to the loop for TICK_INTERVAL_SECS. If the loop
    is held by sync I/O or a deadlock, the sleep does not return on
    schedule and `_last_tick_monotonic` goes stale — that staleness is
    what `is_loop_alive` checks.
    """
    global _last_tick_monotonic
    logger.info(
        f"loop_watchdog: started (tick every {TICK_INTERVAL_SECS}s, "
        f"alive threshold {ALIVE_THRESHOLD_SECS}s)"
    )
    while True:
        try:
            _last_tick_monotonic = time.monotonic()
            await asyncio.sleep(TICK_INTERVAL_SECS)
        except asyncio.CancelledError:
            logger.info("loop_watchdog: cancelled, stopping")
            return
        except Exception as e:
            # Defensive: a healthy watchdog must outlive every transient
            # bug in this loop. Log and keep going so we don't stop
            # ticking while the application is otherwise fine.
            logger.error(
                f"loop_watchdog: unexpected error: {e!r}", exc_info=True
            )
            await asyncio.sleep(TICK_INTERVAL_SECS)


def _log_if_failed(task: asyncio.Task) -> None:
    """
    asyncio fire-and-forget tasks silently swallow exceptions unless
    someone retrieves them. Attach this as a done-callback so a watchdog
    crash is visible immediately rather than being noticed only when the
    container goes unhealthy and autoheal can't tell us why.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            f"loop_watchdog: task died unexpectedly: {exc!r}", exc_info=exc
        )


def start_watchdog() -> None:
    """
    Start the watchdog task. Idempotent — calling this more than once
    while a healthy task is already running is a no-op, so it's safe to
    invoke from lifespan startup without guarding the call.
    """
    global _watchdog_task
    if _watchdog_task is not None and not _watchdog_task.done():
        logger.debug("loop_watchdog: start_watchdog called but already running")
        return
    _watchdog_task = asyncio.create_task(
        _watchdog_loop(), name="loop_watchdog"
    )
    _watchdog_task.add_done_callback(_log_if_failed)


def stop_watchdog() -> None:
    """
    Cancel the watchdog task. Called from lifespan shutdown. Idempotent.
    """
    global _watchdog_task
    if _watchdog_task is not None and not _watchdog_task.done():
        _watchdog_task.cancel()
    _watchdog_task = None
    # Reset the last-tick state so a fresh start (e.g. in tests) doesn't
    # see a stale tick from a prior incarnation.
    global _last_tick_monotonic
    _last_tick_monotonic = None


def last_tick_age_seconds() -> Optional[float]:
    """
    Seconds since the watchdog last ticked, or None if it never ticked
    (process just started, or watchdog wasn't started at all).
    """
    if _last_tick_monotonic is None:
        return None
    return time.monotonic() - _last_tick_monotonic


def is_loop_alive(threshold: float = ALIVE_THRESHOLD_SECS) -> bool:
    """
    True if the watchdog ticked within `threshold` seconds. False means
    the event loop is wedged, starved, or the watchdog never started.

    Special case: during the first TICK_INTERVAL_SECS of process life
    the watchdog may not have ticked yet. Callers that want to avoid a
    boot-time 503 should treat `last_tick_age_seconds() is None` as
    "still warming up" rather than "dead" — but for the simple health
    probe, returning False here is correct: the autoheal start_period
    (30s) already suppresses early restarts.
    """
    age = last_tick_age_seconds()
    if age is None:
        return False
    return age <= threshold


__all__ = [
    "start_watchdog",
    "stop_watchdog",
    "last_tick_age_seconds",
    "is_loop_alive",
    "TICK_INTERVAL_SECS",
    "ALIVE_THRESHOLD_SECS",
]
