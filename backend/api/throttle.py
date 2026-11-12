"""A budget for the on-demand proxies' *uncached* upstream lookups.

The detail endpoints are plain GETs, so any page open in a browser on the LAN
can make this box issue them — and each cache miss is paid upstream on our
address (ESPN) or our key (five Finnhub calls per equity symbol, 60 a minute on
the free tier). The TTL caches only help with repeats; a caller walking through
fresh keys never hits them. Each endpoint therefore draws its misses from a
small token bucket and answers 429 once it is empty. Cached hits never touch it.
"""
from __future__ import annotations

import math
import time

from fastapi.responses import JSONResponse


def _now() -> float:
    return time.monotonic()  # patchable in tests


class Throttle:
    """Token bucket: `burst` lookups at once, one more every `refill_seconds`."""

    def __init__(self, burst: int, refill_seconds: float) -> None:
        self.burst = float(burst)
        self.refill_seconds = float(refill_seconds)
        self._tokens = self.burst
        self._at = _now()

    def _refill(self) -> None:
        now = _now()
        self._tokens = min(self.burst, self._tokens + (now - self._at) / self.refill_seconds)
        self._at = now

    def take(self) -> bool:
        """Spend one lookup; False when the budget is used up."""
        self._refill()
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True

    def retry_after(self) -> int:
        """Whole seconds until the next lookup is available."""
        self._refill()
        return max(1, math.ceil((1.0 - self._tokens) * self.refill_seconds))

    def refused(self) -> JSONResponse:
        # The display treats any non-2xx as "no extra detail" and asks again on
        # the next tap, like the 502 of a failed upstream fetch.
        return JSONResponse(
            {"error": "too many lookups — try again shortly"},
            status_code=429,
            headers={"Retry-After": str(self.retry_after())},
        )
