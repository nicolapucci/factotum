import re
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from factotum.common.config import Environment
from factotum.conversation.settings import ConversationSettings
from factotum.conversation.turn import TurnSettings

REPO_ROOT = Path(__file__).resolve().parents[3]


def load(**values: object) -> ConversationSettings:
    return ConversationSettings(_env_file=None, **values)  # type: ignore[arg-type]


class TestDefaults:
    def test_it_listens_on_loopback_only_and_beside_the_other_services(self) -> None:
        settings = load()

        assert (settings.conversation_host, settings.conversation_port) == ("127.0.0.1", 8083)

    def test_the_gateway_and_the_documents_are_where_they_listen_when_started_by_hand(self) -> None:
        settings = load()

        assert str(settings.conversation_gateway_url) == "http://127.0.0.1:8081/"
        assert str(settings.conversation_knowledge_url) == "http://127.0.0.1:8082/"

    def test_it_asks_no_actions_service_unless_one_is_named(self) -> None:
        settings = load()

        assert settings.conversation_actions_url is None
        assert settings.turn.actions_timeout_s == 120.0

    def test_left_alone_it_is_production_and_so_it_does_not_start(self) -> None:
        assert load().environment is Environment.PRODUCTION

    def test_what_is_said_is_kept_until_somebody_forgets_it(self) -> None:
        assert load().retention is None

    def test_a_shared_device_sees_the_last_minute_and_what_is_said_on_it_is_kept_twice_that(
        self,
    ) -> None:
        settings = load()

        assert settings.turn.shared_window == timedelta(seconds=60)
        assert settings.shared_keep == timedelta(seconds=120)

    def test_a_turn_looks_at_a_few_exchanges_and_a_few_passages(self) -> None:
        assert load().turn == TurnSettings(
            recent_exchanges=3,
            history_hits=3,
            document_passages=6,
            knowledge_timeout_s=4.0,
            session_gap=timedelta(minutes=30),
        )


class TestReadingTheEnvironment:
    def test_the_variables_are_the_field_names_with_the_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FACTOTUM_CONVERSATION_GATEWAY_URL", "http://model-gateway:8081")
        monkeypatch.setenv("FACTOTUM_CONVERSATION_KNOWLEDGE_URL", "http://knowledge:8082")
        monkeypatch.setenv("FACTOTUM_CONVERSATION_ACTIONS_URL", "http://actions:8085")
        monkeypatch.setenv("FACTOTUM_CONVERSATION_ACTIONS_TIMEOUT_S", "30")
        monkeypatch.setenv("FACTOTUM_CONVERSATION_TIMEZONE", "Europe/Rome")
        monkeypatch.setenv("FACTOTUM_CONVERSATION_SESSION_MINUTES", "10")
        monkeypatch.setenv("FACTOTUM_CONVERSATION_RETENTION_DAYS", "90")
        monkeypatch.setenv("FACTOTUM_CONVERSATION_DOCUMENT_PASSAGES", "4")
        monkeypatch.setenv("FACTOTUM_CONVERSATION_SHARED_WINDOW_SECONDS", "90")

        settings = load()

        assert str(settings.conversation_gateway_url) == "http://model-gateway:8081/"
        assert str(settings.conversation_knowledge_url) == "http://knowledge:8082/"
        assert str(settings.conversation_actions_url) == "http://actions:8085/"
        assert settings.turn.actions_timeout_s == 30.0
        assert settings.zone == ZoneInfo("Europe/Rome")
        assert settings.retention == timedelta(days=90)
        assert settings.turn.session_gap == timedelta(minutes=10)
        assert settings.turn.document_passages == 4
        assert settings.turn.shared_window == timedelta(seconds=90)
        assert settings.shared_keep == timedelta(seconds=180)

    def test_the_database_is_the_one_compose_creates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FACTOTUM_POSTGRES_HOST", "postgres")
        monkeypatch.setenv("FACTOTUM_POSTGRES_PASSWORD", "secret")

        settings = load()

        assert settings.postgres_host == "postgres"
        assert settings.password() == "secret"


class TestWhatIsRefused:
    @pytest.mark.parametrize("name", ["Mars/Olympus_Mons", "", "not a zone"])
    def test_a_time_zone_that_does_not_exist(self, name: str) -> None:
        with pytest.raises(ValidationError, match="not a time zone"):
            load(conversation_timezone=name)

    @pytest.mark.parametrize(
        "values",
        [
            {"conversation_port": 0},
            {"conversation_session_minutes": 0},
            {"conversation_shared_window_seconds": 0},
            {"conversation_shared_window_seconds": 3601},
            {"conversation_recent_exchanges": 0},
            {"conversation_history_hits": 11},
            {"conversation_document_passages": 0},
            {"conversation_knowledge_timeout_s": 0},
            {"conversation_actions_timeout_s": 0},
            {"conversation_actions_timeout_s": 601},
            {"conversation_gate_timeout_s": 61},
            {"conversation_history_min_similarity": 1.5},
            {"conversation_retention_days": -1},
            {"conversation_embed_batch": 0},
        ],
    )
    def test_a_number_outside_what_makes_sense(self, values: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            load(**values)


def test_every_setting_of_the_service_is_documented_in_the_env_example() -> None:
    documented = set(
        re.findall(
            r"^#?\s*(FACTOTUM_[A-Z0-9_]+)=", (REPO_ROOT / ".env.example").read_text("utf-8"), re.M
        )
    )
    expected = {f"FACTOTUM_{name.upper()}" for name in ConversationSettings.model_fields}

    assert expected <= documented, f"missing from .env.example: {sorted(expected - documented)}"
