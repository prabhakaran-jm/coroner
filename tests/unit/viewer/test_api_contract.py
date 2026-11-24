"""API contract tests of the Viewer (Requirement 9, 2.9, 2.11, 12, 14, 16.10; design: API field tables).

Synthetic runs in tmp_path only (RunsBuilder). The real app (create_app, real loader and serialisers) is
served in-process through a TestClient with base_url http://127.0.0.1:<port> and a matching Host header.
Nothing reads the real runs/ directory, starts a server, opens a socket or uses Docker.

Expected key sets are written out here from the design field tables, not imported from coroner.viewer.model,
so a field added to or dropped from a response without a design change fails these tests.
"""
from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from coroner.common.timeutil import format_utc_ms, parse_utc_ms
from coroner.viewer.app import create_app
from tests.unit.viewer.builder import (
    GapSpec,
    InspectSpec,
    RunsBuilder,
    SampleSpec,
    StateSpec,
    sample_specs,
)

PORT = 18766
CHART_CAP = 5000
ARTIFACT_LIMIT = 16_777_216

# ---------------------------------------------------------------- expected key sets (design tables)

LIST_ENVELOPE = {"runs_dir_label", "total_found", "listed", "truncated", "max_listed", "runs"}
LIST_ITEM = {
    "run_id", "experiment", "experiment_type", "verdict", "start_time", "start_time_display", "exit_code",
    "oom_killed", "sample_count", "collector_survival", "docker_state_source", "docker_state_discrepancy",
    "reason",
}
DETAIL_BLOCKS = {
    "run", "docker_state", "timing", "termination_markers", "evidence_summary", "chart", "runner_text",
    "artifact_status", "warnings",
}
RUN_KEYS = {
    "run_id", "experiment", "experiment_type", "outcome", "verdict", "failure", "durability_level",
    "start_time", "start_time_display", "start_time_clock", "end_time", "end_time_display", "end_time_clock",
    "record_count_manifest", "collector_survival", "checks", "checks_total", "checks_truncated",
    "collector_health", "termination_record", "diagnostics", "error",
}
FAILURE_KEYS = {
    "code", "code_truncated", "code_original_length", "message", "message_truncated", "message_original_length",
}
CHECK_KEYS = {
    "name", "name_truncated", "name_original_length", "passed", "detail", "detail_truncated",
    "detail_original_length",
}
COLLECTOR_HEALTH_KEYS = {"http_status", "storage_ok", "ok", "checked_time", "checked_time_display"}
TERMINATION_RECORD_KEYS = {"posted", "observed_time", "observed_time_display"}
DIAGNOSTICS_KEYS = {"rss_monotonic_tail", "peak_rss_ratio_of_limit", "sample_count"}
DOCKER_STATE_KEYS = {
    "source", "raw_unavailable_reason", "manifest", "raw", "effective", "fields", "discrepancies",
}
DOCKER_MANIFEST_KEYS = {
    "present", "container_id", "status", "exit_code", "oom_killed", "memory_limit_bytes", "inspect_time",
    "inspect_time_display", "inspect_time_clock", "finished_at", "finished_at_display", "finished_at_clock",
}
DOCKER_RAW_KEYS = {
    "container_id", "status", "exit_code", "oom_killed", "memory_limit_bytes", "finished_at",
    "finished_at_display", "finished_at_clock",
}
DOCKER_EFFECTIVE_KEYS = {
    "container_id", "status", "exit_code", "oom_killed", "memory_limit_bytes", "memory_limit_kind",
}
DOCKER_FIELD_KEYS = {"field", "manifest_value", "raw_value", "result"}
TIMING_KEYS = {
    "start_time", "start_time_display", "start_time_clock", "end_time", "end_time_display", "end_time_clock",
    "inspect_time", "inspect_time_display", "inspect_time_clock", "crash_request_time",
    "crash_request_time_display", "crash_request_time_clock", "sigkill_sent_time", "sigkill_sent_time_display",
    "sigkill_sent_time_clock", "container_finished_at", "container_finished_at_display",
    "container_finished_at_clock", "state_finished_at", "state_finished_at_display", "state_finished_at_clock",
    "clock_offset_ms", "clock_offset_clock", "clock_offset_source", "clock_offset_conflict",
    "clock_uncertainty_ms", "clock_uncertainty_clock", "tolerance_ms", "tolerance_status", "tolerance_clock",
    "time_axis", "correction_available", "correction_unavailable_reason", "sample_timing", "derived",
}
DERIVED_KEYS = {"suppressed", "suppressed_reason", "finished_at_source", "intervals"}
TERMINATION_MARKERS_KEYS = {"sigkill", "text_times"}
SIGKILL_KEYS = {"drawn", "time", "time_display", "x_ms", "not_drawn_reason"}
TEXT_TIME_KEYS = {"kind", "value", "display", "clock"}
EVIDENCE_SUMMARY_KEYS = {
    "sample_count", "valid_record_count", "manifest_record_count", "record_count_mismatch",
    "missing_sample_count", "unreconciled_drop_count", "unreconciled_drop_uncertain", "unreconciled_drops",
    "unreconciled_drops_total", "unreconciled_drops_truncated", "silence_intervals", "silence_intervals_total",
    "silence_intervals_truncated", "malformed_line_count", "malformed_lines", "malformed_lines_total",
    "malformed_lines_truncated", "has_valid_record", "first_rss", "highest_rss", "last_rss",
    "last_persisted_sample", "latest_capture_sample", "last_receive_time_summary",
    "last_receive_time_summary_display", "termination_cause", "unobserved_interval_note", "durability_levels",
}
RSS_ENTRY_KEYS = {"seq", "rss_bytes", "capture_time", "capture_time_display", "basis"}
LAST_PERSISTED_KEYS = {
    "seq", "capture_time", "capture_time_display", "receive_time", "receive_time_display", "rss_bytes",
}
LATEST_CAPTURE_KEYS = {"seq", "capture_time", "capture_time_display", "rss_bytes"}
SILENCE_KEYS = {"last_sample_time", "last_sample_time_display", "detected_time", "detected_time_display"}
MALFORMED_KEYS = {"line_no", "byte_length", "kind"}
CHART_KEYS = {
    "total_samples", "plottable_samples", "unplottable_samples", "plotted_samples", "partial_view", "chart_cap",
    "time_axis", "x_axis", "y_data_max_bytes", "unknown_range_drop_present", "points", "gaps", "gaps_total",
    "gaps_truncated",
}
X_AXIS_KEYS = {"domain_min_ms", "domain_max_ms", "ticks"}
POINT_KEYS = {
    "seq", "capture_time", "capture_time_display", "receive_time", "receive_time_display",
    "corrected_capture_time", "x_ms", "rss_bytes", "gap_before", "skipped_before", "omitted_before",
    "segment_before",
}
GAP_KEYS = {"gap_type", "first_missing_seq", "last_missing_seq", "range_status"}
RUNNER_TEXT_KEYS = {
    "termination_description", "termination_description_truncated", "termination_description_original_length",
    "cause_label", "cause_label_truncated", "cause_label_original_length", "limitations", "limitations_total",
    "limitations_truncated", "sampling_limitations",
}
TEXT_ITEM_KEYS = {"text", "text_truncated", "text_original_length"}
SAMPLING_LIMITATIONS_KEYS = {
    "sample_interval_ms", "approx_leak_rate_bytes_per_s", "statements", "statements_total",
    "statements_truncated",
}
ARTIFACT_STATUS_KEYS = {"manifest", "evidence", "inspect"}
ARTIFACT_ENTRY_KEYS = {"status", "reason", "observed_size_text", "limit_bytes"}
WARNING_KEYS = {"source", "code", "text", "text_truncated", "text_original_length"}
SAMPLE_TIMING_KEYS = {
    "pre_kill_count", "pre_kill_late_received_count", "within_tolerance_count", "post_kill_beyond_tolerance",
    "after_container_stop", "after_container_stop_analysis", "clock_offset_ms", "tolerance_ms",
}
SAMPLE_TIMING_GROUP_KEYS = {"count", "items", "items_total", "items_truncated"}

