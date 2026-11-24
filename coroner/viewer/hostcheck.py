"""Host guard, security headers and method guard for the Viewer.

Pure ASGI classes (no BaseHTTPMiddleware). Error bodies are fixed strings and
never echo any request value.
"""

import json

_API_PREFIX = "/api"

_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)

_BASE_HEADERS = (
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-resource-policy", b"same-origin"),
)

_HTML_HEADERS = (
    (b"content-security-policy", _CSP.encode("ascii")),
    (b"x-frame-options", b"DENY"),
)


def host_allowed(host_header: str, port: int) -> bool:
    """Return True only for the literal Host values the Viewer accepts.

    Accepted: ``127.0.0.1:<port>`` and ``localhost:<port>`` (canonical decimal
    port), plus the two bare hostnames when ``port == 80``. Case-sensitive,
    byte-for-byte comparison, no parsing.
    """
    if not isinstance(host_header, str):
        return False
    if isinstance(port, bool) or not isinstance(port, int):
        return False
    text = str(port)
    allowed = {"127.0.0.1:" + text, "localhost:" + text}
    if port == 80:
        allowed.add("127.0.0.1")
        allowed.add("localhost")
    return host_header in allowed


def _is_api_path(scope) -> bool:
    path = scope.get("path", "")
    return isinstance(path, str) and (
        path == _API_PREFIX or path.startswith(_API_PREFIX + "/")
    )


async def _send_error(scope, send, status, code, message, extra_headers=()):
    body_obj = {"error": {"code": code, "message": message}}
    if _is_api_path(scope):
        body = json.dumps(body_obj, separators=(",", ":")).encode("utf-8")
        content_type = b"application/json"
    else:
        body = (json.dumps(body_obj, separators=(",", ":")) + "\n").encode("utf-8")
        content_type = b"text/plain; charset=utf-8"
    headers = [
        (b"content-type", content_type),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class HostGuard:
    """Reject requests whose Host header is missing, repeated or not allowed."""

    def __init__(self, app, port: int):
        self.app = app
        self.port = port

    async def __call__(self, scope, receive, send):
        scope_type = scope.get("type")
        if scope_type not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        values = [v for k, v in scope.get("headers", ()) if k.lower() == b"host"]
        rejected = None
        if len(values) != 1:
            rejected = (400, "missing_host", "A single Host header is required.")
        elif not host_allowed(values[0].decode("latin-1"), self.port):
            rejected = (421, "misdirected_host", "Host not served here.")
        if rejected is None:
            await self.app(scope, receive, send)
            return
        if scope_type == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await _send_error(scope, send, *rejected)


class SecurityHeaders:
    """Add the fixed security headers and strip any Access-Control-* header."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                original = list(message.get("headers", ()))
                is_html = False
                for name, value in original:
                    if name.lower() == b"content-type":
                        media = value.split(b";", 1)[0].strip().lower()
                        is_html = media == b"text/html"
                added = _BASE_HEADERS + (_HTML_HEADERS if is_html else ())
                replaced = {name for name, _ in added}
                kept = [
                    (name, value)
                    for name, value in original
                    if name.lower() not in replaced
                    and not name.lower().startswith(b"access-control-")
                ]
                message = dict(message)
                message["headers"] = kept + list(added)
            await send(message)

        await self.app(scope, receive, send_wrapper)


class MethodGuard:
    """Answer 405 with ``Allow: GET`` for any HTTP method other than GET."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("method") != "GET":
            await _send_error(
                scope,
                send,
                405,
                "method_not_allowed",
                "Only GET is supported.",
                ((b"allow", b"GET"),),
            )
            return
        await self.app(scope, receive, send)
