"""Experiment B chart fallback when the clock correction overflows the datetime range.

A recorded Clock_Offset that is a valid safe_int but makes ``Capture_Time - offset`` fall outside the
datetime range cannot be applied. ``timeline.build_chart`` then falls back to the raw axis with the
reason ``correction_out_of_range``. The whole response must follow: no SIGKILL marker, no SIGKILL x
position in the x-axis domain, raw Capture_Time x positions, and the recorded SIGKILL time only as
separately labelled text.

Both layers are covered: ``build_chart`` called directly with a drawn-marker position, and the real
API path (``load_detail`` + ``to_detail`` + ``dump_json``) on a synthetic run built under tmp_path.
Nothing here reads the real runs/ directory.
"""
from __future__ import annotations

import json
import time

import pytest

from coroner.viewer.files import DEFAULT_IO, open_runs_root
from coroner.viewer.loader import load_detail
from coroner.viewer.model import dump_json, to_detail
from coroner.viewer.timefmt import epoch_ms_utc
from coroner.viewer.timeline import build_chart, gap_index
from tests.unit.viewer.builder import RunsBuilder, sample_specs
from tests.unit.viewer.test_api_contract import (
    CHART_KEYS,
    DETAIL_BLOCKS,
    SIGKILL_KEYS,
    TEXT_TIME_KEYS,
    TIMING_KEYS,
    TERMINATION_MARKERS_KEYS,
    X_AXIS_KEYS,
    _absolute_path_hits,
    _needles,
)

# Largest JS-safe integer: a valid safe_int, yet ts - offset is far before datetime.min.
HUGE_OFFSET = 2**53 - 1


@pytest.fixture
def loaded(tmp_path):
    builder = RunsBuilder(tmp_path / "runs")
    run_id = builder.add_run("B", samples=sample_specs(5), clock_offset_ms=HUGE_OFFSET)
    run_dir = tmp_path / "runs" / run_id
    record = load_detail(open_runs_root(tmp_path / "runs"), run_id, DEFAULT_IO, time.time_ns)
    body = dump_json(to_detail(record))
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    return record, json.loads(body), body.decode("ascii"), manifest["times"]["sigkill_sent_time"]


def test_build_chart_falls_back_and_leaves_the_sigkill_position_out_of_the_axis(loaded) -> None:
    record, _detail, _body, sigkill_text = loaded
    sigkill_ms = epoch_ms_utc(sigkill_text)
    rows = record.sample_rows

    chart = build_chart(
        rows, gap_index(record.read_result), "corrected", HUGE_OFFSET, sigkill_x_ms=sigkill_ms,
    )

    assert chart.time_axis == "raw_uncorrected"
    assert chart.correction_unavailable_reason == "correction_out_of_range"
    assert all(p.corrected_capture_time is None for p in chart.points)
    assert [p.x_ms for p in chart.points] == [epoch_ms_utc(p.ts) for p in chart.points]
    xs = [p.x_ms for p in chart.points]
    assert sigkill_ms > max(xs), "fixture: the SIGKILL time is later than every Sample"
    assert chart.x_axis_data.domain_min_ms == min(xs)
    assert chart.x_axis_data.domain_max_ms == max(xs)
    assert all(min(xs) <= t <= max(xs) for t in chart.x_axis_data.tick_values_ms)


def test_detail_after_fallback_is_raw_uncorrected_without_a_sigkill_marker(loaded) -> None:
    record, detail, _body, sigkill_text = loaded
    sigkill_ms = epoch_ms_utc(sigkill_text)
    chart, timing, markers = detail["chart"], detail["timing"], detail["termination_markers"]

    assert chart["time_axis"] == "raw_uncorrected" and timing["time_axis"] == "raw_uncorrected"
    assert timing["correction_available"] is False
    assert timing["correction_unavailable_reason"] == "correction_out_of_range"
    assert timing["clock_offset_ms"] == HUGE_OFFSET, "the recorded offset is reported unchanged"

    assert len(chart["points"]) == 5
    for point in chart["points"]:
        assert point["corrected_capture_time"] is None
        assert point["x_ms"] == epoch_ms_utc(point["capture_time"]), "raw Capture_Time epoch ms"
    xs = [p["x_ms"] for p in chart["points"]]
    axis = chart["x_axis"]
    assert axis["domain_min_ms"] == min(xs) and axis["domain_max_ms"] == max(xs)
    assert sigkill_ms > axis["domain_max_ms"]
    assert all(t["x_ms"] != sigkill_ms and min(xs) <= t["x_ms"] <= max(xs) for t in axis["ticks"])
    assert sigkill_text not in [t["label"] for t in axis["ticks"]]

    marker = markers["sigkill"]
    assert marker["drawn"] is False and marker["x_ms"] is None
    assert marker["not_drawn_reason"] == "correction_out_of_range"
    assert marker["time"] == sigkill_text, "the recorded string is kept"


def test_the_recorded_sigkill_time_stays_as_separately_labelled_host_clock_text(loaded) -> None:
    _record, detail, _body, sigkill_text = loaded
    texts = {t["kind"]: t for t in detail["termination_markers"]["text_times"]}

    assert texts["sigkill_sent"]["value"] == sigkill_text
    assert texts["sigkill_sent"]["clock"] == "host"
    assert detail["timing"]["sigkill_sent_time"] == sigkill_text
    assert detail["timing"]["sigkill_sent_time_clock"] == "host"


def test_derived_intervals_follow_the_unusable_correction_rule(loaded) -> None:
    _record, detail, _body, _sigkill_text = loaded
    derived = detail["timing"]["derived"]

    assert derived["suppressed"] is False
    assert [i["kind"] for i in derived["intervals"]] == ["finished_at_to_sigkill"]
    assert all(i["from_corrected_time"] is None for i in derived["intervals"])


def test_fallback_response_is_allowlisted_and_has_no_absolute_path(loaded, tmp_path) -> None:
    _record, detail, body, _sigkill_text = loaded

    assert set(detail) == DETAIL_BLOCKS
    assert set(detail["chart"]) == CHART_KEYS
    assert set(detail["chart"]["x_axis"]) == X_AXIS_KEYS
    assert set(detail["timing"]) == TIMING_KEYS
    assert set(detail["termination_markers"]) == TERMINATION_MARKERS_KEYS
    assert set(detail["termination_markers"]["sigkill"]) == SIGKILL_KEYS
    assert all(set(t) == TEXT_TIME_KEYS for t in detail["termination_markers"]["text_times"])
    assert _absolute_path_hits(detail, _needles(tmp_path)) == []
    assert str(tmp_path) not in body
