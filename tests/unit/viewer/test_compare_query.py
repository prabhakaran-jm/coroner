"""`/compare` query validation tests (task 4.1).

Requests run in process through starlette's `TestClient` with an explicit Host header. A spy at
the filesystem seam proves that a rejected `/compare` request makes zero IO calls, and that a
valid one does too (the page itself resolves slots through the API). Fixtures live under
`tmp_path`; the real `runs/` directory is never read.

Requirements: 8.10, 8.11, 11.24, 13.1, 13.8, 13.16.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from coroner.viewer.app import create_app
from coroner.viewer.compare import CompareQuery, QueryError, parse_compare_query
from coroner.viewer.files import DEFAULT_IO
from tests.unit.viewer.builder import RunsBuilder

PORT = 8765
HOST = {"Host": f"127.0.0.1:{PORT}"}
VALID_A = "b-20261120T211300Z-00000001"
VALID_B = "b-20261120T211300Z-00000002"
VALID_C = "c-20261120T211300Z-00000003"
ABSENT = "b-20261120T211300Z-deadbeef"
FIXED_400 = "Invalid compare parameter.\n"


class SpyIO:
    """Counts every call that reaches the filesystem seam, then delegates to the real IO."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def scandir(self, path: str) -> Any:
        self.calls.append("scandir")
        return DEFAULT_IO.scandir(path)

    def lstat(self, path: str) -> Any:
        self.calls.append("lstat")
        return DEFAULT_IO.lstat(path)

    def open(self, path: str, flags: int) -> int:
        self.calls.append("open")
        return DEFAULT_IO.open(path, flags)

    def fstat(self, fd: int) -> Any:
        self.calls.append("fstat")
        return DEFAULT_IO.fstat(fd)

    def read(self, fd: int, n: int) -> bytes:
        self.calls.append("read")
        return DEFAULT_IO.read(fd, n)

    def close(self, fd: int) -> None:
        self.calls.append("close")
        DEFAULT_IO.close(fd)

    def realpath(self, path: str, strict: bool = True) -> str:
        self.calls.append("realpath")
        return DEFAULT_IO.realpath(path, strict)


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.runs_dir = tmp_path / "runs"
        self.run_id = RunsBuilder(self.runs_dir).add_run("B")
        self.io = SpyIO()
        self.app = create_app(self.runs_dir, PORT, io=self.io)
        self.io.calls.clear()
        self.client = TestClient(
            self.app, base_url=f"http://127.0.0.1:{PORT}", raise_server_exceptions=False
        )

    def get(self, url: str) -> Any:
        return self.client.get(url, headers=HOST)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# ---------------------------------------------------------------- parse_compare_query (pure)


def test_valid_query_parses_each_slot() -> None:
    q = parse_compare_query([("a", VALID_A), ("b", VALID_B), ("c", VALID_C)])
    assert q == CompareQuery(a=VALID_A, b=VALID_B, c=VALID_C)


def test_empty_query_and_partial_slots_are_valid() -> None:
    assert parse_compare_query([]) == CompareQuery(a=None, b=None, c=None)
    assert parse_compare_query([("b", VALID_B)]) == CompareQuery(a=None, b=VALID_B, c=None)


def test_slot_order_does_not_matter() -> None:
    q = parse_compare_query([("c", VALID_C), ("a", VALID_A)])
    assert (q.a, q.b, q.c) == (VALID_A, None, VALID_C)


@pytest.mark.parametrize(
    "items",
    [
        [("d", VALID_A)],
        [("A", VALID_A)],
        [("a ", VALID_A)],
        [("", VALID_A)],
        [("a", VALID_A), ("x", "1")],
        [("a", VALID_A), ("a", VALID_A)],
        [("a", VALID_A), ("a", VALID_B)],
        [("a", "")],
        [("a", VALID_A), ("b", "")],
    ],
)
def test_unknown_repeated_and_empty_are_invalid_query(items: list[tuple[str, str]]) -> None:
    with pytest.raises(QueryError) as info:
        parse_compare_query(items)
    assert info.value.code == "invalid_query"


