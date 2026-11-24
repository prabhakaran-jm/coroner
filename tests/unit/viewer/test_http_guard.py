"""HTTP guard and route-surface tests for the Viewer (task 1.10).

Everything runs in process. Requests go through starlette's `TestClient` (the Host header is set
explicitly on every request) or, where a client cannot express the case (zero or repeated Host
headers, literal `..` path segments), through a tiny raw ASGI caller. No socket is bound or
connected, and port 80 is never bound: the port-80 rules are checked through `host_allowed(h, 80)`
and through `HostGuard(stub_app, 80)`. Fixtures live under pytest's `tmp_path`, built with
`RunsBuilder`; the real `runs/` directory is never read.

Framework behaviour (decides whether `MethodGuard` is needed): the pinned FastAPI/Starlette already
answer HEAD and OPTIONS on a GET-only route with 405 and `Allow: GET`, so `MethodGuard` stays
unwired in the app. `MethodGuard` itself is unit tested directly.

Requirements: 1.10, 1.11, 2.3, 2.4, 2.5, 2.12, 9.10, 11.8, 11.19, 11.20, 11.21, 11.23,
13.1-13.15.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from coroner.viewer.app import create_app
from coroner.viewer.files import DEFAULT_IO
from coroner.viewer.hostcheck import HostGuard, MethodGuard, SecurityHeaders, host_allowed
from tests.unit.viewer.builder import RunsBuilder

PORT = 8765
OTHER_PORT = 8766
UI_ROUTES = ["/", "/runs/{run_id:path}", "/compare", "/static/{asset:path}"]
API_ROUTES = ["/api/runs", "/api/runs/{run_id:path}"]
STATIC_DIR = Path(__file__).resolve().parents[3] / "coroner" / "viewer" / "static"
MUTATING_AND_OTHER = ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
FIXED_404 = '{"error":{"code":"not_found","message":"Not found."}}'


# ---------------------------------------------------------------- helpers


class SpyIO:
    """Counts every call that reaches the filesystem seam, then delegates to the real IO."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _count(self, name: str) -> None:
        self.calls.append(name)

    def scandir(self, path: str) -> Any:
        self._count("scandir")
        return DEFAULT_IO.scandir(path)

    def lstat(self, path: str) -> Any:
        self._count("lstat")
        return DEFAULT_IO.lstat(path)

    def open(self, path: str, flags: int) -> int:
        self._count("open")
        return DEFAULT_IO.open(path, flags)

    def fstat(self, fd: int) -> Any:
        self._count("fstat")
        return DEFAULT_IO.fstat(fd)

    def read(self, fd: int, n: int) -> bytes:
        self._count("read")
        return DEFAULT_IO.read(fd, n)

    def close(self, fd: int) -> None:
        self._count("close")
        DEFAULT_IO.close(fd)

    def realpath(self, path: str, strict: bool = True) -> str:
        self._count("realpath")
        return DEFAULT_IO.realpath(path, strict)


