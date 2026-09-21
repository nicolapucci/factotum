"""``python -m factotum.runner``: run the runner."""

import logging
import sys

import uvicorn
from pydantic import ValidationError

from factotum.common.logs import configure_logging
from factotum.contracts.errors import AuthError
from factotum.runner.app import build_app
from factotum.runner.settings import RunnerSettings


def main() -> None:
    try:
        settings = RunnerSettings()
    except ValidationError as error:
        sys.stderr.write(f"runner: the settings are not valid: {error}\n")
        raise SystemExit(2) from None
    configure_logging(level=settings.log_level, json_format=settings.log_json)
    try:
        app = build_app(settings)
    except (AuthError, OSError, ValueError) as error:
        sys.stderr.write(f"runner: cannot start: {error}\n")
        raise SystemExit(2) from None
    logging.getLogger("factotum.runner").info(
        "starting", extra={"host": settings.runner_host, "port": settings.runner_port}
    )
    # Logging is already configured (and redacted): uvicorn must not replace it.
    uvicorn.run(app, host=settings.runner_host, port=settings.runner_port, log_config=None)


if __name__ == "__main__":
    main()
