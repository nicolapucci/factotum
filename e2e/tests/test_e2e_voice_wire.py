"""A voice that is streamed, over a real socket, from the model gateway to what asks it (ADR 0023).

The services of the house are joined by a transport that hands a response over when it is whole, so
nothing there shows a sentence arriving before the next is made. Here the gateway is served on a
socket of the loopback and the client of the edge talks to it as it does in a deployment, and the
voice is one that makes a sentence only when the test has seen the one before it arrive: if
anything between the voice and the listener held the sound back until the end (the server, the
framing of the events, the client), the second sentence would never be made and this would not
end.
"""

import asyncio
import threading
from collections.abc import Iterator

import httpx
import pytest

from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import DevAuthenticator
from factotum.contracts.errors import ProviderUnavailableError
from factotum.contracts.identity import Owner, Subject
from factotum.contracts.models import ModelRole, ProviderKind
from factotum.contracts.speech import SynthesisFormat, SynthesisSamples
from factotum.contracts.testing import make_model_binding, make_tenant_id, make_user_id
from factotum.e2e import devices, house
from factotum.e2e.devices import Carrier
from factotum.e2e.house import serving
from factotum.e2e.stores import Store
from factotum.model_client import DevDelegation, GatewaySpeech
from factotum.model_gateway.api import create_app
from factotum.model_gateway.bindings import InMemoryBindingRepository
from factotum.model_gateway.core import ModelGateway
from factotum.model_gateway.providers.base import SpeechPiece
from factotum.model_gateway.providers.piper_local import PiperLocalAdapter
from factotum.model_gateway.retry import RetryPolicy
from factotum.model_gateway.secrets import EnvSecretResolver
from factotum.model_gateway.testing import TrackedClients

TENANT = make_tenant_id()
USER = make_user_id()
RATE = 22_050
A_TENTH_OF_A_SECOND = RATE * 2 // 10


@pytest.fixture(autouse=True)
def _how_the_house_is_run(monkeypatch: pytest.MonkeyPatch) -> Store:
    """There is no house here (this is the fixture of ``conftest.py`` by the same name, which would
    run each test three times for nothing)."""
    monkeypatch.setattr(devices, "DEFAULT_CARRIER", Carrier.CLIP)
    monkeypatch.setattr(house, "DEFAULT_STORE", Store.MEMORY)
    return Store.MEMORY


def sentence(n: int) -> bytes:
    """A tenth of a second that is not like the one of any other sentence: one chunk."""
    return bytes((n * 40 + i) % 251 for i in range(A_TENTH_OF_A_SECOND))


class SlowVoice:
    """The engine of a voice that says three sentences, each only after the test has opened its
    gate (the first has none): a voice that is made slowly, on the thread that asks for each."""

    def __init__(self) -> None:
        self.gates = [threading.Event() for _ in range(3)]
        self.made: list[int] = []
        self.fails_after: int | None = None

    def build(self, voice: str) -> "SlowVoice":
        return self

    def synthesize_wav(self, text: str) -> bytes:
        raise NotImplementedError("this voice is only asked for its pieces")

    def synthesize_pieces(self, text: str) -> Iterator[SpeechPiece]:
        for n in range(3):
            if n > 0:
                assert self.gates[n].wait(timeout=10), "the test did not let the voice go on"
            if n == self.fails_after:
                raise RuntimeError("the voice fell over")
            self.made.append(n)
            yield SpeechPiece(sample_rate=RATE, pcm=sentence(n))


def a_gateway_with(voice: SlowVoice) -> ModelGateway:
    binding = make_model_binding(
        tenant_id=TENANT,
        role=ModelRole.TTS,
        provider=ProviderKind.PIPER_LOCAL,
        model="it_IT-paola-medium",
        base_url=None,
        secret_ref=None,
    )
    return ModelGateway(
        InMemoryBindingRepository([binding]),
        EnvSecretResolver({}),
        adapters={ProviderKind.PIPER_LOCAL: PiperLocalAdapter(engine_factory=voice.build)},
        client_factory=TrackedClients(),
        retry=RetryPolicy(attempts=1, base_delay_s=0.0, max_delay_s=0.0),
    )


SETTINGS = ServiceSettings(_env_file=None, environment=Environment.TEST)
OWNER = Owner.of_subject(Subject.tenant(TENANT))


async def read_chunk(stream: object) -> SynthesisSamples:
    event = await asyncio.wait_for(anext(stream), 5)  # type: ignore[call-overload]
    assert isinstance(event, SynthesisSamples), event
    return event


class TestAVoiceOverARealSocket:
    async def test_a_sentence_arrives_before_the_next_one_is_made(self) -> None:
        voice = SlowVoice()
        app = create_app(
            a_gateway_with(voice), DevAuthenticator(SETTINGS, tenant_id=TENANT, user_id=USER)
        )
        async with serving(app) as address, httpx.AsyncClient(base_url=address.http) as client:
            stream = GatewaySpeech(client, DevDelegation(SETTINGS)).synthesize_stream(
                OWNER, "Uno. Due. Tre."
            )

            assert await asyncio.wait_for(anext(stream), 5) == SynthesisFormat(sample_rate=RATE)
            assert (await read_chunk(stream)).data == sentence(0)
            assert voice.made == [0]  # the first is there, and the voice has made no more
            voice.gates[1].set()
            assert (await read_chunk(stream)).data == sentence(1)
            assert voice.made == [0, 1]
            voice.gates[2].set()
            assert (await read_chunk(stream)).data == sentence(2)
            with pytest.raises(StopAsyncIteration):  # `done` is not given to a listener
                await asyncio.wait_for(anext(stream), 5)

    async def test_a_listener_that_goes_away_leaves_the_voice_where_it_was(self) -> None:
        voice = SlowVoice()
        app = create_app(
            a_gateway_with(voice), DevAuthenticator(SETTINGS, tenant_id=TENANT, user_id=USER)
        )
        async with serving(app) as address, httpx.AsyncClient(base_url=address.http) as client:
            stream = GatewaySpeech(client, DevDelegation(SETTINGS)).synthesize_stream(
                OWNER, "Uno. Due. Tre."
            )
            await asyncio.wait_for(anext(stream), 5)  # the format
            await read_chunk(stream)

            await stream.aclose()  # the person spoke over it
            await asyncio.sleep(0.5)  # for the gateway to notice that nobody is listening
            for gate in voice.gates:
                gate.set()  # whatever the voice was waiting for is let go
            await asyncio.sleep(0.3)

            assert voice.made[0] == 0
            assert 2 not in voice.made  # nobody was left to ask for the third sentence

    async def test_a_voice_that_falls_over_after_a_sentence_is_a_failure_and_not_the_end_of_it(
        self,
    ) -> None:
        voice = SlowVoice()
        voice.fails_after = 1
        app = create_app(
            a_gateway_with(voice), DevAuthenticator(SETTINGS, tenant_id=TENANT, user_id=USER)
        )
        async with serving(app) as address, httpx.AsyncClient(base_url=address.http) as client:
            stream = GatewaySpeech(client, DevDelegation(SETTINGS)).synthesize_stream(
                OWNER, "Uno. Due. Tre."
            )
            await asyncio.wait_for(anext(stream), 5)
            await read_chunk(stream)
            voice.gates[1].set()

            with pytest.raises(ProviderUnavailableError, match="could not synthesize"):
                await asyncio.wait_for(anext(stream), 5)
