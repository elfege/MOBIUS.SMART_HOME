"""
In-memory ring buffer of the application's own log stream.

Powers the navbar "Live Logs" modal (operator directive 2026-07-12: Hubitat-
style live logs with per-source filters). A single RingLogHandler is attached
to the ROOT logger, so it captures every module logger in the process:
services (``services.matter_client`` …), per-instance app loggers
(``{AppClass}.{label}`` from apps/base/core.py — one source per running
automation instance, exactly the "running apps/drivers/processes" the filter
list shows), and app.py itself.

Design notes:
- deque(maxlen=...) ring: an all-night session cannot grow memory unbounded;
  the durable stream remains ``docker logs`` — this buffer is UI telemetry.
- Monotonic ``id`` per entry: the UI polls GET /api/logs/tail?after=<id> and
  only ever receives increments; filtering is client-side so filter flips are
  instant and need no cursor reset.
- The handler's own level is DEBUG, but effective verbosity is still governed
  by logger levels (basicConfig INFO) — the stream shows what the app actually
  logs, not more.
- uvicorn.access does not propagate to the root logger, so HTTP access noise
  is naturally excluded.
- emit() must NEVER raise: a logging handler that throws takes down the caller.
"""

import itertools
import logging
import re
import traceback
from collections import deque
from typing import Any, Dict, List, Optional

# Ring capacity — at a typical few lines/second this is well over an hour of
# history, and bounded regardless.
_MAX_ENTRIES = 8000

# Several modules color their terminal output with ANSI escapes; those bytes
# are noise in the UI stream, so strip them at capture time.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class RingLogHandler(logging.Handler):
    """logging.Handler that appends records to a bounded in-memory ring."""

    def __init__(self, capacity: int = _MAX_ENTRIES):
        super().__init__(level=logging.DEBUG)
        self._buf: "deque[Dict[str, Any]]" = deque(maxlen=capacity)
        self._ids = itertools.count(1)

    def emit(self, record: logging.LogRecord) -> None:
        """Append one record. Never raises (a throwing handler kills the caller)."""
        try:
            msg = _ANSI_RE.sub("", record.getMessage())
            if record.exc_info:
                msg += "\n" + "".join(
                    traceback.format_exception(*record.exc_info)).rstrip()
            self._buf.append({
                "id": next(self._ids),
                "ts": record.created,          # epoch seconds (float) — client formats
                "level": record.levelname,     # DEBUG/INFO/WARNING/ERROR/CRITICAL
                "src": record.name,            # logger name = the filterable "source"
                "msg": msg,
            })
        except Exception:  # noqa: BLE001 — logging must never break the app
            pass

    # -- read API (called from the /api/logs endpoints) ----------------------

    def tail(self, after_id: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
        """Entries with id > after_id, oldest first, capped at `limit` newest.
        list(deque) snapshot is atomic under the GIL — no explicit lock needed."""
        snapshot = list(self._buf)
        out = [e for e in snapshot if e["id"] > after_id]
        return out[-limit:] if len(out) > limit else out

    def head_id(self) -> int:
        """Newest id currently in the ring (0 if empty) — the poll cursor."""
        snapshot = list(self._buf)
        return snapshot[-1]["id"] if snapshot else 0

    def sources(self) -> List[Dict[str, Any]]:
        """Distinct logger names in the ring with entry counts, most-active
        first — the modal's 'running apps/drivers/processes' filter list."""
        counts: Dict[str, int] = {}
        for e in list(self._buf):
            counts[e["src"]] = counts.get(e["src"], 0) + 1
        return [{"src": s, "count": n}
                for s, n in sorted(counts.items(), key=lambda kv: -kv[1])]


_handler: Optional[RingLogHandler] = None


def install_ring_log_handler() -> RingLogHandler:
    """Attach the ring handler to the root logger (idempotent). Call once at
    app startup, right after logging.basicConfig()."""
    global _handler
    if _handler is None:
        _handler = RingLogHandler()
        logging.getLogger().addHandler(_handler)
        logging.getLogger(__name__).info("RingLogHandler installed (live-logs UI tap)")
    return _handler


def get_log_handler() -> RingLogHandler:
    """The installed handler (installing on first use if startup didn't)."""
    return _handler or install_ring_log_handler()
