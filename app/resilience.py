"""Timeouts + tenacity retries + a per-upstream circuit breaker."""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

import logfire
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_random_exponential

from app.errors import CircuitOpen
from app.observability.metrics import CIRCUIT_OPEN, CIRCUIT_TRIPS

T = TypeVar("T")


class CircuitBreaker:
    """closed -> open after N consecutive failures -> half-open after reset_s (single probe)."""

    def __init__(self, name: str, failures: int = 5, reset_s: float = 30.0, clock=time.monotonic):
        self.name, self.threshold, self.reset_s, self._clock = name, failures, reset_s, clock
        self._fails = 0
        self._opened_at: float | None = None
        self._probing = False
        self._g_open, self._c_trips = CIRCUIT_OPEN.labels(name), CIRCUIT_TRIPS.labels(name)
        self._g_open.set(0)  # export the series from the start so dashboards show "closed" instead of "no data"

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        return "half-open" if self._clock() - self._opened_at >= self.reset_s else "open"

    def before_call(self) -> None:
        st = self.state
        if st == "open" or (st == "half-open" and self._probing):
            raise CircuitOpen(f"circuit '{self.name}' is open")
        if st == "half-open":
            self._probing = True

    def release_probe(self) -> None:
        """A half-open probe was cancelled (client disconnect, shutdown): free the slot instead of wedging the breaker."""
        self._probing = False

    def record_success(self) -> None:
        was_tripped = self._opened_at is not None
        self._fails, self._opened_at, self._probing = 0, None, False
        if was_tripped:
            self._g_open.set(0)

    def record_failure(self) -> None:
        probe, was_tripped = self._probing, self._opened_at is not None
        self._probing = False
        self._fails += 1
        if self._fails >= self.threshold or was_tripped:
            self._opened_at = self._clock()
            if probe or not was_tripped:  # a real trip, not an in-flight call that failed after the breaker had already opened
                self._c_trips.inc()
            self._g_open.set(1)
            logfire.warn("circuit opened", upstream=self.name)


_HINT_RE = re.compile(r"(?:try again|retry) in\s+(?:(\d+)m)?\s*(\d+(?:\.\d+)?)\s*(ms|s)\b", re.I)


def retry_after_hint(exc: BaseException | None) -> float | None:
    """Seconds the provider asked us to wait: the Retry-After header (groq/httpx/google errors expose .response.headers)
    or the "Please try again in 10.095s" text Groq puts in 429 bodies. None if absent."""
    if exc is None:
        return None
    headers = getattr(getattr(exc, "response", None), "headers", None) or getattr(exc, "headers", None)
    try:
        if headers and (v := headers.get("retry-after") or headers.get("Retry-After")):
            return max(0.0, float(v))
    except (TypeError, ValueError, AttributeError):
        pass
    m = _HINT_RE.search(str(exc))
    if m:
        secs = float(m.group(2)) + (60 * int(m.group(1)) if m.group(1) else 0)
        return secs / 1000 if m.group(3).lower() == "ms" else secs
    return None


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, CircuitOpen):
        return False
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    text = str(exc).lower()
    return any(t in text for t in ("429", "resource_exhausted", "rate limit", "timed out", "timeout", "503", "502"))


async def call_upstream(
    breaker: CircuitBreaker,
    fn: Callable[[], Awaitable[T]],
    *,
    timeout_s: float,
    attempts: int = 3,
    wait_max_s: float = 4.0,
    retry_after_cap_s: float = 0.0,
) -> T:
    """Run fn() under a hard timeout; retry with jittered exponential backoff on 429/5xx/timeouts.

    The breaker counts one failure per *call* (after retries are exhausted), not per attempt.
    With `retry_after_cap_s` > 0 a provider hint (Retry-After / "try again in 10s") replaces the short jittered wait, capped at that value.
    """
    base_wait = wait_random_exponential(multiplier=0.25, max=wait_max_s)

    def wait(rs):
        w = base_wait(rs)
        if retry_after_cap_s > 0 and rs.outcome is not None:
            hint = retry_after_hint(rs.outcome.exception())
            if hint is not None:
                w = max(w, min(hint, retry_after_cap_s))
        return w

    breaker.before_call()
    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(attempts),
            wait=wait,
            retry=retry_if_exception(is_retryable),
            reraise=True,
        ):
            with attempt:
                result = await asyncio.wait_for(fn(), timeout=timeout_s)
        breaker.record_success()
        return result
    except CircuitOpen:
        raise
    except asyncio.CancelledError:
        breaker.release_probe()
        raise
    except BaseException:
        breaker.record_failure()
        raise


class Breakers:
    def __init__(self, failures: int, reset_s: float):
        self._b: dict[str, CircuitBreaker] = {}
        self._f, self._r = failures, reset_s

    def __getitem__(self, name: str) -> CircuitBreaker:
        if name not in self._b:
            self._b[name] = CircuitBreaker(name, self._f, self._r)
        return self._b[name]
