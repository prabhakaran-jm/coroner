"""FastAPI application factory of the Coroner Viewer.

Read-only and local. Exactly six GET routes, a static allowlist served from
memory, fixed-body error responses that never echo a request value, a host
path, an exception message or a traceback.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from coroner.runner.manifest import RunIdError, validate_run_id_for_path
from coroner.viewer.cli import ViewerConfigError
from coroner.viewer.compare import QueryError, parse_compare_query
from coroner.viewer.files import (
    DEFAULT_IO,
    ArtifactIO,
    RunsRootError,
    find_run,
    open_runs_root,
)
from coroner.viewer.hostcheck import HostGuard, SecurityHeaders
from coroner.viewer.loader import RunCache, load_detail, load_listing
from coroner.viewer.model import dump_json, to_detail, to_listing

__all__ = ["DataProvider", "create_app", "make_runs_dir_label"]

_LOG = logging.getLogger("coroner.viewer")

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_ASSET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\.(html|js|css)$")
_CONTENT_TYPES = {
    "html": "text/html; charset=utf-8",
    "js": "text/javascript; charset=utf-8",
    "css": "text/css; charset=utf-8",
}
_SHELL = "index.html"

_ERR_INVALID_RUN_ID = ("invalid_run_id", "The run id is not valid.")
_ERR_NOT_FOUND = ("not_found", "Not found.")
_ERR_METHOD = ("method_not_allowed", "Only GET is supported.")
_ERR_INTERNAL = ("internal_error", "Unexpected error.")
_COMPARE_BAD_QUERY = "Invalid compare parameter."


class DataProvider(Protocol):
    """Source of the API success bodies. The real one arrives in task 2.10."""

    def list_runs(self) -> Mapping[str, Any]: ...

    def load_detail(self, run_id: str) -> Mapping[str, Any] | None: ...


class _UnwiredProvider:
    """Fails closed: any API success body without a provider is an internal error."""

    def list_runs(self) -> Mapping[str, Any]:
        raise RuntimeError("no data provider configured")

    def load_detail(self, run_id: str) -> Mapping[str, Any] | None:
        raise RuntimeError("no data provider configured")


def make_runs_dir_label(runs_dir: str | os.PathLike[str]) -> str:
    """Runs_Directory_Label: as given when relative, final component when absolute."""
    text = os.fspath(runs_dir)
    if os.path.isabs(text) or os.path.splitdrive(text)[0]:
        return os.path.basename(os.path.normpath(text)) or "(runs directory)"
    return text


def _load_static_assets(directory: str) -> dict[str, tuple[bytes, str]]:
    """Read the allowlisted static files into memory (once, at start-up)."""
    assets: dict[str, tuple[bytes, str]] = {}
    with os.scandir(directory) as it:
        for entry in it:
            if _ASSET_NAME_RE.fullmatch(entry.name) is None:
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            with open(entry.path, "rb") as handle:
                data = handle.read()
            assets[entry.name] = (data, _CONTENT_TYPES[entry.name.rsplit(".", 1)[1]])
    return assets


def _is_api(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def _error(
    path: str,
    status: int,
    err: tuple[str, str],
    headers: Mapping[str, str] | None = None,
) -> Response:
    """Fixed error body: JSON for API paths, one line of text/plain for UI paths."""
    code, message = err
    body = '{"error":{"code":"%s","message":"%s"}}' % (code, message)
    if _is_api(path):
        return Response(
            body, status_code=status, media_type="application/json", headers=dict(headers or {})
        )
    return Response(
        body + "\n",
        status_code=status,
        media_type="text/plain; charset=utf-8",
        headers=dict(headers or {}),
    )


def _raw_id_differs(request: Request, prefix: str, run_id: str) -> bool:
    """True when the id Starlette matched is not the id in the raw ASGI path.

    The compiled route regex ends in ``$``, which also matches before a trailing
    newline, so ``x\n`` would reach the handler as ``x``. Compare against
    ``scope["path"]`` (already %-decoded) and reject any control character.
    """
    path = request.scope.get("path", "")
    if not path.startswith(prefix):
        return True
    raw = path[len(prefix) :]
    if raw != run_id:
        return True
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw)


def _safe(handler: Callable[..., Response]) -> Callable[..., Response]:
    """Turn any unexpected error into the fixed 500 body inside the middleware stack.

    Logs only the exception class name and the run id. The wrapped function
    keeps its signature for FastAPI and stays a plain ``def``.
    """

    @functools.wraps(handler)
    def wrapper(*args: Any, **kwargs: Any) -> Response:
        try:
            return handler(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - fixed 500 body by design
            run_id = kwargs.get("run_id")
            _LOG.error(
                "internal_error %s run_id=%s",
                type(exc).__name__,
                run_id if isinstance(run_id, str) else "-",
            )
            return _error(
                "/api/" if handler.__name__.startswith("api_") else "/", 500, _ERR_INTERNAL
            )

    return wrapper


def create_app(
    runs_dir: str | os.PathLike[str],
    port: int,
    *,
    runs_dir_label: str | None = None,
    io: ArtifactIO = DEFAULT_IO,
    cache: Any = None,
    clock_ns: Callable[[], int] = time.time_ns,
    provider: DataProvider | None = None,
) -> FastAPI:
    """Build the Viewer application. Raises ``ViewerConfigError`` for a bad runs dir."""
    try:
        root = open_runs_root(runs_dir)
    except RunsRootError as exc:
        raise ViewerConfigError(str(exc)) from None
    label = runs_dir_label if runs_dir_label is not None else make_runs_dir_label(runs_dir)

    assets = _load_static_assets(_STATIC_DIR)
    if _SHELL not in assets:
        raise ViewerConfigError("the packaged Viewer shell (static/index.html) is missing")
    shell_bytes, shell_type = assets[_SHELL]
    data_provider: DataProvider = provider if provider is not None else _UnwiredProvider()

    app = FastAPI(
        docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False
    )
    app.state.runs_root = root
    app.state.runs_dir_label = label
    app.state.io = io
    app.state.cache = cache
    app.state.clock_ns = clock_ns
    app.state.static_assets = assets

    # --- Real provider (default when provider=None) ---
    _run_cache = cache if cache is not None else RunCache(1024)

    def shell() -> Response:
        return Response(shell_bytes, media_type=shell_type)

    @app.get("/")
    @_safe
    def ui_index() -> Response:
        return shell()

    @app.get("/runs/{run_id:path}")
    @_safe
    def ui_run(request: Request, run_id: str) -> Response:
        if _raw_id_differs(request, "/runs/", run_id):
            return _error("/runs/", 400, _ERR_INVALID_RUN_ID)
        try:
            validate_run_id_for_path(run_id)
        except RunIdError:
            return _error("/runs/", 400, _ERR_INVALID_RUN_ID)
        if find_run(root, run_id, io) is None:
            return _error("/runs/", 404, _ERR_NOT_FOUND)
        return shell()

    @app.get("/compare")
    @_safe
    def ui_compare(request: Request) -> Response:
        # Validate every parameter before anything else; no file access either way.
        try:
            parse_compare_query(request.query_params.multi_items())
        except QueryError:
            return Response(
                _COMPARE_BAD_QUERY + "\n",
                status_code=400,
                media_type="text/plain; charset=utf-8",
            )
        return shell()

    @app.get("/static/{asset:path}")
    @_safe
    def ui_static(asset: str) -> Response:
        found = assets.get(asset)
        if found is None:
            return _error("/static/", 404, _ERR_NOT_FOUND)
        return Response(found[0], media_type=found[1])

    @app.get("/api/runs")
    @_safe
    def api_runs() -> Response:
        if provider is not None:
            return JSONResponse(data_provider.list_runs())
        listing = load_listing(root, io, clock_ns, _run_cache)
        body = to_listing(listing)
        return Response(content=dump_json(body), media_type="application/json")

    @app.get("/api/runs/{run_id:path}")
    @_safe
    def api_run_detail(request: Request, run_id: str) -> Response:
        if _raw_id_differs(request, "/api/runs/", run_id):
            return _error("/api/", 400, _ERR_INVALID_RUN_ID)
        try:
            validate_run_id_for_path(run_id)
        except RunIdError:
            return _error("/api/", 400, _ERR_INVALID_RUN_ID)
        if find_run(root, run_id, io) is None:
            return _error("/api/", 404, _ERR_NOT_FOUND)
        if provider is not None:
            body_map = data_provider.load_detail(run_id)
            if body_map is None:
                return _error("/api/", 404, _ERR_NOT_FOUND)
            return JSONResponse(body_map)
        record = load_detail(root, run_id, io, clock_ns)
        body = to_detail(record)
        return Response(content=dump_json(body), media_type="application/json")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        path = request.url.path
        if exc.status_code == 405:
            return _error(path, 405, _ERR_METHOD, {"Allow": "GET"})
        if exc.status_code == 404:
            return _error(path, 404, _ERR_NOT_FOUND)
        return _error(path, exc.status_code, _ERR_INTERNAL)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> Response:
        _LOG.error("internal_error %s run_id=-", type(exc).__name__)
        return _error(request.url.path, 500, _ERR_INTERNAL)

    # Outermost first: SecurityHeaders, then HostGuard. add_middleware prepends,
    # so register the inner one first.
    app.add_middleware(HostGuard, port=port)
    app.add_middleware(SecurityHeaders)
    return app
