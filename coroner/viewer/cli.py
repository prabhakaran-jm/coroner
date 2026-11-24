"""Command line interface of the Coroner Viewer.

Validates the launch arguments and builds the uvicorn server configuration.
The bind host is the literal constant ``127.0.0.1``; the parsed ``--host``
argument is only validated, never used for binding. Nothing here parses
``sys.argv`` or opens a socket at import time.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import uvicorn

DEFAULT_RUNS_DIR = "runs"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

_BIND_HOST = "127.0.0.1"
_ECHO_LIMIT = 64
_PORT_RE = re.compile(r"[0-9]+")


class ViewerConfigError(Exception):
    """A launch configuration error. The message is safe to print."""


@dataclass(frozen=True)
class ViewerArgs:
    runs_dir: str
    host: str
    port: int


def _echo(value: str) -> str:
    """Return ``repr`` of the value cut to 64 characters."""
    return repr(value[:_ECHO_LIMIT])


def validate_host(host: str) -> str:
    """Accept only the exact string ``127.0.0.1``."""
    if host != _BIND_HOST:
        raise ViewerConfigError(
            f"the Viewer binds only to 127.0.0.1 (got {_echo(host)})"
        )
    return host


def validate_port(text: str) -> int:
    """Accept ASCII digits only, in the range 1 to 65535."""
    if _PORT_RE.fullmatch(text) is None or not text.isascii():
        raise ViewerConfigError(
            f"invalid port {_echo(text)}: expected an integer from 1 to 65535"
        )
    port = int(text)
    if not 1 <= port <= 65535:
        raise ViewerConfigError(
            f"invalid port {_echo(text)}: expected an integer from 1 to 65535"
        )
    return port


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m coroner.viewer",
        description="Read-only local forensic viewer for Coroner runs.",
    )
    parser.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", default=str(DEFAULT_PORT))
    return parser


def parse_args(argv: Sequence[str] | None = None) -> ViewerArgs:
    """Parse and validate arguments.

    argparse usage errors exit with code 2. Host and port problems raise
    ViewerConfigError.
    """
    ns = _build_parser().parse_args(None if argv is None else list(argv))
    host = validate_host(ns.host)
    port = validate_port(ns.port)
    return ViewerArgs(runs_dir=ns.runs_dir, host=host, port=port)


def _check_runs_dir(runs_dir: str) -> None:
    if not os.path.isdir(runs_dir):
        raise ViewerConfigError(
            f"the runs directory does not exist or is not a directory: {runs_dir}"
        )


def build_server_config(app: Any, args: ViewerArgs) -> uvicorn.Config:
    """Build the uvicorn configuration with the literal loopback host."""
    return uvicorn.Config(
        app,
        host="127.0.0.1",
        port=args.port,
        workers=1,
        reload=False,
        log_level="warning",
        access_log=False,
        lifespan="off",
        server_header=False,
        proxy_headers=False,
        ws="none",
        h11_max_incomplete_event_size=16384,
        timeout_keep_alive=5,
        limit_concurrency=64,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Validate arguments, build the app and run the server.

    Returns 2 for every configuration error and 1 for an unexpected
    start-up failure (fixed message, no path or traceback). The server is
    only constructed after all validation has passed.
    """
    try:
        args = parse_args(argv)
        _check_runs_dir(args.runs_dir)
        # Imported here: app.py imports ViewerConfigError from this module.
        from coroner.viewer.app import create_app

        app = create_app(args.runs_dir, args.port)
        label = app.state.runs_dir_label
        config = build_server_config(app, args)
        server = uvicorn.Server(config)
    except ViewerConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("error: the Viewer could not start", file=sys.stderr)
        return 1
    print(
        f"Coroner Viewer: http://127.0.0.1:{args.port}/  (runs: {label}, read-only)",
        flush=True,
    )
    try:
        server.run()
    except Exception:
        print("error: the Viewer stopped unexpectedly", file=sys.stderr)
        return 1
    return 0