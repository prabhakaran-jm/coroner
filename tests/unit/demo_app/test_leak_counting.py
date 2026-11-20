"""Focused tests for LeakStore and RequestCountMiddleware (Task 4.10)."""

from __future__ import annotations

import asyncio
import threading

import pytest

from coroner.demo_app.counting import RequestCountMiddleware
from coroner.demo_app.leak import LeakStore

# ---------------------------------------------------------------- LeakStore


def test_initial_state() -> None:
    s = LeakStore()
    assert s.total == 0
    assert s.chunk_count == 0


def test_retain_one_byte() -> None:
    s = LeakStore()
    assert s.retain(1) == 1
    assert s.total == 1
    assert s._chunks == [b"\xa5"]  # white-box: private list inspected read-only


def test_cumulative_exact_and_chunks_untouched() -> None:
    s = LeakStore()
    sizes = [1, 10, 4096, 100_000, 7]
    running = 0
    seen: list[bytes] = []
    for n in sizes:
        running += n
        assert s.retain(n) == running
        assert s.total == running
        seen.append(s._chunks[-1])
        # earlier chunks are the same objects, unchanged
        assert len(s._chunks) == len(seen)
        for kept, original, size in zip(s._chunks, seen, sizes):
            assert kept is original
            assert len(kept) == size
    assert all(set(c) == {0xA5} for c in s._chunks)
    assert s.chunk_count == len(sizes)


def test_invalid_n_rejected_without_change() -> None:
    s = LeakStore()
    s.retain(5)
    for bad, exc in ((-1, ValueError), (1.5, TypeError), ("3", TypeError), (True, TypeError)):
        with pytest.raises(exc):
            s.retain(bad)  # type: ignore[arg-type]
    assert (s.total, s.chunk_count) == (5, 1)


def test_allocation_failure_is_atomic() -> None:
    s = LeakStore()
    s.retain(3)

    def boom(n: int) -> bytes:
        raise MemoryError

    s._allocate = boom  # type: ignore[method-assign]
    with pytest.raises(MemoryError):
        s.retain(10)
    assert (s.total, s.chunk_count) == (3, 1)
    del s._allocate  # restore class behaviour
    assert s.retain(2) == 5


def test_append_failure_is_atomic() -> None:
    class BadList(list):
        fail = False

        def append(self, item):  # type: ignore[override]
            if self.fail:
                raise RuntimeError("append failed")
            super().append(item)

    s = LeakStore()
    bad = BadList()
    s._chunks = bad
    s.retain(4)
    bad.fail = True
    with pytest.raises(RuntimeError):
        s.retain(9)
    assert s.total == 4
    assert len(bad) == 1
    bad.fail = False
    assert s.retain(1) == 5


class _FlakyStore(LeakStore):
    @staticmethod
    def _allocate(n: int) -> bytes:
        if n % 5 == 0:
            raise MemoryError
        return b"\xa5" * n


def test_concurrent_retain_with_failures() -> None:
    s = _FlakyStore()
    threads_n, calls = 16, 50
    barrier = threading.Barrier(threads_n)
    ok_sizes: list[list[int]] = [[] for _ in range(threads_n)]
    failures = [0] * threads_n
    errors: list[BaseException] = []

    def work(i: int) -> None:
        try:
            barrier.wait()
            for j in range(calls):
                n = 1 + (i * 7 + j * 3) % 40
                try:
                    s.retain(n)
                    ok_sizes[i].append(n)
                except MemoryError:
                    failures[i] += 1
        except BaseException as e:  # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=work, args=(i,)) for i in range(threads_n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    assert sum(failures) > 0
    flat = [n for lst in ok_sizes for n in lst]
    assert s.total == sum(flat)
    assert s.chunk_count == len(flat)
    assert sorted(len(c) for c in s._chunks) == sorted(flat)
    assert all(set(c) == {0xA5} for c in s._chunks)


# ------------------------------------------------------ RequestCountMiddleware


class FakeProbe:
    def __init__(self) -> None:
        self.count = 0

    def count_request(self) -> None:
        self.count += 1


def _run(mw, scope, receive=None, send=None) -> None:
    async def r():
        return {"type": "http.request"}

    async def s(m):
        pass

    asyncio.run(mw(scope, receive or r, send or s))


def _recording_app(probe: FakeProbe, log: dict, status: int = 200, send_body: bool = True):
    async def app(scope, receive, send):
        log["args"] = (scope, receive, send)
        log["count_at_entry"] = probe.count
        if send_body:
            await send({"type": "http.response.start", "status": status, "headers": []})
            await send({"type": "http.response.body", "body": b""})

    return app


@pytest.mark.parametrize("status", [200, 404, 422, 500])
def test_http_counts_once_regardless_of_status(status: int) -> None:
    p, log = FakeProbe(), {}
    _run(RequestCountMiddleware(_recording_app(p, log, status), p), {"type": "http"})
    assert p.count == 1


def test_count_before_downstream_and_identity() -> None:
    p, log = FakeProbe(), {}
    scope = {"type": "http"}

    async def r():
        return {}

    async def s(m):
        pass

    _run(RequestCountMiddleware(_recording_app(p, log), p), scope, r, s)
    assert log["count_at_entry"] == 1
    assert log["args"][0] is scope and log["args"][1] is r and log["args"][2] is s


def test_no_body_still_counted() -> None:
    p, log = FakeProbe(), {}
    _run(RequestCountMiddleware(_recording_app(p, log, send_body=False), p), {"type": "http"})
    assert p.count == 1


def test_multiple_requests_count_individually() -> None:
    p, log = FakeProbe(), {}
    mw = RequestCountMiddleware(_recording_app(p, log), p)
    for _ in range(5):
        _run(mw, {"type": "http"})
    assert p.count == 5


def test_downstream_exception_counted_and_propagates() -> None:
    p = FakeProbe()

    async def app(scope, receive, send):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        _run(RequestCountMiddleware(app, p), {"type": "http"})
    assert p.count == 1


def test_websocket_and_lifespan_not_counted() -> None:
    p, log = FakeProbe(), {}
    mw = RequestCountMiddleware(_recording_app(p, log, send_body=False), p)
    _run(mw, {"type": "websocket"})
    _run(mw, {"type": "lifespan"})
    assert p.count == 0
    assert log["args"][0] == {"type": "lifespan"}  # still forwarded

    # lifespan startup and shutdown messages through one lifespan scope
    msgs = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
    sent: list[str] = []

    async def life_app(scope, receive, send):
        while True:
            m = await receive()
            await send({"type": m["type"] + ".complete"})
            if m["type"] == "lifespan.shutdown":
                return

    async def recv():
        return next(msgs)

    async def snd(m):
        sent.append(m["type"])

    _run(RequestCountMiddleware(life_app, p), {"type": "lifespan"}, recv, snd)
    assert p.count == 0
    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


def test_real_starlette_404_and_200_counted() -> None:
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    async def ok(request):
        return PlainTextResponse("ok")

    p = FakeProbe()
    mw = RequestCountMiddleware(Starlette(routes=[Route("/ok", ok)]), p)
    with TestClient(mw) as c:  # lifespan scope runs, must not count
        assert c.get("/ok").status_code == 200
        assert c.get("/missing").status_code == 404
        assert c.post("/missing").status_code == 404
    assert p.count == 3