# Manifest keys the Viewer never reads (design: never-read keys) plus one unknown key.
NEVER_READ_EXTRA: dict[str, Any] = {
    "phases": {"startup": "NEVERREAD-phases"},
    "leak": {"rate": 123456, "note": "NEVERREAD-leak"},
    "collector": {"readiness": {"state": "NEVERREAD-readiness"}},
    "memory": {"limit": 7777777, "note": "NEVERREAD-memory"},
    "highest_rss_bytes": 987654321,
    "evidence_summary": {"sample_count": 4242424, "note": "NEVERREAD-evidence-summary"},
    "container_left_running": True,
    "container_state_problems": ["NEVERREAD-problem"],
    "zz_unknown_fixture_key": "NEVERREAD-unknown",
}


# ---------------------------------------------------------------- helpers


def _client(runs: Path | str) -> TestClient:
    return TestClient(create_app(runs, PORT), base_url=f"http://127.0.0.1:{PORT}")


def _no_constant(name: str) -> Any:
    raise AssertionError(f"response contains the JSON constant {name}")


def _fetch(client: TestClient, url: str) -> tuple[Any, str]:
    """GET `url` and return (strictly parsed JSON, body text). A NaN or Infinity token fails here."""
    response = client.get(url, headers={"Host": f"127.0.0.1:{PORT}"})
    assert response.status_code == 200, response.text
    return json.loads(response.text, parse_constant=_no_constant), response.text


def _listing(client: TestClient) -> dict[str, Any]:
    return _fetch(client, "/api/runs")[0]


def _detail(client: TestClient, run_id: str) -> dict[str, Any]:
    return _fetch(client, f"/api/runs/{run_id}")[0]


def _edit_manifest(runs: Path, run_id: str, edit: Callable[[dict[str, Any]], None]) -> None:
    path = runs / run_id / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_bytes(json.dumps(data, ensure_ascii=True, indent=2).encode("ascii") + b"\n")


def _walk(value: Any, path: str = "$") -> Iterator[tuple[str, Any]]:
    """Every (path, value) in a JSON document, containers included."""
    yield path, value
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]")


_DRIVE_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")
_POSIX_ABS = re.compile(r"(?:^|[\s\"'(=])/[^\s/]")
_UNC = re.compile(r"\\\\[^\s\\]")


def _absolute_path_hits(body: Any, extra_needles: tuple[str, ...]) -> list[str]:
    hits: list[str] = []
    for where, value in _walk(body):
        if not isinstance(value, str):
            continue
        if _DRIVE_PATH.search(value) or _POSIX_ABS.search(value) or _UNC.search(value):
            hits.append(f"{where}: {value[:80]!r}")
        for needle in extra_needles:
            if needle and needle in value:
                hits.append(f"{where} contains {needle!r}")
    return hits


def _needles(tmp_path: Path) -> tuple[str, ...]:
    return (str(tmp_path), tmp_path.as_posix(), str(tmp_path).replace("\\", "\\\\"))


def _assert_response_invariants(body: Any, tmp_path: Path) -> None:
    """No NaN/Infinity, no empty-string value, no absolute path in a parsed response."""
    for where, value in _walk(body):
        if isinstance(value, float):
            assert math.isfinite(value), where
        if isinstance(value, str):
            assert value != "", f"{where} is an empty string"
    assert _absolute_path_hits(body, _needles(tmp_path)) == []


def _make_paths_absolute(runs: Path, run_id: str) -> dict[str, str]:
    """Rewrite the fixture manifest so that every recorded path is absolute (as M0 may write them)."""
    inspect_abs = str(runs / run_id / "docker_inspect.json")
    evidence_abs = (runs / run_id / "evidence.jsonl").as_posix()
    docker_abs = str(runs.resolve() / run_id / "docker_inspect.json")

    def edit(m: dict[str, Any]) -> None:
        if m.get("container_state"):
            m["container_state"]["raw_inspect_path"] = inspect_abs
        m["evidence"]["host_copy_path"] = evidence_abs
        m["docker_inspect_path"] = docker_abs

    _edit_manifest(runs, run_id, edit)
    return {"raw_inspect_path": inspect_abs, "host_copy_path": evidence_abs, "docker_inspect_path": docker_abs}


def _rich_samples() -> list[SampleSpec]:
    return [
        SampleSpec(seq=0), SampleSpec(seq=1), SampleSpec(seq=2, rss_bytes=None),
        SampleSpec(seq=4), SampleSpec(seq=5, rss_bytes=90_000_000),
    ]


RICH_EXTRA = {
    "checks": [
        {"name": "collector_survival", "passed": True, "detail": "collector answered"},
        {"name": "exit_code", "passed": False, "detail": "exit 137 <b>expected</b>"},
    ],
    "collector_health": {
        "http_status": 200, "storage_ok": True, "ok": True, "checked_time": "2026-11-20T21:13:29.500Z",
    },
    "termination": {"posted": True, "observed_time": "2026-11-20T21:13:20.100Z", "cause_label": "sigkill"},
    "limitations": ["first limitation", "second limitation"],
}


@pytest.fixture
def fleet(tmp_path: Path):
    """Runs A, B (rich), C (rich), plus B/C Docker-state variants, built under tmp_path/runs."""
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    ids: dict[str, str] = {}
    ids["A"] = builder.add_run(
        "A", run_id="a-contract-00000001", samples=sample_specs(5),
    )
    ids["B"] = builder.add_run(
        "B", run_id="b-contract-00000001", samples=_rich_samples(),
        gaps=[GapSpec(first_missing_seq=3, last_missing_seq=3), GapSpec(kind="silence_timeout")],
        manifest_extra=RICH_EXTRA,
    )
    ids["C"] = builder.add_run(
        "C", run_id="c-contract-00000001", samples=sample_specs(5),
        manifest_extra={
            "diagnostics": {"rss_monotonic_tail": True, "peak_rss_ratio_of_limit": 0.97, "sample_count": 5},
            "termination": {"posted": False, "cause_label": "oom"},
        },
    )
    ids["B_oom_false"] = builder.add_run(
        "B", run_id="b-contract-0000000a", samples=sample_specs(3),
        container_state=StateSpec(exit_code=137, oom_killed=False),
    )
    ids["C_oom_true"] = builder.add_run(
        "C", run_id="c-contract-0000000a", samples=sample_specs(3),
        container_state=StateSpec(exit_code=137, oom_killed=True),
    )
    ids["B_failed"] = builder.add_run(
        "B", run_id="b-contract-0000000f", outcome="failed", samples=sample_specs(3),
    )
    return tmp_path, runs, builder, ids


