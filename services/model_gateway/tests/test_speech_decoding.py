"""What faster-whisper does to a recording before any model runs, with the PyAV that is installed.

It is a test of one library against another, and it exists because the first run with a real model
found that they did not fit: faster-whisper passes ``metadata_errors`` to ``av.open`` and PyAV 19
removed it, so with the newest PyAV no recording could be transcribed at all, and nothing that
stands in for the engine could notice. The bound on ``av`` in ``pyproject.toml`` holds the version
back; this is what fails first when a lock moves it past what faster-whisper can use.
"""

import io

import pytest

from factotum.model_gateway.testing import a_tone_wav


@pytest.mark.parametrize("rate", [8000, 16000, 22050, 44100])
def test_a_wav_at_the_rate_of_a_microphone_or_of_a_voice_is_decoded_to_16_khz(rate: int) -> None:
    from faster_whisper.audio import decode_audio

    samples = decode_audio(io.BytesIO(a_tone_wav(rate)), sampling_rate=16000)

    assert str(samples.dtype) == "float32"
    assert len(samples) == pytest.approx(8000, abs=160)  # half a second, give or take the resampler


def test_a_wav_with_two_channels_is_decoded_to_one() -> None:
    from faster_whisper.audio import decode_audio

    samples = decode_audio(io.BytesIO(a_tone_wav(44100, channels=2)), sampling_rate=16000)

    assert samples.ndim == 1
    assert len(samples) == pytest.approx(8000, abs=160)