class StubProvider:
    """DataProvider stub. `fail` makes both methods raise an error whose text holds a path."""

    def __init__(self, secret: str) -> None:
        self.fail = False
        self.secret = secret

    def list_runs(self) -> Mapping[str, Any]:
        if self.fail:
            raise RuntimeError(self.secret)
        return {"stub": "list"}

    def load_detail(self, run_id: str) -> Mapping[str, Any] | None:
        if self.fail:
            raise RuntimeError(self.secret)
        return {"stub": "detail", "run_id": run_id}


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.runs_dir = tmp_path / "runs"
        self.builder = RunsBuilder(self.runs_dir)
        self.run_id = self.builder.add_run("B")
        self.io = SpyIO()
        self.provider = StubProvider(f"boom {tmp_path} Traceback (most recent call last)")
        self.app = create_app(self.runs_dir, PORT, io=self.io, provider=self.provider)
        self.io.calls.clear()
        self.client = TestClient(
            self.app, base_url=f"http://127.0.0.1:{PORT}", raise_server_exceptions=False
        )

    def get(self, path: str, host: str | None = None, **kw: Any) -> Any:
        headers = {"Host": host if host is not None else f"127.0.0.1:{PORT}"}
        headers.update(kw.pop("headers", {}))
        return self.client.get(path, headers=headers, **kw)

    def request(self, method: str, path: str, host: str | None = None, **kw: Any) -> Any:
        headers = {"Host": host if host is not None else f"127.0.0.1:{PORT}"}
        headers.update(kw.pop("headers", {}))
        return self.client.request(method, path, headers=headers, **kw)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def asgi_call(
    app: Any,
    method: str,
    path: str,
    headers: list[tuple[str, str]],
    scope_type: str = "http",
) -> tuple[int | None, dict[str, list[str]], bytes, list[dict[str, Any]]]:
    """Call an ASGI app directly. Returns status, header map, body and every sent message."""
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": scope_type,
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1", "replace"),
        "root_path": "",
        "query_string": b"",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", PORT),
    }
    asyncio.run(app(scope, receive, send))
    status = None
    hmap: dict[str, list[str]] = {}
    body = b""
    for message in sent:
        if message["type"] == "http.response.start":
            status = message["status"]
            for k, v in message["headers"]:
                hmap.setdefault(k.decode("latin-1").lower(), []).append(v.decode("latin-1"))
        elif message["type"] == "http.response.body":
            body += message.get("body", b"")
    return status, hmap, body, sent


class RecordingApp:
    """Stub ASGI app: records calls and answers 200 with a configurable content type."""

    def __init__(self, content_type: bytes = b"application/json", extra: tuple = ()) -> None:
        self.calls = 0
        self.content_type = content_type
        self.extra = extra

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.calls += 1
        if scope["type"] != "http":
            return
        headers = [(b"content-type", self.content_type), *self.extra]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": b"{}"})


def host_header(host: str | None) -> list[tuple[str, str]]:
    return [] if host is None else [("host", host)]


# ---------------------------------------------------------------- host_allowed


@pytest.mark.parametrize(
    ("host", "port", "expected"),
    [
        # port other than 80
        ("localhost:8765", 8765, True),
        ("127.0.0.1:8765", 8765, True),
        ("[::1]:8765", 8765, False),
        ("localhost", 8765, False),
        ("127.0.0.1", 8765, False),
        ("localhost:8766", 8765, False),
        ("127.0.0.1:8766", 8765, False),
        ("127.0.0.1.evil.test:8765", 8765, False),
        ("evil.test:8765", 8765, False),
        ("LOCALHOST:8765", 8765, False),
        ("Localhost:8765", 8765, False),
        ("localhost:08765", 8765, False),
        ("localhost:8765 ", 8765, False),
        (" localhost:8765", 8765, False),
        ("localhost:8765\n", 8765, False),
        ("localhost.:8765", 8765, False),
        ("localhost:8765,localhost:8765", 8765, False),
        ("", 8765, False),
        # port 80 exception
        ("localhost", 80, True),
        ("127.0.0.1", 80, True),
        ("localhost:80", 80, True),
        ("127.0.0.1:80", 80, True),
        ("localhost:8080", 80, False),
        ("127.0.0.1:8080", 80, False),
        ("[::1]", 80, False),
        ("[::1]:80", 80, False),
        ("LOCALHOST", 80, False),
        ("localhost.", 80, False),
        ("evil.test", 80, False),
        # the exception is only for port 80
        ("localhost", 443, False),
        ("localhost:80", 8765, False),
    ],
)
def test_host_allowed_table(host: str, port: int, expected: bool) -> None:
    assert host_allowed(host, port) is expected


# ---------------------------------------------------------------- HostGuard on the app


