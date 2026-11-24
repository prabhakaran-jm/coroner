"""`not_read` tests (task 2.16).

When the manifest of a run is unusable (too large, unreadable, invalid, missing by the containment checks,
or a `run_id` that differs from the directory name) the Viewer must not open `evidence.jsonl` or
`docker_inspect.json`; both get the status `not_read` with the reason `manifest_unusable`. A spy IO records
every filesystem call that names those two files. Fixtures are synthetic (RunsBuilder under `tmp_path`).

Requirements: 3.5, 3.16, 3.17, 11.35, 11.36 (Requirement 3 criteria 13, 16, 17; Requirement 11 criterion 35).
"""
from __future__ import annotations

import os
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from coroner.viewer.files import ARTIFACT_LIMIT, DEFAULT_IO, open_runs_root
from coroner.viewer.loader import load_detail, load_listing
from coroner.viewer.model import to_detail, to_listing
from tests.unit.viewer.builder import EVIDENCE_NAME, INSPECT_NAME, MANIFEST_NAME, RunsBuilder, sample_specs

SIX = {"ok", "missing", "unreadable", "invalid", "too_large", "not_read"}
NOT_READ_TEXT = "not read (manifest unusable)"
RUN = "b-notread-00000001"


class SpyIO:
    """Delegates to the real IO and records the path of every call; optional manifest faults."""

    def __init__(self, *, unreadable: bool = False, linked: bool = False) -> None:
        self.unreadable = unreadable
        self.linked = linked
        self.paths: list[tuple[str, str]] = []

    def _rec(self, call: str, path: str) -> None:
        self.paths.append((call, path))

    def touched(self, *names: str, run_id: str = RUN) -> list[tuple[str, str]]:
        """Calls that name one of `names` inside the directory of `run_id`."""
        return [
            (c, p) for c, p in self.paths
            if os.path.basename(p) in names and os.path.basename(os.path.dirname(p)) == run_id
        ]

    def scandir(self, path: str) -> Any:
        self._rec("scandir", path)
        return DEFAULT_IO.scandir(path)

    def lstat(self, path: str) -> os.stat_result:
        self._rec("lstat", path)
        st = DEFAULT_IO.lstat(path)
        if self.linked and os.path.basename(path) == MANIFEST_NAME:
            values = list(st)
            values[0] = stat.S_IFLNK | 0o777
            return os.stat_result(values)
        return st

    def open(self, path: str, flags: int) -> int:
        self._rec("open", path)
        if self.unreadable and os.path.basename(path) == MANIFEST_NAME:
            raise PermissionError(13, "denied", path)
        return DEFAULT_IO.open(path, flags)

    def fstat(self, fd: int) -> os.stat_result:
        return DEFAULT_IO.fstat(fd)

    def read(self, fd: int, n: int) -> bytes:
        return DEFAULT_IO.read(fd, n)

    def close(self, fd: int) -> None:
        DEFAULT_IO.close(fd)

    def realpath(self, path: str, strict: bool = True) -> str:
        self._rec("realpath", path)
        return DEFAULT_IO.realpath(path, strict)


def _make_too_large(b: RunsBuilder) -> str:
    return b.add_run("B", run_id=RUN, samples=sample_specs(3), manifest_bytes=b" " * (ARTIFACT_LIMIT + 1))


def _make_invalid(b: RunsBuilder) -> str:
    return b.add_run("B", run_id=RUN, samples=sample_specs(3), manifest_bytes=b"{not json")


def _make_plain(b: RunsBuilder) -> str:
    return b.add_run("B", run_id=RUN, samples=sample_specs(3))


def _make_mismatch(b: RunsBuilder) -> str:
    donor = b.add_run("B", run_id="b-donor-00000001")
    foreign = (b.root / donor / MANIFEST_NAME).read_bytes()
    return b.add_run("B", run_id=RUN, samples=sample_specs(3), manifest_bytes=foreign)


# (maker, spy kwargs, expected manifest status, expected manifest reason, expected list reason)
CAUSES: dict[str, tuple[Callable[[RunsBuilder], str], dict[str, bool], str, str, str]] = {
    "too_large": (_make_too_large, {}, "too_large", "too_large", "manifest_too_large"),
    "unreadable": (_make_plain, {"unreadable": True}, "unreadable", "os_error", "manifest_unreadable"),
    "invalid": (_make_invalid, {}, "invalid", "manifest_invalid", "manifest_invalid"),
    "missing_by_containment": (
        _make_plain, {"linked": True}, "missing", "symlink_or_reparse_point", "manifest_unsafe_path",
    ),
    "run_id_mismatch": (_make_mismatch, {}, "invalid", "run_id_mismatch", "run_id_mismatch"),
}


def _detail(tmp_path: Path, cause: str) -> tuple[dict[str, Any], SpyIO, Any]:
    maker, spy_kwargs, *_ = CAUSES[cause]
    builder = RunsBuilder(tmp_path / "runs")
    run_id = maker(builder)
    spy = SpyIO(**spy_kwargs)
    record = load_detail(open_runs_root(tmp_path / "runs"), run_id, spy, time.time_ns)
    return to_detail(record), spy, record


