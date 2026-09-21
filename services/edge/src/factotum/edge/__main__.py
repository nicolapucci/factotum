"""``python -m factotum.edge``: run the edge."""

import logging
import sys

import uvicorn
from pydantic import ValidationError

from factotum.common.logs import configure_logging
from factotum.contracts.edge import MAX_FRAME_BYTES
from factotum.contracts.errors import AuthError
from factotum.edge.app import build_app
from factotum.edge.settings import EdgeSettings


def main() -> None:
    try:
        settings = EdgeSettings()
    except ValidationError as error:
        sys.stderr.write(f"edge: the settings are not valid: {error}\n")
        raise SystemExit(2) from None
    configure_logging(level=settings.log_level, json_format=settings.log_json)
    try:
        app = build_app(settings)
    except (AuthError, OSError, ValueError) as error:
        sys.stderr.write(f"edge: cannot start: {error}\n")
        raise SystemExit(2) from None
    logging.getLogger("factotum.edge").info(
        "starting", extra={"host": settings.edge_host, "port": settings.edge_port}
    )
    # Logging is already configured (and redacted): uvicorn must not replace it. A frame is at most
    # what the protocol allows, whatever a client sends; and the WebSocket implementation is the one
    # that is not deprecated.
    uvicorn.run(
        app,
        host=settings.edge_host,
        port=settings.edge_port,
        log_config=None,
        ws="websockets-sansio",
        ws_max_size=MAX_FRAME_BYTES,
    )


if __name__ == "__main__":
    main()
