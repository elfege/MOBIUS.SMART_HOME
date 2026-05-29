"""
HTTP sync-call offload — defensive wrapper around the blocking `requests`
library so the asyncio event loop is never held by a sync syscall.

WHY THIS EXISTS
---------------
The codebase has ~60 call sites that use `requests.get/post/patch/...` to
talk to PostgREST and the Hubitat hubs. Many of those callers were correctly
defensive enough to pass `timeout=5` (or similar) — but a synchronous
`requests.get(...)` invoked from inside an `async def` blocks the event
loop for the full timeout duration, regardless of the timeout value. Five
seconds is enough to make the UI look frozen; a few of them piled up under
load and the loop visibly stalled (see 2026-05-27 incident).

WHAT THIS DOES
--------------
Routes each HTTP method through `asyncio.to_thread(...)` so the blocking
call runs on a worker thread and the event loop keeps spinning. Also
back-fills a `DEFAULT_TIMEOUT` for any caller that forgot one — a missing
timeout on a blocking HTTP call is the worst-of-both-worlds case (loop
blocked, no upper bound).

USAGE
-----
    from services.http_sync_offload import aget, apost, apatch, adelete, aput

    r = await aget(f"{pg}/devices", params={"capability": "Switch"}, timeout=5)
    r.raise_for_status()

The returned object is a plain `requests.Response` — `.json()`, `.text`,
`.status_code`, `.raise_for_status()` all behave identically to the
synchronous API. Only the dispatch is moved off the loop.

NOTES ON THE THREAD POOL
------------------------
`asyncio.to_thread` uses the default loop executor, a ThreadPoolExecutor
whose `max_workers` defaults to `min(32, os.cpu_count() + 4)`. On a 56-core
host that's 36 threads — plenty of headroom for a single-user app. If you
ever see thread-pool starvation under burst load, switch to a dedicated
`concurrent.futures.ThreadPoolExecutor` and pass it to
`loop.run_in_executor(...)` instead.

LONG-TERM
---------
The idiomatic replacement is `httpx.AsyncClient` (already used in a few
places). This module is the bridge — it lets us protect the loop today
without rewriting every call site. New code should prefer httpx.
"""

import asyncio
from typing import Any

import requests

# Hard upper bound applied when a caller didn't specify a timeout. Picked
# generously enough to accommodate slow PostgREST queries against large
# tables but low enough that a hung remote can't tie up a worker thread for
# minutes. Override per-call via the `timeout=` kwarg.
DEFAULT_TIMEOUT: float = 10.0


async def _arequest(method: str, url: str, **kw: Any) -> requests.Response:
    """
    Common dispatch: ensure a timeout exists, run the sync call on a worker
    thread, return the raw `requests.Response`. The caller is responsible
    for `raise_for_status()` / status-code checks, exactly as with the
    synchronous API — this wrapper deliberately doesn't change semantics.
    """
    kw.setdefault("timeout", DEFAULT_TIMEOUT)
    fn = getattr(requests, method)
    return await asyncio.to_thread(fn, url, **kw)


async def aget(url: str, **kw: Any) -> requests.Response:
    """`requests.get` on a worker thread. Default timeout enforced if absent."""
    return await _arequest("get", url, **kw)


async def apost(url: str, **kw: Any) -> requests.Response:
    """`requests.post` on a worker thread. Default timeout enforced if absent."""
    return await _arequest("post", url, **kw)


async def apatch(url: str, **kw: Any) -> requests.Response:
    """`requests.patch` on a worker thread. Default timeout enforced if absent."""
    return await _arequest("patch", url, **kw)


async def aput(url: str, **kw: Any) -> requests.Response:
    """`requests.put` on a worker thread. Default timeout enforced if absent."""
    return await _arequest("put", url, **kw)


async def adelete(url: str, **kw: Any) -> requests.Response:
    """`requests.delete` on a worker thread. Default timeout enforced if absent."""
    return await _arequest("delete", url, **kw)


async def ahead(url: str, **kw: Any) -> requests.Response:
    """`requests.head` on a worker thread. Default timeout enforced if absent."""
    return await _arequest("head", url, **kw)


__all__ = ["aget", "apost", "apatch", "aput", "adelete", "ahead", "DEFAULT_TIMEOUT"]
