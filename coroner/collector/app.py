"""Collector HTTP API (Task 3.11; Requirements 4.1, 4.3, 4.4, 4.8, 4.17, 4.18, 5.1, 5.5; design "Collector HTTP API", D26, D31, D33).

`create_app` builds the FastAPI app and the `IngestService`, registry, storage probe and watchdog behind it. It does
no I/O. The FastAPI lifespan does the startup work, in this order, before the first request is served:

    1. `registry.start()`: create or verify the evidence directory, take the volume lock, repair and fsync every
       `*.jsonl` file, fsync the directory (design D26). A failure raises `CollectorStartupError` and the process
       exits non-zero (uvicorn reports "Application startup failed").
    2. `probe.run_startup()`: one forced storage probe, logged. A failed probe is NOT fatal: `/healthz` reports
       `storage_ok: false` and stays 200.
    3. `watchdog.start()`.

On shutdown the watchdog is stopped, then `registry.close()` releases the volume lock and closes the writer. Each step
is guarded, so a partial startup is cleaned up. Evidence and `.collector.lock` are never deleted.

Handlers never put parsing or reconstruction in the HTTP layer: the raw body goes to `IngestService`, and the status and
body the service returns go back unchanged. The body is read incrementally and bounded at `max_body_bytes + 1`.
`recv_time` is captured at the start of `IngestService.ingest`, which is after the HTTP layer has received the body;
that is the earliest point the service sees a request and it is unchanged.
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from coroner.collector.config import DEFAULT_EVIDENCE_DIR, CollectorConfig, load_config, load_config_or_exit
from coroner.collector.service import IngestService, create_ingest_service
from coroner.collector.state import CollectorStartupError, VolumeLock
from coroner.collector.storage_probe import StorageProbe
from coroner.collector.watchdog import SilenceWatchdog, create_watchdog
from coroner.common.timeutil import format_utc_ms, utc_now_ms
from coroner.evidence.schema import validate_run_id
from coroner.evidence.writer import REJECTIONS_STEM, fsync_directory

__all__ = ["COLLECTOR_HOST", "COLLECTOR_PORT", "create_app", "main"]

LOGGER_NAME = "coroner.collector.app"
logger = logging.getLogger(LOGGER_NAME)

COLLECTOR_HOST = "0.0.0.0"  # inside the container; Compose publishes it on 127.0.0.1 only (design, topology)
COLLECTOR_PORT = 8081
NDJSON = "application/x-ndjson"
_STORAGE_FAILURE = {"error": "storage_failure"}


def _declared_length(request: Request) -> int | None:
    value = request.headers.get("content-length")
    if value is None or not value.isascii() or not value.isdigit():
        return None
    return int(value)


async def _read_bounded(request: Request, limit: int) -> tuple[bytes, int | None, str | None]:
    """Read the body without ever holding more than `limit + 1` bytes.

    Returns `(raw, body_len, source)`. A declared Content-Length above `limit` returns `(b"", declared, "declared")`
    and reads nothing. Otherwise the stream is read until it ends or exceeds `limit`; in the latter case `raw` is
    exactly `limit + 1` bytes and `body_len` is that count (`source` "lower_bound": the true length was not measured).
    A body within the limit returns `(raw, None, None)`.
    """
    declared = _declared_length(request)
    if declared is not None and declared > limit:
        return b"", declared, "declared"
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        room = limit + 1 - total
        chunks.append(chunk[:room])
        total += min(len(chunk), room)
        if total > limit:
            break
    if total > limit:
        return b"".join(chunks), total, "lower_bound"
    return b"".join(chunks), None, None


def create_app(
    config: CollectorConfig | None = None,
    *,
    env: Mapping[str, str] | None = None,
    evidence_dir: Path | str | None = None,
    fsync: Callable[[int], None] = os.fsync,
    fsync_dir: Callable[[Path], None] = fsync_directory,
    now: Callable[[], str] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    volume_lock: VolumeLock | None = None,
    probe: StorageProbe | None = None,
) -> FastAPI:
    """Build the Collector app. `config` wins; otherwise it is loaded from `env` (default: empty) and `evidence_dir`."""
    if config is None:
        config = load_config({} if env is None else env, evidence_dir=DEFAULT_EVIDENCE_DIR if evidence_dir is None else evidence_dir)
    if probe is None:
        probe = StorageProbe(config.evidence_dir, fsync=fsync, fsync_dir=fsync_dir)
    service: IngestService = create_ingest_service(
        config, fsync=fsync, fsync_dir=fsync_dir, now=now, monotonic=monotonic, storage_probe=probe, volume_lock=volume_lock
    )
    watchdog: SilenceWatchdog = create_watchdog(service)
    registry = service.registry
    clock = now if now is not None else (lambda: format_utc_ms(utc_now_ms()))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ready = False
        try:
            registry.start()  # raises CollectorStartupError: no request is ever served
            result = probe.run_startup()
            if result.ok:
                logger.info("storage probe ok at startup")
            else:
                logger.warning("storage probe failed at startup: %s (serving with storage_ok=false)", result.error)
            started = monotonic()
            app.state.started_monotonic = started
            watchdog.start()
            app.state.ready = True
            yield
        finally:
            app.state.ready = False
            for step in (watchdog.stop, registry.close):
                try:
                    step()
                except Exception:  # keep shutting down; the lock release must still be attempted
                    logger.exception("shutdown step %s failed", getattr(step, "__qualname__", step))

    app = FastAPI(title="coroner-collector", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ready = False
    app.state.started_monotonic = None
    app.state.service = service
    app.state.registry = registry
    app.state.probe = probe
    app.state.watchdog = watchdog

    def not_ready() -> JSONResponse | None:
        if app.state.ready:
            return None
        return JSONResponse(_STORAGE_FAILURE, status_code=503)  # never route to the service before recovery completed

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        result = probe.check()
        started = app.state.started_monotonic
        body: dict[str, Any] = {
            "status": "ok",
            "durability": config.durability.value,
            "server_time": clock(),
            "uptime_s": 0 if started is None else max(0, int(monotonic() - started)),
            "storage_ok": result.ok,
        }
        if not result.ok:
            body["storage_error"] = result.error or "storage probe failed"
        return JSONResponse(body, status_code=200)

    @app.post("/v1/samples")
    async def post_sample(request: Request) -> JSONResponse:
        refused = not_ready()
        if refused is not None:
            return refused
        raw, body_len, source = await _read_bounded(request, config.max_body_bytes)
        result = await run_in_threadpool(service.ingest, raw, body_len=body_len, body_len_source=source)
        return JSONResponse(result.body, status_code=result.http_status)

    @app.post("/v1/runs/{run_id:path}/termination")
    async def post_termination(run_id: str, request: Request) -> JSONResponse:
        refused = not_ready()
        if refused is not None:
            return refused
        raw, body_len, source = await _read_bounded(request, config.max_body_bytes)
        result = await run_in_threadpool(service.record_termination, run_id, raw, body_len=body_len, body_len_source=source)
        return JSONResponse(result.body, status_code=result.http_status)

    @app.get("/v1/runs/{run_id:path}/evidence")
    def get_evidence(run_id: str) -> Response:
        refused = not_ready()
        if refused is not None:
            return refused
        issue = validate_run_id(run_id)
        if issue is not None or run_id == REJECTIONS_STEM:
            reason_code = issue.reason_code if issue is not None else "invalid_run_id"
            reason = issue.reason if issue is not None else "reserved name"
            return JSONResponse(
                {"error": "validation_failed", "reason_code": reason_code, "field": "run_id", "reason": reason}, status_code=422
            )
        path = registry.writer.path_for(run_id)
        if not path.is_file():  # checked first so an unknown id creates no in-memory state and no file
            return JSONResponse({"error": "not_found"}, status_code=404)
        try:
            with registry.lock(run_id):  # a consistent snapshot: no append runs while the file is read
                data = path.read_bytes()
        except FileNotFoundError:
            return JSONResponse({"error": "not_found"}, status_code=404)
        except OSError as exc:
            logger.warning("evidence read failed run_id=%s: %s: %s", run_id, type(exc).__name__, exc)
            return JSONResponse(_STORAGE_FAILURE, status_code=503)
        return Response(data, status_code=200, media_type=NDJSON)

    return app


def main(
    env: Mapping[str, str] | None = None,
    *,
    evidence_dir: Path | str = DEFAULT_EVIDENCE_DIR,
    run: Callable[..., Any] = uvicorn.run,
) -> None:
    """Entry point: load config (exit 2 on a bad value), build the app, serve with one uvicorn worker.

    Recovery, the volume lock, the startup probe and the watchdog happen in the app's lifespan, before the first
    request. A fatal startup error exits non-zero.
    """
    source = os.environ if env is None else env
    config = load_config_or_exit(source, evidence_dir=evidence_dir)
    app = create_app(config)
    try:
        run(app, host=COLLECTOR_HOST, port=COLLECTOR_PORT, workers=1, log_level="info")
    except CollectorStartupError as exc:
        print(f"coroner-collector: {exc}")
        raise SystemExit(1) from exc
