"""The voice of an answer, sent to a device as a stream, end to end (ADR 0023): a question is asked
by voice, the services answer it, and what a device is sent over a real socket is the words and,
with them, the sound of each sentence, in chunks, before the turn ends.

A probe stands for the device (``factotum.e2e.probe``): these tests are about what the edge sent,
and in which order, and not about what a client made of it. The services here hand a response over
when it is whole, so the sound of a sentence is not seen arriving *while* it is made (the edge's
own tests on a real socket, and ``test_e2e_voice_wire``, show that); what is shown is that all of it
is right, from the model that wrote the answer to the last chunk, and that what is made slowly does
not hold back the words nor survive a person speaking over it.
"""

from factotum.contracts.edge import Feature, TurnStatus
from factotum.contracts.knowledge import CollectionOwner
from factotum.e2e.devices import until
from factotum.e2e.house import running
from factotum.e2e.models import ROUTER, SPOKEN_ANSWER
from factotum.e2e.probe import probe
from factotum.e2e.voice import StandInPiper, recording_of, said_in

QUESTION = "How do I reset the router?"
FOLLOW_UP = "What is the reset button for?"

A_TENTH_OF_A_SECOND = 22_050 * 2 // 10
"""What the model gateway puts in a chunk of the voice of the stand-in of Piper, which speaks at
22.05 kHz: a tenth of a second of 16-bit samples."""


class TestAVoiceAsAStream:
    async def test_a_question_is_answered_with_its_words_and_with_a_voice_that_is_streamed(
        self,
    ) -> None:
        async with running() as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with probe(house) as device:
                turn = await device.turn(await device.says(QUESTION))

                assert turn.ended.status is TurnStatus.FINISHED, turn.ended
                assert device.ready.features == {Feature.VOICE_STREAM}
                spoken = turn.spoken()
                assert [part.text for _, part in spoken] == SPOKEN_ANSWER
                assert [part.audio for _, part in spoken] == [None, None]  # the sound is apart
                [track] = turn.sound()
                assert (track.sample_rate, track.ended) == (22_050, True)
                assert [track.said(seq) for seq, _ in spoken] == SPOKEN_ANSWER
                assert house.piper.spoken == SPOKEN_ANSWER  # asked for one sentence at a time

    async def test_the_words_come_before_their_sound_and_the_sound_ends_before_the_turn(
        self,
    ) -> None:
        async with running() as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with probe(house) as device:
                turn = await device.turn(await device.says(QUESTION))

                where = {f.seq: n for n, f in enumerate(turn.frames) if f.type == "part"}
                chunks = [(n, f) for n, f in enumerate(turn.frames) if f.type == "voice_chunk"]
                assert chunks
                assert all(where[f.part_seq] < n for n, f in chunks)
                start = turn.kinds.index("voice_start")
                assert start < chunks[0][0]
                assert turn.kinds[-2:] == ["voice_end", "turn_ended"]
                assert turn.kinds.count("voice_start") == 1  # one voice, one track

    async def test_the_sound_is_in_chunks_of_a_tenth_of_a_second_with_no_gap_between_them(
        self,
    ) -> None:
        async with running() as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with probe(house) as device:
                turn = await device.turn(await device.says(QUESTION))

                chunks = turn.of("voice_chunk")
                assert [c.seq for c in chunks] == list(range(len(chunks)))
                assert len(chunks) > len(SPOKEN_ANSWER)  # a sentence is more than one chunk
                assert all(0 < len(c.data) <= A_TENTH_OF_A_SECOND for c in chunks)
                assert all(len(c.data) % 2 == 0 for c in chunks)  # whole samples

    async def test_a_device_that_did_not_ask_for_a_voice_is_sent_a_clip_in_each_part(self) -> None:
        async with running() as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with probe(house, features=()) as device:
                turn = await device.turn(await device.says(QUESTION))

                assert device.ready.features == frozenset()
                assert turn.ended.status is TurnStatus.FINISHED
                assert not turn.of("voice_start", "voice_chunk", "voice_end")
                clips = [part.audio for _, part in turn.spoken()]
                assert all(clip is not None for clip in clips)
                assert [said_in(clip) for clip in clips if clip] == SPOKEN_ANSWER

    async def test_without_a_voice_bound_the_words_are_sent_and_there_is_no_sound(self) -> None:
        async with running(text_to_speech=False) as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with probe(house) as device:
                turn = await device.turn(await device.says(QUESTION))

                assert turn.ended.status is TurnStatus.FINISHED
                assert [part.text for _, part in turn.spoken()] == SPOKEN_ANSWER
                assert not turn.of("voice_start", "voice_chunk", "voice_end")
                assert house.piper.spoken == []

    async def test_what_is_typed_is_answered_with_a_voice_as_well(self) -> None:
        async with running() as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with probe(house) as device:
                turn = await device.turn(await device.types(QUESTION))

                [track] = turn.sound()
                assert [track.said(seq) for seq, _ in turn.spoken()] == SPOKEN_ANSWER


