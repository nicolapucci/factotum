"""``announce``, the first skill of the platform (ADR 0026): what a message is made into, whose
voice it is asked for, what a device is given, and what is left when a voice cannot be made."""

import asyncio
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.actions.core import Announce
from factotum.actions.core.announce import MAX_BLOCK_CHARS, blocks_of
from factotum.actions.settings import ActionsSettings
from factotum.actions.testing import FakeSpeech
from factotum.common.sentences import MAX_CHUNK_CHARS, sentences_of
from factotum.contracts.audio import AudioPayload
from factotum.contracts.commands import MAX_SPOKEN_CHARS, DeviceCommand, PlayAudio
from factotum.contracts.errors import GatewayUnavailableError, SkillRunError
from factotum.contracts.identity import Owner, Principal, Role, Subject
from factotum.contracts.ids import SkillId, TenantId
from factotum.contracts.runner import RunRequest
from factotum.contracts.skills import ParamValue, SkillMode
from factotum.contracts.testing import make_device_id, make_tenant_id, make_user_id

SENTENCE = "Dinner is ready, so please come to the table while it is still hot."
"""A sentence of 69 characters."""


def a_message(sentences: int) -> str:
    """A message of numbered sentences, each of about fifty characters."""
    return " ".join(f"Sentence number {n} says that dinner is ready." for n in range(sentences))


def sentence_of(length: int) -> str:
    """One sentence of exactly this many characters: a long word and a full stop."""
    return "w" * (length - 1) + "."


def request_of(tenant: TenantId, message: ParamValue | None) -> RunRequest:
    return RunRequest(
        tenant_id=tenant,
        owner=Owner.of_subject(Subject.tenant(tenant)),
        skill_id=SkillId(uuid4()),
        skill_name="announce",
        version=1,
        mode=SkillMode.INSTRUCTIONS,
        params={} if message is None else {"message": message},
        timeout_s=60,
    )


def shared_device(tenant: TenantId) -> Principal:
    """A device of the whole household speaks for the tenant."""
    return Principal(tenant_id=tenant, device_id=make_device_id(), acts_as=Subject.tenant(tenant))


def personal_device(tenant: TenantId) -> Principal:
    """A person's own device speaks for them."""
    return Principal(
        tenant_id=tenant,
        device_id=make_device_id(),
        acts_as=Subject.user(make_user_id()),
        roles=frozenset({Role.MEMBER}),
    )


def said(commands: tuple[DeviceCommand, ...]) -> list[str]:
    """The words that the clips given to a device carry, in the order they are given."""
    clips = [command for command in commands if isinstance(command, PlayAudio)]
    assert len(clips) == len(commands), "every command of an announcement is a clip to play"
    return [FakeSpeech.words_in(clip.audio) for clip in clips]


class AllAtOnce(FakeSpeech):
    """A voice that makes nothing until every clip that it was asked for is being made: only a
    skill that asks for them all at the same time gets any."""

    def __init__(self, parties: int) -> None:
        super().__init__()
        self._barrier = asyncio.Barrier(parties)

    async def synthesize(self, owner: Owner, text: str) -> AudioPayload:
        await self._barrier.wait()
        return await super().synthesize(owner, text)


class FailsAtOnce:
    """A voice that cannot be made for the first block, and for the others waits for ever (so what
    is asked of it and not let go of is seen)."""

    def __init__(self) -> None:
        self.started = 0
        self.let_go = 0

    async def synthesize(self, owner: Owner, text: str) -> AudioPayload:
        self.started += 1
        if text.startswith("Sentence number 0"):
            raise GatewayUnavailableError("the voice is not there")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.let_go += 1
            raise
        raise AssertionError("nobody was to wait for this one")


