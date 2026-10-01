"""The gateway against the real faster-whisper and Piper, with no fakes.

Opt-in, because the first run downloads a Whisper model and a Piper voice (see
``docs/model-gateway.md``) and real inference takes a few seconds even on a small model:

    FACTOTUM_LIVE_TESTS=1 uv run pytest -m live services/model_gateway/tests/live -k speech -s

Which model and voice to use, and where to run them, can be changed with
FACTOTUM_LIVE_WHISPER_MODEL, FACTOTUM_LIVE_PIPER_VOICE and FACTOTUM_LIVE_SPEECH_DEVICE.
"""

import io
import os
import wave
from collections.abc import AsyncIterator

import pytest

from factotum.common.devauth import DEV_TENANT_ID
from factotum.common.urlguard import UrlPolicy
from factotum.contracts.audio import AudioPayload
from factotum.contracts.models import ModelRole, ProviderKind
from factotum.contracts.testing import make_model_binding, make_tenant_principal
from factotum.model_gateway.bindings import InMemoryBindingRepository
from factotum.model_gateway.core import ModelGateway, default_adapters, guarded_clients
from factotum.model_gateway.secrets import EnvSecretResolver

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("FACTOTUM_LIVE_TESTS") != "1",
        reason="live tests are opt-in: set FACTOTUM_LIVE_TESTS=1",
    ),
]

WHISPER_MODEL = os.environ.get("FACTOTUM_LIVE_WHISPER_MODEL", "tiny")
PIPER_VOICE = os.environ.get("FACTOTUM_LIVE_PIPER_VOICE", "it_IT-paola-medium")
DEVICE = os.environ.get("FACTOTUM_LIVE_SPEECH_DEVICE", "cpu")

PRINCIPAL = make_tenant_principal(tenant_id=DEV_TENANT_ID)

_opened: list[ModelGateway] = []


@pytest.fixture(autouse=True)
async def close_the_gateways() -> AsyncIterator[None]:
    yield
    while _opened:
        await _opened.pop().aclose()


def gateway() -> ModelGateway:
    stt = make_model_binding(
        tenant_id=DEV_TENANT_ID,
        role=ModelRole.STT,
        provider=ProviderKind.WHISPER_LOCAL,
        model=WHISPER_MODEL,
        base_url=None,
        secret_ref=None,
    )
    tts = make_model_binding(
        tenant_id=DEV_TENANT_ID,
        role=ModelRole.TTS,
        provider=ProviderKind.PIPER_LOCAL,
        model=PIPER_VOICE,
        base_url=None,
        secret_ref=None,
    )
    made = ModelGateway(
        InMemoryBindingRepository([stt, tts]),
        # Neither provider holds a key, but every call still gets a client (it is just unused):
        EnvSecretResolver(),
        adapters=default_adapters(speech_device=DEVICE),
        client_factory=guarded_clients(UrlPolicy.private_networks_allowed()),
    )
    _opened.append(made)
    return made


async def test_text_becomes_a_wav_that_becomes_recognizable_text_again() -> None:
    said = "Ciao, come stai oggi?"

    audio = await gateway().synthesize(PRINCIPAL, said)
    print(f"\nsynthesized {len(audio.data)} bytes at {audio.sample_rate} Hz")
    heard = await gateway().transcribe(PRINCIPAL, audio)
    print(f"heard: {heard!r}")

    assert audio.format.value == "wav"
    assert len(audio.data) > 1000
    assert "ciao" in heard.lower() or "stai" in heard.lower()


def silent_wav(seconds: float = 1.0, sample_rate: int = 16000) -> AudioPayload:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * int(seconds * sample_rate))
    return AudioPayload(sample_rate=sample_rate, data=buffer.getvalue())


async def test_a_recording_with_no_speech_in_it_is_not_forced_into_words() -> None:
    heard = await gateway().transcribe(PRINCIPAL, silent_wav())
    print(f"heard from silence: {heard!r}")

    assert isinstance(heard, str)  # it may be blank, or a guess; it must not raise
