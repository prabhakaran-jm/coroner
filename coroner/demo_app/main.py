"""Demo_App process entry point: `python -m coroner.demo_app.main` (Requirement 1.1).

Importing this module starts nothing. A supplied but invalid CORONER_RUN_ID raises from
load_config and ends the process with a non-zero exit code; it is never replaced.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping

import uvicorn

from coroner.demo_app.app import create_app
from coroner.probe import Probe, load_config

HOST = "0.0.0.0"  # container-internal; Compose publishes it on 127.0.0.1 only
PORT = 8080


def main(env: Mapping[str, str] | None = None, *, run: Callable[..., object] | None = None) -> None:
    env = os.environ if env is None else env
    log = logging.getLogger("coroner.demo_app")
    config = load_config(env, log.warning)
    app = create_app(env, probe=Probe(config))
    (uvicorn.run if run is None else run)(app, host=HOST, port=PORT, workers=1)


if __name__ == "__main__":
    main()