class TestWhatAMessageIsMadeInto:
    def test_a_short_message_is_one_block(self) -> None:
        assert blocks_of("Dinner is ready.") == ["Dinner is ready."]

    def test_sentences_are_put_together_while_they_fit_in_a_block(self) -> None:
        first, second, third = (f"{n} {SENTENCE * 2}" for n in range(3))  # 140 characters each

        blocks = blocks_of(f"{first} {second} {third}")

        assert blocks == [f"{first} {second}", third]  # a third would make 422: it starts another

    def test_two_sentences_that_fill_a_block_to_its_last_character_are_one_block(self) -> None:
        first, second = sentence_of(199), sentence_of(200)

        [block] = blocks_of(f"{first} {second}")

        assert len(block) == MAX_BLOCK_CHARS

    def test_and_one_character_more_is_another_block(self) -> None:
        first, second = sentence_of(199), sentence_of(201)

        assert blocks_of(f"{first} {second}") == [first, second]

    def test_a_sentence_is_never_cut_to_fill_a_block(self) -> None:
        sentences = sentences_of(a_message(30))

        blocks = blocks_of(a_message(30))

        assert len(blocks) > 1
        assert [sentence for block in blocks for sentence in sentences if sentence in block] == (
            sentences
        )
        assert all(block.endswith(".") for block in blocks)

    def test_a_long_message_is_a_few_blocks_none_longer_than_the_limit(self) -> None:
        blocks = blocks_of(a_message(40))

        assert 2 <= len(blocks) <= 6
        assert all(0 < len(block) <= MAX_BLOCK_CHARS for block in blocks)

    def test_what_is_for_the_eyes_is_not_said(self) -> None:
        assert blocks_of("**Dinner** is [ready](http://example.com/menu) now.") == [
            "Dinner is ready now."
        ]

    @pytest.mark.parametrize("nothing", ["", "   ", "\n\n", "***", "**", "``"])
    def test_what_has_nothing_to_say_is_no_block(self, nothing: str) -> None:
        assert blocks_of(nothing) == []

    def test_a_sentence_without_an_end_is_cut_where_an_answer_is(self) -> None:
        words = " ".join(["porridge"] * 120)  # 1079 characters and not a full stop

        blocks = blocks_of(words)

        assert len(blocks) > 1
        assert all(len(block) <= MAX_BLOCK_CHARS for block in blocks)
        assert " ".join(blocks) == words

    @given(st.text(max_size=900))
    def test_a_block_is_never_empty_nor_too_long_and_nothing_is_lost_or_repeated(
        self, text: str
    ) -> None:
        blocks = blocks_of(text)

        assert all(
            block and block == block.strip() and len(block) <= MAX_BLOCK_CHARS for block in blocks
        )
        assert " ".join(blocks) == " ".join(sentences_of(text))

    def test_a_sentence_fits_in_a_block_for_every_length_it_may_have(self) -> None:
        assert MAX_CHUNK_CHARS < MAX_BLOCK_CHARS


class TestWhatADeviceIsGiven:
    async def test_a_clip_with_the_words_that_were_asked_to_be_said(self) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()

        done = await Announce(speech).run(
            request_of(tenant, "Dinner is ready"), shared_device(tenant), language="en"
        )

        assert said(done.commands) == ["Dinner is ready"]
        [command] = done.commands
        assert isinstance(command, PlayAudio)
        assert command.text == "Dinner is ready"
        assert command.audio == FakeSpeech.clip_of("Dinner is ready")

    async def test_what_is_said_to_the_person_who_asked_is_left_to_the_usual_word(self) -> None:
        tenant = make_tenant_id()

        done = await Announce(FakeSpeech()).run(
            request_of(tenant, "Dinner"), shared_device(tenant), language="en"
        )

        assert done.message is None  # "Done.", in the language of the person

    async def test_a_long_message_is_given_in_clips_that_say_all_of_it_in_order(self) -> None:
        tenant, message = make_tenant_id(), a_message(12)

        done = await Announce(FakeSpeech()).run(
            request_of(tenant, message), shared_device(tenant), language="en"
        )

        assert len(done.commands) > 1
        assert " ".join(said(done.commands)) == message
        assert [c.text for c in done.commands if isinstance(c, PlayAudio)] == blocks_of(message)

    async def test_what_is_for_the_eyes_is_made_into_a_voice_without_it(self) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()
        message = "**Dinner** is [ready](http://example.com/menu)"

        done = await Announce(speech).run(
            request_of(tenant, message), shared_device(tenant), language="en"
        )

        assert said(done.commands) == ["Dinner is ready"]
        assert [text for _, text in speech.asked] == ["Dinner is ready"]

    async def test_a_message_of_the_most_that_may_be_asked_for_is_given(self) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()
        message = (
            "x" * 99 + " "
        ) * 19 + "x" * 100  # 2000 characters of words, no full stop in them
        request = request_of(tenant, message)

        done = await Announce(speech).run(request, shared_device(tenant), language="en")

        assert len(message) == MAX_SPOKEN_CHARS
        assert request.params["message"] == message  # nothing was taken from the ends of it
        assert " ".join(said(done.commands)) == " ".join(message.split())
        assert all(len(text) <= MAX_BLOCK_CHARS for _, text in speech.asked)

    async def test_the_trail_says_what_was_said_and_in_how_many_clips(self) -> None:
        tenant = make_tenant_id()

        one = await Announce(FakeSpeech()).run(
            request_of(tenant, "Dinner"), shared_device(tenant), language="en"
        )
        many = await Announce(FakeSpeech()).run(
            request_of(tenant, a_message(12)), shared_device(tenant), language="en"
        )

        assert one.audit == "said 'Dinner' in 1 clip"
        assert many.audit is not None
        assert many.audit.startswith("said 'Sentence number 0 says")
        assert many.audit.endswith(f"in {len(many.commands)} clips")


