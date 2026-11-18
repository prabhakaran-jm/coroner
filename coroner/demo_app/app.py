"""Demo_App HTTP API (Requirements 1.1, 1.5, 1.6, 1.8, 1.9, 1.10, 1.11, 9.12, 15.6).

Routes:
- GET /health: always routed.
- POST /leak?bytes=N, POST /crash, GET /error: routed ONLY when the env mapping passed to
  create_app has CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS exactly equal to "1". Otherwise they do not
  exist and the framework answers 404 (no 403, no lenient parsing).

POST /crash is a controlled fatal process exit via os._exit(1): it calls the injected exit_fn(1)
(default os._exit), so no exception is raised, no handler runs, and nothing is flushed.
GET /error is the opposite case: an ordinary exception that the framework turns into HTTP 500
while the process keeps running.

The query value of /leak is validated as plain ASCII digits (1 to 9 of them) and then range
checked to 1..104,857,600, because pydantic's lax int parsing would also accept " 5", "+5",
"5.0" and "1_000". Nothing is allocated unless validation passed.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from coroner.demo_app.counting import RequestCountMiddleware
from coroner.demo_app.leak import LeakStore

FLAG_NAME = "CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS"
MIN_LEAK_BYTES = 1
MAX_LEAK_BYTES = 104_857_600
_DIGITS = r"^[0-9]{1,9}$"
ERROR_MESSAGE = "ordinary exception for Experiment non-termination test"


def _run_id(probe) -> str:
    config = getattr(probe, "config", None) or probe._config  # Probe keeps its config private
    return config.run_id


def create_app(env: Mapping[str, str], *, probe, exit_fn: Callable[[int], object] = os._exit) -> FastAPI:
    """Build the Demo_App. Reads only `env` (never os.environ) and opens no network connection."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        probe.start()  # on the running loop; no evidence depends on shutdown
        try:
            yield
        finally:
            await probe.stop()

    app = FastAPI(lifespan=lifespan)
    store = LeakStore()
    app.state.leak_store = store
    app.add_middleware(RequestCountMiddleware, probe=probe)  # app-wide: counts 404s too

    @app.get("/health")
    def health() -> dict:
        state = probe.state
        return {
            "status": "ok",
            "pid": os.getpid(),
            "run_id": _run_id(probe),
            "probe": {"next_seq": state["next_seq"], "dropped": state["dropped"]},
        }

    if env.get(FLAG_NAME) == "1":

        @app.post("/leak")
        def leak(n: Annotated[str, Query(alias="bytes", pattern=_DIGITS)]):
            value = int(n)
            if not MIN_LEAK_BYTES <= value <= MAX_LEAK_BYTES:
                raise RequestValidationError(
                    [{
                        "type": "value_error",
                        "loc": ("query", "bytes"),
                        "msg": f"bytes must be from {MIN_LEAK_BYTES} to {MAX_LEAK_BYTES}",
                        "input": n,
                    }]
                )
            try:
                total = store.retain(value)
            except MemoryError:
                return JSONResponse(
                    {"error": "allocation_failed", "retained_bytes_total": store.total}, status_code=503
                )
            return {"retained_bytes_total": total, "requested_bytes": value}

        @app.post("/crash")
        def crash() -> dict:
            # Controlled fatal process exit via os._exit(1): not an exception, not a 500.
            exit_fn(1)
            # Reached only when a test injects an exit_fn that returns; os._exit never returns.
            return {"status": "exit_fn_returned"}

        @app.get("/error")
        def error() -> dict:
            raise RuntimeError(ERROR_MESSAGE)

    return app
