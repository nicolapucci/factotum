"""Staying connected: asking again when the edge is lost, patiently, and not when it would be
wrong."""

import asyncio
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.client_core import (
    Backoff,
    ConnectionLost,
    CouldNotConnect,
    DeviceClient,
    Ending,
    Refused,
    stay_connected,
)
from factotum.client_core.testing import ClientRig, RecordingRenderer
from factotum.contracts.edge import CloseCode


class Scenario:
    """What happens when a client asks the edge for a connection, in order: each is a failure to
    make one (``CouldNotConnect``, ``Refused``), or how the connection that was made ended (None:
    the person left). It keeps the waits, since a test does not wait."""

    def __init__(
        self, *steps: CouldNotConnect | Refused | ConnectionLost | None, lasts_s: float = 1.0
    ) -> None:
        self._steps = list(steps)
        self._lasts_s = lasts_s
        self.asked = 0
        self.waits: list[float] = []
        self.shown = RecordingRenderer()
        self.clock = 0.0
        self.last: DeviceClient | None = None

    async def open_client(self) -> DeviceClient:
        self.asked += 1
        step = self._steps[self.asked - 1]
        if isinstance(step, CouldNotConnect | Refused):
            raise step
        self.last = ClientRig().client
        return self.last

    async def serve(self, client: DeviceClient) -> ConnectionLost | None:
        step = self._steps[self.asked - 1]
        self.clock += self._lasts_s
        assert not isinstance(step, CouldNotConnect | Refused)
        return step

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)

    def now(self) -> float:
        return self.clock

    async def run(self, *, backoff: Backoff = Backoff(jitter=0.0)) -> Any:  # noqa: B008
        return await stay_connected(
            self.open_client,
            self.serve,
            self.shown,
            backoff=backoff,
            sleep=self.sleep,
            chance=lambda: 0.5,
            now=self.now,
        )


class TestHowLongToWait:
    def test_the_first_wait_is_short_and_each_after_it_is_longer_up_to_a_limit(self) -> None:
        backoff = Backoff(first_s=1.0, max_s=10.0, factor=2.0, jitter=0.0)

        waits = [backoff.wait_s(failures, 0.5) for failures in range(1, 8)]

        assert waits == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0, 10.0]

    def test_a_little_chance_is_in_it_so_that_devices_do_not_all_ask_together(self) -> None:
        backoff = Backoff(first_s=10.0, jitter=0.25)

        assert backoff.wait_s(1, 0.0) == pytest.approx(7.5)
        assert backoff.wait_s(1, 0.5) == pytest.approx(10.0)
        assert backoff.wait_s(1, 1.0) == pytest.approx(12.5)

    def test_no_failure_yet_is_taken_for_the_first(self) -> None:
        assert Backoff(jitter=0.0).wait_s(0, 0.5) == Backoff(jitter=0.0).wait_s(1, 0.5)

    @given(
        failures=st.integers(0, 200),
        chance=st.floats(0.0, 1.0, exclude_max=True),
        jitter=st.floats(0.0, 0.9),
    )
    def test_however_long_it_has_gone_on_the_wait_stays_within_the_limit_and_its_chance(
        self, failures: int, chance: float, jitter: float
    ) -> None:
        backoff = Backoff(first_s=1.0, max_s=30.0, factor=2.0, jitter=jitter)

        wait = backoff.wait_s(failures, chance)

        assert 0 < wait <= 30.0 * (1 + jitter) + 1e-9
        assert wait >= 1.0 * (1 - jitter) - 1e-9

    @given(failures=st.integers(1, 100))
    def test_a_wait_is_never_shorter_than_the_one_before_it_without_chance(
        self, failures: int
    ) -> None:
        backoff = Backoff(jitter=0.0)

        assert backoff.wait_s(failures + 1, 0.5) >= backoff.wait_s(failures, 0.5)