class TestWhatItIsGiven:
    def test_the_time_that_it_has_is_more_than_one_voice_is_waited_for(self) -> None:
        # The whole of it is bounded by the limit of the skill, and each voice by this: a skill that
        # was given less than one voice may take would give up on a voice that was only slow.
        manifest = Announce(FakeSpeech()).loaded.manifest

        assert manifest.timeout_s >= ActionsSettings(_env_file=None).actions_speech_timeout_s


class TestWhoseVoice:
    async def test_the_voice_of_a_household_device_is_the_tenants(self) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()

        await Announce(speech).run(
            request_of(tenant, "Dinner"), shared_device(tenant), language="en"
        )

        assert speech.asked == [(Owner.of_subject(Subject.tenant(tenant)), "Dinner")]

    async def test_the_voice_of_a_persons_own_device_is_theirs(self) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()
        principal = personal_device(tenant)

        await Announce(speech).run(request_of(tenant, "Dinner"), principal, language="en")

        assert speech.asked == [(Owner.of_subject(principal.acts_as), "Dinner")]

    async def test_two_tenants_are_each_asked_for_their_own_voice_and_never_the_others(
        self,
    ) -> None:
        one, two, speech = make_tenant_id(), make_tenant_id(), FakeSpeech()
        announce = Announce(speech)

        await announce.run(request_of(one, "Dinner"), shared_device(one), language="en")
        await announce.run(request_of(two, "Lunch"), shared_device(two), language="en")

        assert speech.asked == [
            (Owner.of_subject(Subject.tenant(one)), "Dinner"),
            (Owner.of_subject(Subject.tenant(two)), "Lunch"),
        ]


class TestAtOnce:
    async def test_every_block_is_asked_for_at_the_same_time_as_the_others(self) -> None:
        tenant, message = make_tenant_id(), a_message(30)
        blocks = blocks_of(message)
        assert len(blocks) >= 3
        speech = AllAtOnce(len(blocks))

        async with asyncio.timeout(5):  # a voice that is asked for one at a time never gets there
            done = await Announce(speech).run(
                request_of(tenant, message), shared_device(tenant), language="en"
            )

        assert said(done.commands) == blocks

    async def test_the_clips_are_given_in_the_order_of_the_message_whichever_is_made_first(
        self,
    ) -> None:
        class Backwards(FakeSpeech):
            """The last block is made first, and the first block last."""

            def __init__(self, blocks: int) -> None:
                super().__init__()
                self._delays = [0.02 * (blocks - n) for n in range(blocks)]

            async def synthesize(self, owner: Owner, text: str) -> AudioPayload:
                await asyncio.sleep(self._delays.pop(0))
                return await super().synthesize(owner, text)

        tenant, message = make_tenant_id(), a_message(30)
        blocks = blocks_of(message)

        speech = Backwards(len(blocks))
        done = await Announce(speech).run(
            request_of(tenant, message), shared_device(tenant), language="en"
        )

        assert [text for _, text in speech.asked] == blocks[::-1]  # made in the other order
        assert said(done.commands) == blocks  # and given in the order of the message


