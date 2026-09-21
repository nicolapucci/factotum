"""Configuration of the runner, read from the environment.

The runner knows the engine it makes containers with, the image and the network they are made on,
what a container may use, and the key that it signs the grants of the egress proxy with. It knows no
model, no skill, no tenant and no device.
"""

from typing import Self

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator

from factotum.common.config import ServiceSettings
from factotum.runner.docker import parse_host
from factotum.runner.sandbox import SandboxSettings

MIN_KEY_CHARS = 32


class RunnerSettings(ServiceSettings):
    runner_host: str = "127.0.0.1"
    """The interface to listen on. Loopback unless told otherwise: the service is for the actions
    service and is not published outside the deployment."""

    runner_port: int = Field(default=8086, ge=1, le=65535)

    runner_docker_host: str = "unix:///var/run/docker.sock"
    """Where the engine that makes the containers is: a socket (``unix://``) or a port
    (``tcp://``). Whoever can speak to it can make a container of any kind, so this is the most
    that the runner is trusted with: a deployment that is careful gives it an engine of its own,
    which can be a rootless one."""

    runner_image: str = "python:3.13-slim"
    """The image that a script runs in. It must be there: the runner does not pull it."""

    runner_network: str = "factotum-sandbox"
    """The network that has nothing on it but the egress proxy."""

    runner_egress_url: AnyHttpUrl | None = None
    """Where the egress proxy is, for a container on that network (``http://egress:3128``).
    Without it no script has a network."""

    runner_egress_key: SecretStr | None = None
    """The key that the grants of the proxy are signed with (at least 32 characters): the proxy
    has the same, and nothing else has."""

    runner_uid: int = Field(default=10002, ge=1000, le=65533)
    """Who a script runs as: nobody that owns anything."""

    runner_memory_mb: int = Field(default=128, ge=16, le=4096)
    runner_cpus: float = Field(default=0.5, gt=0, le=16)
    runner_pids: int = Field(default=64, ge=8, le=1024)
    runner_tmp_mb: int = Field(default=16, ge=1, le=1024)
    """What a script may use: the memory (swap included), the share of a processor, the processes,
    and the space it may write to."""

    runner_log_kb: int = Field(default=256, ge=16, le=4096)
    """How much of what a script writes the engine keeps."""

    runner_max_concurrency: int = Field(default=4, ge=1, le=64)
    """How many scripts run at once; the next waits for its turn."""

    runner_queue_wait_s: float = Field(default=5.0, ge=0, le=120)
    """How long a job waits for its turn before it is told that the runner is busy."""

    runner_grace_s: float = Field(default=10.0, gt=0, le=120)
    """What is allowed for a container to be made, and for what is left of it to be cleaned away,
    beyond the time the script has."""

    @model_validator(mode="after")
    def _the_engine_and_the_key_are_what_they_should_be(self) -> Self:
        parse_host(self.runner_docker_host)
        if self.runner_egress_url is not None and self.runner_egress_key is None:
            raise ValueError("FACTOTUM_RUNNER_EGRESS_KEY is needed for the egress proxy")
        if (
            self.runner_egress_key is not None
            and len(self.runner_egress_key.get_secret_value()) < MIN_KEY_CHARS
        ):
            raise ValueError(f"FACTOTUM_RUNNER_EGRESS_KEY is at least {MIN_KEY_CHARS} characters")
        return self

    @property
    def sandbox(self) -> SandboxSettings:
        key = self.runner_egress_key
        return SandboxSettings(
            image=self.runner_image,
            network=self.runner_network,
            egress_url=None if self.runner_egress_url is None else str(self.runner_egress_url),
            egress_key=None if key is None else key.get_secret_value().encode("utf-8"),
            memory_mb=self.runner_memory_mb,
            cpus=self.runner_cpus,
            pids=self.runner_pids,
            tmp_mb=self.runner_tmp_mb,
            user=f"{self.runner_uid}:{self.runner_uid}",
            log_kb=self.runner_log_kb,
            grace_s=self.runner_grace_s,
            max_concurrency=self.runner_max_concurrency,
            queue_wait_s=self.runner_queue_wait_s,
        )
