"""Configuration of the model gateway, read from the environment."""

from pathlib import Path

from pydantic import AnyHttpUrl, Field

from factotum.common.config import ServiceSettings


class GatewaySettings(ServiceSettings):
    gateway_host: str = "127.0.0.1"
    """The interface to listen on. Loopback unless told otherwise: the gateway is for the other
    services and is not published outside the deployment."""

    gateway_port: int = Field(default=8081, ge=1, le=65535)

    gateway_bindings_file: Path | None = None
    """A YAML file with the model bindings of the development tenant, for development: used only
    until a token secret is configured and ``FACTOTUM_GATEWAY_HUB_URL`` is also set; otherwise still
    a file."""

    gateway_hub_url: AnyHttpUrl | None = None
    """Where the hub is: once a token secret is configured, the bindings it holds for the tenant are
    fetched from here instead of the file above."""

    gateway_speech_device: str = "cpu"
    """Where the local speech-to-text and text-to-speech providers (``whisper_local``,
    ``piper_local``) run: "cpu" or "cuda". Unused until a binding names one of them."""

    gateway_speech_cache_dir: Path | None = None
    """Where the local speech providers cache (and look for) their downloaded models. None uses
    each library's own default location in the user's home directory."""