class TestWhatIsNotAnnounced:
    @pytest.mark.parametrize("message", [None, "", "   ", "\n", 42, 1.5, True])
    async def test_something_that_is_not_a_message_is_nothing_to_announce(
        self, message: ParamValue | None
    ) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()

        with pytest.raises(SkillRunError, match="there is nothing to announce"):
            await Announce(speech).run(
                request_of(tenant, message), shared_device(tenant), language="en"
            )

        assert speech.asked == []  # a voice costs a model's time: it is not made for nothing

    @pytest.mark.parametrize("marks", ["***", "**", "``", "** ** **"])
    async def test_a_message_of_marks_has_nothing_to_say_and_no_voice_is_made(
        self, marks: str
    ) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()

        with pytest.raises(SkillRunError, match="there is nothing to say"):
            await Announce(speech).run(
                request_of(tenant, marks), shared_device(tenant), language="en"
            )

        assert speech.asked == []

    async def test_more_than_may_be_said_is_refused_and_no_voice_is_made(self) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()

        with pytest.raises(SkillRunError, match="at most 2000 characters"):
            await Announce(speech).run(
                request_of(tenant, "word " * 401), shared_device(tenant), language="en"
            )

        assert speech.asked == []


class TestWhenAVoiceCannotBeMade:
    async def test_it_is_one_failure_that_says_what_kind_and_not_a_group_of_errors(self) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()
        speech.failures = [None, GatewayUnavailableError("the voice is not there"), None]

        with pytest.raises(SkillRunError) as raised:
            await Announce(speech).run(
                request_of(tenant, a_message(30)), shared_device(tenant), language="en"
            )

        assert raised.value.message == "the voice could not be made (gateway_unavailable)"
        assert isinstance(raised.value.__cause__, GatewayUnavailableError)

    async def test_what_the_service_said_is_not_in_what_is_said_of_it(self) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()
        speech.failures = [GatewayUnavailableError("http://10.0.0.7:8080 refused, key sk-abc")]

        with pytest.raises(SkillRunError) as raised:
            await Announce(speech).run(
                request_of(tenant, "Dinner"), shared_device(tenant), language="en"
            )

        assert "10.0.0.7" not in raised.value.message
        assert "sk-abc" not in raised.value.message

    async def test_a_failure_that_is_a_mistake_of_the_platform_is_not_dressed_up_as_a_voice(
        self,
    ) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()
        speech.failures = [KeyError("tts")]

        with pytest.raises(KeyError):  # as it is, and not in a group
            await Announce(speech).run(
                request_of(tenant, "Dinner"), shared_device(tenant), language="en"
            )

    async def test_what_could_not_be_made_leaves_the_device_nothing_to_say(self) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()
        speech.failures = [None, GatewayUnavailableError("the voice is not there")]
        announce = Announce(speech)

        with pytest.raises(SkillRunError):
            await announce.run(
                request_of(tenant, a_message(30)), shared_device(tenant), language="en"
            )
        done = await announce.run(
            request_of(tenant, "Dinner"), shared_device(tenant), language="en"
        )

        assert said(done.commands) == ["Dinner"]  # and the failure did not outlive the message

    async def test_the_voices_that_are_being_made_are_let_go_of(self) -> None:
        tenant, speech = make_tenant_id(), FailsAtOnce()
        blocks = blocks_of(a_message(30))

        with pytest.raises(SkillRunError):
            await Announce(speech).run(
                request_of(tenant, a_message(30)), shared_device(tenant), language="en"
            )

        assert speech.started == len(blocks)
        assert speech.let_go == len(blocks) - 1

    async def test_the_time_that_runs_out_lets_go_of_all_of_them(self) -> None:
        class Waits:
            let_go = 0

            async def synthesize(self, owner: Owner, text: str) -> AudioPayload:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    type(self).let_go += 1
                    raise
                raise AssertionError("nobody was to wait for this one")

        tenant, blocks = make_tenant_id(), blocks_of(a_message(30))

        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await Announce(Waits()).run(
                    request_of(tenant, a_message(30)), shared_device(tenant), language="en"
                )

        assert Waits.let_go == len(blocks)
