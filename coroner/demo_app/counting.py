"""Pure ASGI middleware that counts every HTTP request (Requirement 2.4).

The probe is duck-typed: anything with a count_request() method works.
Counting happens once, before the downstream app runs, so 404, 422 and 500
responses and requests that never produce a body are all counted.
"""

from __future__ import annotations


class RequestCountMiddleware:
    def __init__(self, app, probe) -> None:
        self.app = app
        self.probe = probe

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            self.probe.count_request()
        await self.app(scope, receive, send)
