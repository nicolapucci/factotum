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
NETWORKS: dict[str, dict[str, Any]] = COMPOSE["networks"]
SOCKET = "/var/run/docker.sock"


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
    assert SERVICES["conversation"]["profiles"] == ["services"]
    assert SERVICES["edge"]["profiles"] == ["services"]
    assert SERVICES["actions"]["profiles"] == ["services"]
    assert SERVICES["egress"]["profiles"] == ["services"]
    assert SERVICES["sandbox-image"]["profiles"] == ["services"]
    assert SERVICES["runner"]["profiles"] == ["services"]
    assert SERVICES["hub"]["profiles"] == ["services"]


def test_the_database_has_the_shared_memory_a_large_collection_needs() -> None:
    size = str(SERVICES["postgres"]["shm_size"]).lower()

    assert size.endswith("mb")
    assert int(size.removesuffix("mb")) >= 256


def test_the_knowledge_service_waits_for_its_database_and_finds_the_gateway_by_name() -> None:
    knowledge = SERVICES["knowledge"]

    assert knowledge["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert knowledge["environment"]["FACTOTUM_POSTGRES_HOST"] == "postgres"
    assert knowledge["environment"]["FACTOTUM_KNOWLEDGE_GATEWAY_URL"] == "http://model-gateway:8081"


def test_the_conversation_service_waits_for_its_database_and_finds_the_others_by_name() -> None:
    conversation = SERVICES["conversation"]
    environment = conversation["environment"]

    assert conversation["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert environment["FACTOTUM_POSTGRES_HOST"] == "postgres"
    assert environment["FACTOTUM_CONVERSATION_GATEWAY_URL"] == "http://model-gateway:8081"
    assert environment["FACTOTUM_CONVERSATION_KNOWLEDGE_URL"] == "http://knowledge:8082"


def test_the_edge_finds_the_conversation_service_by_name_and_has_no_database_of_its_own() -> None:
    edge = SERVICES["edge"]
    environment = edge["environment"]

    assert edge["depends_on"]["conversation"]["condition"] == "service_healthy"
    assert "postgres" not in edge["depends_on"]
    assert environment["FACTOTUM_EDGE_CONVERSATION_URL"] == "http://conversation:8083"
    assert not [key for key in environment if key.startswith("FACTOTUM_POSTGRES")]


def test_the_edge_reads_its_devices_from_where_the_config_folder_is_mounted() -> None:
    edge = SERVICES["edge"]
    folder = edge["environment"]["FACTOTUM_EDGE_DEVICES_FILE"].rsplit("/", 1)[0]

    assert f"./config:{folder}:ro" in edge["volumes"]


def test_the_actions_service_waits_for_its_database_and_finds_the_gateway_by_name() -> None:
    actions = SERVICES["actions"]
    environment = actions["environment"]

    assert actions["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert environment["FACTOTUM_POSTGRES_HOST"] == "postgres"
    assert environment["FACTOTUM_ACTIONS_GATEWAY_URL"] == "http://model-gateway:8081"


def test_the_hub_waits_for_its_database_and_needs_nothing_else() -> None:
    hub = SERVICES["hub"]

    assert hub["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert hub["environment"]["FACTOTUM_POSTGRES_HOST"] == "postgres"
    assert set(hub["depends_on"]) == {"postgres"}


def test_the_token_secret_reaches_every_service_that_authenticates_a_caller() -> None:
    holders = {
        name
        for name, spec in SERVICES.items()
        if "${FACTOTUM_HUB_TOKEN_SECRET" in str(spec.get("environment", {}))
    }

    assert holders == {
        "model-gateway",
        "knowledge",
        "conversation",
        "edge",
        "actions",
        "runner",
        "hub",
    }


def test_the_actions_service_reads_its_devices_and_its_skills_from_read_only_mounts() -> None:
    actions = SERVICES["actions"]
    environment = actions["environment"]
    devices = environment["FACTOTUM_ACTIONS_DEVICES_FILE"].rsplit("/", 1)[0]
    skills = environment["FACTOTUM_ACTIONS_SKILLS_DIR"]

    assert f"./config:{devices}:ro" in actions["volumes"]
    assert f"./skills:{skills}:ro" in actions["volumes"]


def test_a_skill_is_not_enabled_by_being_read_unless_the_operator_says_so() -> None:
    environment = SERVICES["actions"]["environment"]

    assert (
        environment["FACTOTUM_ACTIONS_ENABLE_SKILLS_DIR"]
        == "${FACTOTUM_ACTIONS_ENABLE_SKILLS_DIR:-false}"
    )


def test_the_conversation_service_finds_the_actions_service_by_name_and_does_not_need_it() -> None:
    conversation = SERVICES["conversation"]
    dependency = conversation["depends_on"]["actions"]

    assert conversation["environment"]["FACTOTUM_CONVERSATION_ACTIONS_URL"] == "http://actions:8085"
    assert dependency["condition"] == "service_healthy"
    assert dependency["required"] is False


def test_the_edge_and_the_actions_service_read_the_same_file_of_devices() -> None:
    edge = SERVICES["edge"]["environment"]["FACTOTUM_EDGE_DEVICES_FILE"]
    actions = SERVICES["actions"]["environment"]["FACTOTUM_ACTIONS_DEVICES_FILE"]

    assert edge == actions


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


@pytest.mark.parametrize(
    "service",
    ["model_gateway", "knowledge", "conversation", "edge", "actions", "egress", "runner", "hub"],
)
def test_the_image_does_not_run_as_root(service: str) -> None:
    dockerfile = (REPO_ROOT / "services" / service / "Dockerfile").read_text("utf-8")
    users = re.findall(r"^USER\s+(\S+)", dockerfile, re.M)

    assert users
    assert users[-1] not in {"root", "0"}


def test_the_build_context_leaves_secrets_and_the_repository_history_out() -> None:
    ignored = (REPO_ROOT / ".dockerignore").read_text("utf-8").splitlines()

    assert {".env", ".git", ".venv"} <= set(ignored)


# ---- the actions service, the runner and the egress proxy


def mounts_of(service: str) -> list[str]:
    return [str(volume) for volume in SERVICES[service].get("volumes", [])]


def test_the_actions_service_finds_the_runner_and_the_edge_by_name() -> None:
    environment = SERVICES["actions"]["environment"]

    assert environment["FACTOTUM_ACTIONS_RUNNER_URL"] == "http://runner:8086"
    assert environment["FACTOTUM_ACTIONS_EDGE_URL"] == "http://edge:8084"


def test_the_actions_service_does_not_need_the_runner_and_does_not_wait_for_the_edge() -> None:
    depends = SERVICES["actions"]["depends_on"]

    assert depends["runner"]["condition"] == "service_healthy"
    assert depends["runner"]["required"] is False
    # The edge waits for the conversation service, which asks this one: waiting for it would be a
    # cycle, and compose refuses one.
    assert "edge" not in depends


def test_the_actions_service_reads_its_secrets_from_the_folder_that_is_mounted_read_only() -> None:
    actions = SERVICES["actions"]
    folder = actions["environment"]["FACTOTUM_ACTIONS_SECRETS_FILE"].rsplit("/", 1)[0]

    assert f"./config:{folder}:ro" in actions["volumes"]


def test_the_actions_service_has_no_secret_but_the_hub_token_its_database_and_no_socket() -> None:
    environment = SERVICES["actions"]["environment"]
    secret_like = [key for key in environment if re.search(r"TOKEN|KEY|PASSWORD", key)]

    assert secret_like == ["FACTOTUM_HUB_TOKEN_SECRET", "FACTOTUM_POSTGRES_PASSWORD"]
    assert SOCKET not in " ".join(mounts_of("actions"))


def test_only_the_runner_is_given_the_socket_of_the_engine() -> None:
    for name in SERVICES:
        mounted = SOCKET in " ".join(mounts_of(name))
        assert mounted == (name == "runner"), f"{name} {'mounts' if mounted else 'does not mount'}"


def test_the_runner_is_given_the_socket_and_nothing_else_of_this_machine() -> None:
    assert mounts_of("runner") == [f"{SOCKET}:{SOCKET}"]


def test_the_runner_is_let_use_the_socket_without_being_root() -> None:
    runner = SERVICES["runner"]

    assert runner["group_add"] == ["${FACTOTUM_DOCKER_GID:-0}"]
    assert runner["environment"]["FACTOTUM_RUNNER_DOCKER_HOST"] == f"unix://{SOCKET}"


@pytest.mark.parametrize("service", ["runner", "egress"])
def test_the_services_at_the_edge_of_the_sandbox_are_as_bare_as_they_can_be(
    service: str,
) -> None:
    spec = SERVICES[service]

    assert spec["cap_drop"] == ["ALL"]
    assert spec["security_opt"] == ["no-new-privileges:true"]
    assert spec["read_only"] is True
    assert not spec.get("privileged")
    assert "cap_add" not in spec


@pytest.mark.parametrize("service", list(SERVICES))
def test_no_service_shares_the_host_or_is_privileged(service: str) -> None:
    spec = SERVICES[service]

    assert not spec.get("privileged")
    assert spec.get("network_mode") != "host"
    assert spec.get("pid") != "host"
    assert spec.get("ipc") != "host"


def test_the_network_of_the_sandboxes_has_no_way_out_and_is_the_one_the_runner_names() -> None:
    sandbox = NETWORKS["sandbox"]

    assert sandbox["internal"] is True
    assert sandbox["name"] == SERVICES["runner"]["environment"]["FACTOTUM_RUNNER_NETWORK"]


def test_nothing_is_on_the_network_of_the_sandboxes_but_the_egress_proxy() -> None:
    members = [name for name, spec in SERVICES.items() if "sandbox" in spec.get("networks", [])]

    assert members == ["egress"]


def test_nothing_is_on_the_network_the_proxy_goes_out_through_but_the_proxy() -> None:
    members = [name for name, spec in SERVICES.items() if "egress" in spec.get("networks", [])]

    assert members == ["egress"]


def test_the_egress_proxy_is_not_on_the_network_of_the_rest_of_the_deployment() -> None:
    # A service that lists networks is on those and not on the default one: the proxy must not be
    # able to reach the database, or be reached from what is not a sandbox.
    assert SERVICES["egress"]["networks"] == ["sandbox", "egress"]


def test_the_runner_is_not_on_the_network_of_the_sandboxes() -> None:
    assert "networks" not in SERVICES["runner"]


def test_the_egress_proxy_is_not_published_at_all() -> None:
    assert "ports" not in SERVICES["egress"]


def test_the_runner_finds_the_proxy_by_the_name_it_has_on_the_network_of_the_sandboxes() -> None:
    environment = SERVICES["runner"]["environment"]

    assert environment["FACTOTUM_RUNNER_EGRESS_URL"] == "http://egress:3128"
    assert "egress" in SERVICES  # and it has that name there, because that is the service's


def test_the_runner_waits_for_the_proxy_and_for_the_image_it_runs_scripts_in() -> None:
    depends = SERVICES["runner"]["depends_on"]

    assert depends["egress"]["condition"] == "service_healthy"
    assert depends["sandbox-image"]["condition"] == "service_completed_successfully"


def test_the_image_scripts_run_in_is_the_one_the_runner_is_told_to_use() -> None:
    image = SERVICES["sandbox-image"]["image"]

    assert image == SERVICES["runner"]["environment"]["FACTOTUM_RUNNER_IMAGE"]
    assert image == "${FACTOTUM_RUNNER_IMAGE:-python:3.13-slim}"


def test_the_image_service_only_makes_sure_the_image_is_there() -> None:
    spec = SERVICES["sandbox-image"]

    assert spec["entrypoint"] == ["true"]
    assert spec["restart"] == "no"
    assert "environment" not in spec
    assert "volumes" not in spec


def test_the_key_of_the_grants_is_given_to_the_proxy_and_the_runner_and_to_nothing_else() -> None:
    holders = {
        name
        for name, spec in SERVICES.items()
        if "${FACTOTUM_EGRESS_KEY" in str(spec.get("environment", {}))
    }

    assert holders == {"egress", "runner"}


def test_the_key_of_the_grants_has_no_default_that_could_be_guessed() -> None:
    proxy = SERVICES["egress"]["environment"]["FACTOTUM_EGRESS_KEY"]
    runner = SERVICES["runner"]["environment"]["FACTOTUM_RUNNER_EGRESS_KEY"]

    # Empty when it is not set, and a service that is given an empty key does not start.
    assert proxy == runner == "${FACTOTUM_EGRESS_KEY:-}"


def test_the_egress_proxy_reaches_a_device_or_a_service_on_this_machine_by_a_name() -> None:
    assert "host.docker.internal:host-gateway" in SERVICES["egress"]["extra_hosts"]


def test_the_limits_of_a_script_are_the_ones_the_runner_is_written_with() -> None:
    environment = SERVICES["runner"]["environment"]

    assert environment["FACTOTUM_RUNNER_MEMORY_MB"] == "${FACTOTUM_RUNNER_MEMORY_MB:-128}"
    assert environment["FACTOTUM_RUNNER_PIDS"] == "${FACTOTUM_RUNNER_PIDS:-64}"
    assert environment["FACTOTUM_RUNNER_CPUS"] == "${FACTOTUM_RUNNER_CPUS:-0.5}"


def test_the_runner_and_the_proxy_are_not_given_a_provider_key_or_a_database() -> None:
    for name in ("runner", "egress", "sandbox-image"):
        environment = SERVICES[name].get("environment", {})
        assert not [key for key in environment if key.endswith("_API_KEY")]
        assert not [key for key in environment if key.startswith("FACTOTUM_POSTGRES")]
        assert "postgres" not in SERVICES[name].get("depends_on", {})
