"""Run discovery tests for the Viewer listing (task 2.16).

All fixtures are synthetic and live under pytest's `tmp_path` (RunsBuilder); the real `runs/` directory is
never read. Failures that a temporary directory cannot produce (an unreadable manifest, a link) are made with
a small filesystem seam wrapper around `DEFAULT_IO`, so no test needs privileges or a skip.

Requirements: 3.1, 3.2, 3.4, 3.5, 3.12, 3.13, 3.16, 11.2.
"""
from __future__ import annotations

import logging
import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from coroner.viewer.app import create_app
from coroner.viewer.files import ARTIFACT_LIMIT, DEFAULT_IO, open_runs_root
from coroner.viewer.loader import MAX_LISTED_RUNS, load_listing
from coroner.viewer.model import to_listing
from tests.unit.viewer.builder import MANIFEST_NAME, RunsBuilder

PORT = 8765
NULL_FIELDS = (
    "experiment", "experiment_type", "start_time", "start_time_display", "exit_code", "oom_killed",
    "sample_count", "collector_survival", "docker_state_source", "docker_state_discrepancy",
)


class SeamIO:
    """Passes everything to the real IO, with two optional faults for the manifest of one run."""

    def __init__(self, *, unreadable: str | None = None, linked: str | None = None) -> None:
        self.unreadable = unreadable
        self.linked = linked

    def scandir(self, path: str) -> Any:
        return DEFAULT_IO.scandir(path)

    def lstat(self, path: str) -> os.stat_result:
        st = DEFAULT_IO.lstat(path)
        if self.linked and path.endswith(os.path.join(self.linked, MANIFEST_NAME)):
            values = list(st)
            values[0] = stat.S_IFLNK | 0o777  # st_mode: a symbolic link
            return os.stat_result(values)
        return st

    def open(self, path: str, flags: int) -> int:
        if self.unreadable and path.endswith(os.path.join(self.unreadable, MANIFEST_NAME)):
            raise PermissionError(13, "denied", path)
        return DEFAULT_IO.open(path, flags)

    def fstat(self, fd: int) -> os.stat_result:
        return DEFAULT_IO.fstat(fd)

    def read(self, fd: int, n: int) -> bytes:
        return DEFAULT_IO.read(fd, n)

    def close(self, fd: int) -> None:
        DEFAULT_IO.close(fd)

    def realpath(self, path: str, strict: bool = True) -> str:
        return DEFAULT_IO.realpath(path, strict)


def _listing(runs: Path, io: Any = DEFAULT_IO) -> dict[str, Any]:
    return to_listing(load_listing(open_runs_root(runs), io, time.time_ns, None))


def _ids(listing: dict[str, Any]) -> list[str]:
    return [item["run_id"] for item in listing["runs"]]


def _item(listing: dict[str, Any], run_id: str) -> dict[str, Any]:
    return next(item for item in listing["runs"] if item["run_id"] == run_id)


def _assert_unknown(item: dict[str, Any], reason: str) -> None:
    assert item["verdict"] == "UNKNOWN"
    assert item["reason"] == reason
    for name in NULL_FIELDS:
        assert item[name] is None, name


def test_loose_files_and_directories_without_a_manifest_are_not_listed(tmp_path: Path) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    good = builder.add_run("B", run_id="b-good-00000001")
    builder.add_loose_file("notes.txt", b"hello")
    builder.add_loose_file("b-loose-file-0001", b"{}")  # a valid run id name, but a file
    builder.add_empty_dir("b-empty-dir-00000001")
    only_evidence = tmp_path / "runs" / "b-only-evidence-0001"
    only_evidence.mkdir()
    (only_evidence / "evidence.jsonl").write_bytes(b"")

    listing = _listing(tmp_path / "runs")

    assert _ids(listing) == [good]
    assert (listing["total_found"], listing["listed"]) == (1, 1)


