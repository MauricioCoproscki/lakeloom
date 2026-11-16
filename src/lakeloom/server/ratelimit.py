"""Token-bucket rate limit per principal (NFR-SEC-16).

One bucket per ``(principal type, subject)``: ``burst`` tokens, refilled at ``per_minute``
per minute. Per replica, in memory. Keys never hold tokens (NFR-SEC-11). A bucket idle for
:data:`IDLE_SECONDS` is dropped, which bounds memory. ``per_minute=0`` switches it off.
"""

import math
import threading
import time
from collections.abc import Callable

from lakeloom.core.callers import Caller
from lakeloom.server.auth import Principal

__all__ = ["IDLE_SECONDS", "RateLimiter", "principal_key"]

IDLE_SECONDS = 600.0
_SWEEP_EVERY = 60.0


def principal_key(p: Principal | Caller) -> tuple[str, str]:
    """``(type, subject)``: an agent and a human with the same ``sub`` do not share a bucket."""
    return (str(p.type), p.subject)  # PrincipalType is a StrEnum: str() is its value


class RateLimiter:
    def __init__(
        self, per_minute: int, burst: int, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._rate = per_minute / 60.0
        self._burst = float(max(burst, 1))
        self._clock = clock
        self.enabled = per_minute > 0
        self._buckets: dict[tuple[str, str], tuple[float, float]] = {}  # key -> (tokens, at)
        self._swept = clock()
        # check() is called from the event loop (MCP) and from threads: guard the buckets.
        self._lock = threading.Lock()

    @property
    def bucket_count(self) -> int:
        with self._lock:
            return len(self._buckets)

    def check(self, key: tuple[str, str]) -> int | None:
        """``None`` when allowed (one token taken); else whole seconds (>= 1) to wait."""
        if not self.enabled:
            return None
        with self._lock:
            return self._check(key)

    def _check(self, key: tuple[str, str]) -> int | None:
        now = self._clock()
        self._sweep(now)
        tokens, at = self._buckets.get(key, (self._burst, now))
        tokens = min(self._burst, tokens + max(0.0, now - at) * self._rate)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            return None
        self._buckets[key] = (tokens, now)
        return max(1, math.ceil((1.0 - tokens) / self._rate))

    def _sweep(self, now: float) -> None:
        if now - self._swept < _SWEEP_EVERY:
            return
        self._swept = now
        # Only a bucket that would be full again anyway: eviction never grants an early burst.
        stale = [
            k
            for k, (tokens, at) in self._buckets.items()
            if now - at >= IDLE_SECONDS and tokens + (now - at) * self._rate >= self._burst
        ]
        for k in stale:
            del self._buckets[k]
