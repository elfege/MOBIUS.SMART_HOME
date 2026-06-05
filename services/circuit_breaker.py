"""
Circuit breaker — Phase E of the defensive-coding stack.

WHY THIS EXISTS
---------------
Phase B wrapped every blocking outbound HTTP call in asyncio.to_thread,
so a slow downstream can no longer hold the event loop. But the thread
pool is still finite (default min(32, cpu_count() + 4) ≈ 36 on this
host), and each slow request consumes a worker slot for the full
timeout duration. Under a degraded downstream — Hubitat hub flaky after
a firmware update, matter-server stuck on a Thread network problem —
every queued request still waits the full timeout before failing.

The user-visible symptom is "everything is slow." The structural
remedy is fail-fast: after N consecutive failures within a window,
**stop trying** for a cooldown period; let a single probe through; if
that succeeds, resume; if it fails, cool down again.

That's the circuit-breaker pattern, with three states:

    CLOSED      normal operation — calls pass through
       │
       │ N consecutive failures within fail_window_secs
       ▼
    OPEN        fail-fast — calls raise CircuitBreakerOpen immediately
       │
       │ reset_timeout_secs elapsed
       ▼
    HALF_OPEN   probe state — exactly one call allowed through
       │
       ├── success ──▶ CLOSED  (resume normal)
       └── failure ──▶ OPEN    (cool down again)

WHAT THIS MODULE PROVIDES
-------------------------
- `CircuitBreaker(name, fail_threshold, reset_timeout_secs,
                  fail_window_secs)`:
      State machine + counters. Methods to call sync or async-wrapped.
- `CircuitBreakerOpen`:
      Exception raised when a call is short-circuited (breaker is OPEN
      or another caller is currently probing HALF_OPEN). Callers can
      catch this and fall back to cached data, return a degraded
      response, etc.
- `CircuitBreakerRegistry`:
      Module-level dict of named breakers for observability. The
      future /api/health/breakers endpoint iterates this.

USAGE
-----
    from services.circuit_breaker import get_breaker, CircuitBreakerOpen

    breaker = get_breaker("hubitat:<LAN_IP>")

    # async path:
    try:
        result = await breaker.call_async(
            lambda: httpx_client.get(url, timeout=5)
        )
    except CircuitBreakerOpen:
        return cached_or_degraded_response()

    # sync path (used inside asyncio.to_thread or a thread executor):
    try:
        result = breaker.call_sync(
            lambda: requests.get(url, timeout=5)
        )
    except CircuitBreakerOpen:
        ...

The breaker treats any exception raised by the wrapped call as a
failure. A successful return (no exception) is a success. If you want
specific exception types to count and others not (e.g. count network
timeouts but not 4xx HTTP responses), wrap the call yourself and only
let the failure exceptions escape.

DESIGN CHOICES
--------------
- **Sync-friendly state machine.** The state transitions are pure
  in-memory bookkeeping with no awaits — the breaker is safe to call
  from sync code (e.g. inside `asyncio.to_thread`) and from async code
  alike. The two `call_sync`/`call_async` entry points differ only in
  how they invoke the wrapped callable.
- **Lock for state transitions.** A single `threading.Lock` guards
  state mutations so concurrent callers from multiple worker threads
  (Phase B `to_thread` dispatched) can't race the CLOSED→OPEN or
  HALF_OPEN→CLOSED transitions. The lock is held only for the brief
  bookkeeping window — never around the wrapped call.
- **No background tasks.** The breaker is a pure data structure
  driven by the call timestamps. No timer needs to fire to reset it;
  the next call checks `time.monotonic()` and transitions OPEN→HALF_OPEN
  if the cooldown has elapsed.
- **Per-instance breakers.** Each named breaker is independent —
  hub_72 degrading doesn't open hub_69's breaker. The registry
  exists so the operator (and the future /api/health/breakers
  endpoint) can survey all breakers in one place.
"""

import logging
import threading
import time
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class BreakerState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpen(Exception):
    """
    Raised when a call is refused because the breaker is OPEN (cooling
    down) or HALF_OPEN with a probe already in flight. The exception
    name is the breaker's name so the operator can correlate with the
    /api/health/breakers output.
    """
    def __init__(self, breaker_name: str, message: str = ""):
        self.breaker_name = breaker_name
        super().__init__(f"breaker '{breaker_name}' open: {message}".rstrip(": "))