# ================================================================ exact key sets


def test_list_envelope_and_item_have_exactly_the_documented_keys(fleet) -> None:
    tmp_path, runs, _builder, ids = fleet

    body = _listing(_client(runs))

    assert set(body) == LIST_ENVELOPE
    assert body["total_found"] == body["listed"] == len(ids)
    assert body["truncated"] is False
    assert body["max_listed"] == 500
    assert len(body["runs"]) == len(ids)
    for item in body["runs"]:
        assert set(item) == LIST_ITEM


def test_detail_has_exactly_the_nine_blocks_and_documented_nested_keys(fleet) -> None:
    tmp_path, runs, _builder, ids = fleet
    client = _client(runs)

    detail = _detail(client, ids["B_failed"])

    assert set(detail) == DETAIL_BLOCKS
    assert set(detail["run"]) == RUN_KEYS
    assert set(detail["run"]["failure"]) == FAILURE_KEYS
    assert set(detail["docker_state"]) == DOCKER_STATE_KEYS
    assert set(detail["docker_state"]["manifest"]) == DOCKER_MANIFEST_KEYS
    assert set(detail["docker_state"]["raw"]) == DOCKER_RAW_KEYS
    assert set(detail["docker_state"]["effective"]) == DOCKER_EFFECTIVE_KEYS
    assert detail["docker_state"]["fields"]
    for entry in detail["docker_state"]["fields"] + detail["docker_state"]["discrepancies"]:
        assert set(entry) == DOCKER_FIELD_KEYS
    assert set(detail["timing"]) == TIMING_KEYS
    assert set(detail["timing"]["derived"]) == DERIVED_KEYS
    assert set(detail["termination_markers"]) == TERMINATION_MARKERS_KEYS
    assert set(detail["termination_markers"]["sigkill"]) == SIGKILL_KEYS
    for entry in detail["termination_markers"]["text_times"]:
        assert set(entry) == TEXT_TIME_KEYS
    assert set(detail["evidence_summary"]) == EVIDENCE_SUMMARY_KEYS
    assert set(detail["chart"]) == CHART_KEYS
    assert set(detail["runner_text"]) == RUNNER_TEXT_KEYS
    assert set(detail["artifact_status"]) == ARTIFACT_STATUS_KEYS
    for entry in detail["artifact_status"].values():
        assert set(entry) == ARTIFACT_ENTRY_KEYS
    assert isinstance(detail["warnings"], list)


def test_rich_detail_nested_objects_have_exactly_the_documented_keys(fleet) -> None:
    tmp_path, runs, _builder, ids = fleet
    client = _client(runs)

    detail = _detail(client, ids["B"])

    run = detail["run"]
    assert len(run["checks"]) == 2
    for check in run["checks"]:
        assert set(check) == CHECK_KEYS
    assert set(run["collector_health"]) == COLLECTOR_HEALTH_KEYS
    assert set(run["termination_record"]) == TERMINATION_RECORD_KEYS
    ev = detail["evidence_summary"]
    for name in ("first_rss", "highest_rss", "last_rss"):
        assert set(ev[name]) == RSS_ENTRY_KEYS
    assert set(ev["last_persisted_sample"]) == LAST_PERSISTED_KEYS
    assert set(ev["latest_capture_sample"]) == LATEST_CAPTURE_KEYS
    assert ev["silence_intervals"]
    for item in ev["silence_intervals"]:
        assert set(item) == SILENCE_KEYS
    chart = detail["chart"]
    assert set(chart["x_axis"]) == X_AXIS_KEYS
    assert chart["points"]
    for point in chart["points"]:
        assert set(point) == POINT_KEYS
    assert chart["gaps"]
    for gap in chart["gaps"]:
        assert set(gap) == GAP_KEYS
    text = detail["runner_text"]
    assert text["limitations"]
    for item in text["limitations"]:
        assert set(item) == TEXT_ITEM_KEYS
    st = detail["timing"]["sample_timing"]
    assert set(st) == SAMPLE_TIMING_KEYS
    assert set(st["post_kill_beyond_tolerance"]) == SAMPLE_TIMING_GROUP_KEYS
    assert set(st["after_container_stop"]) == SAMPLE_TIMING_GROUP_KEYS
    for item in detail["warnings"]:
        assert set(item) == WARNING_KEYS


def test_experiment_c_nested_objects_have_exactly_the_documented_keys(fleet) -> None:
    tmp_path, runs, _builder, ids = fleet

    detail = _detail(_client(runs), ids["C"])

    assert set(detail["run"]["diagnostics"]) == DIAGNOSTICS_KEYS
    sl = detail["runner_text"]["sampling_limitations"]
    assert set(sl) == SAMPLING_LIMITATIONS_KEYS
    assert sl["statements"]
    for item in sl["statements"]:
        assert set(item) == TEXT_ITEM_KEYS


def test_every_response_for_every_fixture_run_has_the_same_top_level_shape(fleet) -> None:
    tmp_path, runs, _builder, ids = fleet
    client = _client(runs)

    for run_id in ids.values():
        detail = _detail(client, run_id)
        assert set(detail) == DETAIL_BLOCKS, run_id
        assert set(detail["run"]) == RUN_KEYS, run_id
        assert set(detail["timing"]) == TIMING_KEYS, run_id
        assert set(detail["evidence_summary"]) == EVIDENCE_SUMMARY_KEYS, run_id
        assert set(detail["chart"]) == CHART_KEYS, run_id
        _assert_response_invariants(detail, tmp_path)
    _assert_response_invariants(_listing(client), tmp_path)


# ================================================================ Requirement 9 criteria 1 to 13 present


def test_req9_1_list_entries_carry_the_required_fields_in_run_order(fleet) -> None:
    tmp_path, runs, _builder, ids = fleet

    body = _listing(_client(runs))

    required = {
        "run_id", "experiment", "verdict", "start_time", "start_time_display", "exit_code", "oom_killed",
        "sample_count", "collector_survival", "docker_state_source", "docker_state_discrepancy", "reason",
    }
    assert body["runs_dir_label"] == "runs"
    for item in body["runs"]:
        assert required <= set(item)
        assert "experiment_type" in item
    listed_ids = [item["run_id"] for item in body["runs"]]
    assert sorted(listed_ids) == sorted(ids.values())
    by_id = {item["run_id"]: item for item in body["runs"]}
    assert by_id[ids["B"]]["experiment_type"] == "B"
    assert by_id[ids["B"]]["sample_count"] == 5
    assert by_id[ids["B"]]["collector_survival"] is True
    assert by_id[ids["B"]]["start_time_display"] == "2026-11-20T21:13:00.000Z"


