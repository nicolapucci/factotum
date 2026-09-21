"""``python -m factotum.actions``: run the actions service."""

import logging
import sys

import uvicorn
from pydantic import ValidationError

from factotum.actions.app import build_app
from factotum.actions.settings import ActionsSettings
from factotum.common.logs import configure_logging
from factotum.contracts.errors import AuthError


def main() -> None:
    try:
        settings = ActionsSettings()
    except ValidationError as error:
        sys.stderr.write(f"actions: the settings are not valid: {error}\n")
        raise SystemExit(2) from None
    configure_logging(level=settings.log_level, json_format=settings.log_json)
    try:
        app = build_app(settings)
    except (AuthError, OSError, ValueError) as error:
        sys.stderr.write(f"actions: cannot start: {error}\n")
        raise SystemExit(2) from None
    logging.getLogger("factotum.actions").info(
        "starting", extra={"host": settings.actions_host, "port": settings.actions_port}
    )
    # Logging is already configured (and redacted): uvicorn must not replace it.
    uvicorn.run(app, host=settings.actions_host, port=settings.actions_port, log_config=None)


if __name__ == "__main__":
    main()