class CircuitBreaker:
    """
    State-machine + counters. See module docstring for state diagram.

    Args:
        name:                  Identifier for logs + the registry +
                               /api/health/breakers.
        fail_threshold:        Consecutive failures within fail_window_secs
                               that trip the breaker CLOSED → OPEN. Default 5.
        reset_timeout_secs:    Cooldown after OPEN before transitioning
                               to HALF_OPEN. Default 30.
        fail_window_secs:      Rolling window for fail_threshold. If
                               failures are slow (one every minute),
                               we don't trip on noise. Default 60.
    """

    def __init__(
        self,
        name: str,
        *,
        fail_threshold: int = 5,
        reset_timeout_secs: float = 30.0,
        fail_window_secs: float = 60.0,
    ) -> None:
        self.name = name
        self.fail_threshold = fail_threshold
        self.reset_timeout_secs = reset_timeout_secs
        self.fail_window_secs = fail_window_secs

        # State + counters. Guarded by self._lock.
        self._state: BreakerState = BreakerState.CLOSED
        self._failure_count: int = 0
        self._first_failure_at: Optional[float] = None
        self._opened_at: Optional[float] = None
        self._last_failure_at: Optional[float] = None
        self._last_failure_reason: Optional[str] = None
        # HALF_OPEN admit-one-probe gate. Set when a caller passes the
        # OPEN→HALF_OPEN check; cleared by the probe's success/failure.
        self._probe_in_flight: bool = False

        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def call_sync(self, fn: Callable[[], Any]) -> Any:
        """
        Invoke `fn()` under breaker supervision. Synchronous version —
        use from sync code or from inside asyncio.to_thread.

        Raises CircuitBreakerOpen if the breaker is OPEN (or HALF_OPEN
        with a probe already in flight). Otherwise calls fn() and
        records success/failure.
        """
        self._before_call()
        try:
            result = fn()
        except Exception as exc:
            self._record_failure(exc)
            raise
        else:
            self._record_success()
            return result

    async def call_async(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        """
        Invoke an async `fn()` under breaker supervision. The callable
        must return an awaitable (e.g. a coroutine).
        """
        self._before_call()
        try:
            result = await fn()
        except Exception as exc:
            self._record_failure(exc)
            raise
        else:
            self._record_success()
            return result

    # ------------------------------------------------------------------
    # State inspection — used by /api/health/breakers
    # ------------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """
        Read-only snapshot of the breaker's current state. Safe to
        call from any thread. Used by the observability endpoint.
        """
        with self._lock:
            return {
                "name": self.name,
                "state": self._state.value,
                "failure_count": self._failure_count,
                "fail_threshold": self.fail_threshold,
                "reset_timeout_secs": self.reset_timeout_secs,
                "fail_window_secs": self.fail_window_secs,
                "opened_at_monotonic": self._opened_at,
                "last_failure_at_monotonic": self._last_failure_at,
                "last_failure_reason": self._last_failure_reason,
                "probe_in_flight": self._probe_in_flight,
                "secs_until_half_open": self._secs_until_half_open(),
            }

    def reset(self) -> None:
        """
        Manually force the breaker back to CLOSED + clear counters.
        For operator use (e.g. after fixing the downstream); not used
        on the happy path.
        """
        with self._lock:
            old_state = self._state
            self._state = BreakerState.CLOSED
            self._failure_count = 0
            self._first_failure_at = None
            self._opened_at = None
            self._probe_in_flight = False
        logger.info(
            "breaker '%s': manual reset (was %s)", self.name, old_state.value
        )

    # ------------------------------------------------------------------
    # Private state machine
    # ------------------------------------------------------------------

    def _before_call(self) -> None:
        """
        Gate that runs before every call. Either:
          - returns normally → caller proceeds with the wrapped call
          - raises CircuitBreakerOpen → caller short-circuits

        OPEN state may transition to HALF_OPEN here if the cooldown has
        elapsed; in HALF_OPEN, exactly one probe is admitted.
        """
        now = time.monotonic()
        with self._lock:
            if self._state is BreakerState.OPEN:
                # Has the cooldown elapsed? If yes, transition to
                # HALF_OPEN and let this caller through as the probe.
                if (self._opened_at is not None
                        and now - self._opened_at >= self.reset_timeout_secs):
                    self._state = BreakerState.HALF_OPEN
                    self._probe_in_flight = True
                    logger.info(
                        "breaker '%s': OPEN → HALF_OPEN (cooldown elapsed)",
                        self.name,
                    )
                    return
                # Still cooling down.
                raise CircuitBreakerOpen(
                    self.name,
                    f"cooling down, {self._secs_until_half_open():.1f}s left",
                )

            if self._state is BreakerState.HALF_OPEN:
                # In HALF_OPEN, at most one call (the probe) is admitted.
                # Concurrent callers fail-fast until the probe resolves.
                if self._probe_in_flight:
                    raise CircuitBreakerOpen(
                        self.name, "probe already in flight"
                    )
                self._probe_in_flight = True
                return

            # CLOSED — happy path, no gate.

    def _record_failure(self, exc: BaseException) -> None:
        """
        Update counters after a wrapped-call failure. Transitions
        CLOSED → OPEN if the failure threshold is reached, or
        HALF_OPEN → OPEN if the probe failed.
        """
        now = time.monotonic()
        with self._lock:
            self._last_failure_at = now
            self._last_failure_reason = f"{type(exc).__name__}: {exc!s}"[:200]

            if self._state is BreakerState.HALF_OPEN:
                # Probe failed → reopen.
                self._probe_in_flight = False
                self._state = BreakerState.OPEN
                self._opened_at = now
                logger.warning(
                    "breaker '%s': HALF_OPEN → OPEN (probe failed: %s)",
                    self.name, self._last_failure_reason,
                )
                return

            # CLOSED. Accumulate failures within the rolling window.
            if (self._first_failure_at is None
                    or now - self._first_failure_at > self.fail_window_secs):
                # Either this is the first failure, or the window expired
                # — restart the count from this failure.
                self._first_failure_at = now
                self._failure_count = 1
            else:
                self._failure_count += 1

            if self._failure_count >= self.fail_threshold:
                self._state = BreakerState.OPEN
                self._opened_at = now
                logger.warning(
                    "breaker '%s': CLOSED → OPEN (%d failures within %.0fs, last: %s)",
                    self.name,
                    self._failure_count,
                    self.fail_window_secs,
                    self._last_failure_reason,
                )

    def _record_success(self) -> None:
        """
        Update counters after a wrapped-call success. Closes the
        breaker if it was HALF_OPEN.
        """
        with self._lock:
            if self._state is BreakerState.HALF_OPEN:
                # Probe succeeded → close.
                self._probe_in_flight = False
                self._state = BreakerState.CLOSED
                self._failure_count = 0
                self._first_failure_at = None
                self._opened_at = None
                logger.info(
                    "breaker '%s': HALF_OPEN → CLOSED (probe succeeded)",
                    self.name,
                )
                return

            # CLOSED — a success resets the rolling failure window.
            self._failure_count = 0
            self._first_failure_at = None

    def _secs_until_half_open(self) -> Optional[float]:
        """
        Seconds remaining before the breaker transitions OPEN → HALF_OPEN.
        Returns None when not in OPEN state. Read inside the lock.
        """
        if self._state is not BreakerState.OPEN or self._opened_at is None:
            return None
        elapsed = time.monotonic() - self._opened_at
        remaining = self.reset_timeout_secs - elapsed
        return max(0.0, remaining)


# ----------------------------------------------------------------------
# Registry — single entry point so the breakers are observable
# ----------------------------------------------------------------------

_REGISTRY: Dict[str, CircuitBreaker] = {}
_REGISTRY_LOCK = threading.Lock()


def get_breaker(
    name: str,
    *,
    fail_threshold: int = 5,
    reset_timeout_secs: float = 30.0,
    fail_window_secs: float = 60.0,
) -> CircuitBreaker:
    """
    Get or lazily create the named breaker. Identifying breakers by
    name (e.g. "hubitat:<LAN_IP>", "matter-server") makes the
    /api/health/breakers output legible and lets each per-hub breaker
    open independently.

    The configuration args are applied only on FIRST creation — later
    calls with the same name return the existing breaker regardless of
    arg values. If you need to reconfigure, restart the process.
    """
    with _REGISTRY_LOCK:
        existing = _REGISTRY.get(name)
        if existing is not None:
            return existing
        breaker = CircuitBreaker(
            name,
            fail_threshold=fail_threshold,
            reset_timeout_secs=reset_timeout_secs,
            fail_window_secs=fail_window_secs,
        )
        _REGISTRY[name] = breaker
        return breaker


def all_breakers() -> Dict[str, CircuitBreaker]:
    """
    Snapshot of every registered breaker, keyed by name. Used by the
    /api/health/breakers endpoint to dump current state.
    """
    with _REGISTRY_LOCK:
        return dict(_REGISTRY)


__all__ = [
    "BreakerState",
    "CircuitBreaker",
    "CircuitBreakerOpen",
    "get_breaker",
    "all_breakers",
]
