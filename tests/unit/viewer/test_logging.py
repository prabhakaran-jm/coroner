"""Logging tests for the Viewer (task 2.17).

While the Viewer serves hostile fixtures (path-like strings, the temporary directory path, HTML) in the
manifest, the evidence, the inspect document and the request path, the captured log must contain no fixture
string and no temporary path, and WARNING lines (and above) may hold only a run id, an artifact name and a
reason code. The log check itself has a negative control, so it cannot pass by accident.

Fixtures are synthetic (RunsBuilder under `tmp_path`); the real `runs/` directory is never read.

Requirements: 2.12, 9.10, 16.10.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from coroner.viewer.app import create_app
from coroner.viewer.files import DEFAULT_IO
from tests.unit.viewer.builder import (
    EVIDENCE_NAME,
    INSPECT_NAME,
    MANIFEST_NAME,
    RunsBuilder,
    sample_specs,
)

PORT = 8765
ARTIFACT_NAMES = {MANIFEST_NAME, EVIDENCE_NAME, INSPECT_NAME, "manifest", "evidence", "inspect"}
REASON_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
TOKEN_SPLIT = re.compile(r"[\s=:,;]+")


def _hostile(tmp_path: Path) -> list[str]:
    return [
        "C:\\Users\\victim\\secret-token.txt",
        "/etc/shadow-hostile",
        "../../outside-hostile/escape",
        "<img src=x onerror=alert(1)>",
        "<script>alert('hostile-html')</script>",
        str(tmp_path),
        tmp_path.as_posix(),
    ]


def _check_log(caplog: pytest.LogCaptureFixture, tmp_path: Path, hostile: list[str], run_ids: set[str]) -> None:
    # Only the Viewer's own loggers are checked. The HTTP test client (`httpx`) logs every request URL
    # on the client side, which includes the hostile request strings the test itself sent.
    records = [r for r in caplog.records if r.name == "coroner" or r.name.startswith("coroner.")]
    assert all(r.name.startswith("coroner") for r in records)
    text = "\n".join(r.getMessage() for r in records)
    for value in hostile:
        assert value not in text, value
    assert tmp_path.name not in text
    for record in records:
        message = record.getMessage()
        assert str(tmp_path) not in message
        if record.levelno >= logging.ERROR:
            # Design: only the exception class name and the run id are logged for an unexpected error.
            assert re.fullmatch(r"internal_error [A-Za-z_]+ run_id=(-|[A-Za-z0-9._-]+)", message), message
        elif record.levelno == logging.WARNING:
            offending = [t for t in TOKEN_SPLIT.split(message) if t and not _allowed(t, run_ids)]
            assert not offending, (record.levelname, message, offending)


def _allowed(token: str, run_ids: set[str]) -> bool:
    return token in run_ids or token in ARTIFACT_NAMES or REASON_CODE.fullmatch(token) is not None


class FaultyIO:
    """Raises an exception whose message holds hostile text when the evidence of `target` is opened."""

    def __init__(self, target: str, message: str) -> None:
        self.target = target
        self.message = message

    def __getattr__(self, name: str) -> Any:
        return getattr(DEFAULT_IO, name)

    def open(self, path: str, flags: int) -> int:
        if path.endswith(EVIDENCE_NAME) and self.target in path:
            raise RuntimeError(self.message)
        return DEFAULT_IO.open(path, flags)


def _build(tmp_path: Path, hostile: list[str]) -> tuple[Path, set[str]]:
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    text = " | ".join(hostile)
    ids = set()
    good = builder.add_run(
        "B",
        run_id="b-log-hostile-0001",
        samples=sample_specs(3),
        manifest_extra={
            "notes": text,
            "checks": [{"name": text, "passed": True, "detail": text}],
            "raw_inspect_path": hostile[0],
            "host_copy_path": hostile[1],
            "docker_inspect_path": hostile[5],
        },
    )
    ids.add(good)
    evidence = (runs / good / EVIDENCE_NAME).read_bytes()
    (runs / good / EVIDENCE_NAME).write_bytes(evidence + text.encode("utf-8") + b"\n" + b'{"record_type": "' + text.encode("utf-8").replace(b'"', b"'") + b'"}\n')
    ids.add(builder.add_run("B", run_id="b-log-hostile-0002", inspect=text.encode("utf-8")))
    ids.add(builder.add_run("B", run_id="b-log-hostile-0003", manifest_bytes=text.encode("utf-8")))
    ids.add(builder.add_run("B", run_id="b-log-hostile-0004", evidence_bytes=text.encode("utf-8") + b"\n"))
    return runs, ids


def _serve_everything(client: TestClient, run_ids: set[str], hostile: list[str]) -> None:
    host = {"Host": f"127.0.0.1:{PORT}"}
    client.get("/api/runs", headers=host)
    for run_id in sorted(run_ids):
        client.get(f"/api/runs/{run_id}", headers=host)
        client.get(f"/runs/{run_id}", headers=host)
    client.get("/api/runs/b-log-hostile-9999", headers=host)
    client.get("/", headers=host)
    client.get("/compare", headers=host)
    for value in hostile:
        client.get("/api/runs/" + value, headers=host)
        client.get("/runs/" + value, headers=host)
        client.get("/static/" + value, headers=host)
        client.get("/nothing", params={"q": value}, headers=host)


def test_logs_hold_no_fixture_string_and_no_temporary_path(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    hostile = _hostile(tmp_path)
    runs, run_ids = _build(tmp_path, hostile)
    client = TestClient(create_app(runs, PORT), base_url=f"http://127.0.0.1:{PORT}", raise_server_exceptions=False)

    with caplog.at_level(logging.DEBUG):
        _serve_everything(client, run_ids, hostile)

    _check_log(caplog, tmp_path, hostile, run_ids)


def test_logs_after_an_unexpected_error_hold_no_exception_text_or_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    hostile = _hostile(tmp_path)
    runs, run_ids = _build(tmp_path, hostile)
    io = FaultyIO("b-log-hostile-0001", "boom " + " ".join(hostile))
    client = TestClient(create_app(runs, PORT, io=io), base_url=f"http://127.0.0.1:{PORT}", raise_server_exceptions=False)

    with caplog.at_level(logging.DEBUG):
        _serve_everything(client, run_ids, hostile)

    assert any(r.levelno >= logging.ERROR for r in caplog.records), "the injected error must be logged"
    assert "boom" not in caplog.text
    _check_log(caplog, tmp_path, hostile, run_ids)


def test_the_log_check_detects_a_leaking_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Negative control: a WARNING with a path or free text must fail the check; a coded one must pass."""
    hostile = _hostile(tmp_path)
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("coroner.viewer.test").warning("run_id=b-log-hostile-0001 artifact=evidence.jsonl reason=manifest_unusable")
    _check_log(caplog, tmp_path, hostile, {"b-log-hostile-0001"})

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("coroner.viewer.test").warning("cannot read %s", hostile[1])
    with pytest.raises(AssertionError):
        _check_log(caplog, tmp_path, hostile, set())

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("coroner.viewer.test").warning("Could Not Read The File")
    with pytest.raises(AssertionError):
        _check_log(caplog, tmp_path, hostile, set())
