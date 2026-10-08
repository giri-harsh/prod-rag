"""Timeouts + tenacity retries + a per-upstream circuit breaker."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

import logfire
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_random_exponential

from app.errors import CircuitOpen

T = TypeVar("T")


class CircuitBreaker:
    """closed -> open after N consecutive failures -> half-open after reset_s (single probe)."""

    def __init__(self, name: str, failures: int = 5, reset_s: float = 30.0, clock=time.monotonic):
        self.name, self.threshold, self.reset_s, self._clock = name, failures, reset_s, clock
        self._fails = 0
        self._opened_at: float | None = None
        self._probing = False

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
        self._fails, self._opened_at, self._probing = 0, None, False

    def record_failure(self) -> None:
        self._probing = False
        self._fails += 1
        if self._fails >= self.threshold or self._opened_at is not None:
            self._opened_at = self._clock()
            logfire.warn("circuit opened", upstream=self.name)


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
) -> T:
    """Run fn() under a hard timeout; retry with jittered exponential backoff on 429/5xx/timeouts.

    The breaker counts one failure per *call* (after retries are exhausted), not per attempt.
    """
    breaker.before_call()
    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(attempts),
            wait=wait_random_exponential(multiplier=0.25, max=wait_max_s),
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
