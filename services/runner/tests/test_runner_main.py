"""Starting the runner the way a container does: from the environment."""

import pytest
import uvicorn

from factotum.runner import __main__ as entrypoint


class TestStarting:
    def test_it_does_not_start_in_production(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("FACTOTUM_ENVIRONMENT", raising=False)

        with pytest.raises(SystemExit) as raised:
            entrypoint.main()

        assert raised.value.code == 2
        assert "cannot start" in capsys.readouterr().err

    def test_with_settings_that_are_not_valid_it_says_which_and_does_not_start(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("FACTOTUM_ENVIRONMENT", "development")
        monkeypatch.setenv("FACTOTUM_RUNNER_DOCKER_HOST", "not a host")

        with pytest.raises(SystemExit) as raised:
            entrypoint.main()

        assert raised.value.code == 2
        assert "unix:// socket or a tcp://" in capsys.readouterr().err

    def test_in_development_it_serves_where_it_is_told_and_leaves_its_logging_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FACTOTUM_ENVIRONMENT", "development")
        monkeypatch.setenv("FACTOTUM_RUNNER_PORT", "18086")
        served: dict[str, object] = {}

        def serve(app: object, **options: object) -> None:
            served.update(options, app=app)

        monkeypatch.setattr(uvicorn, "run", serve)

        entrypoint.main()

        assert (served["host"], served["port"], served["log_config"]) == ("127.0.0.1", 18086, None)
        assert served["app"] is not None