class TestUntilThePersonLeaves:
    async def test_a_person_who_leaves_ends_it_with_no_waiting(self) -> None:
        scenario = Scenario(None)

        outcome = await scenario.run()

        assert outcome.ending is Ending.LEFT
        assert outcome.succeeded
        assert scenario.waits == []
        assert scenario.shown.of("notice") == []

    async def test_an_edge_that_cannot_be_reached_is_asked_again_a_little_later_each_time(
        self,
    ) -> None:
        scenario = Scenario(
            CouldNotConnect("could not connect to ws://edge: refused"),
            CouldNotConnect("could not connect to ws://edge: refused"),
            CouldNotConnect("could not connect to ws://edge: refused"),
            None,
        )

        outcome = await scenario.run()

        assert outcome.ending is Ending.LEFT
        assert scenario.asked == 4
        assert scenario.waits == [1.0, 2.0, 4.0]
        assert scenario.shown.of("notice")[0] == (
            "could not connect to ws://edge: refused; trying again in 1 s"
        )

    async def test_a_connection_that_was_lost_is_made_again_and_the_reason_is_said(self) -> None:
        scenario = Scenario(ConnectionLost(1006, ""), ConnectionLost(1001, "going away"), None)

        outcome = await scenario.run()

        assert outcome.ending is Ending.LEFT
        assert scenario.waits == [1.0, 2.0]
        assert scenario.shown.of("notice") == [
            "the connection ended (1006); trying again in 1 s",
            "the connection ended (1001): going away; trying again in 2 s",
        ]

    @pytest.mark.parametrize("code", [1001, 1006, 1011, int(CloseCode.IDLE)])
    async def test_what_the_edge_may_do_and_still_want_the_device_back_is_asked_again(
        self, code: int
    ) -> None:
        scenario = Scenario(ConnectionLost(code, ""), None)

        outcome = await scenario.run()

        assert (outcome.ending, scenario.asked) == (Ending.LEFT, 2)

    async def test_an_edge_that_ends_the_connection_before_it_said_ready_is_tried_again(
        self,
    ) -> None:
        scenario = Scenario(Refused("connection_lost", "the edge closed the connection"), None)

        outcome = await scenario.run()

        assert outcome.ending is Ending.LEFT
        assert scenario.shown.of("notice") == [
            "the edge closed the connection; trying again in 1 s"
        ]

    async def test_a_chance_in_the_wait_is_what_the_caller_gives_it(self) -> None:
        scenario = Scenario(ConnectionLost(1006, ""), None)

        await scenario.run(backoff=Backoff(first_s=10.0, jitter=0.5))

        assert scenario.waits == [10.0]  # a chance of a half is no more and no less


class TestWhenAskingAgainWouldBeWrong:
    @pytest.mark.parametrize("code", ["auth_failed", "unsupported_protocol", "invalid_frame"])
    async def test_a_device_that_the_edge_does_not_accept_is_not_asked_for_again(
        self, code: str
    ) -> None:
        scenario = Scenario(Refused(code, "no"))

        outcome = await scenario.run()

        assert outcome.ending is Ending.REFUSED
        assert not outcome.succeeded
        assert outcome.detail == f"{code}: no"
        assert scenario.asked == 1
        assert scenario.waits == []

    @pytest.mark.parametrize(
        ("code", "ending"),
        [
            (int(CloseCode.REPLACED), Ending.REPLACED),
            (int(CloseCode.UNSUPPORTED_VERSION), Ending.UNSUPPORTED),
            (int(CloseCode.POLICY), Ending.CLOSED),
        ],
    )
    async def test_a_connection_that_the_edge_has_no_use_for_is_not_asked_for_again(
        self, code: int, ending: Ending
    ) -> None:
        scenario = Scenario(ConnectionLost(code, "because"))

        outcome = await scenario.run()

        assert outcome.ending is ending
        assert outcome.detail == "because"
        assert scenario.asked == 1
        assert scenario.waits == []


class TestAConnectionThatHeld:
    async def test_connections_that_do_not_hold_make_the_waits_longer(self) -> None:
        scenario = Scenario(*[ConnectionLost(1006, "")] * 4, None, lasts_s=1.0)

        await scenario.run(backoff=Backoff(jitter=0.0, steady_s=30.0))

        assert scenario.waits == [1.0, 2.0, 4.0, 8.0]

    async def test_a_connection_that_held_for_a_while_starts_the_waits_over(self) -> None:
        scenario = Scenario(*[ConnectionLost(1006, "")] * 4, None, lasts_s=60.0)

        await scenario.run(backoff=Backoff(jitter=0.0, steady_s=30.0))

        assert scenario.waits == [1.0, 1.0, 1.0, 1.0]

    async def test_it_is_the_connection_that_held_that_counts_and_not_the_failures_before_it(
        self,
    ) -> None:
        scenario = Scenario(
            CouldNotConnect("down"),
            CouldNotConnect("down"),
            ConnectionLost(1006, ""),
            None,
            lasts_s=60.0,
        )

        await scenario.run(backoff=Backoff(jitter=0.0, steady_s=30.0))

        assert scenario.waits == [1.0, 2.0, 1.0]


class TestStopping:
    async def test_cancelling_it_while_it_waits_ends_it(self) -> None:
        asked = 0
        asking = asyncio.Event()

        async def open_client() -> DeviceClient:
            nonlocal asked
            asked += 1
            asking.set()
            raise CouldNotConnect("down")

        async def serve(client: DeviceClient) -> ConnectionLost | None:
            raise AssertionError("never connected")

        async def a_long_sleep(seconds: float) -> None:
            await asyncio.Event().wait()

        running = asyncio.create_task(
            stay_connected(open_client, serve, RecordingRenderer(), sleep=a_long_sleep)
        )
        await asyncio.wait_for(asking.wait(), 1)
        await asyncio.sleep(0)

        running.cancel()

        with pytest.raises(asyncio.CancelledError):
            await running
        assert asked == 1