@pytest.mark.parametrize(
    "value",
    ["..", "a/b", "a\\b", "x" * 65, "-leading", " ", "a\x00b", "b-1\n", "evil<script>", "é"],
)
def test_invalid_run_id_value_is_invalid_run_id(value: str) -> None:
    for slot in ("a", "b", "c"):
        with pytest.raises(QueryError) as info:
            parse_compare_query([(slot, value)])
        assert info.value.code == "invalid_run_id"


def test_error_never_carries_the_request_value() -> None:
    secret = "secret-value/../x"
    with pytest.raises(QueryError) as info:
        parse_compare_query([("a", secret)])
    assert secret not in str(info.value)


# ---------------------------------------------------------------- GET /compare


@pytest.mark.parametrize("slot", ["a", "b", "c"])
@pytest.mark.parametrize("bad", ["..", "%2e%2e", "a%2Fb", "a%5Cb", "x" * 65, "-leading", "%20", "a%00b", "b-1%0A"])
def test_invalid_slot_value_is_400_with_zero_io(env: Env, slot: str, bad: str) -> None:
    response = env.get(f"/compare?{slot}={bad}")
    assert response.status_code == 400
    assert response.text == FIXED_400
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert env.io.calls == []


@pytest.mark.parametrize(
    "query",
    [
        "x=1",
        f"a={VALID_A}&x=1",
        "A=" + VALID_A,
        f"a={VALID_A}&a={VALID_A}",
        f"a={VALID_A}&a={VALID_B}",
        f"b={VALID_B}&b={VALID_B}",
        f"c={VALID_C}&c={VALID_C}",
        "a=",
        "b=",
        "c=",
        f"a={VALID_A}&b=",
        "a",
        "=x",
        f"a={VALID_A}&b=..",
    ],
)
def test_unknown_repeated_empty_and_mixed_are_400_with_zero_io(env: Env, query: str) -> None:
    response = env.get(f"/compare?{query}")
    assert response.status_code == 400
    assert response.text == FIXED_400
    assert env.io.calls == []


def test_400_body_does_not_echo_the_value(env: Env) -> None:
    response = env.get("/compare?a=evil%3Cscript%3E")
    assert response.status_code == 400
    assert "evil" not in response.text
    assert "script" not in response.text


def test_valid_nonexistent_ids_are_200_with_zero_io(env: Env) -> None:
    response = env.get(f"/compare?a={ABSENT}&b={ABSENT}&c={ABSENT}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert env.io.calls == []


def test_valid_existing_ids_are_200_with_zero_io(env: Env) -> None:
    response = env.get(f"/compare?a={VALID_A}&b={env.run_id}")
    assert response.status_code == 200
    assert env.io.calls == []


def test_compare_without_query_is_200(env: Env) -> None:
    response = env.get("/compare")
    assert response.status_code == 200
    assert env.io.calls == []


def test_valid_compare_serves_the_same_shell_as_root(env: Env) -> None:
    assert env.get(f"/compare?a={VALID_A}").content == env.get("/").content


def test_compare_never_yields_422(env: Env) -> None:
    for query in ("a=1&a=2", "a[]=1", "a=%ff", "b=" + "x" * 5000, "%ff=1"):
        assert env.get(f"/compare?{query}").status_code != 422


# ---------------------------------------------------------------- run routes keep their codes


@pytest.mark.parametrize("bad", ["%2e%2e", "a%2Fb", "a%5Cb", "x" * 65, "-leading", "%20", "a%00b", "b-1%0A"])
def test_run_routes_with_invalid_id_are_400(env: Env, bad: str) -> None:
    assert env.get(f"/runs/{bad}").status_code == 400
    response = env.get(f"/api/runs/{bad}")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_run_id"


def test_run_routes_with_valid_nonexistent_id_are_404(env: Env) -> None:
    assert env.get(f"/runs/{ABSENT}").status_code == 404
    response = env.get(f"/api/runs/{ABSENT}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
