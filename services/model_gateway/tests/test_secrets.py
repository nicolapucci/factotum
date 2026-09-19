import pytest

from factotum.contracts.errors import SecretNotFoundError
from factotum.model_gateway.secrets import EnvSecretResolver


def test_a_reference_is_resolved_from_the_environment() -> None:
    resolver = EnvSecretResolver({"OPENAI_API_KEY": "sk-secret-value"})

    secret = resolver.resolve("env:OPENAI_API_KEY")

    assert secret is not None
    assert secret.get_secret_value() == "sk-secret-value"


def test_no_reference_means_no_secret() -> None:
    assert EnvSecretResolver({}).resolve(None) is None


def test_the_secret_does_not_show_in_its_representation() -> None:
    secret = EnvSecretResolver({"K": "sk-secret-value"}).resolve("env:K")

    assert "sk-secret-value" not in repr(secret)
    assert "sk-secret-value" not in str(secret)


@pytest.mark.parametrize("environ", [{}, {"K": ""}])
def test_a_missing_or_empty_variable_is_an_error_that_names_the_reference(
    environ: dict[str, str],
) -> None:
    with pytest.raises(SecretNotFoundError, match="K is not set") as caught:
        EnvSecretResolver(environ).resolve("env:K")

    assert caught.value.details == {"secret_ref": "env:K"}


def test_an_unsupported_scheme_is_an_error() -> None:
    with pytest.raises(SecretNotFoundError, match="scheme 'vault'"):
        EnvSecretResolver({}).resolve("vault:x")


def test_the_process_environment_is_used_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTOTUM_TEST_SECRET", "from-the-environment")

    secret = EnvSecretResolver().resolve("env:FACTOTUM_TEST_SECRET")

    assert secret is not None
    assert secret.get_secret_value() == "from-the-environment"


def test_the_error_never_contains_a_value() -> None:
    resolver = EnvSecretResolver({"OTHER": "sk-should-not-appear"})

    with pytest.raises(SecretNotFoundError) as caught:
        resolver.resolve("env:MISSING")

    assert "sk-should-not-appear" not in str(caught.value)
    assert "sk-should-not-appear" not in str(caught.value.details)
