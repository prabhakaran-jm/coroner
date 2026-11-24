"""Clock, correction, ordering and derived-interval tests (task 2.15).

Two layers are covered:
- the pure functions of `coroner.viewer.clocks`;
- the real API path: a synthetic run written with RunsBuilder under tmp_path, read with `load_detail`
  and serialised with `to_detail` (then through `dump_json` so the assertions see what the API returns).

Nothing here reads the real runs/ directory.

Adaptations to the real API (the task text names concepts the Viewer carries differently):
- "labelled derived and not independently proven" is UI text (a later task). The API marks a derived
  interval with `from_basis == "corrected"` (or `docker_finished_at`) and a non-null `from_corrected_time`.
- "axis label code" is `time_axis` (`raw_capture`, `raw_uncorrected`, `corrected`).
- "reason `offset_not_recorded`" is `timing.correction_unavailable_reason`.
"""
from __future__ import annotations

import json
import time
from datetime import timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from coroner.common.timeutil import format_utc_ms, parse_utc_ms
from coroner.viewer import clocks
from coroner.viewer.clocks import (
    VIEWER_CLOCK_TOLERANCE_MAX_MS,
    OffsetResolution,
    axis_mode,
    corrected_ms,
    derived_intervals,
    ordering,
    resolve_offset,
    tolerance_status,
)
from coroner.viewer.files import DEFAULT_IO, open_runs_root
from coroner.viewer.loader import load_detail
from coroner.viewer.model import dump_json, to_detail
from coroner.viewer.samples import SampleRow
from coroner.viewer.timefmt import epoch_ms_to_text, epoch_ms_utc
from tests.unit.viewer.builder import (
    DEFAULT_FINISHED_AT,
    EVIDENCE_NAME,
    InspectSpec,
    RunsBuilder,
    SampleSpec,
    sample_specs,
)

SIGKILL_TIME = "2026-11-20T21:13:20.000Z"
SIGKILL_MS = epoch_ms_utc(SIGKILL_TIME)
FINISHED_MS = 1_795_209_201_760  # DEFAULT_FINISHED_AT truncated to the millisecond (21:13:21.760)
OFFSET = 9
UNCERTAINTY = 12.5


# ---------------------------------------------------------------- fixture helpers


def load(tmp_path, experiment, **kwargs):
    builder = RunsBuilder(tmp_path / "runs")
    run_id = builder.add_run(experiment, **kwargs)
    record = load_detail(open_runs_root(tmp_path / "runs"), run_id, DEFAULT_IO, time.time_ns)
    detail = json.loads(dump_json(to_detail(record)))
    return record, detail, tmp_path / "runs" / run_id


def raw_samples(run_dir):
    """seq -> (ts, recv_time) exactly as written to evidence.jsonl."""
    out = {}
    for line in (run_dir / EVIDENCE_NAME).read_bytes().splitlines():
        record = json.loads(line)
        if record["record_type"] == "sample":
            out[record["sample"]["seq"]] = (record["sample"]["ts"], record["recv_time"])
    return out


def timing_with(**overrides):
    block = {
        "tolerance_ms": 250,
        "pre_kill_count": 5,
        "pre_kill_late_received_count": 0,
        "within_tolerance_count": 5,
        "post_kill_beyond_tolerance": {"count": 0, "items": []},
        "after_container_stop": {"count": 0, "items": []},
        "after_container_stop_analysis": "available",
        "clock_offset_ms": OFFSET,
    }
    block.update(overrides)
    return block


def sample_at(seq, diff_ms):
    """A Sample whose corrected Capture_Time is `diff_ms` after the SIGKILL time (offset OFFSET)."""
    return SampleSpec(seq=seq, ts=epoch_ms_to_text(SIGKILL_MS + diff_ms + OFFSET))


def sigkill_intervals(detail):
    return [i for i in detail["timing"]["derived"]["intervals"] if i["kind"] == "sample_to_sigkill"]


def expected_ordering(diff, uncertainty, tolerance):
    if abs(diff) <= max(uncertainty, tolerance):
        return "not_established"
    return "before_sigkill" if diff < 0 else "after_sigkill"


