import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from factotum.common.config import Environment
from factotum.knowledge.chunker import ChunkingConfig
from factotum.knowledge.settings import KnowledgeSettings

REPO_ROOT = Path(__file__).resolve().parents[3]


def load(**values: object) -> KnowledgeSettings:
    return KnowledgeSettings(_env_file=None, **values)  # type: ignore[arg-type]


class TestDefaults:
    def test_it_listens_on_loopback_only(self) -> None:
        assert load().knowledge_host == "127.0.0.1"

    def test_the_gateway_is_where_a_gateway_started_by_hand_listens(self) -> None:
        assert str(load().knowledge_gateway_url) == "http://127.0.0.1:8081/"

    def test_a_file_may_be_as_big_as_the_prototype_allowed(self) -> None:
        assert load().limits.max_bytes == 25 * 1024 * 1024

    def test_the_pieces_are_the_ones_the_chunker_defaults_to(self) -> None:
        assert load().ingestion.chunking == ChunkingConfig()

    def test_it_reads_a_couple_of_documents_at_a_time(self) -> None:
        assert load().knowledge_workers == 2

    def test_left_alone_it_is_production_and_so_it_does_not_start(self) -> None:
        assert load().environment is Environment.PRODUCTION


class TestReadingTheEnvironment:
    def test_the_variables_are_the_field_names_with_the_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FACTOTUM_KNOWLEDGE_MAX_UPLOAD_MB", "50")
        monkeypatch.setenv("FACTOTUM_KNOWLEDGE_CHILD_TOKENS", "128")
        monkeypatch.setenv("FACTOTUM_KNOWLEDGE_PARENT_TOKENS", "512")
        monkeypatch.setenv("FACTOTUM_KNOWLEDGE_EMBED_BATCH", "8")
        monkeypatch.setenv("FACTOTUM_KNOWLEDGE_GATEWAY_URL", "http://model-gateway:8081")

        settings = load()

        assert settings.limits.max_bytes == 50 * 1024 * 1024
        assert settings.ingestion.chunking == ChunkingConfig(128, 512)
        assert settings.ingestion.batch == 8
        assert str(settings.knowledge_gateway_url) == "http://model-gateway:8081/"

    def test_the_database_is_the_one_compose_creates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FACTOTUM_POSTGRES_HOST", "postgres")
        monkeypatch.setenv("FACTOTUM_POSTGRES_PASSWORD", "secret")

        settings = load()

        assert settings.postgres_host == "postgres"
        assert settings.password() == "secret"

    def test_the_limits_are_those_of_the_upload_and_of_the_reader(self) -> None:
        limits = load(knowledge_max_upload_mb=10).limits

        assert limits.max_bytes == 10 * 1024 * 1024
        assert limits.max_pages == 2000


class TestWhatIsRefused:
    @pytest.mark.parametrize(
        "values",
        [
            {"knowledge_child_tokens": 63},
            {"knowledge_child_tokens": 500, "knowledge_parent_tokens": 400},
            {"knowledge_max_upload_mb": 0},
            {"knowledge_max_upload_mb": 101},
            {"knowledge_workers": -1},
            {"knowledge_workers": 17},
            {"knowledge_embed_batch": 0},
            {"knowledge_embed_batch": 257},
            {"knowledge_index_threshold": 0},
            {"knowledge_port": 0},
            {"knowledge_port": 70000},
            {"knowledge_gateway_url": "not a url"},
            {"knowledge_gateway_url": "ftp://gateway"},
        ],
    )
    def test_a_value_that_makes_no_sense(self, values: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            load(**values)

    def test_a_process_that_only_answers_requests_is_fine(self) -> None:
        assert load(knowledge_workers=0).knowledge_workers == 0


def test_every_setting_of_the_service_is_documented_in_the_env_example() -> None:
    documented = set(
        re.findall(
            r"^#?\s*(FACTOTUM_[A-Z0-9_]+)=", (REPO_ROOT / ".env.example").read_text("utf-8"), re.M
        )
    )
    expected = {f"FACTOTUM_{name.upper()}" for name in KnowledgeSettings.model_fields}

    assert expected <= documented, f"missing from .env.example: {sorted(expected - documented)}"
