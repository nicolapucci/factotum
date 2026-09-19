import pytest
from pydantic import ValidationError

from factotum.contracts.embeddings import (
    MAX_INPUTS_PER_REQUEST,
    EmbeddingRequest,
    EmbeddingResult,
    EmbeddingSpace,
)
from factotum.contracts.models import DataClassification, ProviderKind


class TestEmbeddingSpace:
    def test_the_id_is_stable(self) -> None:
        first = EmbeddingSpace(
            provider=ProviderKind.OLLAMA, model="nomic-embed-text", dimensions=768
        )
        again = EmbeddingSpace(
            provider=ProviderKind.OLLAMA, model="nomic-embed-text", dimensions=768
        )

        assert first.id == again.id
        assert first.id.startswith("emb-")
        assert len(first.id) == len("emb-") + 16

    @pytest.mark.parametrize(
        ("provider", "model", "dimensions"),
        [
            (ProviderKind.OPENAI, "nomic-embed-text", 768),  # another provider
            (ProviderKind.OLLAMA, "nomic-embed-text-v2", 768),  # another model
            (ProviderKind.OLLAMA, "nomic-embed-text", 512),  # another size
        ],
    )
    def test_a_different_provider_model_or_size_is_a_different_space(
        self, provider: ProviderKind, model: str, dimensions: int
    ) -> None:
        base = EmbeddingSpace(
            provider=ProviderKind.OLLAMA, model="nomic-embed-text", dimensions=768
        )

        other = EmbeddingSpace(provider=provider, model=model, dimensions=dimensions)

        assert other.id != base.id

    def test_the_separator_cannot_make_two_spaces_collide(self) -> None:
        a = EmbeddingSpace(provider=ProviderKind.OLLAMA, model="a|1", dimensions=2)
        b = EmbeddingSpace(provider=ProviderKind.OLLAMA, model="a", dimensions=12)

        assert a.id != b.id

    def test_the_size_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            EmbeddingSpace(provider=ProviderKind.OLLAMA, model="m", dimensions=0)


class TestEmbeddingRequest:
    def test_a_request_holds_texts_and_may_go_anywhere_by_default(self) -> None:
        request = EmbeddingRequest(inputs=("a", "b"))

        assert request.data_classification is DataClassification.ANY

    def test_at_least_one_text_is_needed(self) -> None:
        with pytest.raises(ValidationError):
            EmbeddingRequest(inputs=())

    def test_there_is_a_limit_per_request(self) -> None:
        with pytest.raises(ValidationError):
            EmbeddingRequest(inputs=tuple("x" for _ in range(MAX_INPUTS_PER_REQUEST + 1)))

    def test_the_limit_itself_is_allowed(self) -> None:
        request = EmbeddingRequest(inputs=tuple("x" for _ in range(MAX_INPUTS_PER_REQUEST)))

        assert len(request.inputs) == MAX_INPUTS_PER_REQUEST

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
    def test_a_blank_text_is_rejected(self, blank: str) -> None:
        with pytest.raises(ValidationError, match="cannot be blank"):
            EmbeddingRequest(inputs=("fine", blank))


class TestEmbeddingResult:
    def test_every_vector_has_the_declared_size(self) -> None:
        result = EmbeddingResult(vectors=((0.1, 0.2), (0.3, 0.4)), dimensions=2, space_id="emb-x")

        assert len(result.vectors) == 2

    def test_a_vector_of_the_wrong_size_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="declared number of dimensions"):
            EmbeddingResult(vectors=((0.1, 0.2), (0.3,)), dimensions=2, space_id="emb-x")

    def test_at_least_one_vector_is_needed(self) -> None:
        with pytest.raises(ValidationError):
            EmbeddingResult(vectors=(), dimensions=2, space_id="emb-x")

    def test_a_result_round_trips_through_json(self) -> None:
        result = EmbeddingResult(vectors=((0.5, -1.0),), dimensions=2, space_id="emb-x")

        assert EmbeddingResult.model_validate_json(result.model_dump_json()) == result
