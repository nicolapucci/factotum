"""Retrying a call that failed for a reason that may pass."""

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from factotum.contracts.errors import ProviderError, RateLimitedError


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 3
    """Total tries, the first included."""

    base_delay_s: float = 0.5
    max_delay_s: float = 8.0
    """The longest a retry may wait. A voice assistant cannot sit silent for a minute: when a
    provider asks for longer than this, the error is raised instead."""


async def with_retries[T](
    operation: Callable[[], Awaitable[T]],
    policy: RetryPolicy = RetryPolicy(),  # noqa: B008 - a frozen value, safe to share
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[], float] = random.random,
) -> T:
    """Run ``operation``, retrying it after a retryable ``ProviderError``.

    The wait grows exponentially with jitter, or follows the provider's ``Retry-After`` when it is
    short enough. Errors that are not retryable, and the last failure, are raised untouched.
    """
    attempt = 1
    while True:
        try:
            return await operation()
        except ProviderError as error:
            if not error.retryable or attempt >= policy.attempts:
                raise
            delay = _delay(error, attempt, policy, jitter)
            if delay is None:
                raise
            await sleep(delay)
            attempt += 1


def _delay(
    error: ProviderError, attempt: int, policy: RetryPolicy, jitter: Callable[[], float]
) -> float | None:
    if isinstance(error, RateLimitedError) and error.retry_after_s is not None:
        return error.retry_after_s if error.retry_after_s <= policy.max_delay_s else None
    backoff = min(policy.max_delay_s, policy.base_delay_s * 2.0 ** (attempt - 1))
    return backoff * (0.5 + jitter() / 2)
