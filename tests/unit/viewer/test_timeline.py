"""Timeline selection, full-run statistics and gap tests (task 2.14, part 1).

Chart-level tests use in-memory evidence records (a stand-in for a ReadResult that only carries
`records`) passed through the real `extract_samples`, `gap_index` and `build_chart`. The end-to-end
statistics test writes a synthetic run with RunsBuilder under tmp_path and reads it with `load_detail`.
Nothing here reads the real runs/ directory.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from coroner.viewer.files import DEFAULT_IO, open_runs_root
from coroner.viewer.loader import load_detail
from coroner.viewer.model import to_detail
from coroner.viewer.samples import extract_samples, rss_stats
from coroner.viewer.timefmt import epoch_ms_to_text, epoch_ms_utc
from coroner.viewer.timeline import CHART_CAP, build_chart, gap_index
from tests.unit.viewer.builder import (
    DEFAULT_START_TIME,
    EVIDENCE_NAME,
    GapSpec,
    RunsBuilder,
    SampleSpec,
    envelope,
)

RUN_ID = "b-timeline-00000001"
START_MS = epoch_ms_utc(DEFAULT_START_TIME)
MIB = 1024 * 1024


def peaked_rss(n: int):
    """RSS with one unique peak at index n // 2; the first and last values differ."""
    mid = n // 2
    return lambda i: 10_000_000 + 1000 * (mid - abs(i - mid))


def make_records(
    seqs,
    *,
    rss=None,
    null_idx=(),
    gaps_before=None,
):
    """Evidence records: one run_metadata, then one sample per entry of `seqs` (Capture time from seq).

    `gaps_before` maps a sample index to a list of gap records inserted just before that sample.
    """
    gaps_before = gaps_before or {}
    rss_fn = rss if rss is not None else (lambda i: 50_000_000 + i * 1000)
    meta = envelope(RUN_ID, "run_metadata", epoch_ms_to_text(START_MS), event="created")
    records = [meta]
    for i, seq in enumerate(seqs):
        records.extend(gaps_before.get(i, []))
        ts_ms = START_MS + seq * 500
        body = {
            "run_id": RUN_ID,
            "seq": seq,
            "ts": epoch_ms_to_text(ts_ms),
            "rss_bytes": None if i in null_idx else rss_fn(i),
        }
        records.append(envelope(RUN_ID, "sample", epoch_ms_to_text(ts_ms + 20), sample=body))
    return records


def result_of(records):
    return SimpleNamespace(records=records)


def chart_of(records, *, cap=CHART_CAP, mode="raw_capture", offset_ms=None, sigkill_x_ms=None):
    result = result_of(records)
    rows = extract_samples(result)
    return rows, build_chart(rows, gap_index(result), mode, offset_ms, cap=cap, sigkill_x_ms=sigkill_x_ms)


def silence_gap():
    return envelope(
        RUN_ID, "gap", epoch_ms_to_text(START_MS), gap_type="silence_timeout", gap_id="gap-s",
    )


def discontinuity_gap(first, last):
    return envelope(
        RUN_ID, "gap", epoch_ms_to_text(START_MS), gap_type="sequence_discontinuity", gap_id="gap-d",
        first_missing_seq=first, last_missing_seq=last, missing_count=last - first + 1,
    )


# ---------------------------------------------------------------- selection at the cap


@pytest.mark.parametrize("n", [4999, 5000, 5001, 20000])
def test_selection_at_cap_boundaries(n) -> None:
    records = make_records(range(n), rss=peaked_rss(n))
    rows, chart = chart_of(records)
    fixture = {(r.seq, r.ts, r.recv, r.file_pos): r for r in rows}
    peak_seq = n // 2

    assert chart.chart_cap == CHART_CAP == 5000
    assert chart.partial_view is (n > 5000)
    assert chart.plotted_samples == len(chart.points)
    if n <= 5000:
        assert chart.plotted_samples == n
    else:
        assert chart.plotted_samples <= 5000

    seqs = [p.seq for p in chart.points]
    assert seqs[0] == 0, "first Sample is plotted"
    assert seqs[-1] == n - 1, "last Sample is plotted"
    assert peak_seq in seqs, "peak Sample is plotted"
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "Capture order, no repeats"
    ts_values = [p.ts_ms for p in chart.points]
    assert ts_values == sorted(ts_values)
    for p in chart.points:
        row = fixture[(p.seq, p.ts, p.recv, p.file_pos)]  # KeyError: a point that is not a fixture Sample
        assert round(p.rss_mib * MIB) == row.rss

    again = chart_of(records)[1]
    assert again == chart, "identical across calls"


