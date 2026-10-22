"""Command line entry point: python -m s3lobby, or the s3lobby command after installation."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence

from s3lobby import __version__
from s3lobby.constants import DEFAULT_SERVER_PORT
from s3lobby.server import ALL_INTERFACES, DEFAULT_SERVER_NAME, ServerSettings, serve_forever

LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s"
LOG_TIME_FORMAT = "%H:%M:%S"


def parse_arguments(arguments: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="s3lobby",
        description="Lobby server for the original The Settlers III online lobby.",
    )
    parser.add_argument("--host", default=ALL_INTERFACES, help="address to listen on (default: all interfaces)")
    parser.add_argument("--port", type=int, default=DEFAULT_SERVER_PORT, help="UDP port (default: %(default)s)")
    parser.add_argument("--name", default=DEFAULT_SERVER_NAME, help="server name shown in the game (default: %(default)s)")
    parser.add_argument(
        "--public-host",
        default="",
        help="host name or IP that clients from the internet log on to, e.g. your router's public address",
    )
    parser.add_argument("--verbose", action="store_true", help="log every datagram with its bytes")
    parser.add_argument("--log-file", help="also write the log to this file")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(arguments)


def configure_logging(is_verbose: bool, log_file: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    level = logging.DEBUG if is_verbose else logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT, datefmt=LOG_TIME_FORMAT, handlers=handlers)
    # asyncio announces its event loop selector at debug level; that is noise in this log.
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def main(arguments: Sequence[str] | None = None) -> int:
    options = parse_arguments(arguments)
    configure_logging(options.verbose, options.log_file)
    settings = ServerSettings(
        bind_host=options.host,
        port=options.port,
        server_name=options.name,
        public_host=options.public_host,
    )
    try:
        asyncio.run(serve_forever(settings))
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("stopped")
    except OSError as error:
        logging.getLogger(__name__).error("cannot serve on UDP %s:%d: %s", options.host, options.port, error)
        return 1
    return 0
