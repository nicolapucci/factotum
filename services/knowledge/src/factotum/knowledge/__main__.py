"""``python -m factotum.knowledge``: run the knowledge service."""

import logging
import sys

import uvicorn
from pydantic import ValidationError

from factotum.common.logs import configure_logging
from factotum.contracts.errors import AuthError
from factotum.knowledge.app import build_app
from factotum.knowledge.settings import KnowledgeSettings


def main() -> None:
    try:
        settings = KnowledgeSettings()
    except ValidationError as error:
        sys.stderr.write(f"knowledge: the settings are not valid: {error}\n")
        raise SystemExit(2) from None
    configure_logging(level=settings.log_level, json_format=settings.log_json)
    try:
        app = build_app(settings)
    except (AuthError, OSError, ValueError) as error:
        sys.stderr.write(f"knowledge: cannot start: {error}\n")
        raise SystemExit(2) from None
    logging.getLogger("factotum.knowledge").info(
        "starting", extra={"host": settings.knowledge_host, "port": settings.knowledge_port}
    )
    # Logging is already configured (and redacted): uvicorn must not replace it.
    uvicorn.run(app, host=settings.knowledge_host, port=settings.knowledge_port, log_config=None)


if __name__ == "__main__":
    main()