def test_req9_3_to_5_chart_fields_and_partial_view_for_a_small_run(fleet) -> None:
    tmp_path, runs, _builder, ids = fleet

    chart = _detail(_client(runs), ids["B"])["chart"]

    assert chart["chart_cap"] == CHART_CAP
    assert chart["total_samples"] == 5
    assert chart["plottable_samples"] == 4
    assert chart["unplottable_samples"] == 1
    assert chart["plotted_samples"] == len(chart["points"]) == 4
    assert chart["partial_view"] is False
    assert chart["partial_view"] == (chart["plottable_samples"] > chart["chart_cap"])
    seqs = [p["seq"] for p in chart["points"]]
    assert seqs == [0, 1, 4, 5]
    point_after_gap = next(p for p in chart["points"] if p["seq"] == 4)
    assert point_after_gap["gap_before"] is True
    assert chart["points"][0]["gap_before"] is False
    assert point_after_gap["skipped_before"] == 1  # seq 2 is a Sample with no RSS between seq 1 and seq 4


@pytest.mark.parametrize("count", [CHART_CAP, CHART_CAP + 1])
def test_req9_5_partial_view_is_true_exactly_when_plottable_exceeds_the_cap(tmp_path: Path, count: int) -> None:
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    run_id = builder.add_run("B", samples=sample_specs(count, rss_step=1000))

    detail = _detail(_client(runs), run_id)

    chart = detail["chart"]
    assert chart["plottable_samples"] == count
    assert chart["partial_view"] is (count > CHART_CAP)
    assert chart["plotted_samples"] == len(chart["points"])
    assert len(chart["points"]) <= CHART_CAP
    if count > CHART_CAP:
        assert len(chart["points"]) < count
    # Req 9.6: the full-run statistics do not depend on the chart selection.
    ev = detail["evidence_summary"]
    assert ev["sample_count"] == count
    assert ev["first_rss"]["seq"] == 0
    assert ev["last_rss"]["seq"] == count - 1
    assert ev["highest_rss"]["seq"] == count - 1
    assert ev["highest_rss"]["rss_bytes"] == 50_000_000 + (count - 1) * 1000
    seqs = [p["seq"] for p in chart["points"]]
    assert seqs == sorted(seqs)
    assert seqs[0] == 0 and seqs[-1] == count - 1


