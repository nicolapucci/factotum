from collections.abc import Awaitable, Callable

import pytest

from factotum.contracts.errors import (
    InvalidRequestError,
    ProviderAuthError,
    ProviderUnavailableError,
    RateLimitedError,
)
from factotum.model_gateway.retry import RetryPolicy, with_retries

FULL_JITTER = 1.0  # backoff * (0.5 + 1.0 / 2) == backoff


class Recorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)


def flaky(*outcomes: Exception | str) -> Callable[[], Awaitable[str]]:
    """An operation that fails or succeeds as told, one outcome per call."""
    remaining = list(outcomes)

    async def operation() -> str:
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return operation


def constant(value: float) -> Callable[[], float]:
    return lambda: value


async def run(
    operation: Callable[[], Awaitable[str]],
    recorder: Recorder,
    *,
    attempts: int = 3,
    base_delay_s: float = 0.5,
    max_delay_s: float = 8.0,
) -> str:
    policy = RetryPolicy(attempts=attempts, base_delay_s=base_delay_s, max_delay_s=max_delay_s)
    return await with_retries(operation, policy, sleep=recorder.sleep, jitter=constant(FULL_JITTER))


async def test_a_call_that_works_is_not_delayed() -> None:
    recorder = Recorder()

    assert await run(flaky("ok"), recorder) == "ok"
    assert recorder.delays == []


async def test_a_transient_failure_is_retried_with_a_growing_delay() -> None:
    recorder = Recorder()
    operation = flaky(ProviderUnavailableError("down"), ProviderUnavailableError("down"), "ok")

    assert await run(operation, recorder, attempts=3, base_delay_s=0.5) == "ok"
    assert recorder.delays == [0.5, 1.0]


async def test_the_last_failure_is_raised_when_the_attempts_run_out() -> None:
    recorder = Recorder()
    operation = flaky(ProviderUnavailableError("one"), ProviderUnavailableError("two"))

    with pytest.raises(ProviderUnavailableError, match="two"):
        await run(operation, recorder, attempts=2)

    assert len(recorder.delays) == 1


@pytest.mark.parametrize("error", [ProviderAuthError("no"), InvalidRequestError("bad")])
async def test_an_error_that_cannot_pass_is_raised_at_once(error: Exception) -> None:
    recorder = Recorder()

    with pytest.raises(type(error)):
        await run(flaky(error, "never reached"), recorder)

    assert recorder.delays == []


async def test_a_short_retry_after_is_honoured() -> None:
    recorder = Recorder()
    operation = flaky(RateLimitedError("slow down", retry_after_s=2.0), "ok")

    assert await run(operation, recorder) == "ok"
    assert recorder.delays == [2.0]


async def test_a_retry_after_longer_than_we_can_wait_is_raised_instead() -> None:
    recorder = Recorder()
    operation = flaky(RateLimitedError("slow down", retry_after_s=30.0), "never reached")

    with pytest.raises(RateLimitedError):
        await run(operation, recorder, max_delay_s=8.0)

    assert recorder.delays == []


async def test_a_rate_limit_without_a_hint_falls_back_to_backoff() -> None:
    recorder = Recorder()

    assert (
        await run(flaky(RateLimitedError("slow down"), "ok"), recorder, base_delay_s=0.25) == "ok"
    )
    assert recorder.delays == [0.25]


async def test_the_backoff_never_exceeds_the_maximum() -> None:
    recorder = Recorder()
    outcomes: list[Exception | str] = [ProviderUnavailableError("down") for _ in range(5)]
    outcomes.append("ok")

    await run(flaky(*outcomes), recorder, attempts=6, base_delay_s=1.0, max_delay_s=3.0)

    assert recorder.delays == [1.0, 2.0, 3.0, 3.0, 3.0]


async def test_jitter_spreads_the_delay_between_half_and_all_of_it() -> None:
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    for jitter in (0.0, 1.0):
        await with_retries(
            flaky(ProviderUnavailableError("down"), "ok"),
            RetryPolicy(base_delay_s=1.0),
            sleep=sleep,
            jitter=constant(jitter),
        )

    assert delays == [0.5, 1.0]


async def test_errors_that_are_not_provider_errors_are_not_retried() -> None:
    recorder = Recorder()

    with pytest.raises(ValueError, match="bug"):
        await run(flaky(ValueError("bug"), "never reached"), recorder)

    assert recorder.delays == []