class TestAVoiceThatIsMadeSlowly:
    async def test_the_words_do_not_wait_for_the_sound_and_the_first_sentence_is_heard_first(
        self,
    ) -> None:
        async with running() as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            assert isinstance(house.piper, StandInPiper)
            house.piper.hold_after = 1  # the second sentence is slow to be said
            try:
                async with probe(house) as device:
                    asked = await device.says(QUESTION)
                    await device.until("voice_chunk", asked)
                    await until(house.piper.held.is_set)

                    so_far = device.so_far(asked)
                    assert [part.text for _, part in so_far.spoken()] == SPOKEN_ANSWER
                    [(first, _), (second, _)] = so_far.spoken()
                    [track] = so_far.sound()
                    assert track.said(first) == SPOKEN_ANSWER[0]
                    assert second not in track.by_part  # it has no sound yet
                    assert not device.has("turn_ended", asked)  # the turn waits for the sound

                    house.piper.release.set()
                    turn = await device.turn(asked)
                    assert turn.ended.status is TurnStatus.FINISHED
                    [whole] = turn.sound()
                    assert [whole.said(seq) for seq, _ in turn.spoken()] == SPOKEN_ANSWER
            finally:
                house.piper.release.set()

    async def test_a_person_who_speaks_over_the_sound_stops_it_where_it_is(self) -> None:
        async with running() as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            assert isinstance(house.piper, StandInPiper)
            house.piper.hold_after = 1
            try:
                async with probe(house) as device:
                    first = await device.says(QUESTION)
                    await device.until("voice_chunk", first)
                    await until(house.piper.held.is_set)  # the second sentence is being made

                    second = await device.begins(recording_of(FOLLOW_UP), finish=False)
                    cut = await device.turn(first)

                    # The sound stopped with the person's first breath: no end of a track, no more
                    # chunks, and what was being made is given up on.
                    assert cut.ended.status is TurnStatus.CANCELLED
                    assert cut.kinds[-1] == "turn_ended"
                    assert not cut.of("voice_end")
                    [stopped] = cut.sound()
                    assert not stopped.ended
                    assert len(stopped.by_part) == 1
                    house.piper.hold_after = None
                    house.piper.release.set()  # what comes next is not held

                    await device.ends(second)
                    answered = await device.turn(second)
                    assert answered.ended.status is TurnStatus.FINISHED, answered.ended
                    assert house.whisper.heard == [QUESTION, FOLLOW_UP]
                    [whole] = answered.sound()
                    assert [whole.said(seq) for seq, _ in answered.spoken()] == [
                        part.text for _, part in answered.spoken()
                    ]
                    assert device.so_far(first).kinds[-1] == "turn_ended"  # and nothing after it
            finally:
                house.piper.release.set()