@pytest.mark.parametrize("host", [f"localhost:{PORT}", f"127.0.0.1:{PORT}"])
def test_host_accepted_on_non_80_port(env: Env, host: str) -> None:
    assert env.get("/", host=host).status_code == 200
    assert env.get("/api/runs", host=host).status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        f"[::1]:{PORT}",
        "localhost",
        "127.0.0.1",
        f"localhost:{OTHER_PORT}",
        f"127.0.0.1:{OTHER_PORT}",
        f"127.0.0.1.evil.test:{PORT}",
        f"LOCALHOST:{PORT}",
        f"evil.test:{PORT}",
    ],
)
@pytest.mark.parametrize("path", ["/", "/api/runs", "/static/index.html", "/nope"])
def test_host_rejected_is_421_with_fixed_body(env: Env, host: str, path: str) -> None:
    response = env.get(path, host=host)
    assert response.status_code == 421
    assert host not in response.text
    assert "evil" not in response.text
    assert "misdirected_host" in response.text
    if path.startswith("/api"):
        assert response.json() == {
            "error": {"code": "misdirected_host", "message": "Host not served here."}
        }
        assert response.headers["content-type"].startswith("application/json")
    else:
        assert response.headers["content-type"].startswith("text/plain")
        assert response.text.count("\n") == 1


def test_host_missing_is_400(env: Env) -> None:
    status, headers, body, _ = asgi_call(env.app, "GET", "/api/runs", host_header(None))
    assert status == 400
    assert b"missing_host" in body
    assert headers["cache-control"] == ["no-store"]
    status, _, body, _ = asgi_call(env.app, "GET", "/", host_header(None))
    assert status == 400
    assert body.endswith(b"\n")


def test_host_repeated_is_400_even_when_both_are_valid(env: Env) -> None:
    both = [("host", f"localhost:{PORT}"), ("host", f"127.0.0.1:{PORT}")]
    status, _, body, _ = asgi_call(env.app, "GET", "/api/runs", both)
    assert status == 400
    assert b"missing_host" in body
    same = [("host", f"localhost:{PORT}"), ("host", f"localhost:{PORT}")]
    assert asgi_call(env.app, "GET", "/", same)[0] == 400


def test_host_guard_rejection_does_no_file_io(env: Env) -> None:
    env.get(f"/api/runs/{env.run_id}", host="evil.test")
    asgi_call(env.app, "GET", f"/api/runs/{env.run_id}", [])
    assert env.io.calls == []


# ---------------------------------------------------------------- HostGuard on a stub ASGI app


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "localhost:80", "127.0.0.1:80"])
def test_port_80_guard_accepts(host: str) -> None:
    inner = RecordingApp()
    status, _, _, _ = asgi_call(HostGuard(inner, 80), "GET", "/api/runs", host_header(host))
    assert status == 200
    assert inner.calls == 1


@pytest.mark.parametrize("host", ["localhost:8080", "[::1]", "[::1]:80", "LOCALHOST", "evil.test"])
def test_port_80_guard_rejects_421(host: str) -> None:
    inner = RecordingApp()
    status, _, body, _ = asgi_call(HostGuard(inner, 80), "GET", "/", host_header(host))
    assert status == 421
    assert inner.calls == 0
    assert host.encode() not in body


def test_port_80_guard_missing_and_repeated_are_400() -> None:
    inner = RecordingApp()
    guard = HostGuard(inner, 80)
    assert asgi_call(guard, "GET", "/", [])[0] == 400
    assert asgi_call(guard, "GET", "/", [("host", "localhost"), ("host", "localhost")])[0] == 400
    assert inner.calls == 0


def test_port_other_than_80_guard_rejects_bare_hostname() -> None:
    inner = RecordingApp()
    assert asgi_call(HostGuard(inner, PORT), "GET", "/", host_header("localhost"))[0] == 421
    assert inner.calls == 0


