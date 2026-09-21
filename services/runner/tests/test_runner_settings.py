import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from factotum.runner.sandbox import SandboxSettings
from factotum.runner.settings import MIN_KEY_CHARS, RunnerSettings

REPO_ROOT = Path(__file__).resolve().parents[3]
KEY = "k" * MIN_KEY_CHARS


def load(**values: object) -> RunnerSettings:
    return RunnerSettings(_env_file=None, **values)  # type: ignore[arg-type]


class TestDefaults:
    def test_it_listens_on_loopback_only_beside_the_other_services(self) -> None:
        settings = load()

        assert (settings.runner_host, settings.runner_port) == ("127.0.0.1", 8086)

    def test_left_alone_it_is_production_and_so_it_does_not_start(self) -> None:
        assert load().environment.value == "production"

    def test_a_script_has_no_network_until_a_proxy_and_a_key_are_named(self) -> None:
        settings = load()

        assert settings.runner_egress_url is None
        assert settings.sandbox.egress_url is None
        assert settings.sandbox.egress_key is None

    def test_a_script_gets_a_little_of_everything_and_nobody_s_name(self) -> None:
        sandbox = load().sandbox

        assert sandbox == SandboxSettings(
            image="python:3.13-slim",
            network="factotum-sandbox",
            egress_url=None,
            egress_key=None,
            memory_mb=128,
            cpus=0.5,
            pids=64,
            tmp_mb=16,
            user="10002:10002",
            log_kb=256,
            grace_s=10.0,
            max_concurrency=4,
            queue_wait_s=5.0,
        )

    def test_the_engine_is_the_socket_that_a_container_of_the_runner_is_given(self) -> None:
        assert load().runner_docker_host == "unix:///var/run/docker.sock"


class TestReadingTheEnvironment:
    def test_the_variables_are_the_field_names_with_the_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FACTOTUM_RUNNER_DOCKER_HOST", "tcp://engine:2375")
        monkeypatch.setenv("FACTOTUM_RUNNER_IMAGE", "python:3.12-slim")
        monkeypatch.setenv("FACTOTUM_RUNNER_NETWORK", "sandbox")
        monkeypatch.setenv("FACTOTUM_RUNNER_EGRESS_URL", "http://egress:3128")
        monkeypatch.setenv("FACTOTUM_RUNNER_EGRESS_KEY", KEY)
        monkeypatch.setenv("FACTOTUM_RUNNER_MEMORY_MB", "256")
        monkeypatch.setenv("FACTOTUM_RUNNER_CPUS", "1.5")
        monkeypatch.setenv("FACTOTUM_RUNNER_MAX_CONCURRENCY", "2")

        sandbox = load().sandbox

        assert sandbox.image == "python:3.12-slim"
        assert sandbox.network == "sandbox"
        assert sandbox.egress_url == "http://egress:3128/"
        assert sandbox.egress_key == KEY.encode()
        assert (sandbox.memory_mb, sandbox.cpus, sandbox.max_concurrency) == (256, 1.5, 2)

    def test_the_key_is_never_shown(self) -> None:
        settings = load(runner_egress_url="http://egress:3128", runner_egress_key=KEY)

        assert KEY not in repr(settings)
        assert KEY not in str(settings.model_dump())

    def test_who_a_script_runs_as_is_a_user_and_a_group(self) -> None:
        assert load(runner_uid=20000).sandbox.user == "20000:20000"


class TestWhatIsRefused:
    def test_a_proxy_without_a_key_to_sign_grants_with(self) -> None:
        with pytest.raises(ValidationError, match="FACTOTUM_RUNNER_EGRESS_KEY is needed"):
            load(runner_egress_url="http://egress:3128")

    def test_a_key_that_is_too_short(self) -> None:
        with pytest.raises(ValidationError, match="at least 32 characters"):
            load(runner_egress_url="http://egress:3128", runner_egress_key="short")

    @pytest.mark.parametrize("host", ["", "/var/run/docker.sock", "npipe:////./pipe/docker_engine"])
    def test_an_engine_that_is_not_where_an_engine_is(self, host: str) -> None:
        with pytest.raises(ValidationError, match="unix:// socket or a tcp://"):
            load(runner_docker_host=host)

    @pytest.mark.parametrize(
        "values",
        [
            {"runner_port": 0},
            {"runner_uid": 0},
            {"runner_uid": 999},
            {"runner_memory_mb": 8},
            {"runner_cpus": 0},
            {"runner_pids": 1},
            {"runner_tmp_mb": 0},
            {"runner_log_kb": 1},
            {"runner_max_concurrency": 0},
            {"runner_queue_wait_s": -1},
            {"runner_grace_s": 0},
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
    expected = {f"FACTOTUM_{name.upper()}" for name in RunnerSettings.model_fields}

    assert expected <= documented, f"missing from .env.example: {sorted(expected - documented)}"
