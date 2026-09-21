"""Configuration of the edge, read from the environment.

Nothing here names a model, a document or a key: the edge is only the door of the devices, and asks
the conversation service for every answer.
"""

from pathlib import Path

from pydantic import AnyHttpUrl, Field

from factotum.common.config import ServiceSettings
from factotum.contracts.edge import HEARTBEAT_SECONDS


class EdgeSettings(ServiceSettings):
    edge_host: str = "127.0.0.1"
    """The interface to listen on. Loopback unless told otherwise: in a deployment the devices
    reach the edge through whatever the deployment puts in front of it."""

    edge_port: int = Field(default=8084, ge=1, le=65535)

    edge_conversation_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8083")
    """Where the conversation service is: it is asked for every answer."""

    edge_devices_file: Path | None = None
    """The devices that may connect (development only, until the hub pairs and registers them). A
    file that is named and is not there stops the edge; without one no device is known."""

    edge_hello_timeout_s: float = Field(default=10.0, gt=0, le=120)
    """How long a connection may take to say who it is before it is closed."""

    edge_heartbeat_s: int = Field(default=HEARTBEAT_SECONDS, ge=1, le=300)
    """How often a device with nothing to say is asked to say that it is there."""

    edge_idle_timeout_s: float = Field(default=60.0, gt=0, le=3600)
    """How long a device may say nothing at all before it is taken to be gone. It should be a good
    few heartbeats."""

    edge_command_timeout_s: float = Field(default=10.0, gt=0, le=300)
    """How long a device is given to answer a command before it is reported as not answering."""

    edge_turn_timeout_s: float = Field(default=130.0, gt=0, le=1800)
    """How long the conversation service may be silent while it answers an utterance. It is a little
    more than the conversation service's own limit on its model, so that the reason is its."""

    edge_send_timeout_s: float = Field(default=10.0, gt=0, le=120)
    """How long sending one frame to a device may take before the device is taken to be gone."""