def test_host_guard_closes_websocket_and_passes_lifespan() -> None:
    inner = RecordingApp()
    guard = HostGuard(inner, PORT)
    _, _, _, sent = asgi_call(guard, "GET", "/", host_header("evil.test"), scope_type="websocket")
    assert sent == [{"type": "websocket.close", "code": 1008}]
    assert inner.calls == 0
    asgi_call(guard, "GET", "/", [], scope_type="lifespan")
    assert inner.calls == 1


# ---------------------------------------------------------------- SecurityHeaders


def test_security_headers_on_json_response() -> None:
    inner = RecordingApp(b"application/json")
    _, headers, _, _ = asgi_call(SecurityHeaders(inner), "GET", "/api/runs", [])
    assert headers["cache-control"] == ["no-store"]
    assert headers["x-content-type-options"] == ["nosniff"]
    assert headers["referrer-policy"] == ["no-referrer"]
    assert headers["cross-origin-resource-policy"] == ["same-origin"]
    assert "content-security-policy" not in headers
    assert "x-frame-options" not in headers


def test_security_headers_on_html_response() -> None:
    inner = RecordingApp(b"text/html; charset=utf-8")
    _, headers, _, _ = asgi_call(SecurityHeaders(inner), "GET", "/", [])
    csp = headers["content-security-policy"][0]
    for directive in ("default-src 'none'", "script-src 'self'", "style-src 'self'",
                      "img-src 'self'", "font-src 'self'", "connect-src 'self'",
                      "frame-ancestors 'none'"):
        assert directive in csp
    assert headers["x-frame-options"] == ["DENY"]
    assert headers["cache-control"] == ["no-store"]


def test_security_headers_replace_cache_control_and_strip_cors() -> None:
    inner = RecordingApp(
        b"application/json",
        extra=(
            (b"cache-control", b"max-age=999"),
            (b"access-control-allow-origin", b"*"),
            (b"Access-Control-Allow-Credentials", b"true"),
        ),
    )
    _, headers, _, _ = asgi_call(SecurityHeaders(inner), "GET", "/", [])
    assert headers["cache-control"] == ["no-store"]
    assert not [name for name in headers if name.startswith("access-control-")]


# ---------------------------------------------------------------- MethodGuard (unit, unwired)


def test_method_guard_passes_get_through() -> None:
    inner = RecordingApp()
    status, _, _, _ = asgi_call(MethodGuard(inner), "GET", "/api/runs", [])
    assert status == 200
    assert inner.calls == 1


@pytest.mark.parametrize("method", MUTATING_AND_OTHER)
def test_method_guard_answers_405_with_allow_get(method: str) -> None:
    inner = RecordingApp()
    status, headers, body, _ = asgi_call(MethodGuard(inner), method, "/api/runs", [])
    assert status == 405
    assert headers["allow"] == ["GET"]
    assert inner.calls == 0
    assert b'"code":"method_not_allowed"' in body
    status, headers, body, _ = asgi_call(MethodGuard(inner), method, "/compare", [])
    assert status == 405
    assert headers["content-type"] == ["text/plain; charset=utf-8"]
    assert body.endswith(b"\n")


def test_method_guard_ignores_non_http_scopes() -> None:
    inner = RecordingApp()
    asgi_call(MethodGuard(inner), "POST", "/", [], scope_type="lifespan")
    assert inner.calls == 1


# ---------------------------------------------------------------- route surface


def test_registered_route_set_is_exactly_the_specified_surface(env: Env) -> None:
    paths = sorted(route.path for route in env.app.routes)
    assert paths == sorted(UI_ROUTES + API_ROUTES)
    for route in env.app.routes:
        assert set(route.methods) == {"GET"}
    api_paths = [p for p in paths if p == "/api" or p.startswith("/api/")]
    assert sorted(api_paths) == sorted(API_ROUTES)


def test_docs_endpoints_are_disabled_in_the_factory(env: Env) -> None:
    assert env.app.docs_url is None
    assert env.app.redoc_url is None
    assert env.app.openapi_url is None
    assert env.app.router.redirect_slashes is False