def test_req9_6_evidence_summary_is_identical_with_and_without_partial_view(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    small = builder.add_run("B", run_id="b-partial-00000001", samples=sample_specs(CHART_CAP))
    big_runs = tmp_path / "other"
    big_builder = RunsBuilder(big_runs)
    big = big_builder.add_run("B", run_id="b-partial-00000001", samples=sample_specs(CHART_CAP + 1))

    ev_small = _detail(_client(runs), small)["evidence_summary"]
    ev_big = _detail(_client(big_runs), big)["evidence_summary"]

    assert ev_small["sample_count"] == CHART_CAP
    assert ev_big["sample_count"] == CHART_CAP + 1
    assert ev_big["valid_record_count"] == CHART_CAP + 2  # run_metadata record plus every Sample
    assert ev_big["last_rss"]["seq"] == CHART_CAP
    assert ev_big["last_persisted_sample"]["seq"] == CHART_CAP


def test_req9_9_artifact_status_for_every_status_with_stable_reasons(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    ok = builder.add_run("B", run_id="b-status-0000000a", samples=sample_specs(3))
    no_evidence = builder.add_run("B", run_id="b-status-0000000b", samples=sample_specs(3))
    (runs / no_evidence / "evidence.jsonl").unlink()
    no_inspect = builder.add_run("B", run_id="b-status-0000000c", samples=sample_specs(3), inspect=None)
    bad_inspect = builder.add_run("B", run_id="b-status-0000000d", samples=sample_specs(3), inspect=b"{not json")
    big_evidence = builder.add_run(
        "B", run_id="b-status-0000000e", samples=sample_specs(3), evidence_bytes=b"x" * (ARTIFACT_LIMIT + 1)
    )
    bad_manifest = builder.add_run("B", run_id="b-status-0000000f", manifest_bytes=b"{not json")
    client = _client(runs)

    status = {name: _detail(client, rid)["artifact_status"] for name, rid in {
        "ok": ok, "no_evidence": no_evidence, "no_inspect": no_inspect, "bad_inspect": bad_inspect,
        "big_evidence": big_evidence, "bad_manifest": bad_manifest,
    }.items()}

    assert status["ok"]["manifest"] == {"status": "ok", "reason": None, "observed_size_text": None, "limit_bytes": None}
    assert status["ok"]["evidence"]["status"] == "ok" and status["ok"]["evidence"]["reason"] is None
    assert status["ok"]["inspect"]["status"] == "ok" and status["ok"]["inspect"]["reason"] is None
    assert status["no_evidence"]["evidence"]["status"] == "missing"
    assert isinstance(status["no_evidence"]["evidence"]["reason"], str) and status["no_evidence"]["evidence"]["reason"]
    assert status["no_inspect"]["inspect"]["status"] == "missing"
    assert isinstance(status["no_inspect"]["inspect"]["reason"], str) and status["no_inspect"]["inspect"]["reason"]
    assert status["bad_inspect"]["inspect"]["status"] == "invalid"
    assert isinstance(status["bad_inspect"]["inspect"]["reason"], str) and status["bad_inspect"]["inspect"]["reason"]
    too_large = status["big_evidence"]["evidence"]
    assert too_large["status"] == "too_large"
    assert too_large["reason"] == "too_large"
    assert too_large["limit_bytes"] == ARTIFACT_LIMIT
    assert isinstance(too_large["observed_size_text"], str) and "16,777,216" in too_large["observed_size_text"]
    assert status["bad_manifest"]["manifest"]["status"] == "invalid"
    assert status["bad_manifest"]["manifest"]["reason"]
    for name in ("evidence", "inspect"):
        assert status["bad_manifest"][name]["status"] == "not_read"
        assert status["bad_manifest"][name]["reason"] == "manifest_unusable"
    for block in status.values():
        for entry in block.values():
            assert entry["status"] in {"ok", "missing", "unreadable", "invalid", "too_large", "not_read"}
            if entry["status"] != "ok":
                assert entry["reason"]
            if entry["status"] != "too_large":
                assert entry["observed_size_text"] is None and entry["limit_bytes"] is None


def test_req9_11_last_persisted_latest_capture_and_receive_summary_are_separate_objects(fleet) -> None:
    tmp_path, runs, _builder, ids = fleet

    ev = _detail(_client(runs), ids["B"])["evidence_summary"]

    lp, lc = ev["last_persisted_sample"], ev["latest_capture_sample"]
    assert lp is not lc
    assert lp["seq"] == 5 and lc["seq"] == 5
    assert lp["capture_time"] == "2026-11-20T21:13:02.500Z"
    assert lp["capture_time_display"] == "2026-11-20T21:13:02.500Z"
    assert lp["receive_time"] == "2026-11-20T21:13:02.520Z"
    assert lp["receive_time_display"] == "2026-11-20T21:13:02.520Z"
    assert "receive_time" not in lc
    assert ev["last_receive_time_summary"] == "2026-11-20T21:13:02.520Z"
    assert ev["last_receive_time_summary_display"] == "2026-11-20T21:13:02.520Z"


def test_req9_13_timing_block_has_every_timestamp_with_its_clock(fleet) -> None:
    tmp_path, runs, _builder, ids = fleet
    client = _client(runs)

    timing_b = _detail(client, ids["B"])["timing"]

    assert timing_b["start_time"] == "2026-11-20T21:13:00.000Z"
    assert timing_b["start_time_clock"] == "host"
    assert timing_b["end_time"] == "2026-11-20T21:13:30.000Z"
    assert timing_b["end_time_clock"] == "host"
    assert timing_b["inspect_time"] == "2026-11-20T21:13:30.000Z"
    assert timing_b["inspect_time_clock"] == "host"
    assert timing_b["sigkill_sent_time"] == "2026-11-20T21:13:20.000Z"
    assert timing_b["sigkill_sent_time_clock"] == "host"
    assert timing_b["crash_request_time"] is None
    assert timing_b["crash_request_time_display"] is None
    assert timing_b["container_finished_at"] == "2026-11-20T21:13:21.760Z"
    assert timing_b["container_finished_at_clock"] == "docker"
    assert timing_b["state_finished_at"] == "2026-11-20T21:13:21.760545826Z"
    assert timing_b["state_finished_at_clock"] == "docker"
    assert timing_b["clock_offset_ms"] == 9
    assert timing_b["clock_offset_clock"] == "collector_minus_host"
    assert timing_b["clock_uncertainty_ms"] == 12.5
    assert timing_b["clock_uncertainty_clock"] == "collector_minus_host"
    assert timing_b["tolerance_ms"] == 250
    assert timing_b["tolerance_status"]
    assert timing_b["tolerance_clock"] == "host"
    for stem in (
        "start_time", "end_time", "inspect_time", "sigkill_sent_time", "container_finished_at",
        "state_finished_at",
    ):
        assert f"{stem}_display" in timing_b


@pytest.mark.parametrize("which", ["A", "C"])
def test_req9_13_experiments_a_and_c_have_no_host_equivalent_and_no_derived_interval(fleet, which: str) -> None:
    tmp_path, runs, _builder, ids = fleet

    detail = _detail(_client(runs), ids[which])

    timing = detail["timing"]
    assert timing["derived"]["intervals"] == []
    assert timing["clock_offset_ms"] is None
    assert timing["correction_available"] is False
    assert timing["sample_timing"] is None
    assert timing["sigkill_sent_time"] is None
    assert all(p["corrected_capture_time"] is None for p in detail["chart"]["points"])
    assert detail["termination_markers"]["sigkill"]["drawn"] is False
    assert detail["termination_markers"]["sigkill"]["x_ms"] is None
    assert detail["run"]["experiment_type"] == which


def test_req9_13_experiment_b_with_offset_returns_corrected_capture_time(fleet) -> None:
    tmp_path, runs, _builder, ids = fleet

    detail = _detail(_client(runs), ids["B"])

    assert detail["timing"]["correction_available"] is True
    assert all(isinstance(p["corrected_capture_time"], str) for p in detail["chart"]["points"])


# ================================================================ unread keys never change a response


def test_extra_and_never_read_manifest_keys_do_not_change_any_response(tmp_path: Path) -> None:
    def build(root: Path, extra: dict[str, Any]) -> tuple[Path, str]:
        runs = root / "runs"
        builder = RunsBuilder(runs)
        merged = {**RICH_EXTRA, **extra}
        run_id = builder.add_run(
            "B", run_id="b-neverread-0000001", samples=_rich_samples(),
            gaps=[GapSpec(first_missing_seq=3, last_missing_seq=3)], manifest_extra=merged,
        )
        return runs, run_id

    plain_runs, run_id = build(tmp_path / "plain", {})
    extra_runs, same_id = build(tmp_path / "extra", NEVER_READ_EXTRA)
    assert same_id == run_id
    manifest_text = (extra_runs / run_id / "manifest.json").read_text(encoding="utf-8")
    for sentinel in ("NEVERREAD-phases", "NEVERREAD-leak", "NEVERREAD-readiness", "NEVERREAD-memory"):
        assert sentinel in manifest_text  # the keys really are in the fixture manifest

    plain_detail, plain_detail_text = _fetch(_client(plain_runs), f"/api/runs/{run_id}")
    extra_detail, extra_detail_text = _fetch(_client(extra_runs), f"/api/runs/{run_id}")
    plain_list, plain_list_text = _fetch(_client(plain_runs), "/api/runs")
    extra_list, extra_list_text = _fetch(_client(extra_runs), "/api/runs")

    assert extra_detail == plain_detail
    assert extra_list == plain_list
    for text in (extra_detail_text, extra_list_text):
        assert "NEVERREAD" not in text
        assert "4242424" not in text and "987654321" not in text and "7777777" not in text
        assert "zz_unknown_fixture_key" not in text
    ev = extra_detail["evidence_summary"]
    assert ev["sample_count"] == 5  # not the 4242424 recorded in the unread manifest key


# ================================================================ null policy


def _all_values_null(obj: dict[str, Any]) -> bool:
    return all(v is None for v in obj.values())


def test_missing_evidence_returns_null_not_zero_false_or_empty(fleet) -> None:
    tmp_path, runs, _builder, ids = fleet
    (runs / ids["A"] / "evidence.jsonl").unlink()
    client = _client(runs)

    detail = _detail(client, ids["A"])
    item = next(i for i in _listing(client)["runs"] if i["run_id"] == ids["A"])

    assert detail["artifact_status"]["evidence"]["status"] == "missing"
    assert _all_values_null(detail["evidence_summary"])
    chart = detail["chart"]
    for key in ("total_samples", "plottable_samples", "unplottable_samples", "x_axis", "y_data_max_bytes"):
        assert chart[key] is None, key
    assert chart["points"] == [] and chart["gaps"] == []
    assert item["sample_count"] is None
    assert item["collector_survival"] is None
    assert detail["termination_markers"]["text_times"]
    for entry in detail["termination_markers"]["text_times"]:
        if entry["kind"] in {"last_persisted_capture", "latest_capture", "last_persisted_receive"}:
            assert entry["value"] is None and entry["display"] is None
    _assert_response_invariants(detail, tmp_path)


def test_missing_inspect_returns_null_for_raw_values_and_keeps_manifest_values(fleet) -> None:
    tmp_path, runs, _builder, ids = fleet
    (runs / ids["B"] / "docker_inspect.json").unlink()
    client = _client(runs)

    detail = _detail(client, ids["B"])

    assert detail["artifact_status"]["inspect"]["status"] == "missing"
    docker = detail["docker_state"]
    assert docker["raw"] is None
    assert docker["raw_unavailable_reason"]
    assert docker["manifest"]["exit_code"] == 137  # recorded in the manifest, so still present
    assert docker["manifest"]["oom_killed"] is False
    timing = detail["timing"]
    assert timing["state_finished_at"] is None
    assert timing["state_finished_at_display"] is None
    for entry in detail["termination_markers"]["text_times"]:
        if entry["kind"] == "docker_finished_at":
            assert entry["value"] is None and entry["display"] is None
    _assert_response_invariants(detail, tmp_path)


def test_missing_optional_fields_return_null_not_substitutes(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    run_id = builder.add_run(
        "B", samples=sample_specs(3), clock_offset_ms=None, uncertainty_ms=None, sample_timing=None,
    )
    _edit_manifest(runs, run_id, lambda m: m.pop("times", None))
    client = _client(runs)

    detail = _detail(client, run_id)

    run = detail["run"]
    assert run["failure"] is None
    assert run["checks"] is None and run["checks_total"] is None and run["checks_truncated"] is None
    assert run["collector_health"] is None
    assert run["termination_record"] is None
    assert run["collector_survival"] is None
    assert run["diagnostics"] is None
    assert run["error"] is None
    text = detail["runner_text"]
    for stem in ("cause_label",):
        assert text[stem] is None
        assert text[f"{stem}_truncated"] is None
        assert text[f"{stem}_original_length"] is None
    timing = detail["timing"]
    assert timing["clock_offset_ms"] is None
    assert timing["clock_uncertainty_ms"] is None
    assert timing["tolerance_ms"] is None
    assert timing["sample_timing"] is None
    assert timing["sigkill_sent_time"] is None and timing["sigkill_sent_time_display"] is None
    assert timing["correction_available"] is False
    assert timing["correction_unavailable_reason"]
    assert detail["termination_markers"]["sigkill"]["drawn"] is False
    assert detail["termination_markers"]["sigkill"]["x_ms"] is None
    assert all(p["corrected_capture_time"] is None for p in detail["chart"]["points"])
    _assert_response_invariants(detail, tmp_path)


def test_invalid_manifest_returns_null_values_and_not_read_artifacts(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    run_id = builder.add_run("B", run_id="b-badmanifest-00000001", manifest_bytes=b'{"truncated": ')
    client = _client(runs)

    detail = _detail(client, run_id)
    item = _listing(client)["runs"][0]

    assert set(detail) == DETAIL_BLOCKS
    run = detail["run"]
    assert run["run_id"] == run_id
    assert run["verdict"] == "UNKNOWN"
    assert run["error"] and run["error"]["reason"]
    for key in (
        "experiment", "experiment_type", "outcome", "failure", "durability_level", "start_time",
        "start_time_display", "end_time", "end_time_display", "record_count_manifest", "collector_survival",
        "checks", "collector_health", "termination_record", "diagnostics",
    ):
        assert run[key] is None, key
    assert _all_values_null(detail["evidence_summary"])
    assert detail["chart"]["total_samples"] is None
    assert detail["chart"]["points"] == []
    assert detail["runner_text"]["termination_description"] is None
    assert detail["runner_text"]["cause_label"] is None
    assert detail["artifact_status"]["manifest"]["status"] == "invalid"
    assert detail["artifact_status"]["evidence"] == {
        "status": "not_read", "reason": "manifest_unusable", "observed_size_text": None, "limit_bytes": None,
    }
    assert detail["artifact_status"]["inspect"]["status"] == "not_read"
    assert item["run_id"] == run_id
    assert item["verdict"] == "UNKNOWN"
    assert item["reason"]
    for key in (
        "experiment", "experiment_type", "start_time", "start_time_display", "exit_code", "oom_killed",
        "sample_count", "collector_survival", "docker_state_source", "docker_state_discrepancy",
    ):
        assert item[key] is None, key
    _assert_response_invariants(detail, tmp_path)


def test_malformed_lines_are_counted_and_no_value_is_invented(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    only_bad = builder.add_run(
        "B", run_id="b-allmalformed-0000001", evidence_bytes=b"not json\n{broken\n\xff\xfe\n",
    )
    partly_bad = builder.add_run("B", run_id="b-partmalformed-0000001", samples=sample_specs(3))
    path = runs / partly_bad / "evidence.jsonl"
    path.write_bytes(path.read_bytes() + b"garbage line\n")
    client = _client(runs)

    all_bad = _detail(client, only_bad)["evidence_summary"]
    some_bad = _detail(client, partly_bad)["evidence_summary"]

    assert all_bad["malformed_line_count"] == 3
    assert all_bad["malformed_lines_total"] == 3
    assert [e["line_no"] for e in all_bad["malformed_lines"]] == [1, 2, 3]
    for entry in all_bad["malformed_lines"]:
        assert set(entry) == MALFORMED_KEYS
        assert isinstance(entry["kind"], str) and entry["kind"]
    assert all_bad["has_valid_record"] is False
    assert all_bad["sample_count"] == 0
    for key in ("first_rss", "highest_rss", "last_rss", "last_persisted_sample", "latest_capture_sample"):
        assert all_bad[key] is None, key
    assert some_bad["malformed_line_count"] == 1
    assert some_bad["has_valid_record"] is True
    assert some_bad["sample_count"] == 3
    chart = _detail(client, only_bad)["chart"]
    assert chart["points"] == [] and chart["x_axis"] is None and chart["y_data_max_bytes"] is None


def test_sample_without_rss_has_null_rss_not_zero(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    run_id = builder.add_run(
        "B", samples=[SampleSpec(seq=0), SampleSpec(seq=1, rss_bytes=None), SampleSpec(seq=2)],
    )

    ev = _detail(_client(runs), run_id)["evidence_summary"]

    assert ev["sample_count"] == 3
    assert ev["latest_capture_sample"]["seq"] == 2
    assert ev["first_rss"]["seq"] == 0
    assert all(ev[k]["rss_bytes"] != 0 for k in ("first_rss", "highest_rss", "last_rss"))


def test_nan_and_infinity_never_reach_a_response(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    run_id = builder.add_run(
        "B", run_id="b-nan-00000000001", samples=sample_specs(3),
        manifest_extra={"diagnostics": {"peak_rss_ratio_of_limit": 0.5}},
    )
    manifest_path = runs / run_id / "manifest.json"
    text = manifest_path.read_text(encoding="utf-8")
    assert '"clock_offset_uncertainty_ms": 12.5' in text
    manifest_path.write_bytes(
        text.replace('"clock_offset_uncertainty_ms": 12.5', '"clock_offset_uncertainty_ms": NaN').encode("utf-8")
    )
    evidence_path = runs / run_id / "evidence.jsonl"
    evidence_path.write_bytes(evidence_path.read_bytes().replace(b'"loop_delay_ms":0.84', b'"loop_delay_ms":Infinity', 1))
    client = _client(runs)

    for url in ("/api/runs", f"/api/runs/{run_id}"):
        parsed, body = _fetch(client, url)  # _fetch fails on any NaN or Infinity token
        for token in ("NaN", "Infinity"):
            assert token not in body
        _assert_response_invariants(parsed, tmp_path)


# ================================================================ HTML markup is data


def test_html_markup_in_artifact_strings_is_returned_as_json_data_unchanged(tmp_path: Path) -> None:
    markup = '<script>alert("x")</script><img src=x onerror=alert(1)>&amp; \u00e9\u2028'
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    run_id = builder.add_run("B", outcome="failed", samples=sample_specs(3))

    def edit(m: dict[str, Any]) -> None:
        m["termination_description"] = markup
        m["failure"] = {"code": markup, "message": markup}
        m["checks"] = [{"name": markup, "passed": False, "detail": markup}]
        m["termination"] = {"posted": True, "cause_label": markup}
        m["limitations"] = [markup]

    _edit_manifest(runs, run_id, edit)
    client = _client(runs)

    parsed, body = _fetch(client, f"/api/runs/{run_id}")

    assert parsed["runner_text"]["termination_description"] == markup
    assert parsed["runner_text"]["cause_label"] == markup
    assert parsed["runner_text"]["limitations"][0]["text"] == markup
    assert parsed["run"]["failure"]["code"] == markup
    assert parsed["run"]["failure"]["message"] == markup
    assert parsed["run"]["checks"][0]["name"] == markup
    assert parsed["run"]["checks"][0]["detail"] == markup
    assert parsed["run"]["failure"]["message_truncated"] is False
    response = client.get(f"/api/runs/{run_id}", headers={"Host": f"127.0.0.1:{PORT}"})
    assert response.headers["content-type"].startswith("application/json")
    assert "text/html" not in response.headers["content-type"]
    assert "&amp;amp;" not in body and "&lt;" not in body  # nothing was HTML-escaped by the server


# ================================================================ no absolute path anywhere


def test_no_absolute_path_in_any_response_though_manifests_record_absolute_paths(fleet) -> None:
    tmp_path, runs, _builder, ids = fleet
    recorded = {run_id: _make_paths_absolute(runs, run_id) for run_id in ids.values()}
    for paths in recorded.values():
        assert re.match(r"^[A-Za-z]:\\", paths["raw_inspect_path"]) or paths["raw_inspect_path"].startswith("/")
    client = _client(runs)

    bodies = [_fetch(client, "/api/runs")]
    bodies += [_fetch(client, f"/api/runs/{run_id}") for run_id in ids.values()]

    for parsed, text in bodies:
        assert _absolute_path_hits(parsed, _needles(tmp_path)) == []
        for needle in _needles(tmp_path):
            assert needle not in text
        for paths in recorded.values():
            for value in paths.values():
                assert value not in text
        assert "raw_inspect_path" not in text and "host_copy_path" not in text
        assert "docker_inspect_path" not in text


def test_absolute_runs_dir_is_labelled_by_its_final_component_only(tmp_path: Path) -> None:
    runs = tmp_path / "lab-evidence" / "my-runs"
    builder = RunsBuilder(runs)
    run_id = builder.add_run("B", samples=sample_specs(3))
    assert runs.is_absolute()

    body, text = _fetch(_client(str(runs)), "/api/runs")

    assert body["runs_dir_label"] == "my-runs"
    assert "lab-evidence" not in text
    _assert_response_invariants(body, tmp_path)
    _assert_response_invariants(_detail(_client(str(runs)), run_id), tmp_path)


def test_relative_runs_dir_is_labelled_as_supplied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    builder = RunsBuilder(tmp_path / "rel-runs")
    run_id = builder.add_run("B", samples=sample_specs(3))
    nested = RunsBuilder(tmp_path / "outer" / "inner-runs")
    nested.add_run("B", samples=sample_specs(3))

    simple_body, simple_text = _fetch(_client("rel-runs"), "/api/runs")
    nested_label = os.path.join("outer", "inner-runs")
    nested_body, nested_text = _fetch(_client(nested_label), "/api/runs")

    assert simple_body["runs_dir_label"] == "rel-runs"
    assert nested_body["runs_dir_label"] == nested_label
    for parsed, text in ((simple_body, simple_text), (nested_body, nested_text)):
        for needle in _needles(tmp_path):
            assert needle not in text
    _assert_response_invariants(_detail(_client("rel-runs"), run_id), tmp_path)


# ================================================================ timestamps


def test_nine_digit_original_is_unchanged_and_display_comes_from_format_utc_ms(fleet) -> None:
    tmp_path, runs, _builder, ids = fleet
    original = "2026-11-20T21:13:21.760545826Z"

    detail = _detail(_client(runs), ids["B"])

    expected_display = format_utc_ms(parse_utc_ms("2026-11-20T21:13:21.760Z"))
    assert expected_display == "2026-11-20T21:13:21.760Z"
    assert detail["docker_state"]["raw"]["finished_at"] == original
    assert detail["docker_state"]["raw"]["finished_at_display"] == expected_display
    assert detail["timing"]["state_finished_at"] == original
    assert detail["timing"]["state_finished_at_display"] == expected_display
    docker_text = next(e for e in detail["termination_markers"]["text_times"] if e["kind"] == "docker_finished_at")
    assert docker_text["value"] == original
    assert docker_text["display"] == expected_display
    assert docker_text["clock"] == "docker"


def test_nine_digit_fraction_is_truncated_never_rounded_in_the_display(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    run_id = builder.add_run(
        "B", samples=sample_specs(3), inspect=InspectSpec(finished_at="2026-11-20T21:13:21.999999999Z"),
    )

    timing = _detail(_client(runs), run_id)["timing"]

    assert timing["state_finished_at"] == "2026-11-20T21:13:21.999999999Z"
    assert timing["state_finished_at_display"] == "2026-11-20T21:13:21.999Z"


def test_unparseable_timestamp_has_null_display_and_the_original_intact(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    run_id = builder.add_run("B", samples=sample_specs(3))

    def edit(m: dict[str, Any]) -> None:
        m["container_finished_at"] = "not-a-timestamp"
        m["times"]["sigkill_sent_time"] = "yesterday at noon"

    _edit_manifest(runs, run_id, edit)

    detail = _detail(_client(runs), run_id)

    timing = detail["timing"]
    assert timing["container_finished_at"] == "not-a-timestamp"
    assert timing["container_finished_at_display"] is None
    assert timing["sigkill_sent_time"] == "yesterday at noon"
    assert timing["sigkill_sent_time_display"] is None
    sigkill = detail["termination_markers"]["sigkill"]
    assert sigkill["time"] == "yesterday at noon"
    assert sigkill["time_display"] is None
    assert sigkill["drawn"] is False
    _assert_response_invariants(detail, tmp_path)


def test_unparseable_raw_finished_at_is_not_recorded_so_null_with_a_null_display(tmp_path: Path) -> None:
    """Requirement 14.1: a Docker zero date or an unparseable raw State.FinishedAt is Not_Recorded."""
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    run_id = builder.add_run(
        "B", samples=sample_specs(3), inspect=InspectSpec(finished_at="2026-13-45 not a time"),
    )

    detail = _detail(_client(runs), run_id)

    assert detail["timing"]["state_finished_at"] is None
    assert detail["timing"]["state_finished_at_display"] is None
    assert detail["docker_state"]["raw"]["finished_at"] is None
    assert detail["docker_state"]["raw"]["finished_at_display"] is None
    # The builder copies the same text into the manifest's container_finished_at; that field keeps its original.
    assert detail["timing"]["container_finished_at"] == "2026-13-45 not a time"
    assert detail["timing"]["container_finished_at_display"] is None


def test_timestamps_in_the_list_are_original_plus_display(fleet) -> None:
    tmp_path, runs, _builder, ids = fleet

    item = next(i for i in _listing(_client(runs))["runs"] if i["run_id"] == ids["B"])

    assert item["start_time"] == "2026-11-20T21:13:00.000Z"
    assert item["start_time_display"] == format_utc_ms(parse_utc_ms(item["start_time"]))


# ================================================================ Verdict from the Stored_Outcome


@pytest.mark.parametrize(
    ("outcome", "verdict"), [("passed", "PASS"), ("failed", "FAIL"), ("aborted", "UNKNOWN")]
)
def test_verdict_comes_from_the_stored_outcome_only(tmp_path: Path, outcome: str, verdict: str) -> None:
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    # Docker state that disagrees with the expectation and failing recorded checks must not change the Verdict.
    run_id = builder.add_run(
        "B", outcome=outcome, samples=sample_specs(3),
        container_state=StateSpec(exit_code=0, oom_killed=True),
        manifest_extra={"checks": [{"name": "exit_code", "passed": outcome != "passed", "detail": "x"}]},
    )
    client = _client(runs)

    detail = _detail(client, run_id)
    item = _listing(client)["runs"][0]

    assert detail["run"]["outcome"] == outcome
    assert detail["run"]["verdict"] == verdict
    assert item["verdict"] == verdict


def test_failed_outcome_stays_fail_with_matching_docker_state_and_passing_checks(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    run_id = builder.add_run(
        "B", outcome="failed", samples=sample_specs(3),
        manifest_extra={"checks": [{"name": "exit_code", "passed": True, "detail": "ok"}]},
    )

    detail = _detail(_client(runs), run_id)

    assert detail["docker_state"]["discrepancies"] == []
    assert detail["run"]["verdict"] == "FAIL"


# ================================================================ Docker state recorded as-is


@pytest.mark.parametrize("experiment", ["A", "B", "C"])
@pytest.mark.parametrize(("exit_code", "oom"), [(137, False), (137, True), (1, False), (0, False)])
def test_exit_code_and_oom_killed_are_recorded_as_is_and_never_inferred(
    tmp_path: Path, experiment: str, exit_code: int, oom: bool
) -> None:
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    run_id = builder.add_run(
        experiment, samples=sample_specs(3), container_state=StateSpec(exit_code=exit_code, oom_killed=oom),
    )
    client = _client(runs)

    item = _listing(client)["runs"][0]
    docker = _detail(client, run_id)["docker_state"]

    assert item["exit_code"] == exit_code
    assert item["oom_killed"] is oom
    for block in ("manifest", "raw", "effective"):
        assert docker[block]["exit_code"] == exit_code, block
        assert docker[block]["oom_killed"] is oom, block
    assert docker["discrepancies"] == []


def test_fleet_b_oom_false_and_c_oom_true_with_exit_137(fleet) -> None:
    tmp_path, runs, _builder, ids = fleet
    client = _client(runs)
    by_id = {i["run_id"]: i for i in _listing(client)["runs"]}

    assert (by_id[ids["B"]]["exit_code"], by_id[ids["B"]]["oom_killed"]) == (137, False)
    assert (by_id[ids["B_oom_false"]]["exit_code"], by_id[ids["B_oom_false"]]["oom_killed"]) == (137, False)
    assert (by_id[ids["C"]]["exit_code"], by_id[ids["C"]]["oom_killed"]) == (137, True)
    assert (by_id[ids["C_oom_true"]]["exit_code"], by_id[ids["C_oom_true"]]["oom_killed"]) == (137, True)
    assert (by_id[ids["A"]]["exit_code"], by_id[ids["A"]]["oom_killed"]) == (1, False)


def test_missing_inspect_does_not_infer_oom_from_exit_code(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    run_id = builder.add_run(
        "C", samples=sample_specs(3), container_state=StateSpec(exit_code=137, oom_killed=False), inspect=None,
    )

    docker = _detail(_client(runs), run_id)["docker_state"]

    assert docker["manifest"]["exit_code"] == 137
    assert docker["manifest"]["oom_killed"] is False
    assert docker["raw"] is None


# ================================================================ end-to-end read-only


def _tree(root: Path) -> dict[str, tuple[str, int]]:
    out: dict[str, tuple[str, int]] = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            out[(Path(current) / name).relative_to(root).as_posix()] = ("dir", 0)
        for name in files:
            path = Path(current) / name
            out[path.relative_to(root).as_posix()] = ("file", path.stat().st_size)
    return out


def test_list_and_detail_requests_leave_the_runs_fixture_identical(fleet) -> None:
    tmp_path, runs, builder, ids = fleet
    client = _client(runs)
    before_files = builder.snapshot()
    before_tree = _tree(tmp_path)

    _fetch(client, "/api/runs")
    for run_id in ids.values():
        _fetch(client, f"/api/runs/{run_id}")
    _fetch(client, "/api/runs")  # again, now served partly from the listing cache

    assert builder.snapshot() == before_files
    assert _tree(tmp_path) == before_tree
    assert set(before_files) == {
        f"{run_id}/{name}"
        for run_id in ids.values()
        for name in ("manifest.json", "evidence.jsonl", "docker_inspect.json")
    }


def test_requests_on_damaged_fixture_runs_create_no_file(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    ids = [
        builder.add_run("B", run_id="b-damaged-00000001", manifest_bytes=b"{not json"),
        builder.add_run("B", run_id="b-damaged-00000002", samples=sample_specs(3), inspect=None),
        builder.add_run("B", run_id="b-damaged-00000003", evidence_bytes=b"junk\n"),
    ]
    (runs / ids[1] / "evidence.jsonl").unlink()
    client = _client(runs)
    before_files = builder.snapshot()
    before_tree = _tree(tmp_path)

    _fetch(client, "/api/runs")
    for run_id in ids:
        _fetch(client, f"/api/runs/{run_id}")

    assert builder.snapshot() == before_files
    assert _tree(tmp_path) == before_tree
