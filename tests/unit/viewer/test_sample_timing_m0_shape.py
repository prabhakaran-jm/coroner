"""V14 regression: the Viewer reads `sample_timing` in the shape M0 really writes.

M0 (`coroner/runner/evaluate.py`, `to_dict`) writes `post_kill_beyond_tolerance` and `after_container_stop` as plain
lists of Capture_Time strings (the real Experiment B manifest has both as `[]`). Design rule: `count` is `len(list)`;
an absent or non-list value is not recorded (`null`); `[]` is a recorded zero.

Synthetic runs live in tmp_path (RunsBuilder). One test reads a real saved Experiment B run strictly read-only
(skipped when that run is not present).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coroner.viewer.files import DEFAULT_IO, open_runs_root
from coroner.viewer.loader import load_detail
from coroner.viewer.model import dump_json, to_detail
from tests.unit.viewer.builder import RunsBuilder, sample_specs

REAL_RUNS = Path(__file__).resolve().parents[3] / "runs"
REAL_B = "b-20261120T211311Z-ce313da1"


def _detail(runs: Path, run_id: str) -> dict:
    record = load_detail(open_runs_root(runs), run_id, DEFAULT_IO, time.time_ns)
    return json.loads(dump_json(to_detail(record)))


def _timing(tmp_path: Path, **groups) -> dict:
    base = {"tolerance_ms": 250, "clock_offset_ms": 9, "pre_kill_count": 3, "pre_kill_late_received_count": 0,
            "within_tolerance_count": 0, "after_container_stop_analysis": "available"}
    runs = tmp_path / "runs"
    run_id = RunsBuilder(runs).add_run("B", samples=sample_specs(3), sample_timing={**base, **groups})
    return _detail(runs, run_id)["timing"]["sample_timing"]


def test_builder_default_is_the_m0_list_shape_and_counts_zero(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    run_id = RunsBuilder(runs).add_run("B", samples=sample_specs(3))
    manifest = json.loads((runs / run_id / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sample_timing"]["post_kill_beyond_tolerance"] == []
    assert manifest["sample_timing"]["after_container_stop"] == []
    st = _detail(runs, run_id)["timing"]["sample_timing"]
    assert st["post_kill_beyond_tolerance"]["count"] == 0 and st["post_kill_beyond_tolerance"]["items"] == []


def test_m0_plain_timestamp_lists_give_counts_and_items(tmp_path: Path) -> None:
    st = _timing(
        tmp_path,
        post_kill_beyond_tolerance=["2026-11-20T21:13:20.700Z", "2026-11-20T21:13:20.800Z"],
        after_container_stop=["2026-11-20T21:13:22.100Z"],
    )
    pk, ac = st["post_kill_beyond_tolerance"], st["after_container_stop"]
    assert pk["count"] == 2 and pk["items_total"] == 2 and pk["items_truncated"] is False
    assert [i["capture_time"] for i in pk["items"]] == ["2026-11-20T21:13:20.700Z", "2026-11-20T21:13:20.800Z"]
    assert all(i["capture_time_display"] for i in pk["items"])
    assert ac["count"] == 1 and ac["items"][0]["capture_time"] == "2026-11-20T21:13:22.100Z"


def test_empty_list_is_zero_and_absent_or_non_list_is_null(tmp_path: Path) -> None:
    st = _timing(tmp_path, post_kill_beyond_tolerance=[], after_container_stop=[])
    assert st["post_kill_beyond_tolerance"]["count"] == 0 and st["after_container_stop"]["count"] == 0

    for bad in ("x", 3, None, True):
        st = _timing(tmp_path / f"bad-{bad!r}".replace("'", ""), post_kill_beyond_tolerance=bad, after_container_stop=bad)
        for group in ("post_kill_beyond_tolerance", "after_container_stop"):
            assert st[group]["count"] is None and st[group]["items"] == [], (bad, group)


def test_long_m0_list_is_capped_with_truncation_metadata_and_full_count(tmp_path: Path) -> None:
    stamps = [f"2026-11-20T21:13:{20 + i // 1000:02d}.{i % 1000:03d}Z" for i in range(60)]
    pk = _timing(tmp_path, post_kill_beyond_tolerance=stamps, after_container_stop=[])["post_kill_beyond_tolerance"]
    assert pk["count"] == 60 and pk["items_total"] == 60 and pk["items_truncated"] is True
    assert len(pk["items"]) == 50 and pk["items"][0]["capture_time"] == stamps[0]


def test_unavailable_analysis_keeps_a_null_after_stop_count_for_the_m0_shape(tmp_path: Path) -> None:
    st = _timing(tmp_path, after_container_stop=[], after_container_stop_analysis="unavailable")
    assert st["after_container_stop"]["count"] is None
    assert st["post_kill_beyond_tolerance"]["count"] is None  # key absent from this block: not recorded


@pytest.mark.skipif(not (REAL_RUNS / REAL_B / "manifest.json").is_file(), reason="real Experiment B run not present")
def test_real_b_run_read_only_shows_recorded_zero_counts_not_null() -> None:
    manifest = json.loads((REAL_RUNS / REAL_B / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sample_timing"]["post_kill_beyond_tolerance"] == []  # the M0 shape on disk
    st = _detail(REAL_RUNS, REAL_B)["timing"]["sample_timing"]
    assert st["post_kill_beyond_tolerance"]["count"] == 0
    assert st["after_container_stop"]["count"] == 0
    assert st["after_container_stop_analysis"] == "available"
