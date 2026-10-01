"""``python -m factotum.hub``: run the hub."""

import logging
import sys

import uvicorn
from pydantic import ValidationError

from factotum.common.logs import configure_logging
from factotum.contracts.errors import AuthError
from factotum.hub.app import build_app
from factotum.hub.settings import HubSettings


def main() -> None:
    try:
        settings = HubSettings()
    except ValidationError as error:
        sys.stderr.write(f"hub: the settings are not valid: {error}\n")
        raise SystemExit(2) from None
    configure_logging(level=settings.log_level, json_format=settings.log_json)
    try:
        app = build_app(settings)
    except (AuthError, OSError, ValueError) as error:
        sys.stderr.write(f"hub: cannot start: {error}\n")
        raise SystemExit(2) from None
    logging.getLogger("factotum.hub").info(
        "starting", extra={"host": settings.hub_host, "port": settings.hub_port}
    )
    # Logging is already configured (and redacted): uvicorn must not replace it.
    uvicorn.run(app, host=settings.hub_host, port=settings.hub_port, log_config=None)


if __name__ == "__main__":
    main()