def iter_numbers(obj):
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from iter_numbers(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from iter_numbers(value)


def iter_keys(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from iter_keys(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from iter_keys(value)


def row(seq, ts, rss=1, file_pos=None):
    return SampleRow(
        file_pos=seq + 1 if file_pos is None else file_pos, seq=seq, ts=ts,
        ts_ms=epoch_ms_utc(ts), recv=ts, rss=rss,
    )


# ---------------------------------------------------------------- Experiment B, usable offset


def test_b_with_usable_offset_corrects_capture_time_and_leaves_receive_time(tmp_path) -> None:
    record, detail, run_dir = load(tmp_path, "B", samples=sample_specs(5))
    raw = raw_samples(run_dir)
    timing, chart, markers = detail["timing"], detail["chart"], detail["termination_markers"]

    assert chart["time_axis"] == "corrected"
    assert timing["correction_available"] is True
    assert timing["correction_unavailable_reason"] is None
    assert timing["clock_offset_ms"] == OFFSET and timing["clock_offset_source"] == "sample_timing"
    assert [p["seq"] for p in chart["points"]] == [0, 1, 2, 3, 4]
    for point in chart["points"]:
        ts, recv = raw[point["seq"]]
        expected = format_utc_ms(parse_utc_ms(ts) - timedelta(milliseconds=OFFSET))
        assert point["capture_time"] == ts, "the raw Capture_Time stays as recorded"
        assert point["corrected_capture_time"] == expected
        assert point["x_ms"] == epoch_ms_utc(ts) - OFFSET, "x_ms is the corrected epoch"
        assert point["receive_time"] == recv, "Receive_Time is never shifted"
    marker = markers["sigkill"]
    assert marker == {
        "drawn": True, "time": SIGKILL_TIME, "time_display": SIGKILL_TIME, "x_ms": SIGKILL_MS,
        "not_drawn_reason": None,
    }
    axis = chart["x_axis"]
    assert axis["domain_min_ms"] <= min(p["x_ms"] for p in chart["points"])
    assert axis["domain_max_ms"] >= SIGKILL_MS


@pytest.mark.parametrize("case,expected", [
    ("b_corrected", "corrected"),
    ("b_no_offset", "raw_uncorrected"),
    ("b_conflict", "raw_uncorrected"),
    ("a", "raw_capture"),
    ("c", "raw_capture"),
])
def test_timing_time_axis_is_the_axis_code_and_equals_the_chart_time_axis(tmp_path, case, expected) -> None:
    # design: `timing.time_axis` is an enum string with the same value as `chart.time_axis`
    kwargs = {
        "b_corrected": ("B", {}),
        "b_no_offset": ("B", {"clock_offset_ms": None}),
        "b_conflict": ("B", {"clock_offset_ms": 9, "sample_timing": timing_with(clock_offset_ms=11)}),
        "a": ("A", {}),
        "c": ("C", {}),
    }[case]
    _, detail, _ = load(tmp_path, kwargs[0], samples=sample_specs(3), **kwargs[1])

    assert detail["chart"]["time_axis"] == expected
    assert detail["timing"]["time_axis"] == expected


def test_b_offset_recorded_only_at_top_level_is_used(tmp_path) -> None:
    _, detail, _ = load(
        tmp_path, "B", samples=sample_specs(3), clock_offset_ms=OFFSET,
        sample_timing=timing_with(clock_offset_ms=None),
    )
    assert detail["chart"]["time_axis"] == "corrected"
    assert detail["timing"]["clock_offset_source"] == "manifest"
    assert detail["timing"]["clock_offset_ms"] == OFFSET


def test_b_intervals_for_last_persisted_and_latest_capture_have_distinct_samples(tmp_path) -> None:
    # file order: seq 0 has the latest Capture_Time, seq 1 (last in the file) an earlier one
    samples = [sample_at(0, -100), sample_at(1, -3000)]
    _, detail, _ = load(tmp_path, "B", samples=samples)
    derived = detail["timing"]["derived"]
    by_key = {(i["kind"], i["sample"]): i for i in derived["intervals"]}

    assert set(by_key) == {
        ("sample_to_sigkill", "last_persisted"), ("sample_to_sigkill", "latest_capture"),
        ("sample_to_finished_at", "last_persisted"), ("sample_to_finished_at", "latest_capture"),
    }
    assert derived["suppressed"] is False and derived["finished_at_source"] == "manifest"
    for kind in ("sample_to_sigkill", "sample_to_finished_at"):
        last = by_key[(kind, "last_persisted")]
        latest = by_key[(kind, "latest_capture")]
        assert (last["sample_seq"], latest["sample_seq"]) == (1, 0)
        assert last["difference_ms"] != latest["difference_ms"]
        assert last["from_time"] != latest["from_time"]
    for interval in derived["intervals"]:
        assert interval["from_basis"] == "corrected", "derived, on the corrected Capture_Time"
        assert interval["from_corrected_time"] is not None
        assert interval["from_clock"] == "container"
    assert by_key[("sample_to_sigkill", "latest_capture")]["difference_ms"] == -100
    assert by_key[("sample_to_sigkill", "last_persisted")]["difference_ms"] == -3000
    assert by_key[("sample_to_sigkill", "last_persisted")]["to_clock"] == "host"
    assert by_key[("sample_to_finished_at", "latest_capture")]["difference_ms"] == -100 - (FINISHED_MS - SIGKILL_MS)
    assert detail["evidence_summary"]["last_persisted_sample"]["seq"] == 1
    assert detail["evidence_summary"]["latest_capture_sample"]["seq"] == 0


def test_docker_target_intervals_never_carry_an_ordering(tmp_path) -> None:
    _, detail, _ = load(tmp_path, "B", samples=sample_specs(3))
    docker_kinds = [i for i in detail["timing"]["derived"]["intervals"] if i["kind"] != "sample_to_sigkill"]

    assert {i["kind"] for i in docker_kinds} == {"sample_to_finished_at"}
    for interval in docker_kinds:
        assert interval["ordering"] is None and interval["bounded"] is False
        assert interval["reason"] == "no_recorded_bound_for_docker_clock"
        assert interval["to_clock"] == "docker"


# ---------------------------------------------------------------- Experiment B, no offset


def test_b_without_offset_uses_the_raw_axis_and_draws_no_marker(tmp_path) -> None:
    _, detail, run_dir = load(tmp_path, "B", samples=sample_specs(4), clock_offset_ms=None)
    raw = raw_samples(run_dir)
    timing, chart = detail["timing"], detail["chart"]

    assert chart["time_axis"] == "raw_uncorrected"
    assert timing["correction_available"] is False
    assert timing["correction_unavailable_reason"] == "offset_not_recorded"
    assert timing["clock_offset_ms"] is None and timing["clock_offset_conflict"] is None
    for point in chart["points"]:
        assert point["corrected_capture_time"] is None
        assert point["x_ms"] == epoch_ms_utc(raw[point["seq"]][0]), "raw uncorrected position"
    marker = detail["termination_markers"]["sigkill"]
    assert marker["drawn"] is False and marker["x_ms"] is None
    assert marker["not_drawn_reason"] == "offset_not_recorded"
    assert marker["time"] == SIGKILL_TIME, "the SIGKILL time is still listed as text"


def test_b_without_correction_has_only_the_finished_at_to_sigkill_interval(tmp_path) -> None:
    _, detail, _ = load(tmp_path, "B", samples=sample_specs(4), clock_offset_ms=None)
    intervals = detail["timing"]["derived"]["intervals"]

    assert [i["kind"] for i in intervals] == ["finished_at_to_sigkill"]
    only = intervals[0]
    assert only["sample"] is None and only["sample_seq"] is None
    assert only["difference_ms"] == FINISHED_MS - SIGKILL_MS
    assert only["from_basis"] == "docker_finished_at" and only["from_corrected_time"] is None
    assert only["ordering"] is None and only["bounded"] is False
    assert only["reason"] == "no_recorded_bound_for_docker_clock"
    assert only["from_clock"] == "docker" and only["to_clock"] == "host"
    assert not any(i["kind"].startswith("sample_to_") for i in intervals)


def test_b_without_correction_and_a_missing_time_has_no_interval(tmp_path) -> None:
    no_sigkill = {"times": {"sigkill_sent_time": None, "crash_request_time": None}}
    _, without_sigkill, _ = load(
        tmp_path / "a", "B", samples=sample_specs(3), clock_offset_ms=None, manifest_extra=no_sigkill,
    )
    _, without_finished, _ = load(
        tmp_path / "b", "B", samples=sample_specs(3), clock_offset_ms=None,
        manifest_extra={"container_finished_at": None}, inspect=None,
    )

    assert without_sigkill["timing"]["derived"]["intervals"] == []
    assert without_finished["timing"]["derived"]["intervals"] == []
    assert without_finished["timing"]["derived"]["finished_at_source"] is None


# ---------------------------------------------------------------- Experiment B, conflicting offsets


def test_b_with_conflicting_offsets_returns_both_and_suppresses_everything_derived(tmp_path) -> None:
    _, detail, run_dir = load(
        tmp_path, "B", samples=sample_specs(4), clock_offset_ms=9,
        sample_timing=timing_with(clock_offset_ms=11),
    )
    raw = raw_samples(run_dir)
    timing, chart = detail["timing"], detail["chart"]

    assert timing["clock_offset_conflict"] == {"sample_timing": 11, "manifest": 9}
    assert timing["clock_offset_ms"] is None and timing["clock_offset_source"] is None
    assert chart["time_axis"] == "raw_uncorrected"
    assert timing["correction_available"] is False
    assert timing["correction_unavailable_reason"] == "clock_offsets_conflict"
    for point in chart["points"]:
        assert point["corrected_capture_time"] is None
        assert point["x_ms"] == epoch_ms_utc(raw[point["seq"]][0])
    assert detail["termination_markers"]["sigkill"]["drawn"] is False
    assert detail["termination_markers"]["sigkill"]["x_ms"] is None
    assert timing["derived"]["suppressed"] is True
    assert timing["derived"]["suppressed_reason"] == "clock_offsets_conflict"
    assert timing["derived"]["intervals"] == []
    assert timing["sample_timing"]["clock_offset_ms"] == 11, "the recorded sample_timing value is returned"


# ---------------------------------------------------------------- missing sample_timing, uncertainty


def test_b_with_a_missing_sample_timing(tmp_path) -> None:
    _, detail, _ = load(tmp_path, "B", samples=sample_specs(3), sample_timing=None)
    timing = detail["timing"]

    assert timing["sample_timing"] is None
    assert timing["tolerance_ms"] is None and timing["tolerance_status"] == "not_recorded"
    assert detail["chart"]["time_axis"] == "corrected", "the top-level offset still corrects"
    intervals = sigkill_intervals(detail)
    assert intervals and all(i["ordering"] is None for i in intervals)
    assert all(i["reason"] == "tolerance_not_recorded" for i in intervals)
    assert all(i["difference_ms"] is not None for i in intervals)


def test_b_with_a_missing_uncertainty_states_no_ordering(tmp_path) -> None:
    _, detail, _ = load(tmp_path, "B", samples=[sample_at(0, -500)], uncertainty_ms=None)
    timing = detail["timing"]

    assert timing["clock_uncertainty_ms"] is None
    assert timing["tolerance_status"] == "valid"
    intervals = sigkill_intervals(detail)
    assert len(intervals) == 2
    for interval in intervals:
        assert interval["ordering"] is None
        assert interval["bounded"] is False
        assert interval["reason"] == "uncertainty_not_recorded"
        assert interval["uncertainty_ms"] is None


def test_b_with_a_missing_uncertainty_returns_no_difference(tmp_path) -> None:
    # Design Property 10 and requirement 15.19: with the uncertainty not recorded the sample_to_sigkill
    # interval has difference_ms and ordering both null.
    _, detail, _ = load(tmp_path, "B", samples=[sample_at(0, -500)], uncertainty_ms=None)

    for interval in sigkill_intervals(detail):
        assert interval["difference_ms"] is None
        assert interval["ordering"] is None


# ---------------------------------------------------------------- Experiments A and C


@pytest.mark.parametrize("experiment", ["A", "C"])
def test_a_and_c_have_raw_axis_no_marker_no_corrected_values_and_no_intervals(tmp_path, experiment) -> None:
    _, detail, run_dir = load(tmp_path, experiment, samples=sample_specs(20))
    raw = raw_samples(run_dir)
    timing, chart = detail["timing"], detail["chart"]

    assert chart["time_axis"] == "raw_capture"
    assert timing["correction_available"] is False
    assert timing["clock_offset_ms"] is None and timing["sample_timing"] is None
    assert len(chart["points"]) == 20
    for point in chart["points"]:
        assert point["corrected_capture_time"] is None
        assert point["x_ms"] == epoch_ms_utc(raw[point["seq"]][0])
    marker = detail["termination_markers"]["sigkill"]
    assert marker["drawn"] is False and marker["x_ms"] is None
    assert timing["derived"]["intervals"] == []
    assert timing["derived"]["suppressed"] is False
    kinds = {i["kind"] for i in timing["derived"]["intervals"]}
    assert not kinds & {"sample_to_finished_at", "sample_to_sigkill", "finished_at_to_sigkill"}


@pytest.mark.parametrize("experiment", ["A", "C"])
def test_a_and_c_return_capture_times_and_finished_at_as_separate_labelled_values(tmp_path, experiment) -> None:
    record, detail, run_dir = load(tmp_path, experiment, samples=sample_specs(20))
    raw = raw_samples(run_dir)
    rows = {t["kind"]: t for t in detail["termination_markers"]["text_times"]}
    last_ts, latest_ts = raw[19][0], raw[19][0]

    assert rows["last_persisted_capture"]["value"] == last_ts
    assert rows["last_persisted_capture"]["clock"] == "container"
    assert rows["latest_capture"]["value"] == latest_ts
    assert rows["latest_capture"]["clock"] == "container"
    assert rows["docker_finished_at"]["value"] == DEFAULT_FINISHED_AT, "the raw nine-digit value"
    assert rows["docker_finished_at"]["display"] == "2026-11-20T21:13:21.760Z"
    assert rows["docker_finished_at"]["clock"] == "docker"
    if experiment == "A":
        assert rows["crash_requested"]["clock"] == "host"
    else:
        assert "crash_requested" not in rows


@pytest.mark.parametrize("experiment", ["A", "C"])
def test_a_and_c_response_holds_no_capture_time_to_finished_at_difference(tmp_path, experiment) -> None:
    _, detail, run_dir = load(tmp_path, experiment, samples=sample_specs(20))
    raw = raw_samples(run_dir)
    capture_ms = [epoch_ms_utc(ts) for ts, _ in raw.values()]
    other_ms = [FINISHED_MS, epoch_ms_utc("2026-11-20T21:13:20.000Z"), epoch_ms_utc(DEFAULT_FINISHED_AT[:23] + "Z")]
    forbidden = set()
    for c in capture_ms:
        for o in other_ms:
            forbidden.update({c - o, o - c, c - OFFSET - o, o - (c - OFFSET)})
    forbidden.discard(0)

    found = [n for n in iter_numbers(detail) if n in forbidden]
    keys = [k for k in iter_keys(detail) if "difference" in k or "ordering" in k]

    assert found == [], f"a number equal to a Capture_Time to FinishedAt difference is in the response: {found}"
    assert [k for k in keys if k != "difference_ms"] == []
    assert detail["timing"]["derived"]["intervals"] == [], "difference_ms only lives inside intervals"


def test_derived_intervals_function_is_empty_for_a_c_and_unknown() -> None:
    sample = row(0, "2026-11-20T21:13:01.000Z")
    res = OffsetResolution(offset_ms=None, source=None, conflict=None)
    for exp in ("A", "C", None, "Z"):
        block = derived_intervals(
            exp, res, sample, sample, SIGKILL_MS, FINISHED_MS, 12.5, 250, "valid", "manifest",
        )
        assert block.intervals == [] and block.suppressed is False
        assert axis_mode(exp, res) == ("raw_capture", None)
        assert resolve_offset(exp, {"clock_offset_ms": 9}) == res


# ---------------------------------------------------------------- recorded sample_timing


def test_recorded_sample_timing_is_returned_unchanged_and_never_recomputed(tmp_path) -> None:
    recorded = timing_with(
        pre_kill_count=77, pre_kill_late_received_count=5, within_tolerance_count=60,
        post_kill_beyond_tolerance={"count": 2, "items": [
            {"capture_time": "2026-11-20T21:13:20.700Z"}, {"capture_time": "2026-11-20T21:13:20.800Z"},
        ]},
        after_container_stop={"count": 1, "items": [{"capture_time": "2026-11-20T21:13:22.100Z"}]},
        after_container_stop_analysis="available",
    )
    _, detail, _ = load(tmp_path, "B", samples=sample_specs(3), sample_timing=recorded)
    st_block = detail["timing"]["sample_timing"]

    assert (st_block["pre_kill_count"], st_block["pre_kill_late_received_count"],
            st_block["within_tolerance_count"]) == (77, 5, 60), "the evidence has 3 Samples; not recomputed"
    assert st_block["post_kill_beyond_tolerance"]["count"] == 2
    assert [i["capture_time"] for i in st_block["post_kill_beyond_tolerance"]["items"]] == [
        "2026-11-20T21:13:20.700Z", "2026-11-20T21:13:20.800Z",
    ]
    assert st_block["after_container_stop"]["count"] == 1
    assert st_block["after_container_stop"]["items"][0]["capture_time"] == "2026-11-20T21:13:22.100Z"
    assert st_block["after_container_stop_analysis"] == "available"
    assert st_block["tolerance_ms"] == 250 and st_block["clock_offset_ms"] == OFFSET


def test_after_container_stop_unavailable_gives_a_null_count_never_zero(tmp_path) -> None:
    # the builder default records count 0 with analysis "unavailable"
    _, detail, _ = load(tmp_path, "B", samples=sample_specs(3))
    group = detail["timing"]["sample_timing"]["after_container_stop"]

    assert detail["timing"]["sample_timing"]["after_container_stop_analysis"] == "unavailable"
    assert group["count"] is None and group["count"] != 0
    assert group["items"] == []


# ---------------------------------------------------------------- FinishedAt


def test_nine_digit_finished_at_is_returned_unchanged_with_a_millisecond_display(tmp_path) -> None:
    _, detail, _ = load(tmp_path, "B", samples=sample_specs(3), inspect=InspectSpec(finished_at=DEFAULT_FINISHED_AT))
    timing = detail["timing"]

    assert timing["state_finished_at"] == "2026-11-20T21:13:21.760545826Z"
    assert timing["state_finished_at_display"] == "2026-11-20T21:13:21.760Z"
    assert timing["state_finished_at_clock"] == "docker"
    assert timing["container_finished_at_display"] == "2026-11-20T21:13:21.760Z"
    assert timing["container_finished_at_clock"] == "docker"


def test_finished_at_versus_sigkill_has_no_ordering_with_or_without_correction(tmp_path) -> None:
    for name, offset in (("with", OFFSET), ("without", None)):
        _, detail, _ = load(tmp_path / name, "B", samples=sample_specs(3), clock_offset_ms=offset)
        docker_to_host = [i for i in detail["timing"]["derived"]["intervals"] if i["kind"] == "finished_at_to_sigkill"]
        if offset is None:
            assert len(docker_to_host) == 1
        for interval in docker_to_host:
            assert interval["ordering"] is None and interval["bounded"] is False
        # FinishedAt (after SIGKILL by 1,760 ms) is far outside any uncertainty and still has no ordering
        assert all(
            i["ordering"] is None
            for i in detail["timing"]["derived"]["intervals"] if i["kind"] != "sample_to_sigkill"
        )


# ---------------------------------------------------------------- ordering suppression (API)


@pytest.mark.parametrize("tolerance", [0, 250])
@pytest.mark.parametrize("diff", [-100, 5, 400, -12, 12, 13])
def test_valid_tolerance_gives_ordering_by_the_threshold_rule(tmp_path, tolerance, diff) -> None:
    _, detail, _ = load(
        tmp_path, "B", samples=[sample_at(0, diff)], sample_timing=timing_with(tolerance_ms=tolerance),
    )
    intervals = sigkill_intervals(detail)

    assert detail["timing"]["tolerance_status"] == "valid"
    assert len(intervals) == 2
    for interval in intervals:
        assert interval["difference_ms"] == diff
        assert interval["ordering"] == expected_ordering(diff, UNCERTAINTY, tolerance)
        assert interval["threshold_ms"] == max(UNCERTAINTY, tolerance)
        assert interval["tolerance_ms"] == tolerance
        assert interval["bounded"] is (interval["ordering"] != "not_established")


# ---------------------------------------------------------------- tolerance matrix (API)

MISSING = object()
TOLERANCE_MATRIX = [
    (0, "valid"), (250, "valid"),
    (-1, "out_of_range"), (251, "out_of_range"),
    (True, "invalid_type"), (False, "invalid_type"),
    (250.0, "invalid_type"), (12.5, "invalid_type"), ("250", "invalid_type"),
    (None, "not_recorded"), (MISSING, "not_recorded"),
]


@pytest.mark.parametrize(
    "stored,status", TOLERANCE_MATRIX,
    ids=[f"{s!r}" if s is not MISSING else "missing" for s, _ in TOLERANCE_MATRIX],
)
def test_tolerance_matrix_through_the_api(tmp_path, stored, status) -> None:
    block = timing_with()
    if stored is MISSING:
        del block["tolerance_ms"]
    else:
        block["tolerance_ms"] = stored
    diff = -100  # outside the uncertainty (12.5); inside tolerance 250, outside tolerance 0
    _, detail, _ = load(tmp_path, "B", samples=[sample_at(0, diff)], sample_timing=block)
    timing = detail["timing"]
    intervals = sigkill_intervals(detail)

    assert timing["tolerance_status"] == status
    recorded = None if stored is MISSING else stored
    assert timing["tolerance_ms"] == recorded and type(timing["tolerance_ms"]) is type(recorded), (
        "the recorded value is returned as stored: never clamped, converted or replaced"
    )
    if stored is not MISSING:
        assert type(timing["sample_timing"]["tolerance_ms"]) is type(stored)
        assert timing["sample_timing"]["tolerance_ms"] == stored
    assert len(intervals) == 2
    for interval in intervals:
        assert interval["difference_ms"] == diff, "the difference is still present"
        assert type(interval["tolerance_ms"]) is type(recorded) and interval["tolerance_ms"] == recorded
        if status == "valid":
            assert interval["ordering"] == expected_ordering(diff, UNCERTAINTY, stored)
            assert interval["ordering"] in {"before_sigkill", "after_sigkill", "not_established"}
        else:
            assert interval["ordering"] is None
            assert interval["reason"] == f"tolerance_{status}"
            assert interval["threshold_ms"] is None and interval["bounded"] is False


# ---------------------------------------------------------------- pure functions


@pytest.mark.parametrize("value,expected", [
    (0, "valid"), (250, "valid"), (1, "valid"),
    (-1, "out_of_range"), (251, "out_of_range"), (10 ** 6, "out_of_range"),
    (True, "invalid_type"), (False, "invalid_type"), (250.0, "invalid_type"), (12.5, "invalid_type"),
    ("250", "invalid_type"), ([250], "invalid_type"), ({"v": 1}, "invalid_type"),
    (None, "not_recorded"),
])
def test_tolerance_status_function(value, expected) -> None:
    assert tolerance_status(value) == expected
    assert VIEWER_CLOCK_TOLERANCE_MAX_MS == 250


def test_ordering_function_boundaries_and_reasons() -> None:
    assert ordering(250, 12.5, 250, "valid") == ("not_established", "within_uncertainty_and_tolerance")
    assert ordering(-250, 12.5, 250, "valid")[0] == "not_established"
    assert ordering(251, 12.5, 250, "valid") == ("after_sigkill", None)
    assert ordering(-251, 12.5, 250, "valid") == ("before_sigkill", None)
    assert ordering(300, 300.0, 250, "valid")[0] == "not_established", "uncertainty larger than tolerance"
    assert ordering(301, 300.0, 250, "valid")[0] == "after_sigkill"
    assert ordering(5, 12.5, 0, "valid")[0] == "not_established"
    assert ordering(13, 12.5, 0, "valid")[0] == "after_sigkill"
    assert ordering(-100, None, 250, "valid") == (None, "uncertainty_not_recorded")
    for status in ("not_recorded", "invalid_type", "out_of_range"):
        assert ordering(-100, 12.5, 250, status) == (None, f"tolerance_{status}")


@settings(max_examples=200, deadline=None)
@given(
    diff=st.integers(min_value=-10_000, max_value=10_000),
    uncertainty=st.floats(min_value=0, max_value=1000, allow_nan=False, allow_infinity=False),
    tolerance=st.integers(min_value=0, max_value=250),
)
def test_ordering_property(diff, uncertainty, tolerance) -> None:
    code, reason = ordering(diff, uncertainty, tolerance, "valid")
    assert code == expected_ordering(diff, uncertainty, tolerance)
    assert (reason is None) == (code != "not_established")


def test_resolve_offset_rules() -> None:
    both_equal = resolve_offset("B", {"clock_offset_ms": 9, "sample_timing": {"clock_offset_ms": 9}})
    assert (both_equal.offset_ms, both_equal.source, both_equal.conflict) == (9, "sample_timing", None)
    conflict = resolve_offset("B", {"clock_offset_ms": 9, "sample_timing": {"clock_offset_ms": 11}})
    assert (conflict.offset_ms, conflict.source, conflict.conflict) == (None, None, (11, 9))
    only_top = resolve_offset("B", {"clock_offset_ms": 9})
    assert (only_top.offset_ms, only_top.source) == (9, "manifest")
    only_timing = resolve_offset("B", {"sample_timing": {"clock_offset_ms": 4}})
    assert (only_timing.offset_ms, only_timing.source) == (4, "sample_timing")
    for bad in (9.0, True, "9", None):
        assert resolve_offset("B", {"clock_offset_ms": bad}).offset_ms is None, repr(bad)
    assert resolve_offset("B", {}) == OffsetResolution(None, None, None)
    assert resolve_offset("B", {"sample_timing": "text", "clock_offset_ms": 9}).offset_ms == 9


def test_axis_mode_and_corrected_ms() -> None:
    usable = OffsetResolution(9, "manifest", None)
    assert axis_mode("B", usable) == ("corrected", None)
    assert axis_mode("B", OffsetResolution(None, None, (11, 9))) == ("raw_uncorrected", "clock_offsets_conflict")
    assert axis_mode("B", OffsetResolution(None, None, None)) == ("raw_uncorrected", "offset_not_recorded")
    assert axis_mode("A", usable) == ("raw_capture", None)
    assert corrected_ms(1_795_209_180_500, 9) == 1_795_209_180_491
    assert corrected_ms(1_000, -5) == 1_005


def test_derived_intervals_function_for_b_cases() -> None:
    sample = row(3, "2026-11-20T21:13:10.000Z")
    usable = OffsetResolution(OFFSET, "sample_timing", None)
    block = derived_intervals("B", usable, sample, sample, SIGKILL_MS, FINISHED_MS, 12.5, 250, "valid", "manifest")
    assert [(i.kind, i.sample) for i in block.intervals] == [
        ("sample_to_sigkill", "last_persisted"), ("sample_to_finished_at", "last_persisted"),
        ("sample_to_sigkill", "latest_capture"), ("sample_to_finished_at", "latest_capture"),
    ]
    first = block.intervals[0]
    assert first.difference_ms == epoch_ms_utc(sample.ts) - OFFSET - SIGKILL_MS
    assert first.ordering == "before_sigkill"

    none = OffsetResolution(None, None, None)
    plain = derived_intervals("B", none, sample, sample, SIGKILL_MS, FINISHED_MS, 12.5, 250, "valid", "manifest")
    assert [i.kind for i in plain.intervals] == ["finished_at_to_sigkill"]
    assert derived_intervals("B", none, sample, sample, None, FINISHED_MS, 12.5, 250, "valid", None).intervals == []

    conflict = OffsetResolution(None, None, (11, 9))
    suppressed = derived_intervals("B", conflict, sample, sample, SIGKILL_MS, FINISHED_MS, 12.5, 250, "valid", "manifest")
    assert suppressed.suppressed is True and suppressed.intervals == []
    assert clocks.derived_intervals is derived_intervals
