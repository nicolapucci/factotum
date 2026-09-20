"""``python -m factotum.conversation``: run the conversation service."""

import logging
import sys

import uvicorn
from pydantic import ValidationError

from factotum.common.logs import configure_logging
from factotum.contracts.errors import AuthError
from factotum.conversation.app import build_app
from factotum.conversation.settings import ConversationSettings


def main() -> None:
    try:
        settings = ConversationSettings()
    except ValidationError as error:
        sys.stderr.write(f"conversation: the settings are not valid: {error}\n")
        raise SystemExit(2) from None
    configure_logging(level=settings.log_level, json_format=settings.log_json)
    try:
        app = build_app(settings)
    except (AuthError, OSError, ValueError) as error:
        sys.stderr.write(f"conversation: cannot start: {error}\n")
        raise SystemExit(2) from None
    logging.getLogger("factotum.conversation").info(
        "starting", extra={"host": settings.conversation_host, "port": settings.conversation_port}
    )
    # Logging is already configured (and redacted): uvicorn must not replace it.
    uvicorn.run(
        app, host=settings.conversation_host, port=settings.conversation_port, log_config=None
    )


if __name__ == "__main__":
    main()
