"""The deployment files are code too: what they promise about exposure and settings is tested."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_TEXT = (REPO_ROOT / "docker-compose.yml").read_text("utf-8")
COMPOSE: dict[str, Any] = yaml.safe_load(COMPOSE_TEXT)
SERVICES: dict[str, dict[str, Any]] = COMPOSE["services"]


def published_ports() -> list[tuple[str, str]]:
    return [
        (name, str(port)) for name, service in SERVICES.items() for port in service.get("ports", [])
    ]


@pytest.mark.parametrize(("service", "port"), published_ports())
def test_a_published_port_listens_on_loopback_only(service: str, port: str) -> None:
    assert port.startswith("127.0.0.1:"), f"{service} publishes {port} on every interface"


@pytest.mark.parametrize("service", [name for name, spec in SERVICES.items() if "image" in spec])
def test_an_image_is_pinned_to_an_exact_version(service: str) -> None:
    image: str = SERVICES[service]["image"]

    assert ":" in image, f"{service} does not pin a tag"
    assert not image.endswith(":latest"), f"{service} uses latest"


@pytest.mark.parametrize("service", [name for name, spec in SERVICES.items() if "build" in spec])
def test_a_built_service_has_its_dockerfile(service: str) -> None:
    build = SERVICES[service]["build"]

    assert (REPO_ROOT / build["context"] / build["dockerfile"]).is_file()


def test_the_database_starts_by_default_and_the_services_only_on_request() -> None:
    assert "profiles" not in SERVICES["postgres"]
    assert SERVICES["model-gateway"]["profiles"] == ["services"]
    assert SERVICES["knowledge"]["profiles"] == ["services"]


def test_the_database_has_the_shared_memory_a_large_collection_needs() -> None:
    size = str(SERVICES["postgres"]["shm_size"]).lower()

    assert size.endswith("mb")
    assert int(size.removesuffix("mb")) >= 256


def test_the_knowledge_service_waits_for_its_database_and_finds_the_gateway_by_name() -> None:
    knowledge = SERVICES["knowledge"]

    assert knowledge["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert knowledge["environment"]["FACTOTUM_POSTGRES_HOST"] == "postgres"
    assert knowledge["environment"]["FACTOTUM_KNOWLEDGE_GATEWAY_URL"] == "http://model-gateway:8081"


def test_no_service_is_given_a_provider_key_but_the_gateway() -> None:
    for name, service in SERVICES.items():
        keys = [key for key in service.get("environment", {}) if key.endswith("_API_KEY")]
        assert (name == "model-gateway") or not keys, f"{name} is given {keys}"


def test_every_factotum_variable_compose_reads_is_documented_in_the_env_example() -> None:
    read = set(re.findall(r"\$\{(FACTOTUM_[A-Z0-9_]+)", COMPOSE_TEXT))
    documented = set(
        re.findall(
            r"^#?\s*(FACTOTUM_[A-Z0-9_]+)=", (REPO_ROOT / ".env.example").read_text("utf-8"), re.M
        )
    )

    assert read <= documented, f"missing from .env.example: {sorted(read - documented)}"


def test_the_provider_keys_compose_passes_on_are_documented_in_the_env_example() -> None:
    passed = set(re.findall(r"\$\{([A-Z0-9]+_API_KEY)", COMPOSE_TEXT))
    documented = set(
        re.findall(
            r"^#?\s*([A-Z0-9]+_API_KEY)=", (REPO_ROOT / ".env.example").read_text("utf-8"), re.M
        )
    )

    assert passed
    assert passed <= documented, f"missing from .env.example: {sorted(passed - documented)}"


@pytest.mark.parametrize("service", ["model_gateway", "knowledge"])
def test_the_image_does_not_run_as_root(service: str) -> None:
    dockerfile = (REPO_ROOT / "services" / service / "Dockerfile").read_text("utf-8")
    users = re.findall(r"^USER\s+(\S+)", dockerfile, re.M)

    assert users
    assert users[-1] not in {"root", "0"}


def test_the_build_context_leaves_secrets_and_the_repository_history_out() -> None:
    ignored = (REPO_ROOT / ".dockerignore").read_text("utf-8").splitlines()

    assert {".env", ".git", ".venv"} <= set(ignored)