def test_no_cors_middleware_installed_and_methodguard_unwired(env: Env) -> None:
    classes = {m.cls.__name__ for m in env.app.user_middleware}
    assert classes == {"HostGuard", "SecurityHeaders"}
    assert not any("cors" in name.lower() for name in classes)
    assert MethodGuard not in {m.cls for m in env.app.user_middleware}


@pytest.mark.parametrize(
    "path",
    [
        "/docs",
        "/redoc",
        "/openapi.json",
        "/docs/oauth2-redirect",
        "/nope",
        "/runs",
        "/compare/x",
        "/static",
        "/api",
        "/api/",
        "/index.html",
        "/api/runs/../other-marker",
    ],
)
def test_unregistered_paths_are_404_with_fixed_body(env: Env, path: str) -> None:
    response = env.get(path)
    assert response.status_code == 404
    assert "other-marker" not in response.text
    if path.startswith("/api"):
        assert response.text == FIXED_404
    else:
        assert response.headers["content-type"].startswith("text/plain")
        assert response.text == FIXED_404 + "\n"


def test_unregistered_path_with_post_is_404_not_405(env: Env) -> None:
    assert env.request("POST", "/docs").status_code == 404
    assert env.request("POST", "/openapi.json").status_code == 404


def _route_urls(run_id: str) -> list[str]:
    return ["/", f"/runs/{run_id}", "/compare", "/static/index.html", "/api/runs",
            f"/api/runs/{run_id}"]


@pytest.mark.parametrize("method", MUTATING_AND_OTHER)
@pytest.mark.parametrize("index", range(6))
def test_non_get_methods_are_405_with_allow_get_on_every_route(
    env: Env, method: str, index: int
) -> None:
    url = _route_urls(env.run_id)[index]
    before = env.builder.snapshot()
    response = env.request(method, url, headers={"Origin": "http://evil.test"})
    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert response.headers["cache-control"] == "no-store"
    if method != "HEAD":
        assert "method_not_allowed" in response.text
        assert "evil" not in response.text
    assert env.builder.snapshot() == before
    assert not [h for h in response.headers if h.lower().startswith("access-control-")]


def test_framework_answers_head_and_options_with_405_allow_get() -> None:
    """The framework behaviour that keeps MethodGuard unwired (task 1.6 / 1.10)."""
    bare = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @bare.get("/x")
    def x() -> dict[str, int]:
        return {"x": 1}

    client = TestClient(bare, base_url="http://127.0.0.1:1", raise_server_exceptions=False)
    assert client.get("/x").status_code == 200
    for method in ("HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"):
        response = client.request(method, "/x")
        assert response.status_code == 405, method
        assert response.headers["allow"] == "GET", method


# ---------------------------------------------------------------- response headers


def test_cache_control_on_every_kind_of_response(env: Env) -> None:
    env.provider.fail = False
    responses = [
        env.get("/"),
        env.get(f"/runs/{env.run_id}"),
        env.get("/compare"),
        env.get("/static/index.html"),
        env.get("/api/runs"),
        env.get(f"/api/runs/{env.run_id}"),
        env.get("/api/runs/bad%2Fid"),
        env.get("/runs/bad%2Fid"),
        env.get("/api/runs/b-20261120T211300Z-ffffffff"),
        env.get("/nope"),
        env.get("/docs"),
        env.get("/", host="evil.test"),
        env.request("POST", "/api/runs"),
    ]
    env.provider.fail = True
    responses.append(env.get("/api/runs"))
    assert responses[-1].status_code == 500
    for response in responses:
        assert response.headers["cache-control"] == "no-store", response.status_code
        assert response.headers["x-content-type-options"] == "nosniff"
        assert not [h for h in response.headers if h.lower().startswith("access-control-")]


