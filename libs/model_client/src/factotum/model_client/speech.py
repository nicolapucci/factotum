"""Speech, through the model gateway: a recording turned into text, and text turned into a
recording, each behind its own role and never knowing which engine does either."""

from typing import Protocol

import httpx
from pydantic import ValidationError

from factotum.contracts.audio import AudioPayload
from factotum.contracts.errors import GatewayError, GatewayUnavailableError
from factotum.contracts.identity import Owner
from factotum.contracts.models import ModelRole
from factotum.contracts.speech import (
    SynthesizeRequest,
    SynthesizeResult,
    TranscribeRequest,
    TranscribeResult,
)
from factotum.model_client.gateway import Delegation, error_from


class Speech(Protocol):
    async def transcribe(self, owner: Owner, audio: AudioPayload) -> str:
        """What was said in ``audio``. Raises ``SttNotConfiguredError`` when the owner has no
        speech-to-text model bound."""
        ...

    async def synthesize(self, owner: Owner, text: str) -> AudioPayload:
        """``text``, spoken aloud. Raises ``TtsNotConfiguredError`` when the owner has no
        text-to-speech model bound."""
        ...


class GatewaySpeech:
    def __init__(self, client: httpx.AsyncClient, delegation: Delegation) -> None:
        self._client = client
        self._delegation = delegation

    async def transcribe(self, owner: Owner, audio: AudioPayload) -> str:
        body = TranscribeRequest(audio=audio).model_dump(mode="json")
        headers = dict(self._delegation.headers(owner))
        try:
            response = await self._client.post("/v1/transcribe", json=body, headers=headers)
        except httpx.HTTPError:
            raise GatewayUnavailableError("the model gateway could not be reached") from None
        if response.status_code != 200:
            raise error_from(response, ModelRole.STT)
        try:
            return TranscribeResult.model_validate(response.json()).text
        except (ValueError, ValidationError):
            raise GatewayError(
                "the model gateway answered with something that is not a transcription"
            ) from None

    async def synthesize(self, owner: Owner, text: str) -> AudioPayload:
        body = SynthesizeRequest(text=text).model_dump(mode="json")
        headers = dict(self._delegation.headers(owner))
        try:
            response = await self._client.post("/v1/synthesize", json=body, headers=headers)
        except httpx.HTTPError:
            raise GatewayUnavailableError("the model gateway could not be reached") from None
        if response.status_code != 200:
            raise error_from(response, ModelRole.TTS)
        try:
            return SynthesizeResult.model_validate(response.json()).audio
        except (ValueError, ValidationError):
            raise GatewayError(
                "the model gateway answered with something that is not an audio clip"
            ) from None
