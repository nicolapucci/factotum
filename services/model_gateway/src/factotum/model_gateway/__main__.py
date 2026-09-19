"""``python -m factotum.model_gateway``: run the gateway."""

import logging
import sys

import uvicorn

from factotum.common.logs import configure_logging
from factotum.contracts.errors import AuthError
from factotum.model_gateway.app import build_app
from factotum.model_gateway.settings import GatewaySettings


def main() -> None:
    settings = GatewaySettings()
    configure_logging(level=settings.log_level, json_format=settings.log_json)
    try:
        app = build_app(settings)
    except (AuthError, OSError, ValueError) as error:
        sys.stderr.write(f"model gateway: cannot start: {error}\n")
        raise SystemExit(2) from None
    logging.getLogger("factotum.model_gateway").info(
        "starting", extra={"host": settings.gateway_host, "port": settings.gateway_port}
    )
    # Logging is already configured (and redacted): uvicorn must not replace it.
    uvicorn.run(app, host=settings.gateway_host, port=settings.gateway_port, log_config=None)


if __name__ == "__main__":
    main()