@pytest.mark.parametrize("cause", sorted(CAUSES))
def test_unusable_manifest_gives_not_read_for_evidence_and_inspect(tmp_path: Path, cause: str) -> None:
    _, _, status, reason, _ = CAUSES[cause]

    detail, spy, record = _detail(tmp_path, cause)

    states = detail["artifact_status"]
    assert states["manifest"]["status"] == status
    assert states["manifest"]["reason"] == reason
    for name in ("evidence", "inspect"):
        assert states[name]["status"] == "not_read"
        assert states[name]["reason"] == "manifest_unusable"
        assert states[name]["status"] != "missing"
    assert {s["status"] for s in states.values()} <= SIX
    assert spy.touched(EVIDENCE_NAME, INSPECT_NAME) == []
    assert record.read_result is None and record.summary is None and record.inspect_excerpt is None


@pytest.mark.parametrize("cause", sorted(CAUSES))
def test_unusable_manifest_derived_values_are_null(tmp_path: Path, cause: str) -> None:
    detail, _, record = _detail(tmp_path, cause)

    assert record.sample_rows == []
    assert record.rss_stats_data is None and record.chart_data is None
    assert detail["run"]["verdict"] == "UNKNOWN"
    assert detail["run"]["experiment_type"] is None
    assert detail["run"]["outcome"] is None
    assert detail["evidence_summary"]
    assert all(value is None for value in detail["evidence_summary"].values())


@pytest.mark.parametrize("cause", sorted(CAUSES))
def test_not_read_warning_text_and_no_absent_wording(tmp_path: Path, cause: str) -> None:
    detail, _, _ = _detail(tmp_path, cause)

    by_code = {w["code"]: w for w in detail["warnings"] if w["source"] == "viewer"}
    for name in ("evidence", "inspect"):
        warning = by_code[f"artifact_{name}_not_read"]
        assert warning["text"] == NOT_READ_TEXT
        assert "missing" not in warning["text"] and "absent" not in warning["text"]
        assert f"artifact_{name}_missing" not in by_code


@pytest.mark.parametrize("cause", sorted(CAUSES))
def test_listing_does_not_touch_evidence_or_inspect_of_an_unusable_manifest(tmp_path: Path, cause: str) -> None:
    maker, spy_kwargs, _status, _reason, list_reason = CAUSES[cause]
    builder = RunsBuilder(tmp_path / "runs")
    run_id = maker(builder)
    spy = SpyIO(**spy_kwargs)

    listing = to_listing(load_listing(open_runs_root(tmp_path / "runs"), spy, time.time_ns, None))

    item = next(i for i in listing["runs"] if i["run_id"] == run_id)
    assert item["verdict"] == "UNKNOWN" and item["reason"] == list_reason
    assert spy.touched(EVIDENCE_NAME, INSPECT_NAME) == []


def test_not_read_differs_from_missing(tmp_path: Path) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    missing_run = builder.add_run("B", run_id="b-missing-00000001", samples=sample_specs(3), inspect=None)
    (tmp_path / "runs" / missing_run / EVIDENCE_NAME).unlink()
    root = open_runs_root(tmp_path / "runs")

    usable = to_detail(load_detail(root, missing_run, DEFAULT_IO, time.time_ns))
    states = usable["artifact_status"]
    assert states["evidence"]["status"] == "missing"
    assert states["inspect"]["status"] == "missing"
    assert states["evidence"]["reason"] != "manifest_unusable"
    assert {w["code"] for w in usable["warnings"] if w["source"] == "viewer"} >= {
        "artifact_evidence_missing", "artifact_inspect_missing",
    }
    assert all(w["text"] != NOT_READ_TEXT for w in usable["warnings"])

    unusable, _, _ = _detail(tmp_path / "other", "invalid")
    assert unusable["artifact_status"]["evidence"]["status"] != states["evidence"]["status"]


def test_usable_manifest_never_produces_not_read(tmp_path: Path) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    ok_run = builder.add_run("B", run_id="b-usable-00000001", samples=sample_specs(3))
    no_evidence = builder.add_run("B", run_id="b-usable-00000002", samples=sample_specs(3))
    (tmp_path / "runs" / no_evidence / EVIDENCE_NAME).unlink()
    bad_evidence = builder.add_run("B", run_id="b-usable-00000003", evidence_bytes=b"not jsonl\n")
    bad_inspect = builder.add_run("B", run_id="b-usable-00000004", inspect=b"{broken")
    root = open_runs_root(tmp_path / "runs")

    for run_id in (ok_run, no_evidence, bad_evidence, bad_inspect):
        states = to_detail(load_detail(root, run_id, DEFAULT_IO, time.time_ns))["artifact_status"]
        assert states["manifest"]["status"] == "ok"
        for name in ("manifest", "evidence", "inspect"):
            assert states[name]["status"] in SIX - {"not_read"}
            assert states[name]["reason"] != "manifest_unusable"
