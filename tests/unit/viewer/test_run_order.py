"""Run_Order tests for the Viewer listing (task 2.16).

Run_Order: valid `start_time` descending; equal instants by `run_id` descending as plain strings; runs
without a valid `start_time` (unparseable, absent, unusable manifest, run_id mismatch) after all others, by
`run_id` descending. Fixtures are synthetic (RunsBuilder under `tmp_path`).

Requirements: 3.11, 8.9, 9.9, 11.35, 11.36.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from coroner.viewer.app import create_app
from coroner.viewer.files import DEFAULT_IO, open_runs_root
from coroner.viewer.loader import load_listing
from coroner.viewer.model import to_listing
from tests.unit.viewer.builder import MANIFEST_NAME, RunsBuilder

PORT = 8765


def _listing(runs: Path) -> dict[str, Any]:
    return to_listing(load_listing(open_runs_root(runs), DEFAULT_IO, time.time_ns, None))


def _ids(runs: Path) -> list[str]:
    return [item["run_id"] for item in _listing(runs)["runs"]]


def _t(second: int) -> str:
    return f"2026-11-20T21:{second // 60:02d}:{second % 60:02d}.000Z"


def test_different_start_times_are_ordered_descending(tmp_path: Path) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    # Run ids ascend while start times are scrambled, so id order can never explain the result.
    middle = builder.add_run("B", run_id="b-order-0001", start_time=_t(30))
    newest = builder.add_run("B", run_id="b-order-0002", start_time=_t(50))
    oldest = builder.add_run("B", run_id="b-order-0003", start_time=_t(10))

    assert _ids(tmp_path / "runs") == [newest, middle, oldest]


def test_equal_start_times_are_ordered_by_run_id_descending_as_plain_strings(tmp_path: Path) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    # Plain string order (code points): lower-case > upper-case > digits, and "9" > "10".
    names = ["tie-10", "tie-9", "tie-B", "tie-a"]
    for name in names:
        builder.add_run("B", run_id=name, start_time=_t(20))

    assert _ids(tmp_path / "runs") == sorted(names, reverse=True)
    assert _ids(tmp_path / "runs") == ["tie-a", "tie-B", "tie-9", "tie-10"]


def test_runs_without_a_valid_start_time_come_after_all_others_by_run_id_descending(tmp_path: Path) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    valid_old = builder.add_run("B", run_id="b-valid-old", start_time=_t(1))
    valid_new = builder.add_run("B", run_id="b-valid-new", start_time=_t(40))
    unparseable = builder.add_run("B", run_id="z-unparseable", start_time="not-a-timestamp")
    invalid_manifest = builder.add_run("B", run_id="y-invalid-manifest", manifest_bytes=b"{nope")
    # A manifest with no start_time key at all.
    donor = builder.add_run("B", run_id="x-donor", start_time=_t(5))
    doc = json.loads((tmp_path / "runs" / donor / MANIFEST_NAME).read_bytes())
    doc.pop("start_time", None)
    no_start = builder.add_run("B", run_id="w-no-start-time", manifest_bytes=json.dumps(doc).encode("utf-8"))
    # A manifest that belongs to another run (run_id mismatch).
    mismatch = builder.add_run(
        "B", run_id="v-mismatch", manifest_bytes=(tmp_path / "runs" / valid_new / MANIFEST_NAME).read_bytes()
    )
    # donor has a valid start time of its own.
    ordered = _ids(tmp_path / "runs")

    with_start = [valid_new, donor, valid_old]
    without_start = [unparseable, invalid_manifest, no_start, mismatch]
    assert ordered[:3] == with_start
    assert ordered[3:] == sorted(without_start, reverse=True)
    items = {i["run_id"]: i for i in _listing(tmp_path / "runs")["runs"]}
    for run_id in without_start:
        assert items[run_id]["start_time_display"] is None


def test_first_run_of_each_type_is_the_overview_card_and_default_compare_run(tmp_path: Path) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    expected: dict[str, str] = {}
    layout = [("A", 10), ("A", 45), ("B", 20), ("B", 55), ("C", 30), ("C", 15), ("A", 25)]
    best: dict[str, int] = {}
    for index, (experiment, second) in enumerate(layout):
        run_id = builder.add_run(experiment, run_id=f"{experiment.lower()}-card-{index:04d}", start_time=_t(second))
        if second > best.get(experiment, -1):
            best[experiment] = second
            expected[experiment] = run_id
    # Unknown runs sort last even with the greatest ids, so they can never become a card.
    builder.add_run("B", run_id="zz-unknown", manifest_bytes=b"garbage")

    items = _listing(tmp_path / "runs")["runs"]

    first_of_type: dict[str, str] = {}
    for item in items:
        first_of_type.setdefault(item["experiment_type"], item["run_id"])
    assert {k: v for k, v in first_of_type.items() if k is not None} == expected
    assert items[-1]["run_id"] == "zz-unknown"


def test_order_is_identical_on_repeated_calls_and_does_not_depend_on_creation_order(tmp_path: Path) -> None:
    specs = [
        ("b-rep-0001", _t(30)), ("b-rep-0002", _t(30)), ("b-rep-0003", _t(12)),
        ("b-rep-0004", "not-a-timestamp"), ("b-rep-0005", "not-a-timestamp"),
    ]
    first = RunsBuilder(tmp_path / "first")
    for run_id, start in specs:
        first.add_run("B", run_id=run_id, start_time=start)
    second = RunsBuilder(tmp_path / "second")
    for run_id, start in reversed(specs):
        second.add_run("B", run_id=run_id, start_time=start)

    repeated = [_ids(tmp_path / "first") for _ in range(5)]

    assert all(order == repeated[0] for order in repeated)
    assert repeated[0] == ["b-rep-0002", "b-rep-0001", "b-rep-0003", "b-rep-0005", "b-rep-0004"]
    assert _ids(tmp_path / "second") == repeated[0]

    client = TestClient(create_app(tmp_path / "first", PORT), base_url=f"http://127.0.0.1:{PORT}")
    bodies = [client.get("/api/runs", headers={"Host": f"127.0.0.1:{PORT}"}).content for _ in range(3)]
    assert bodies[0] == bodies[1] == bodies[2]
