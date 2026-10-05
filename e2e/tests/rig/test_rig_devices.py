"""The devices of the rig hand a recording to the edge the way they are told to, whole or in the
chunks a microphone sends, and say what the edge took it for."""

import pytest

from factotum.contracts.audio import AudioPayload, pcm_of
from factotum.e2e.devices import Carrier, Device
from factotum.e2e.voice import recording_of
from factotum.terminal.audio import AudioStream
from factotum.terminal.client import DeviceClient
from factotum.terminal.device import TerminalDevice
from factotum.terminal.profiles import get_profile
from factotum.terminal.testing import FakeVoiceOutput, MemoryConnection, RecordingRenderer

QUESTION = "How do I reset the router?"


def a_device(carrier: Carrier, *, timeout_s: float = 1.0) -> tuple[Device, MemoryConnection]:
    profile = get_profile("speaker")
    connection = MemoryConnection()
    shown = RecordingRenderer()
    hardware = TerminalDevice(profile.capabilities, shown)
    client = DeviceClient(connection, profile, shown, hardware)
    return Device(client, shown, FakeVoiceOutput(), hardware, timeout_s, carrier), connection


class TestHandingARecordingOver:
    async def test_a_clip_goes_whole_in_one_frame(self) -> None:
        device, connection = a_device(Carrier.CLIP)
        recording = recording_of(QUESTION)

        identity = await device.hands_over(recording)

        assert identity == "u1"
        assert [u.audio for u in connection.frames("audio_utterance")] == [recording]
        assert connection.frames("audio_start", "audio_chunk", "audio_end") == []

    async def test_a_stream_goes_in_chunks_that_are_the_same_samples(self) -> None:
        device, connection = a_device(Carrier.STREAM)
        recording = recording_of(QUESTION, seconds=1)  # a second of it: ten chunks and a bit

        identity = await device.hands_over(recording)

        assert identity == "u1"
        assert connection.frames("audio_utterance") == []
        start, *chunks, end = connection.frames("audio_start", "audio_chunk", "audio_end")
        pcm, rate = pcm_of(recording.data)
        assert (start.type, start.sample_rate, end.type) == ("audio_start", rate, "audio_end")
        assert len(chunks) > 1
        assert b"".join(chunk.data for chunk in chunks) == pcm

    @pytest.mark.parametrize("carrier", list(Carrier))
    async def test_the_answer_is_not_waited_for_when_a_recording_is_only_handed_over(
        self, carrier: Carrier
    ) -> None:
        device, _ = a_device(carrier)

        identity = await device.hands_over(recording_of(QUESTION))

        assert device.client.active == identity


class TestAMicrophone:
    async def next_of(self, device: Device, count: int) -> list[AudioPayload | AudioStream]:
        listening = aiter(device.microphone(QUESTION, "Where is it kept?").listen())
        return [await anext(listening) for _ in range(count)]

    async def test_a_device_that_sends_clips_has_a_microphone_that_hands_over_recordings(
        self,
    ) -> None:
        device, _ = a_device(Carrier.CLIP)

        heard = await self.next_of(device, 2)

        assert heard == [recording_of(QUESTION), recording_of("Where is it kept?")]

    async def test_one_that_streams_has_one_that_hands_over_the_same_samples_as_streams(
        self,
    ) -> None:
        device, _ = a_device(Carrier.STREAM)

        heard = await self.next_of(device, 2)

        assert all(isinstance(item, AudioStream) for item in heard)
        said = []
        for item in heard:
            assert isinstance(item, AudioStream)
            said.append((item.sample_rate, b"".join([chunk async for chunk in item.chunks])))
        assert said == [
            pcm_of(recording_of(QUESTION).data)[::-1],
            pcm_of(recording_of("Where is it kept?").data)[::-1],
        ]


class TestWhatItMadeOfIt:
    async def test_what_the_edge_said_it_heard_is_what_the_device_understood(self) -> None:
        device, _ = a_device(Carrier.CLIP)

        assert device.understood() == []
        device.shown.heard(QUESTION)
        device.shown.heard("and where is it kept?")

        assert device.understood() == [QUESTION, "and where is it kept?"]

    async def test_an_answer_that_never_came_is_not_waited_for_for_ever(self) -> None:
        device, _ = a_device(Carrier.CLIP, timeout_s=0.05)
        identity = await device.hands_over(recording_of(QUESTION))

        with pytest.raises(TimeoutError):
            await device.answered(identity)

    async def test_a_connection_that_ended_before_the_answer_is_said(self) -> None:
        device, _ = a_device(Carrier.CLIP)

        with pytest.raises(AssertionError, match="the connection ended before the answer did"):
            await device.answered("u99")  # nobody was waiting for it: the same as a connection gone
