import pytest

from factotum.contracts.models import Locality
from factotum.model_gateway.locality import infer_locality

LOCAL = Locality.LOCAL
REMOTE = Locality.REMOTE


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        # This machine and the private network.
        ("http://localhost:11434", LOCAL),
        ("http://127.0.0.1:11434", LOCAL),
        ("http://[::1]:8080", LOCAL),
        ("http://192.168.1.50:1234/v1", LOCAL),
        ("http://10.0.0.7", LOCAL),
        ("http://172.16.5.4", LOCAL),
        ("http://host.docker.internal:11434", LOCAL),
        ("http://gateway.docker.internal", LOCAL),
        ("http://ollama:11434", LOCAL),
        ("http://mini-pc.local:8000", LOCAL),
        ("http://server.lan", LOCAL),
        ("http://box.home.arpa", LOCAL),
        ("http://vllm.internal", LOCAL),
        ("http://LOCALHOST:11434", LOCAL),
        ("http://localhost.:11434", LOCAL),
        # Somebody else's servers.
        ("https://api.openai.com/v1", REMOTE),
        ("https://api.anthropic.com", REMOTE),
        ("https://generativelanguage.googleapis.com", REMOTE),
        ("https://openrouter.ai/api/v1", REMOTE),
        ("http://8.8.8.8", REMOTE),
        ("http://[2606:4700:4700::1111]", REMOTE),
        # The safe default when in doubt.
        ("not a url", REMOTE),
        ("", REMOTE),
        ("http://", REMOTE),
    ],
)
def test_locality_is_inferred_from_the_address(base_url: str, expected: Locality) -> None:
    assert infer_locality(base_url) is expected


def test_a_name_that_merely_contains_a_local_word_is_still_remote() -> None:
    assert infer_locality("https://localhost.evil.example.com") is REMOTE
    assert infer_locality("https://local.example.com") is REMOTE
