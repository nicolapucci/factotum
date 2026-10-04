"""The assistant end to end: somebody talks to a device and the answer comes out of its speaker.

Everything between the two is the real code, put together in one process (``factotum.e2e.house``):
the terminal client on one side of a real WebSocket, the edge on the other, and behind the edge the
conversation, knowledge, actions and model-gateway applications. What is made up is what a unit test
cannot have: the hardware of the device and the models.

* **The hardware of the device.** A recording is a WAV file that carries the words that were said in
  it (``recording_of``), and a speaker only remembers what it was asked to play. The wake word is a
  ``FakeVoiceInput`` that hands the terminal one recording after another. This is the mock of the
  client. A client that streams audio and can be interrupted (ADR 0021) will need one of its own.
* **The models.** Whisper and Piper are the real adapters of the gateway on top of stand-in engines:
  the first reads the words out of a recording, the second writes a clip that carries the words it
  was asked to say, so that a test can tell what a speaker was made to say. The chat model writes
  an answer out of the sources it was shown, and the embedding model gives words a direction.

These are the questions answered from documents. What is asked to be done is in
``test_e2e_actions.py``.
"""

import asyncio
import io

from factotum.contracts.edge import CloseCode, TurnStatus
from factotum.contracts.knowledge import CollectionOwner
from factotum.e2e.devices import connected, joined, remembered, until
from factotum.e2e.house import TIMEOUT_S, running
from factotum.e2e.models import CLOSING, NOT_FOUND, OFFER, STT_MODEL, VOICE
from factotum.e2e.voice import recording_of
from factotum.terminal.cli import converse_by_voice
from factotum.terminal.testing import FakeVoiceInput

ROUTER = "To reset the router, hold the reset button for ten seconds."
ROUTER_IN_ITALIAN = "Il router è nello studio, accanto alla libreria, perché lì arriva il cavo."
SAFE = "The code of the safe is 4711."
QUESTION = "How do I reset the router?"
SAFE_QUESTION = "What is the code of the safe?"

FROM_THE_MANUAL = "From your documents: To reset the router, hold the reset button for ten seconds"
SPOKEN_ANSWER = [f"{FROM_THE_MANUAL}.", CLOSING]
"""The two sentences a speaker is made to say to a question about the router: what it says has
no references in it."""


class TestAskingByVoice:
    async def test_a_spoken_question_is_answered_aloud_from_the_documents(self) -> None:
        async with running() as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with connected(house, "speaker") as speaker:
                ended = await speaker.says_aloud(QUESTION)

                assert ended.status is TurnStatus.FINISHED, ended
                # What was said was heard, with the model the binding named...
                assert (house.whisper.models, house.whisper.heard) == ([STT_MODEL], [QUESTION])
                # ...it reached the model that wrote the answer, with the manual in front of it...
                [prompt] = house.model.prompts
                assert ROUTER in prompt
                assert prompt.endswith(f"Message: {QUESTION}")
                # ...and what was written was said, a sentence at a time, in the voice that was
                # bound (what a speaker says has no references in it).
                assert speaker.shown.of("speech") == SPOKEN_ANSWER
                assert speaker.spoken() == SPOKEN_ANSWER
                assert (house.piper.voices, house.piper.spoken) == ([VOICE], speaker.spoken())
                assert speaker.shown.of("text") == []
                assert speaker.shown.of("card") == []
                assert speaker.shown.of("violation") == []

    async def test_a_screen_with_a_speaker_shows_the_answer_and_where_it_comes_from_and_says_it(
        self,
    ) -> None:
        async with running() as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with connected(house, "smart-display") as display:
                ended = await display.says_aloud(QUESTION)

                assert ended.status is TurnStatus.FINISHED, ended
                assert " ".join(display.shown.of("text")) == f"{FROM_THE_MANUAL} [1]. {CLOSING}"
                [card] = display.shown.of("card")
                assert card.title == "Sources"
                assert card.items[0].label == "[1] Router manual"
                assert display.spoken() == display.shown.of("speech") == SPOKEN_ANSWER
                assert display.shown.of("violation") == []

    async def test_the_longest_recording_a_microphone_makes_goes_all_the_way(self) -> None:
        # Twelve seconds is where a terminal stops listening (``MicrophoneInput.max_seconds``): a
        # frame of that size is more than the whole protocol allowed before it carried audio.
        recording = recording_of(QUESTION, seconds=12)
        assert len(recording.data) > 64 * 1024
        async with running() as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with connected(house, "speaker") as speaker:
                ended = await speaker.sends(recording)

                assert ended.status is TurnStatus.FINISHED, ended
                assert house.whisper.heard == [QUESTION]
                assert speaker.spoken() == SPOKEN_ANSWER

    async def test_a_question_in_italian_keeps_its_accents_all_the_way(self) -> None:
        question = "Dov'è il router?"
        async with running() as house:
            await house.library.add(
                "Casa", "Il router", ROUTER_IN_ITALIAN, owner=CollectionOwner.TENANT
            )
            async with connected(house, "speaker") as speaker:
                ended = await speaker.says_aloud(question)

                assert ended.status is TurnStatus.FINISHED, ended
                assert house.whisper.heard == [question]
                [prompt] = house.model.prompts
                assert ROUTER_IN_ITALIAN in prompt
                assert prompt.endswith(f"Message: {question}")
                assert speaker.spoken() == [
                    f"From your documents: {ROUTER_IN_ITALIAN.rstrip('.')}.",
                    CLOSING,
                ]

    async def test_a_question_that_is_typed_is_answered_aloud_too(self) -> None:
        async with running() as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with connected(house, "speaker") as speaker:
                ended = await speaker.types(QUESTION)

                assert ended.status is TurnStatus.FINISHED, ended
                assert house.whisper.heard == []  # nothing was a recording
                assert speaker.spoken() == SPOKEN_ANSWER

    async def test_what_was_heard_is_what_the_conversation_service_remembers(self) -> None:
        async with running() as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with connected(house, "phone") as phone:
                first = await phone.says_aloud(QUESTION)
                second = await phone.says_aloud("What is the reset button for?")

                assert (first.status, second.status) == (TurnStatus.FINISHED, TurnStatus.FINISHED)
                questions_and_cited = [
                    (exchange.question, [c.document_title for c in exchange.citations])
                    for exchange in sorted(await remembered(house), key=lambda e: e.seq)
                ]
                assert questions_and_cited == [
                    (QUESTION, ["Router manual"]),
                    ("What is the reset button for?", ["Router manual"]),
                ]
                # A message is read for a reference to the past only when there is a past: the
                # first was not, the second was, and the answer to it was written all the same.
                assert house.model.readings == 1
                assert len(house.model.prompts) == 2

    async def test_the_wake_word_of_the_terminal_answers_one_recording_after_another(self) -> None:
        async with running() as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with joined(house, "speaker") as speaker:
                heard = FakeVoiceInput(recording_of(QUESTION), recording_of("Where is it kept?"))
                conversing = asyncio.create_task(
                    converse_by_voice(speaker.client, heard, io.StringIO())
                )
                await until(lambda: len(speaker.shown.of("turn_ended")) == 2)
                await house.sessions.close_all()
                lost = await asyncio.wait_for(conversing, TIMEOUT_S)

                assert lost is not None
                assert lost.code == CloseCode.GOING_AWAY
                assert [ended.status for ended in speaker.shown.of("turn_ended")] == [
                    TurnStatus.FINISHED,
                    TurnStatus.FINISHED,
                ]
                assert house.whisper.heard == [QUESTION, "Where is it kept?"]
                assert speaker.spoken() == SPOKEN_ANSWER * 2