@pytest.mark.parametrize("n", [4999, 5000, 5001, 20000])
def test_statistics_come_from_the_full_fixture_not_the_selection(n) -> None:
    null_idx = {i for i in range(3, n - 1, 7)}
    rss = peaked_rss(n)
    records = make_records(range(n), rss=rss, null_idx=null_idx)
    rows, chart = chart_of(records)

    assert chart.total_samples == n
    assert chart.unplottable_samples == len(null_idx)
    assert chart.plottable_samples == n - len(null_idx)
    assert chart.partial_view is (chart.plottable_samples > 5000)
    expected_peak = max(rss(i) for i in range(n) if i not in null_idx)
    assert chart.y_data_max_bytes == expected_peak

    stats = rss_stats(rows)
    assert stats.first.rss == rss(0)
    assert stats.highest.rss == expected_peak
    assert stats.last.rss == rss(n - 1)
    # the plotted first, peak and last match the full values
    plotted = {p.seq: round(p.rss_mib * MIB) for p in chart.points}
    assert plotted[0] == rss(0)
    assert plotted[n - 1] == rss(n - 1)
    assert expected_peak in plotted.values()
    assert all(p.rss_mib is not None for p in chart.points), "null-RSS Samples are never plotted"


@pytest.mark.parametrize("n", [4999, 5000, 5001, 20000])
def test_missing_sample_and_malformed_line_counts_equal_the_full_fixture(tmp_path, n) -> None:
    missing = {10, 11, 12}
    seqs = [i for i in range(n + len(missing)) if i not in missing]
    rss = peaked_rss(len(seqs))
    specs = [SampleSpec(seq=s, rss_bytes=rss(i)) for i, s in enumerate(seqs)]
    builder = RunsBuilder(tmp_path / "runs")
    run_id = builder.add_run(
        "B", run_id=RUN_ID, samples=specs,
        gaps=[GapSpec(kind="sequence_discontinuity", first_missing_seq=10, last_missing_seq=12)],
    )
    evidence = tmp_path / "runs" / run_id / EVIDENCE_NAME
    evidence.write_bytes(evidence.read_bytes() + b"not json\n{broken\n")

    record = load_detail(open_runs_root(tmp_path / "runs"), run_id, DEFAULT_IO, time.time_ns)
    detail = to_detail(record)
    summary, chart = detail["evidence_summary"], detail["chart"]

    assert summary["missing_sample_count"] == len(missing)
    assert summary["malformed_line_count"] == 2
    assert summary["sample_count"] == n
    assert summary["first_rss"]["rss_bytes"] == rss(0)
    assert summary["highest_rss"]["rss_bytes"] == rss(n // 2)
    assert summary["last_rss"]["rss_bytes"] == rss(n - 1)
    assert chart["total_samples"] == n
    assert chart["plottable_samples"] == n
    assert chart["partial_view"] is (n > 5000)
    assert chart["plotted_samples"] == len(chart["points"])
    assert chart["plotted_samples"] == (n if n <= 5000 else chart["plotted_samples"])
    assert chart["plotted_samples"] <= 5000
    assert chart["y_data_max_bytes"] == rss(n // 2)
    plotted_seqs = [p["seq"] for p in chart["points"]]
    assert plotted_seqs[0] == 0 and plotted_seqs[-1] == seqs[-1]
    assert seqs[n // 2] in plotted_seqs


# ---------------------------------------------------------------- smaller chart_cap


def test_partial_view_and_statistics_for_a_smaller_chart_cap() -> None:
    n = 100
    null_idx = {5, 20, 40}
    rss = peaked_rss(n)
    records = make_records(range(n), rss=rss, null_idx=null_idx)

    _, small = chart_of(records, cap=10)
    _, exact = chart_of(records, cap=n - len(null_idx))
    _, over = chart_of(records, cap=n - len(null_idx) - 1)

    assert small.chart_cap == 10
    assert small.partial_view is True
    assert 0 < small.plotted_samples <= 10
    assert small.total_samples == n
    assert small.plottable_samples == n - 3
    assert small.unplottable_samples == 3
    assert small.y_data_max_bytes == rss(n // 2)
    seqs = [p.seq for p in small.points]
    assert seqs[0] == 0 and seqs[-1] == n - 1 and n // 2 in seqs

    assert exact.partial_view is False and exact.plotted_samples == n - 3
    assert over.partial_view is True
    # statistics never depend on the cap
    for chart in (exact, over):
        assert (chart.total_samples, chart.plottable_samples, chart.y_data_max_bytes) == (
            small.total_samples, small.plottable_samples, small.y_data_max_bytes,
        )


# ---------------------------------------------------------------- gaps


def _gap_flags(chart):
    return [p.gap_before for p in chart.points]


def test_sequence_discontinuity_sets_gap_before_in_full_view() -> None:
    seqs = list(range(0, 50)) + list(range(53, 100))  # 50, 51, 52 missing
    records = make_records(seqs, gaps_before={50: [discontinuity_gap(50, 52)]})
    _, chart = chart_of(records)

    flagged = [p.seq for p in chart.points if p.gap_before]
    assert flagged == [53]
    point = next(p for p in chart.points if p.seq == 53)
    assert point.segment_before == "none"
    assert chart.partial_view is False
    assert chart.unknown_range_drop_present is False


def test_sequence_discontinuity_gap_before_in_partial_view() -> None:
    seqs = list(range(0, 50)) + list(range(53, 100))
    records = make_records(seqs, gaps_before={50: [discontinuity_gap(50, 52)]})
    _, chart = chart_of(records, cap=10)

    assert chart.partial_view is True
    pts = chart.points
    spanning = [i for i in range(1, len(pts)) if pts[i - 1].seq < 50 <= 52 < pts[i].seq]
    assert len(spanning) == 1
    assert [i for i, p in enumerate(pts) if p.gap_before] == spanning
    assert pts[spanning[0]].segment_before == "none"
    assert pts[0].gap_before is False


def test_silence_timeout_sets_gap_before_in_full_view() -> None:
    records = make_records(range(100), gaps_before={50: [silence_gap()]})
    _, chart = chart_of(records)

    assert [p.seq for p in chart.points if p.gap_before] == [50]
    assert next(p for p in chart.points if p.seq == 50).segment_before == "none"
    assert any(g["gap_type"] == "silence_timeout" for g in chart.gaps)


def test_silence_timeout_gap_before_in_partial_view() -> None:
    records = make_records(range(100), gaps_before={50: [silence_gap()]})
    _, chart = chart_of(records, cap=10)

    assert chart.partial_view is True
    pts = chart.points
    spanning = [i for i in range(1, len(pts)) if pts[i - 1].seq < 50 <= pts[i].seq]
    assert len(spanning) == 1
    assert [i for i, p in enumerate(pts) if p.gap_before] == spanning
    assert pts[spanning[0]].segment_before == "none"


@pytest.mark.parametrize("status", ["uncertain", "unknown"])
def test_unknown_range_drop_sets_the_flag_and_never_gap_before(status) -> None:
    drop = envelope(
        RUN_ID, "gap", epoch_ms_to_text(START_MS), gap_type="probe_reported_drop", gap_id="gap-p",
        range_status=status,
    )
    records = make_records(range(100), gaps_before={50: [drop]})

    for cap in (CHART_CAP, 10):
        _, chart = chart_of(records, cap=cap)
        assert chart.unknown_range_drop_present is True
        assert not any(_gap_flags(chart)), "an unknown-range drop has no position, so no gap_before"
        assert all(p.segment_before != "none" for p in chart.points[1:])
