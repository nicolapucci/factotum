"""``python -m factotum.egress``: run the egress proxy."""

import asyncio
import logging
import sys

from pydantic import ValidationError

from factotum.common.logs import configure_logging
from factotum.egress.proxy import EgressProxy, Limits
from factotum.egress.settings import EgressSettings


async def serve(settings: EgressSettings) -> None:
    proxy = EgressProxy(
        settings.key,
        limits=Limits(
            max_connections=settings.egress_max_connections,
            header_timeout_s=settings.egress_header_timeout_s,
            connect_timeout_s=settings.egress_connect_timeout_s,
            idle_timeout_s=settings.egress_idle_timeout_s,
            max_duration_s=settings.egress_max_duration_s,
            max_bytes=settings.egress_max_mb * 1_000_000,
            connect_ports=frozenset(settings.egress_connect_ports),
        ),
    )
    server = await proxy.serve(settings.egress_host, settings.egress_port)
    logging.getLogger("factotum.egress").info(
        "starting", extra={"host": settings.egress_host, "port": settings.egress_port}
    )
    async with server:
        await server.serve_forever()


def main() -> None:
    try:
        settings = EgressSettings()
        settings.key  # noqa: B018 - a proxy with no key has nothing to check a grant by
    except (ValidationError, ValueError) as error:
        sys.stderr.write(f"egress: the settings are not valid: {error}\n")
        raise SystemExit(2) from None
    configure_logging(level=settings.log_level, json_format=settings.log_json)
    try:
        asyncio.run(serve(settings))
    except OSError as error:
        sys.stderr.write(f"egress: cannot start: {error}\n")
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
