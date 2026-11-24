"""Segment style, omitted/skipped counts, x-axis domain and tick tests (task 2.14, part 2).

In-memory evidence records go through the real `extract_samples`, `gap_index`, `segment_styles`,
`nice_ticks`, `x_axis` and `build_chart`. Nothing here reads the real runs/ directory.

Adaptation: the design signature of `segment_styles` takes an `order_pos` mapping and gap flags; the
real function takes the full Capture-ordered sequence and a GapIndex. The tests call the real API.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from coroner.common.timeutil import format_utc_ms
from coroner.viewer.samples import capture_order, extract_samples
from coroner.viewer.timefmt import EPOCH, epoch_ms_to_text, epoch_ms_utc
from coroner.viewer.timeline import (
    CHART_CAP,
    build_chart,
    gap_index,
    nice_ticks,
    segment_styles,
    x_axis,
)
from tests.unit.viewer.builder import DEFAULT_START_TIME, envelope

RUN_ID = "b-segments-00000001"
START_MS = epoch_ms_utc(DEFAULT_START_TIME)

# The documented step list (design: X axis and ticks); beyond the last entry the step doubles.
STEPS = [
    1, 2, 5, 10, 20, 50, 100, 200, 500,
    1000, 2000, 5000, 10000, 15000, 30000,
    60000, 120000, 300000, 600000, 900000, 1800000,
    3600000, 7200000, 10800000, 21600000, 43200000,
    86400000, 172800000, 345600000, 604800000,
]


def make_records(seqs, *, null_idx=(), silence_before=(), step_ms=500, offset_ms=0):
    """Evidence records: run_metadata, then one sample per entry of `seqs`.

    `silence_before` lists sample indexes that get a silence_timeout record just before them.
    `offset_ms` shifts every Capture time (to place ticks away from Sample times).
    """
    records = [envelope(RUN_ID, "run_metadata", epoch_ms_to_text(START_MS), event="created")]
    for i, seq in enumerate(seqs):
        if i in silence_before:
            records.append(envelope(
                RUN_ID, "gap", epoch_ms_to_text(START_MS), gap_type="silence_timeout", gap_id=f"gap-{i}",
            ))
        ts_ms = START_MS + offset_ms + seq * step_ms
        body = {
            "run_id": RUN_ID, "seq": seq, "ts": epoch_ms_to_text(ts_ms),
            "rss_bytes": None if i in null_idx else 50_000_000 + i * 1000,
        }
        records.append(envelope(RUN_ID, "sample", epoch_ms_to_text(ts_ms + 20), sample=body))
    return records


def chart_of(records, *, cap=CHART_CAP, mode="raw_capture", offset_ms=None, sigkill_x_ms=None):
    result = SimpleNamespace(records=records)
    rows = extract_samples(result)
    return rows, build_chart(rows, gap_index(result), mode, offset_ms, cap=cap, sigkill_x_ms=sigkill_x_ms)


def reference_label(ms):
    return format_utc_ms(EPOCH + timedelta(milliseconds=ms))


# ---------------------------------------------------------------- segment_before, full view


def test_full_view_solid_none_and_dashed_with_a_null_rss_sample_between() -> None:
    rows, chart = chart_of(make_records(range(10), null_idx={4}))

    assert chart.partial_view is False
    by_seq = {p.seq: p for p in chart.points}
    assert 4 not in by_seq, "the null-RSS Sample is not plotted"
    assert by_seq[0].segment_before == "none"
    for seq in (1, 2, 3, 6, 7, 8, 9):
        assert by_seq[seq].segment_before == "solid", seq
        assert (by_seq[seq].omitted_before, by_seq[seq].skipped_before) == (0, 0)
    assert by_seq[5].segment_before == "dashed"
    assert by_seq[5].skipped_before == 1
    assert by_seq[5].omitted_before == 1
    assert not any(p.gap_before for p in chart.points)


def test_two_adjacent_null_samples_are_both_counted_as_skipped() -> None:
    _, chart = chart_of(make_records(range(8), null_idx={3, 4}))

    point = next(p for p in chart.points if p.seq == 5)
    assert (point.segment_before, point.skipped_before, point.omitted_before) == ("dashed", 2, 2)


def test_gap_takes_precedence_over_a_dashed_style() -> None:
    # a null-RSS Sample (index 4) and a silence_timeout record both lie between seq 3 and seq 5
    records = make_records(range(10), null_idx={4}, silence_before={5})
    _, chart = chart_of(records)

    point = next(p for p in chart.points if p.seq == 5)
    assert point.gap_before is True
    assert point.segment_before == "none"
    assert point.omitted_before == 1 and point.skipped_before == 1


def test_legend_condition_is_at_least_one_dashed_point() -> None:
    _, with_null = chart_of(make_records(range(10), null_idx={4}))
    _, without_null = chart_of(make_records(range(10)))
    _, with_gap_only = chart_of(make_records(range(10), silence_before={5}))

    assert any(p.segment_before == "dashed" for p in with_null.points)
    assert not any(p.segment_before == "dashed" for p in without_null.points)
    assert not any(p.segment_before == "dashed" for p in with_gap_only.points)
    assert all(p.segment_before == "solid" for p in without_null.points[1:])


# ---------------------------------------------------------------- segment_before, Partial_View


def test_partial_view_stride_skips_make_dashed_segments_with_counts() -> None:
    n = 100
    null_idx = {5, 20, 40, 41}
    rows, chart = chart_of(make_records(range(n), null_idx=null_idx), cap=10)

    assert chart.partial_view is True
    full = capture_order([r for r in rows if r.ts_ms is not None])
    pos = {r.seq: i for i, r in enumerate(full)}
    pts = chart.points
    assert pts[0].segment_before == "none"
    for prev, cur in zip(pts, pts[1:]):
        between = full[pos[prev.seq] + 1:pos[cur.seq]]
        assert cur.omitted_before == len(between)
        assert cur.skipped_before == sum(1 for r in between if r.rss is None)
        assert cur.segment_before == ("solid" if not between else "dashed")
        assert (cur.segment_before == "dashed") == (cur.omitted_before > 0)
    assert any(p.segment_before == "dashed" for p in pts)
    # every valid Sample is either plotted or counted once in an omitted_before
    assert sum(p.omitted_before for p in pts) == n - len(pts)
    assert sum(p.skipped_before for p in pts) == len(null_idx)


def test_partial_view_keeps_solid_segments_between_adjacent_plotted_points() -> None:
    n = CHART_CAP + 1  # a stride that skips one Sample leaves most neighbours adjacent
    _, chart = chart_of(make_records(range(n)))

    assert chart.partial_view is True
    kinds = [p.segment_before for p in chart.points]
    assert kinds[0] == "none"
    assert kinds.count("dashed") >= 1
    assert kinds.count("solid") > kinds.count("dashed")
    assert sum(p.omitted_before for p in chart.points) == n - chart.plotted_samples


def test_segment_styles_function_with_a_gap_between_selected_points() -> None:
    records = make_records(range(6), silence_before={3})
    result = SimpleNamespace(records=records)
    rows = extract_samples(result)
    selected = [rows[0], rows[2], rows[3], rows[5]]

    styles = segment_styles(selected, capture_order(rows), gap_index(result))

    assert styles == [
        ("none", 0, 0),      # first point
        ("dashed", 1, 0),    # one omitted Sample
        ("none", 0, 0),      # silence_timeout between seq 2 and seq 3
        ("dashed", 1, 0),
    ]


# ---------------------------------------------------------------- x_axis domain


def test_x_axis_domain_contains_every_point_and_the_sigkill_x() -> None:
    records = make_records(range(10))
    _, base = chart_of(records)
    xs = [p.x_ms for p in base.points]
    after = max(xs) + 7_000
    before = min(xs) - 3_000

    _, with_after = chart_of(records, sigkill_x_ms=after)
    _, with_before = chart_of(records, sigkill_x_ms=before)
    _, inside = chart_of(records, sigkill_x_ms=xs[3] + 1)

    assert (base.x_axis_data.domain_min_ms, base.x_axis_data.domain_max_ms) == (min(xs), max(xs))
    assert with_after.x_axis_data.domain_max_ms == after
    assert with_after.x_axis_data.domain_min_ms == min(xs)
    assert with_before.x_axis_data.domain_min_ms == before
    assert with_before.x_axis_data.domain_max_ms == max(xs)
    assert (inside.x_axis_data.domain_min_ms, inside.x_axis_data.domain_max_ms) == (min(xs), max(xs))
    for chart in (base, with_after, with_before, inside):
        axis = chart.x_axis_data
        assert all(axis.domain_min_ms <= p.x_ms <= axis.domain_max_ms for p in chart.points)


def test_x_axis_domain_uses_corrected_positions_and_the_raw_sigkill_x() -> None:
    records = make_records(range(10))
    rows, raw = chart_of(records)
    sigkill = max(p.x_ms for p in raw.points) + 500
    _, corrected = chart_of(records, mode="corrected", offset_ms=9, sigkill_x_ms=sigkill)

    xs = [p.x_ms for p in corrected.points]
    assert xs == [r.ts_ms - 9 for r in rows]
    assert corrected.x_axis_data.domain_min_ms == min(xs)
    assert corrected.x_axis_data.domain_max_ms == sigkill


def test_x_axis_is_absent_without_points_or_sigkill_and_present_with_only_a_sigkill() -> None:
    _, empty = chart_of(make_records([]))
    only_sigkill = x_axis([], 1_795_209_200_000)

    assert empty.x_axis_data is None and empty.points == []
    assert only_sigkill.domain_min_ms == only_sigkill.domain_max_ms == 1_795_209_200_000
    assert only_sigkill.tick_values_ms == [1_795_209_200_000]


# ---------------------------------------------------------------- ticks


def expected_step(span):
    step = next((s for s in STEPS if span <= 6 * s), None)
    if step is None:
        step = STEPS[-1]
        while span > 6 * step:
            step *= 2
    return step


def assert_tick_rules(lo, hi):
    ticks = nice_ticks(lo, hi)
    span = hi - lo
    if span == 0:
        assert ticks == [lo]
        return ticks
    assert 2 <= len(ticks) <= 7, (lo, hi, ticks)
    step = expected_step(span)
    assert all(t % step == 0 for t in ticks), "every tick is a multiple of the one step"
    assert all(b - a == step for a, b in zip(ticks, ticks[1:]))
    assert all(lo <= t <= hi for t in ticks)
    assert ticks[0] - step < lo and ticks[-1] + step > hi, "no tick of that step is left out"
    return ticks


@pytest.mark.parametrize(
    "span",
    [1, 2, 6, 7, 30, 599, 1000, 29_999, 30_000, 31_000, 86_400_000, 604_800_000 * 6, 604_800_000 * 7, 10 ** 11],
)
def test_tick_count_and_step_for_representative_spans(span) -> None:
    lo = START_MS + 137
    ticks = assert_tick_rules(lo, lo + span)
    assert 1 <= len(ticks) <= 7


def test_step_list_is_used_and_extended_by_doubling() -> None:
    assert nice_ticks(0, 6)[1] - nice_ticks(0, 6)[0] == 1
    ticks = nice_ticks(0, 60_000)  # span / 10 s = 6 -> 10 s step
    assert ticks == [0, 10_000, 20_000, 30_000, 40_000, 50_000, 60_000]
    ticks = nice_ticks(0, 604_800_000 * 6 + 1)  # needs the doubled step 14 days
    assert ticks[1] - ticks[0] == 1_209_600_000


@settings(max_examples=200, deadline=None)
@given(
    lo=st.integers(min_value=0, max_value=4_000_000_000_000),
    span=st.integers(min_value=0, max_value=10 ** 12),
)
def test_tick_rules_hold_for_any_domain(lo, span) -> None:
    assert_tick_rules(lo, lo + span)


def test_every_tick_label_is_the_exact_time_of_its_own_x_ms() -> None:
    _, chart = chart_of(make_records(range(30), offset_ms=137))
    axis = chart.x_axis_data

    assert len(axis.tick_labels) == len(axis.tick_values_ms) >= 2
    for value, label in zip(axis.tick_values_ms, axis.tick_labels):
        assert label == reference_label(value)


@settings(max_examples=100, deadline=None)
@given(lo=st.integers(min_value=0, max_value=4_000_000_000_000), span=st.integers(min_value=0, max_value=10 ** 11))
def test_tick_labels_for_any_domain(lo, span) -> None:
    axis = x_axis([lo, lo + span], None)
    assert axis.tick_labels == [reference_label(t) for t in axis.tick_values_ms]


def test_a_tick_coincides_with_no_sample_and_no_label_borrows_a_sample_time() -> None:
    # Capture times sit 137 ms past each half second, so no tick (a multiple of the step) equals one.
    _, chart = chart_of(make_records(range(30), offset_ms=137))
    axis = chart.x_axis_data
    sample_xs = {p.x_ms for p in chart.points}
    sample_texts = {p.ts for p in chart.points}

    assert axis.tick_values_ms
    assert not (set(axis.tick_values_ms) & sample_xs)
    assert not (set(axis.tick_labels) & sample_texts)


def test_single_point_domain_has_one_tick_labelled_with_its_own_time() -> None:
    _, chart = chart_of(make_records([0], offset_ms=137))

    axis = chart.x_axis_data
    x = chart.points[0].x_ms
    assert (axis.domain_min_ms, axis.domain_max_ms) == (x, x)
    assert axis.tick_values_ms == [x]
    assert axis.tick_labels == [reference_label(x)]


def test_zero_width_domain_from_samples_at_one_instant_and_the_sigkill() -> None:
    records = make_records([0, 0, 0], step_ms=500)  # three Samples at the same Capture_Time
    _, chart = chart_of(records)
    x = chart.points[0].x_ms
    _, with_sigkill = chart_of(records, sigkill_x_ms=x)

    for c in (chart, with_sigkill):
        assert {p.x_ms for p in c.points} == {x}
        assert c.x_axis_data.domain_min_ms == c.x_axis_data.domain_max_ms == x
        assert c.x_axis_data.tick_values_ms == [x]
        assert c.x_axis_data.tick_labels == [reference_label(x)]