def test_valid_names_that_are_not_runs_are_skipped_silently(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    good = builder.add_run("A", run_id="a-good-00000001")
    builder.add_empty_dir("t31")
    builder.add_loose_file("t32", b"x")

    with caplog.at_level(logging.DEBUG):
        listing = _listing(tmp_path / "runs")

    assert _ids(listing) == [good]
    assert listing["total_found"] == 1
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_empty_runs_directory_returns_an_empty_list_with_the_label(tmp_path: Path) -> None:
    runs = tmp_path / "saved-runs"
    runs.mkdir()

    listing = _listing(runs)
    assert listing == {
        "runs_dir_label": "saved-runs", "total_found": 0, "listed": 0, "truncated": False,
        "max_listed": MAX_LISTED_RUNS, "runs": [],
    }

    client = TestClient(create_app(runs, PORT), base_url=f"http://127.0.0.1:{PORT}")
    response = client.get("/api/runs", headers={"Host": f"127.0.0.1:{PORT}"})
    assert response.status_code == 200
    assert response.json() == listing
    assert str(tmp_path) not in response.text


@pytest.mark.parametrize(
    ("manifest_bytes", "reason"),
    [
        pytest.param(b"{this is not json", "manifest_invalid", id="unparseable"),
        pytest.param(b'{"unexpected": 1}', "manifest_invalid", id="invalid-shape"),
        pytest.param(b"[]", "manifest_invalid", id="invalid-type"),
    ],
)
def test_unparseable_or_invalid_manifest_is_listed_unknown(
    tmp_path: Path, manifest_bytes: bytes, reason: str
) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    bad = builder.add_run("B", run_id="b-bad-manifest-0001", manifest_bytes=manifest_bytes)
    good = builder.add_run("B", run_id="b-good-00000002")

    listing = _listing(tmp_path / "runs")

    assert sorted(_ids(listing)) == sorted([bad, good])
    _assert_unknown(_item(listing, bad), reason)
    assert _item(listing, good)["verdict"] != "UNKNOWN"


def test_oversized_manifest_is_listed_unknown(tmp_path: Path) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    big = builder.add_run("B", run_id="b-big-manifest-0001", manifest_bytes=b" " * (ARTIFACT_LIMIT + 1))

    listing = _listing(tmp_path / "runs")

    _assert_unknown(_item(listing, big), "manifest_too_large")


def test_unreadable_manifest_is_listed_unknown_without_a_path(tmp_path: Path) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    bad = builder.add_run("B", run_id="b-unreadable-0001")

    listing = _listing(tmp_path / "runs", SeamIO(unreadable=bad))

    _assert_unknown(_item(listing, bad), "manifest_unreadable")
    assert str(tmp_path) not in repr(listing)


def test_linked_manifest_is_listed_unknown(tmp_path: Path) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    bad = builder.add_run("B", run_id="b-linked-00000001")

    listing = _listing(tmp_path / "runs", SeamIO(linked=bad))

    _assert_unknown(_item(listing, bad), "manifest_unsafe_path")


def test_manifest_that_is_a_directory_is_listed_unknown(tmp_path: Path) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    bad = builder.add_run("B", run_id="b-dir-manifest-0001")
    manifest = tmp_path / "runs" / bad / MANIFEST_NAME
    manifest.unlink()
    manifest.mkdir()

    listing = _listing(tmp_path / "runs")

    _assert_unknown(_item(listing, bad), "manifest_not_regular_file")


def test_manifest_run_id_differing_from_the_directory_name_is_listed_unknown(tmp_path: Path) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    other = builder.add_run("B", run_id="b-other-00000001")
    foreign = (tmp_path / "runs" / other / MANIFEST_NAME).read_bytes()
    mismatched = builder.add_run("B", run_id="b-mismatch-00000001", manifest_bytes=foreign)

    listing = _listing(tmp_path / "runs")

    _assert_unknown(_item(listing, mismatched), "run_id_mismatch")
    assert _item(listing, other)["verdict"] != "UNKNOWN"


def test_listing_cap_fields_at_and_above_the_cap(tmp_path: Path) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    for index in range(MAX_LISTED_RUNS):
        builder.add_run("A", run_id=f"a-cap-{index:08d}", container_state=None)

    at_cap = _listing(tmp_path / "runs")
    assert (at_cap["total_found"], at_cap["listed"], at_cap["truncated"], at_cap["max_listed"]) == (
        500, 500, False, 500,
    )

    builder.add_run("A", run_id="a-cap-00000500", container_state=None)
    over = _listing(tmp_path / "runs")
    assert (over["total_found"], over["listed"], over["truncated"], over["max_listed"]) == (501, 500, True, 500)
    assert len(over["runs"]) == 500
    assert len(set(_ids(over))) == 500