def test_api_responses_are_json_with_nosniff(env: Env) -> None:
    for url in ("/api/runs", f"/api/runs/{env.run_id}", "/api/runs/bad%2Fid", "/api/zzz"):
        response = env.get(url)
        assert response.headers["content-type"].startswith("application/json"), url
        assert response.headers["x-content-type-options"] == "nosniff"
    assert env.get("/api/runs").json() == {"stub": "list"}
    detail = env.get(f"/api/runs/{env.run_id}")
    assert detail.status_code == 200
    assert detail.json() == {"stub": "detail", "run_id": env.run_id}


@pytest.mark.parametrize("index", [0, 1, 2, 3])
def test_html_responses_carry_csp_and_frame_options(env: Env, index: int) -> None:
    response = env.get(_route_urls(env.run_id)[index])
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["x-frame-options"] == "DENY"


def test_no_cors_headers_even_for_cross_origin_requests(env: Env) -> None:
    preflight = {
        "Origin": "http://evil.test",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "x-test",
    }
    for url in ("/", "/api/runs", "/static/index.html", "/nope"):
        for method in ("GET", "OPTIONS"):
            response = env.request(method, url, headers=preflight)
            assert not [h for h in response.headers if h.lower().startswith("access-control-")]
    assert env.request("OPTIONS", "/api/runs", headers=preflight).status_code == 405


# ---------------------------------------------------------------- static allowlist


def test_static_index_is_served_from_the_package_directory(env: Env) -> None:
    response = env.get("/static/index.html")
    assert response.status_code == 200
    assert response.content == (STATIC_DIR / "index.html").read_bytes()
    assert set(env.app.state.static_assets) <= {p.name for p in STATIC_DIR.iterdir()}


@pytest.mark.parametrize(
    "path",
    [
        "/static/%2e%2e%2fcli.py",
        "/static/..%2f..%2fcoroner%2Fviewer%2Fcli.py",
        "/static/%2e%2e",
        "/static/a%2Fb",
        "/static/a%5Cb",
        "/static/..%5Cindex.html",
        "/static/C:",
        "/static/C%3A%5Cwindows%5Cwin.ini",
        "/static/C:index.html",
        "/static/INDEX.HTML",
        "/static/index.html%2F",
        "/static/index.html%00",
        "/static/",
        "/static/cli.py",
        "/static/__init__.py",
        "/static/.hidden.js",
        "/static/missing.js",
        "/static/manifest.json",
        "/static/evidence.jsonl",
    ],
)
def test_static_traversal_and_unlisted_names_are_404(env: Env, path: str) -> None:
    response = env.get(path)
    assert response.status_code == 404
    assert response.text.strip() == FIXED_404
    assert "root:" not in response.text


@pytest.mark.parametrize("name", ["..", "a/b", "a\\b", "C:", "../index.html", "..\\index.html"])
def test_static_literal_names_are_404_and_do_no_file_io(env: Env, name: str) -> None:
    status, _, body, _ = asgi_call(
        env.app, "GET", "/static/" + name, host_header(f"127.0.0.1:{PORT}")
    )
    assert status == 404
    assert body.decode() == FIXED_404 + "\n"
    assert env.io.calls == []


def test_no_run_directory_file_is_served_through_static(env: Env) -> None:
    for name in ("manifest.json", "evidence.jsonl", "docker_inspect.json"):
        for path in (f"/static/{env.run_id}/{name}", f"/static/{env.run_id}%2F{name}",
                     f"/static/{name}"):
            assert env.get(path).status_code == 404
    assert env.io.calls == []


# ---------------------------------------------------------------- run id handling


@pytest.mark.parametrize("prefix", ["/api/runs/", "/runs/"])
@pytest.mark.parametrize(
    "encoded",
    ["%2e%2e", "a%2Fb", "a%5Cb", "x" * 65, "b-20261120T211300Z-00000001%0A", "", "%20", "a%00b",
     "-leading", "evil%3Cscript%3E"],
)
def test_invalid_run_ids_are_400_with_zero_io(env: Env, prefix: str, encoded: str) -> None:
    response = env.get(prefix + encoded)
    assert response.status_code == 400
    assert "invalid_run_id" in response.text
    assert "script" not in response.text
    assert encoded not in response.text or encoded == ""
    assert env.io.calls == []
    assert "evil" not in response.text
    if prefix.startswith("/api"):
        assert response.json() == {
            "error": {"code": "invalid_run_id", "message": "The run id is not valid."}
        }
    else:
        assert response.headers["content-type"].startswith("text/plain")


