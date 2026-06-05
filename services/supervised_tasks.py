"""
Supervised background task spawn helper — Phase D of the defensive-coding stack.

WHY THIS EXISTS
---------------
`asyncio.create_task(coro)` returns a Task. Two failure modes that the bare
API exposes:

1. **GC collection of fire-and-forget tasks.** If the caller doesn't keep a
   strong reference to the Task object, Python's garbage collector can
   collect it mid-execution. The CPython docs warn explicitly:
       "Save a reference to the result of [create_task], to avoid the
        task disappearing mid-execution."
   In this codebase we have ~10 call sites that wrote
   `asyncio.create_task(coro())` without storing the result — every one
   was a latent silent-cancellation bug.

2. **Silent exception swallow.** A Task that crashes with an exception
   surfaces nothing unless someone awaits it or calls `.exception()`. For
   long-lived background tasks (eventsocket reconnect loop, per-instance
   workers, the loop watchdog itself) a crash is invisible — the task
   just stops doing its job and the symptom appears hours later as
   stale data or a wedged loop.

WHAT THIS DOES
--------------
`supervised_spawn(coro, *, name, on_done=None)` wraps `asyncio.create_task`
with two guarantees:

- The returned Task is also recorded in a module-level set so the GC
  cannot collect it. The reference is removed automatically when the
  task completes (via the done-callback). Caller does NOT need to keep
  their own reference; storing one is harmless but no longer required.
- An `add_done_callback` is attached that LOGS any uncaught exception
  as ERROR with the task name. Cancellations are noted at INFO. A clean
  completion is silent. If the caller passes `on_done`, that callable
  is invoked with the Task after the supervision logging — useful for
  per-task cleanup (closing a connection, releasing a lock, etc.).

USAGE
-----
    from services.supervised_tasks import supervised_spawn

    # Fire-and-forget (no caller reference needed):
    supervised_spawn(broadcaster.broadcast(payload),
                     name="e2e-broadcast")

    # Long-lived stored task (caller keeps reference too; helper's set
    # still holds it for done-callback dispatch):
    self._listen_task = supervised_spawn(self._listen_loop(),
                                          name="matter-listen-loop")

    # With per-task cleanup:
    supervised_spawn(child_coro(),
                     name="child",
                     on_done=lambda t: parent.release_slot())

The Task's name is what `asyncio.all_tasks()` reports — picking a
descriptive one makes debugging "what's pending in the loop" answerable
without traceback archaeology.

DESIGN NOTES
------------
The module-level `_BACKGROUND_TASKS` set is intentionally a plain `set`,
not a `WeakSet`. A WeakSet would allow the same GC collection we're
trying to prevent — defeating point #1. The done-callback that removes
the entry runs before the Task is garbage-collected, so the set never
grows unbounded.

This module is the Phase D generalization of the pattern
`services/loop_watchdog.py` introduced in Phase C (the watchdog already
attaches `_log_if_failed` as a done-callback). After Phase D, every
background task in the codebase uses this same supervision wrapper;
loop_watchdog will be migrated to use supervised_spawn too.
"""

import asyncio
import logging
from typing import Any, Callable, Coroutine, Optional, Set

logger = logging.getLogger(__name__)

# Strong-reference set: keeps fire-and-forget tasks alive past the caller's
# stack frame. Entries removed by the done-callback. Module-global so one
# set per process — there is exactly one event loop per process, so a per-
# loop registry isn't necessary.
_BACKGROUND_TASKS: Set[asyncio.Task] = set()


def _supervisor_done_callback(
    task: asyncio.Task,
    *,
    on_done: Optional[Callable[[asyncio.Task], Any]] = None,
) -> None:
    """
    Done-callback installed on every supervised task. Surfaces uncaught
    exceptions as ERROR logs (so silent task death becomes loud), drops
    the task from _BACKGROUND_TASKS (so the set doesn't grow without
    bound), and invokes the caller's optional on_done after.
    """
    # Drop the strong reference regardless of completion mode so GC can
    # eventually reclaim the Task object after the callback returns.
    _BACKGROUND_TASKS.discard(task)

    # Resolve a usable name. asyncio.Task.get_name() always returns
    # something (it defaults to "Task-N" if no explicit name was given),
    # but our convention is that supervised_spawn callers ALWAYS pass a
    # descriptive name, so the default would itself be a smell.
    task_name = task.get_name()

    if task.cancelled():
        # Cancellation is a normal-shutdown signal in most cases (e.g.
        # lifespan teardown). Log at INFO so the operator sees it
        # happened without it looking like a bug.
        logger.info("supervised task cancelled: %s", task_name)
    else:
        exc = task.exception()
        if exc is not None:
            # The interesting case: the task died with an uncaught
            # exception. Without this log line the failure would be
            # invisible. exc_info=exc preserves the original traceback
            # for the log handler.
            logger.error(
                "supervised task died with uncaught exception: %s",
                task_name,
                exc_info=exc,
            )

    # Per-task cleanup hook (e.g. close a connection, release a slot).
    # Runs after the supervision logging so a crash inside on_done
    # doesn't mask the original failure.
    if on_done is not None:
        try:
            on_done(task)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "supervised task on_done callback raised: %s (task=%s)",
                e,
                task_name,
                exc_info=True,
            )


def supervised_spawn(
    coro: Coroutine,
    *,
    name: str,
    on_done: Optional[Callable[[asyncio.Task], Any]] = None,
) -> asyncio.Task:
    """
    Spawn `coro` as a supervised asyncio.Task.

    Guarantees over `asyncio.create_task(coro)`:
      - Task is kept alive past the caller's stack frame (no GC collection
        of fire-and-forget tasks).
      - Uncaught exceptions in the task surface as ERROR log lines with
        the task name and traceback.
      - Cancellations log at INFO.
      - Optional `on_done(task)` cleanup hook runs after supervision logs.

    Args:
        coro:    The coroutine to schedule. Must be a coroutine object
                 (calling an `async def`), not the async function itself.
        name:    Descriptive task name. Surfaces in `asyncio.all_tasks()`
                 and in the supervision log lines. REQUIRED — bare-default
                 "Task-N" names defeat the debuggability win.
        on_done: Optional callable invoked with the completed Task after
                 the supervision logging. Use for per-task cleanup.

    Returns:
        The asyncio.Task. Caller may store the reference (e.g. for
        explicit cancel later) or discard it — either is safe.
    """
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(
        lambda t: _supervisor_done_callback(t, on_done=on_done)
    )
    return task


def background_task_count() -> int:
    """
    Number of supervised tasks currently in flight. Used by the watchdog
    and by /api/health for observability — a slow drift upward is the
    fingerprint of a leak (tasks being spawned faster than they finish).
    """
    return len(_BACKGROUND_TASKS)


def background_task_names() -> list[str]:
    """
    List of (name, state) snapshots of the in-flight supervised tasks.
    For ad-hoc debugging — should not be called on a hot path.
    """
    return [t.get_name() for t in _BACKGROUND_TASKS if not t.done()]


__all__ = [
    "supervised_spawn",
    "background_task_count",
    "background_task_names",
]
