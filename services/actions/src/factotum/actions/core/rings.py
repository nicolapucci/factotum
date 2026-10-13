"""The sound that a timer or an alarm rings with: made once, when it is set (ADR 0029).

The device rings by itself when the moment comes, so the platform has to hand it the sound in
advance: the words of the ring, said by the voice of whom the device acts for, as a clip that the
device keeps. A ring is a sentence, and a clip costs a model's time: it is one request to the voice,
made when the timer is set and never again.
"""

from factotum.actions.core.skills import SpeechMaker
from factotum.contracts.audio import AudioPayload
from factotum.contracts.errors import FactotumError, SkillRunError
from factotum.contracts.identity import Owner, Principal


async def ring_of(speech: SpeechMaker, principal: Principal, words: str) -> AudioPayload:
    """The clip of ``words`` in the voice of whom the device acts for. A voice that cannot be made
    is a skill that failed, and what the trail says of it is what kind of failure it was (the code
    of the error), never what the service said."""
    try:
        return await speech.synthesize(Owner.of_subject(principal.acts_as), words)
    except FactotumError as failure:
        raise SkillRunError(f"the voice could not be made ({failure.code})") from failure
