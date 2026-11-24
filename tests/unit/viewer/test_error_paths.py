"""Error path tests for the Viewer (task 2.17).

An unexpected exception is injected through the filesystem seam (`ArtifactIO`): the fake raises a
`RuntimeError` whose message holds the temporary directory path, a traceback marker and an HTML string, so
any leak of the exception text into a response or a log shows up. Fixtures are synthetic (RunsBuilder under
`tmp_path`); the real `runs/` directory is never read.

Requirements: 2.12, 3.18, 9.10, 11.40.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from coroner.viewer.app import create_app
from coroner.viewer.files import DEFAULT_IO
from tests.unit.viewer.builder import EVIDENCE_NAME, RunsBuilder, sample_specs

PORT = 8765
BAD = "b-error-00000001"
GOOD = "b-error-00000002"
FIXED_500 = {"error": {"code": "internal_error", "message": "Unexpected error."}}


class FaultyIO:
    """Real IO, except that the first `budget` opens of `EVIDENCE_NAME` inside run `BAD` raise."""

    def __init__(self, secret: str, budget: int) -> None:
        self.secret = secret
        self.budget = budget
        self.raised = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(DEFAULT_IO, name)

    def open(self, path: str, flags: int) -> int:
        if self.budget > 0 and path.endswith(EVIDENCE_NAME) and BAD in path:
            self.budget -= 1
            self.raised += 1
            raise RuntimeError(f"boom {self.secret} <script>alert(1)</script> Traceback (most recent call last)")
        return DEFAULT_IO.open(path, flags)


def _client(tmp_path: Path, budget: int) -> tuple[TestClient, FaultyIO]:
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    builder.add_run("B", run_id=BAD, samples=sample_specs(3))
    builder.add_run("B", run_id=GOOD, samples=sample_specs(3))
    io = FaultyIO(str(tmp_path), budget)
    app = create_app(runs, PORT, io=io)
    client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", raise_server_exceptions=False)
    return client, io


def _get(client: TestClient, url: str) -> Any:
    return client.get(url, headers={"Host": f"127.0.0.1:{PORT}"})


def test_unexpected_error_in_the_detail_path_returns_a_fixed_500_and_the_next_request_succeeds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client, io = _client(tmp_path, budget=1)

    with caplog.at_level(logging.DEBUG):
        failed = _get(client, f"/api/runs/{BAD}")

    assert io.raised == 1
    assert failed.status_code == 500
    assert failed.headers["content-type"].startswith("application/json")
    assert failed.json() == FIXED_500
    for leak in (str(tmp_path), "boom", "Traceback", "RuntimeError", "<script>", "File \"", ".py"):
        assert leak not in failed.text, leak
    assert str(tmp_path) not in caplog.text and "<script>" not in caplog.text and "boom" not in caplog.text

    again = _get(client, f"/api/runs/{BAD}")  # the fault budget is spent: the same run now loads
    assert again.status_code == 200
    assert set(again.json()) == {
        "run", "docker_state", "timing", "termination_markers", "evidence_summary", "chart", "runner_text",
        "artifact_status", "warnings",
    }
    assert _get(client, f"/api/runs/{GOOD}").status_code == 200


def test_unexpected_error_for_one_run_in_the_list_leaves_the_other_entries_intact(tmp_path: Path) -> None:
    healthy_client, _ = _client(tmp_path / "healthy", budget=0)
    expected_good = next(
        r for r in _get(healthy_client, "/api/runs").json()["runs"] if r["run_id"] == GOOD
    )
    client, io = _client(tmp_path / "faulty", budget=10)

    response = _get(client, "/api/runs")

    assert io.raised >= 1
    assert response.status_code == 200
    body = response.json()
    assert body["total_found"] == 2 and body["listed"] == 2
    items = {r["run_id"]: r for r in body["runs"]}
    assert set(items) == {BAD, GOOD}
    bad = items[BAD]
    assert bad["verdict"] == "UNKNOWN" and bad["reason"] == "internal_error"
    for name, value in bad.items():
        if name not in ("run_id", "verdict", "reason"):
            assert value is None, name
    assert items[GOOD] == expected_good
    assert items[GOOD]["verdict"] != "UNKNOWN" and items[GOOD]["reason"] is None
    for leak in (str(tmp_path), "boom", "Traceback", "<script>"):
        assert leak not in response.text, leak