class TestWhoHearsWhat:
    async def test_a_private_document_is_read_out_by_its_owners_phone_and_never_by_the_speaker(
        self,
    ) -> None:
        async with running() as house:
            await house.library.add("Private", "The safe", SAFE, owner=CollectionOwner.USER)
            async with connected(house, "speaker") as speaker, connected(house, "phone") as phone:
                await phone.says_aloud(SAFE_QUESTION)
                await speaker.says_aloud(SAFE_QUESTION)

                [for_the_phone, for_the_speaker] = house.model.prompts
                assert SAFE in for_the_phone
                assert "SOURCES" not in for_the_speaker  # nothing of it was in front of the model
                assert phone.spoken()[0] == "From your documents: The code of the safe is 4711."
                assert speaker.spoken() == [NOT_FOUND, OFFER]
                assert "4711" not in str(speaker.shown.shown)

    async def test_what_the_tenant_shares_is_read_out_by_both(self) -> None:
        async with running() as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with connected(house, "speaker") as speaker, connected(house, "phone") as phone:
                await phone.says_aloud(QUESTION)
                await speaker.says_aloud(QUESTION)

                assert phone.spoken() == speaker.spoken() == SPOKEN_ANSWER


class TestWhenSomethingIsMissing:
    async def test_without_a_speech_to_text_model_the_device_is_told_and_nothing_is_asked(
        self,
    ) -> None:
        async with (
            running(speech_to_text=False) as house,
            connected(house, "speaker") as speaker,
        ):
            ended = await speaker.says_aloud(QUESTION)

            assert (ended.status, ended.code) == (TurnStatus.FAILED, "stt_not_configured")
            assert house.model.prompts == []
            assert speaker.speaker.played == []

    async def test_without_a_text_to_speech_model_the_answer_comes_without_a_voice(self) -> None:
        async with running(text_to_speech=False) as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with connected(house, "speaker") as speaker:
                ended = await speaker.says_aloud(QUESTION)

                assert ended.status is TurnStatus.FINISHED, ended
                assert speaker.shown.of("speech") == SPOKEN_ANSWER
                assert speaker.speaker.played == []

    async def test_a_recording_of_silence_is_not_a_question(self) -> None:
        async with running() as house, connected(house, "speaker") as speaker:
            ended = await speaker.says_aloud("")

            assert (ended.status, ended.code) == (TurnStatus.FAILED, "nothing_heard")
            assert house.model.prompts == []

    async def test_a_speech_model_that_falls_over_ends_that_turn_and_the_next_one_works(
        self,
    ) -> None:
        async with running() as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with connected(house, "speaker") as speaker:
                house.whisper.broken = True
                failed = await speaker.says_aloud(QUESTION)
                house.whisper.broken = False
                answered = await speaker.says_aloud(QUESTION)

                assert (failed.status, failed.code) == (TurnStatus.FAILED, "provider_unavailable")
                assert answered.status is TurnStatus.FINISHED, answered
                assert speaker.spoken() == SPOKEN_ANSWER