@pytest.mark.parametrize("name", ["..", ".", "a/b", "a\\b", "C:", "x\n", ""])
@pytest.mark.parametrize("prefix", ["/api/runs/", "/runs/"])
def test_invalid_run_ids_literal_path_are_400_with_zero_io(env: Env, prefix: str, name: str) -> None:
    status, _, body, _ = asgi_call(
        env.app, "GET", prefix + name, host_header(f"127.0.0.1:{PORT}")
    )
    assert status == 400
    assert b"invalid_run_id" in body
    assert env.io.calls == []


def test_valid_unknown_run_id_is_404(env: Env) -> None:
    unknown = "b-20261120T211300Z-deadbeef"
    for url in (f"/api/runs/{unknown}", f"/runs/{unknown}"):
        response = env.get(url)
        assert response.status_code == 404
        assert "not_found" in response.text
        assert unknown not in response.text
    assert env.io.calls  # a valid id does reach the listing


def test_valid_known_run_id_is_served(env: Env) -> None:
    assert env.get(f"/runs/{env.run_id}").status_code == 200
    assert env.get(f"/api/runs/{env.run_id}").status_code == 200


# ---------------------------------------------------------------- internal errors


def test_provider_exception_is_500_internal_error_without_leaks(env: Env, tmp_path: Path) -> None:
    env.provider.fail = True
    for url in ("/api/runs", f"/api/runs/{env.run_id}"):
        response = env.get(url)
        assert response.status_code == 500
        assert response.json() == {
            "error": {"code": "internal_error", "message": "Unexpected error."}
        }
        text = response.text
        for leak in (str(tmp_path), "Traceback", "boom", "RuntimeError", ".py", os.sep + "runs"):
            assert leak not in text
        assert response.headers["cache-control"] == "no-store"
    env.provider.fail = False
    assert env.get("/api/runs").status_code == 200
    assert env.get(f"/api/runs/{env.run_id}").status_code == 200


def test_app_without_provider_fails_closed_with_500(tmp_path: Path) -> None:
    """With a provider that raises an error, the app returns 500 with a fixed body and no path leak."""
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    run_id = builder.add_run("B")
    # Inject a failing provider (simulates an unexpected internal error from the provider)
    provider = StubProvider(str(tmp_path))
    provider.fail = True
    app = create_app(runs, PORT, provider=provider)
    client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", raise_server_exceptions=False)
    for url in ("/api/runs", f"/api/runs/{run_id}"):
        response = client.get(url, headers={"Host": f"127.0.0.1:{PORT}"})
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "internal_error"
        assert str(tmp_path) not in response.text


# ---------------------------------------------------------------- error bodies never echo


def test_error_bodies_never_echo_request_values(env: Env) -> None:
    marker = "ZQmarker7391"
    bodies = [
        env.get(f"/{marker}").text,
        env.get(f"/api/{marker}").text,
        env.get(f"/api/runs/{marker}%2Fx").text,
        env.get(f"/runs/{marker}%2Fx").text,
        env.get(f"/static/{marker}.js").text,
        env.get("/nope", host=f"{marker}.test").text,
        env.request("POST", f"/api/runs/{marker}").text,
        env.request("DELETE", f"/{marker}").text,
        env.get(f"/api/runs/{marker}", headers={"X-Probe": marker}).text,
        env.get(f"/nope?q={marker}").text,
    ]
    for body in bodies:
        assert marker not in body
        assert str(env.runs_dir) not in body
