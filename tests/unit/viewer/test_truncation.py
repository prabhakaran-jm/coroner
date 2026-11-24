"""Truncation contract of the Viewer API (design: Capped free-text fields and List caps; Requirement 9.15).

Synthetic runs in tmp_path only (RunsBuilder), served by the real app through an in-process TestClient.
Nothing reads the real runs/ directory, starts a server or opens a socket.

Caps used here are the ones documented in coroner/viewer/model.py and the design:
  200 code points   failure.code, checks[].name, runner_text.cause_label
  2,000 code points failure.message, checks[].detail, runner_text.termination_description,
                    runner_text.limitations[].text, sampling_limitations.statements[].text,
                    summarize warnings text
  list caps         checks, limitations, statements, warnings (summarize) 50; malformed_lines 20;
                    unreconciled_drops, silence_intervals 50; chart.gaps 100
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from coroner.viewer.app import create_app
from coroner.viewer.model import capped
from tests.unit.viewer.builder import GapSpec, RunsBuilder, SampleSpec, envelope, sample_specs

PORT = 18765
CAP_SHORT = 200
CAP_LONG = 2000
# Non-BMP characters make a code point count differ from a UTF-16 unit count.
UNIT = "a\u00e9\U0001F600"  # 3 code points, 4 UTF-16 units


def _text(length: int) -> str:
    return (UNIT * (length // len(UNIT) + 1))[:length]


def _client(runs: Path) -> TestClient:
    return TestClient(create_app(runs, PORT), base_url=f"http://127.0.0.1:{PORT}")


def _get(client: TestClient, url: str) -> Any:
    response = client.get(url, headers={"Host": f"127.0.0.1:{PORT}"})
    assert response.status_code == 200, response.text
    return response.json()


def _edit_manifest(runs: Path, run_id: str, edit: Callable[[dict[str, Any]], None]) -> None:
    path = runs / run_id / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_bytes(json.dumps(data, ensure_ascii=True, indent=2).encode("ascii") + b"\n")


# ------------------------------------------------------------------ capped() unit behaviour


@pytest.mark.parametrize("cap", [CAP_SHORT, CAP_LONG])
def test_capped_longer_than_cap_returns_first_cap_code_points(cap: int) -> None:
    value = _text(cap + 7)

    result = capped(value, cap)

    assert result.value == value[:cap]
    assert len(result.value) == cap
    assert result.truncated is True
    assert result.original_length == cap + 7


@pytest.mark.parametrize("length", [0, 1, 199, 200])
def test_capped_within_cap_is_unchanged(length: int) -> None:
    value = _text(length)

    result = capped(value, CAP_SHORT)

    assert result.value == value
    assert result.truncated is False
    assert result.original_length == length


def test_capped_non_string_is_all_none() -> None:
    for value in (None, 5, True, 1.5, ["x"], {"a": 1}):
        assert capped(value, CAP_SHORT) == (None, None, None)


# ------------------------------------------------------------------ scalar caps through the API


def _scalar_cases() -> list[tuple[str, str, int, str, Callable[[dict[str, Any], str], None]]]:
    """(name, experiment, cap, outcome, setter) for each capped scalar; getters are in _GETTERS."""

    def failure_code(m: dict[str, Any], v: str) -> None:
        m["failure"]["code"] = v

    def failure_message(m: dict[str, Any], v: str) -> None:
        m["failure"]["message"] = v

    def check_name(m: dict[str, Any], v: str) -> None:
        m["checks"] = [{"name": v, "passed": True, "detail": "d"}]

    def check_detail(m: dict[str, Any], v: str) -> None:
        m["checks"] = [{"name": "n", "passed": True, "detail": v}]

    def termination_description(m: dict[str, Any], v: str) -> None:
        m["termination_description"] = v

    def cause_label(m: dict[str, Any], v: str) -> None:
        m["termination"] = {"posted": True, "cause_label": v}

    def limitation(m: dict[str, Any], v: str) -> None:
        m["limitations"] = [v]

    def statement(m: dict[str, Any], v: str) -> None:
        m["sampling_limitations"] = {"statements": [v]}

    return [
        ("failure.code", "B", CAP_SHORT, "failed", failure_code),
        ("failure.message", "B", CAP_LONG, "failed", failure_message),
        ("checks.name", "B", CAP_SHORT, "passed", check_name),
        ("checks.detail", "B", CAP_LONG, "passed", check_detail),
        ("termination_description", "B", CAP_LONG, "passed", termination_description),
        ("cause_label", "B", CAP_SHORT, "passed", cause_label),
        ("limitations.text", "B", CAP_LONG, "passed", limitation),
        ("statements.text", "C", CAP_LONG, "passed", statement),
    ]


def _extract(name: str, detail: dict[str, Any]) -> dict[str, Any]:
    """The object that holds the field and its `_truncated` / `_original_length` siblings, plus the field key."""
    if name == "failure.code":
        return {"obj": detail["run"]["failure"], "key": "code"}
    if name == "failure.message":
        return {"obj": detail["run"]["failure"], "key": "message"}
    if name == "checks.name":
        return {"obj": detail["run"]["checks"][0], "key": "name"}
    if name == "checks.detail":
        return {"obj": detail["run"]["checks"][0], "key": "detail"}
    if name == "termination_description":
        return {"obj": detail["runner_text"], "key": "termination_description"}
    if name == "cause_label":
        return {"obj": detail["runner_text"], "key": "cause_label"}
    if name == "limitations.text":
        return {"obj": detail["runner_text"]["limitations"][0], "key": "text"}
    if name == "statements.text":
        return {"obj": detail["runner_text"]["sampling_limitations"]["statements"][0], "key": "text"}
    raise AssertionError(name)


def _served(tmp_path: Path, experiment: str, outcome: str, setter: Callable[[dict[str, Any], str], None], value: str):
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    run_id = builder.add_run(experiment, outcome=outcome, samples=sample_specs(3))
    _edit_manifest(runs, run_id, lambda m: setter(m, value))
    return _get(_client(runs), f"/api/runs/{run_id}")


@pytest.mark.parametrize("case", _scalar_cases(), ids=lambda c: c[0])
def test_capped_field_over_cap_is_cut_with_flag_and_original_length(tmp_path: Path, case) -> None:
    name, experiment, cap, outcome, setter = case
    value = _text(cap + 25)

    detail = _served(tmp_path, experiment, outcome, setter, value)

    found = _extract(name, detail)
    obj, key = found["obj"], found["key"]
    assert obj[key] == value[:cap]
    assert len(obj[key]) == cap
    assert obj[f"{key}_truncated"] is True
    assert obj[f"{key}_original_length"] == cap + 25


@pytest.mark.parametrize("case", _scalar_cases(), ids=lambda c: c[0])
def test_capped_field_exactly_at_cap_is_unchanged_with_truncated_false(tmp_path: Path, case) -> None:
    name, experiment, cap, outcome, setter = case
    value = _text(cap)

    detail = _served(tmp_path, experiment, outcome, setter, value)

    found = _extract(name, detail)
    obj, key = found["obj"], found["key"]
    assert obj[key] == value
    assert obj[f"{key}_truncated"] is False
    assert obj[f"{key}_original_length"] == cap


@pytest.mark.parametrize("case", _scalar_cases(), ids=lambda c: c[0])
def test_capped_field_short_value_is_unchanged_with_its_length(tmp_path: Path, case) -> None:
    name, experiment, _cap, outcome, setter = case
    value = "short <b>text</b> \u00e9"

    detail = _served(tmp_path, experiment, outcome, setter, value)

    found = _extract(name, detail)
    obj, key = found["obj"], found["key"]
    assert obj[key] == value
    assert obj[f"{key}_truncated"] is False
    assert obj[f"{key}_original_length"] == len(value)


# ------------------------------------------------------------------ uncapped strings


def test_uncapped_strings_longer_than_every_cap_are_returned_unchanged(tmp_path: Path) -> None:
    """Recorded strings with no documented cap are never cut, even at 5,000 code points."""
    longer = _text(5000)
    assert len(longer) > CAP_LONG
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    run_id = builder.add_run("B", samples=sample_specs(3))

    def edit(m: dict[str, Any]) -> None:
        m["durability_level"] = longer

    _edit_manifest(runs, run_id, edit)
    client = _client(runs)

    detail = _get(client, f"/api/runs/{run_id}")

    assert detail["run"]["durability_level"] == longer
    assert len(detail["run"]["durability_level"]) == 5000
    assert "durability_level_truncated" not in detail["run"]


def test_recorded_timestamp_strings_are_never_capped(tmp_path: Path) -> None:
    long_ts = "2026-11-20T21:13:21." + "7" * 3000 + "Z"
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    run_id = builder.add_run("B", samples=sample_specs(3))
    _edit_manifest(runs, run_id, lambda m: m.__setitem__("container_finished_at", long_ts))

    detail = _get(_client(runs), f"/api/runs/{run_id}")

    assert detail["timing"]["container_finished_at"] == long_ts
    assert detail["timing"]["container_finished_at_display"] is None


# ------------------------------------------------------------------ list caps


def _list_case(tmp_path: Path, count: int, cap: int, setter: Callable[[dict[str, Any], list[Any]], None]):
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    run_id = builder.add_run("B", samples=sample_specs(3))
    items = [f"item-{i:03d}" for i in range(count)]
    _edit_manifest(runs, run_id, lambda m: setter(m, items))
    return _get(_client(runs), f"/api/runs/{run_id}"), items


@pytest.mark.parametrize("count", [49, 50, 51, 75])
def test_checks_list_cap_is_50_with_total_and_truncated(tmp_path: Path, count: int) -> None:
    def setter(m: dict[str, Any], items: list[Any]) -> None:
        m["checks"] = [{"name": n, "passed": True, "detail": "d"} for n in items]

    detail, items = _list_case(tmp_path, count, 50, setter)

    run = detail["run"]
    assert [c["name"] for c in run["checks"]] == items[:50]
    assert run["checks_total"] == count
    assert run["checks_truncated"] is (count > 50)


@pytest.mark.parametrize("count", [50, 51, 75])
def test_limitations_list_cap_is_50_with_total_and_truncated(tmp_path: Path, count: int) -> None:
    detail, items = _list_case(tmp_path, count, 50, lambda m, i: m.__setitem__("limitations", i))

    text = detail["runner_text"]
    assert [x["text"] for x in text["limitations"]] == items[:50]
    assert text["limitations_total"] == count
    assert text["limitations_truncated"] is (count > 50)


@pytest.mark.parametrize("count", [50, 51, 75])
def test_statements_list_cap_is_50_with_total_and_truncated(tmp_path: Path, count: int) -> None:
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    run_id = builder.add_run("C", samples=sample_specs(3))
    items = [f"statement-{i:03d}" for i in range(count)]
    _edit_manifest(runs, run_id, lambda m: m.__setitem__("sampling_limitations", {"statements": items}))

    detail = _get(_client(runs), f"/api/runs/{run_id}")

    sl = detail["runner_text"]["sampling_limitations"]
    assert [x["text"] for x in sl["statements"]] == items[:50]
    assert sl["statements_total"] == count
    assert sl["statements_truncated"] is (count > 50)


@pytest.mark.parametrize("count", [20, 21, 30])
def test_malformed_lines_cap_is_20_with_total_and_truncated(tmp_path: Path, count: int) -> None:
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    run_id = builder.add_run("B", samples=sample_specs(3))
    path = runs / run_id / "evidence.jsonl"
    path.write_bytes(path.read_bytes() + b"".join(b"this is not json %d\n" % i for i in range(count)))

    detail = _get(_client(runs), f"/api/runs/{run_id}")

    ev = detail["evidence_summary"]
    assert ev["malformed_line_count"] == count
    assert len(ev["malformed_lines"]) == min(count, 20)
    assert ev["malformed_lines_total"] == count
    assert ev["malformed_lines_truncated"] is (count > 20)
    assert ev["malformed_lines"] == sorted(ev["malformed_lines"], key=lambda e: e["line_no"])


@pytest.mark.parametrize("count", [50, 51, 60])
def test_silence_intervals_cap_is_50_with_total_and_truncated(tmp_path: Path, count: int) -> None:
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    gaps = [GapSpec(kind="silence_timeout", gap_id=f"gap-{i:04d}") for i in range(count)]
    run_id = builder.add_run("B", samples=sample_specs(3), gaps=gaps)

    detail = _get(_client(runs), f"/api/runs/{run_id}")

    ev = detail["evidence_summary"]
    assert len(ev["silence_intervals"]) == min(count, 50)
    assert ev["silence_intervals_total"] == count
    assert ev["silence_intervals_truncated"] is (count > 50)


@pytest.mark.parametrize("count", [50, 51, 60])
def test_unreconciled_drops_cap_is_50_with_total_and_truncated(tmp_path: Path, count: int) -> None:
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    drops = [
        envelope(
            "b-trunc-drops-00000001", "gap", "2026-11-20T21:13:05.000Z",
            gap_id=f"drop-{i:04d}", gap_type="probe_reported_drop",
            start_time="2026-11-20T21:13:05.000Z", end_time="2026-11-20T21:13:05.000Z",
            dropped_count=1, range_status="unknown", first_missing_seq=None, last_missing_seq=None,
            reconciliation="not_applicable", reconciles_with=[], detected_at_seq=2,
        )
        for i in range(count)
    ]
    run_id = builder.add_run(
        "B", run_id="b-trunc-drops-00000001", samples=sample_specs(3), extra_records=drops
    )

    detail = _get(_client(runs), f"/api/runs/{run_id}")

    ev = detail["evidence_summary"]
    assert ev["malformed_line_count"] == 0
    assert ev["unreconciled_drop_count"] == count
    assert len(ev["unreconciled_drops"]) == min(count, 50)
    assert ev["unreconciled_drops_total"] == count
    assert ev["unreconciled_drops_truncated"] is (count > 50)


@pytest.mark.parametrize("count", [100, 101, 120])
def test_chart_gaps_cap_is_100_with_total_and_truncated(tmp_path: Path, count: int) -> None:
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    # Samples at even seq numbers; every odd number between them is one missing range.
    samples = [SampleSpec(seq=2 * i) for i in range(count + 1)]
    gaps = [
        GapSpec(first_missing_seq=2 * i + 1, last_missing_seq=2 * i + 1, gap_id=f"gap-{i:04d}")
        for i in range(count)
    ]
    run_id = builder.add_run("B", samples=samples, gaps=gaps)

    detail = _get(_client(runs), f"/api/runs/{run_id}")

    chart = detail["chart"]
    assert chart["gaps_total"] == count
    assert len(chart["gaps"]) == min(count, 100)
    assert chart["gaps_truncated"] is (count > 100)


@pytest.mark.parametrize("count", [50, 51, 60])
def test_summarize_warnings_cap_is_50_and_text_is_capped_at_2000(tmp_path: Path, count: int) -> None:
    """Each gap whose stored reconciliation disagrees with the ranges adds one summarize warning.

    The first gap id is long so that its warning text is longer than the 2,000 code point cap.
    """
    run = "b-trunc-warn-00000001"
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    gaps = []
    for i in range(count):
        gap_id = ("g" + _text(2500)) if i == 0 else f"gap-{i:04d}"
        gaps.append(
            envelope(
                run, "gap", "2026-11-20T21:13:05.000Z", gap_id=gap_id, gap_type="sequence_discontinuity",
                start_time="2026-11-20T21:13:05.000Z", end_time="2026-11-20T21:13:05.000Z",
                first_missing_seq=3 + 2 * i, last_missing_seq=3 + 2 * i, missing_count=1,
                reconciliation="partial_overlap", reconciles_with=["no-such-gap"], detected_at_seq=100 + i,
            )
        )
    run_id = builder.add_run("B", run_id=run, samples=sample_specs(3), extra_records=gaps)

    detail = _get(_client(runs), f"/api/runs/{run_id}")

    summarize = [w for w in detail["warnings"] if w["source"] == "summarize"]
    assert detail["evidence_summary"]["malformed_line_count"] == 0
    assert len(summarize) == min(count, 50)
    first = summarize[0]
    assert first["text_truncated"] is True
    assert len(first["text"]) == CAP_LONG
    assert first["text_original_length"] > CAP_LONG
    for other in summarize[1:]:
        assert other["text_truncated"] is False
        assert other["text_original_length"] == len(other["text"])
