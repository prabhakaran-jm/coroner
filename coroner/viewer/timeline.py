"""RSS timeline chart computation for the Coroner Viewer.

Implements deterministic Partial_View selection (integer-arithmetic stride
plus forced peak), gap detection, segment styles, x-axis domain and tick
labels, and the main ``build_chart`` assembler.

Design decisions enforced here:
- Every full-run statistic (total_samples, plottable_samples, first/peak/last
  RSS, etc.) is computed BEFORE selection so ``cap`` never feeds a statistic.
- ``partial_view`` is True if and only if ``plottable_samples > cap``.
- For Experiment B the x position is ``ts_ms - offset_ms`` (corrected);
  for A and C it is the raw ``ts_ms``.  An ``OverflowError`` or ``ValueError``
  in ``format_utc_ms`` when producing the corrected display falls back to
  ``raw_uncorrected`` with reason ``correction_out_of_range``.
- The original artifact rows are NEVER modified.

Import constraints: stdlib only, plus coroner.viewer.samples (SampleRow,
is_plottable, capture_order), coroner.viewer.timefmt (epoch_ms_to_text),
and coroner.evidence.reader (ReadResult) for type annotations.
Never imports coroner.runner.experiments.base, coroner.runner.evaluate, or
any other forbidden module.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from coroner.viewer.samples import SampleRow, capture_order, is_plottable
from coroner.viewer.timefmt import epoch_ms_to_text

__all__ = [
    "CHART_CAP",
    "select_points",
    "gap_index",
    "segment_styles",
    "nice_ticks",
    "x_axis",
    "build_chart",
    "GapIndex",
    "XAxis",
    "Chart",
    "AxisMode",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHART_CAP: int = 5000

# Design step list (ms) for nice_ticks, plus infinite doubling from last value.
_STEPS: list[int] = [
    1, 2, 5, 10, 20, 50, 100, 200, 500,                     # ms
    1_000, 2_000, 5_000, 10_000, 15_000, 30_000,            # seconds
    60_000, 120_000, 300_000, 600_000, 900_000, 1_800_000,  # minutes
    3_600_000, 7_200_000, 10_800_000, 21_600_000, 43_200_000,  # hours
    86_400_000, 172_800_000, 345_600_000, 604_800_000,      # days (1,2,4,7)
]

# Axis mode type alias
AxisMode = str  # "raw_capture" | "corrected" | "raw_uncorrected"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class GapIndex:
    """Pre-computed gap information from the full parsed evidence."""
    # Sorted list of [lo, hi] closed intervals of missing sequence numbers.
    seq_intervals: list[tuple[int, int]]
    # Sorted file positions (index in ReadResult.records) of silence_timeout records.
    silence_positions: list[int]
    # True when any probe_reported_drop has range_status uncertain or unknown.
    unknown_range_drop_present: bool


@dataclass
class XAxis:
    """Server-computed x-axis domain and tick labels."""
    domain_min_ms: int
    domain_max_ms: int
    tick_values_ms: list[int]
    tick_labels: list[str]


@dataclass
class ChartPoint:
    """One plotted chart point."""
    # Identifying fields
    file_pos: int
    seq: int
    ts: str                     # original Capture_Time string
    ts_ms: int | None
    recv: str                   # original Receive_Time string
    rss_mib: float | None       # None when rss is None (should not appear; guard)
    # Axis
    x_ms: int | None
    corrected_capture_time: str | None  # format_utc_ms of corrected ms; None for A/C
    # Gap / segment metadata
    gap_before: bool
    segment_before: str         # "solid" | "dashed" | "none"
    skipped_before: int         # Samples with null RSS between this and prev plotted
    omitted_before: int         # total valid Samples between this and prev not plotted


@dataclass
class Chart:
    """Complete chart payload returned by build_chart."""
    # Statistics from the full evidence (never from the selection)
    total_samples: int | None
    plottable_samples: int | None
    unplottable_samples: int | None
    # Selection
    plotted_samples: int
    partial_view: bool
    chart_cap: int
    # Time axis
    time_axis: AxisMode
    correction_unavailable_reason: str | None
    x_axis_data: XAxis | None
    y_data_max_bytes: int | None
    unknown_range_drop_present: bool
    # Content
    points: list[ChartPoint]
    gaps: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def select_points(rows: Sequence[SampleRow], cap: int = CHART_CAP) -> list[int]:
    """Return a sorted list of indices into the **Capture-ordered plottable** list.

    ``rows`` must already be sorted in Capture order (output of
    ``capture_order(plottable_rows)``).  The returned indices index into that
    list, not into the original file-order list.

    Algorithm (design pseudocode):
        n = len(rows)
        if n <= cap: return [0 .. n-1]       # full view
        peak = earliest max-rss in Capture order
        extra = 0 if peak in {0, n-1} else 1
        m = cap - extra
        uniform = { (j*(n-1)) // (m-1)  for j in 0..m-1 }  (integer arithmetic)
        return sorted(uniform | {peak})

    The result always contains 0 (first), n-1 (last), and ``peak``.
    """
    n = len(rows)
    if n <= cap:
        return list(range(n))

    # Find the peak: maximum rss, earliest in Capture order on ties
    peak_idx = 0
    peak_rss: int = rows[0].rss if rows[0].rss is not None else -1
    for i, r in enumerate(rows):
        rv = r.rss if r.rss is not None else -1
        if rv > peak_rss:
            peak_rss = rv
            peak_idx = i

    extra = 0 if peak_idx in (0, n - 1) else 1
    m = cap - extra

    # Integer-arithmetic uniform indices; always includes 0 and n-1
    uniform: set[int] = set()
    if m == 1:
        # degenerate: only the first (and last if same) point
        uniform.add(0)
        uniform.add(n - 1)
    else:
        for j in range(m):
            uniform.add((j * (n - 1)) // (m - 1))

    selected = sorted(uniform | {peak_idx})
    return selected


# ---------------------------------------------------------------------------
# Gap detection
# ---------------------------------------------------------------------------

def gap_index(result: object) -> GapIndex:
    """Build a :class:`GapIndex` from the full parsed evidence.

    Reads ``result.records`` in file order and collects:
    - Sorted, merged sequence intervals from ``sequence_discontinuity`` and
      ``probe_reported_drop`` (range_status ``"known"``).
    - Sorted file positions of ``silence_timeout`` records.
    - The ``unknown_range_drop_present`` flag for drops with uncertain/unknown range.
    """
    raw_intervals: list[tuple[int, int]] = []
    silence_positions: list[int] = []
    unknown_range_drop_present = False

    for file_pos, record in enumerate(result.records):  # type: ignore[attr-defined]
        rtype = record.get("record_type")
        if rtype != "gap":
            continue
        gap_type = record.get("gap_type") or record.get("gap", {}).get("gap_type")
        # Support both flat and nested gap shapes
        if gap_type is None:
            gap_data = record.get("gap") or record
            gap_type = gap_data.get("gap_type")
        else:
            gap_data = record

        if gap_type == "sequence_discontinuity":
            lo = gap_data.get("first_missing_seq")
            hi = gap_data.get("last_missing_seq")
            if isinstance(lo, int) and not isinstance(lo, bool) and \
               isinstance(hi, int) and not isinstance(hi, bool):
                raw_intervals.append((lo, hi))

        elif gap_type == "probe_reported_drop":
            range_status = gap_data.get("range_status")
            if range_status == "known":
                lo = gap_data.get("first_missing_seq")
                hi = gap_data.get("last_missing_seq")
                if isinstance(lo, int) and not isinstance(lo, bool) and \
                   isinstance(hi, int) and not isinstance(hi, bool):
                    raw_intervals.append((lo, hi))
            elif range_status in ("uncertain", "unknown"):
                unknown_range_drop_present = True

        elif gap_type == "silence_timeout":
            silence_positions.append(file_pos)

    # Merge overlapping/adjacent intervals
    merged = _merge_intervals(raw_intervals)

    return GapIndex(
        seq_intervals=merged,
        silence_positions=sorted(silence_positions),
        unknown_range_drop_present=unknown_range_drop_present,
    )


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort and merge overlapping or adjacent [lo, hi] closed intervals."""
    if not intervals:
        return []
    sorted_iv = sorted(intervals)
    merged: list[tuple[int, int]] = [sorted_iv[0]]
    for lo, hi in sorted_iv[1:]:
        cur_lo, cur_hi = merged[-1]
        if lo <= cur_hi + 1:
            merged[-1] = (cur_lo, max(cur_hi, hi))
        else:
            merged.append((lo, hi))
    return merged


def _gap_before_pair(
    prev: SampleRow,
    cur: SampleRow,
    gaps: GapIndex,
) -> bool:
    """Return True when an Evidence_Gap lies between *prev* and *cur*.

    Checks:
    1. Sequence intervals: any [lo, hi] that intersects (min_seq, max_seq).
    2. Silence positions: any file_pos strictly between the two file positions.
    """
    # Sequence check
    min_seq = min(prev.seq, cur.seq)
    max_seq = max(prev.seq, cur.seq)
    if max_seq > min_seq + 1 and gaps.seq_intervals:
        # Look for an interval that overlaps (min_seq, max_seq) exclusively
        # i.e. lo < max_seq and hi > min_seq
        idx = bisect.bisect_left(gaps.seq_intervals, (min_seq + 1, -1))
        for lo, hi in gaps.seq_intervals[max(0, idx - 1):idx + 2]:
            if lo < max_seq and hi > min_seq:
                return True

    # Silence check
    if gaps.silence_positions:
        min_fp = min(prev.file_pos, cur.file_pos)
        max_fp = max(prev.file_pos, cur.file_pos)
        # Find positions strictly between min_fp and max_fp
        idx_lo = bisect.bisect_right(gaps.silence_positions, min_fp)
        idx_hi = bisect.bisect_left(gaps.silence_positions, max_fp)
        if idx_lo < idx_hi:
            return True

    return False


# ---------------------------------------------------------------------------
# Segment styles
# ---------------------------------------------------------------------------

def segment_styles(
    selected: Sequence[SampleRow],
    full_sequence: Sequence[SampleRow],
    gaps: GapIndex,
) -> list[tuple[str, int, int]]:
    """Return per-point (segment_before, omitted_before, skipped_before).

    ``selected`` is the list of plotted SampleRows in Capture order.
    ``full_sequence`` is ALL valid Samples with a parseable Capture_Time
    (plottable or not), in Capture order — the "full sequence S" from the design.

    Rules (design "Segment style"):
        i == 0:                       "none", 0, 0
        gap_before(i):                "none", omitted_before, skipped_before
        pos(i) == pos(i-1) + 1:       "solid", 0, 0
        else:                         "dashed", omitted_before, skipped_before

    ``omitted_before`` = pos(cur) - pos(prev) - 1 (number of full-sequence
    Samples between the two plotted points that are not plotted).
    ``skipped_before`` = number of those with rss is None (null-RSS Samples).
    """
    # Build a mapping from SampleRow.file_pos to its index in full_sequence
    pos_of: dict[int, int] = {r.file_pos: i for i, r in enumerate(full_sequence)}

    result: list[tuple[str, int, int]] = []
    for i, cur in enumerate(selected):
        if i == 0:
            result.append(("none", 0, 0))
            continue

        prev = selected[i - 1]

        # Compute omitted_before and skipped_before first (needed for gap "none" too)
        pos_cur = pos_of.get(cur.file_pos, -1)
        pos_prev = pos_of.get(prev.file_pos, -1)
        if pos_cur >= 0 and pos_prev >= 0 and pos_cur > pos_prev:
            between = full_sequence[pos_prev + 1:pos_cur]
            omitted_before = len(between)
            skipped_before = sum(1 for r in between if r.rss is None)
        else:
            omitted_before = 0
            skipped_before = 0

        if _gap_before_pair(prev, cur, gaps):
            result.append(("none", omitted_before, skipped_before))
        elif pos_cur == pos_prev + 1:
            result.append(("solid", 0, 0))
        else:
            result.append(("dashed", omitted_before, skipped_before))

    return result


# ---------------------------------------------------------------------------
# Tick generation
# ---------------------------------------------------------------------------

def nice_ticks(x_min: int, x_max: int) -> list[int]:
    """Return a list of epoch-ms int values for the x-axis ticks.

    Rules (design "X axis and ticks"):
    - ``span == 0``: return ``[x_min]``.
    - Choose the smallest step from _STEPS (extended by doubling) such that
      ``span / step <= 6``.
    - Ticks are multiples of that step in [x_min, x_max].
    - If the resulting list is empty, return ``[x_min, x_max]``.
    """
    span = x_max - x_min
    if span == 0:
        return [x_min]

    # Find step: smallest where span / step <= 6
    step = _STEPS[-1]
    for s in _STEPS:
        if span <= 6 * s:
            step = s
            break
    else:
        # Extend by doubling until span / step <= 6
        s = _STEPS[-1]
        while span > 6 * s:
            s *= 2
        step = s

    # First tick: ceil(x_min / step) * step
    import math as _math
    t = _math.ceil(x_min / step) * step
    ticks: list[int] = []
    while t <= x_max:
        ticks.append(t)
        t += step

    if not ticks:
        return [x_min, x_max]

    return ticks


# ---------------------------------------------------------------------------
# X axis
# ---------------------------------------------------------------------------

def x_axis(
    xs: Sequence[int],
    extra_x_ms: int | None,
) -> XAxis:
    """Compute domain and labelled tick values from a sequence of x positions.

    ``xs`` is the list of plotted x_ms values (corrected or raw).
    ``extra_x_ms`` is the SIGKILL x_ms when it is drawn.
    """
    all_xs: list[int] = list(xs)
    if extra_x_ms is not None:
        all_xs.append(extra_x_ms)

    if not all_xs:
        # No points — degenerate
        return XAxis(
            domain_min_ms=0,
            domain_max_ms=0,
            tick_values_ms=[0],
            tick_labels=[epoch_ms_to_text(0)],
        )

    domain_min = min(all_xs)
    domain_max = max(all_xs)
    tick_vals = nice_ticks(domain_min, domain_max)
    tick_labels = [epoch_ms_to_text(t) for t in tick_vals]

    return XAxis(
        domain_min_ms=domain_min,
        domain_max_ms=domain_max,
        tick_values_ms=tick_vals,
        tick_labels=tick_labels,
    )


# ---------------------------------------------------------------------------
# build_chart
# ---------------------------------------------------------------------------

def build_chart(
    rows: Sequence[SampleRow],
    gaps: GapIndex,
    mode: AxisMode,
    offset_ms: int | None,
    cap: int = CHART_CAP,
    sigkill_x_ms: int | None = None,
) -> Chart:
    """Assemble the full chart payload.

    ``rows`` is the full list of SampleRows in file order (from
    ``extract_samples``).  ``gaps`` is from ``gap_index``.  ``mode`` is from
    ``axis_mode`` (``"corrected"``, ``"raw_uncorrected"``, ``"raw_capture"``).
    ``offset_ms`` is the usable integer offset (B-mode only), or None.
    ``cap`` is the chart cap (always CHART_CAP in production).
    ``sigkill_x_ms`` is the drawn SIGKILL position (epoch ms), or None.

    Statistics are computed from ALL rows before selection.
    """
    # ----- Full-run statistics (computed BEFORE selection) -----
    all_plottable = [r for r in rows if is_plottable(r)]
    all_plottable_ordered = capture_order(all_plottable)

    total_samples = len(rows)
    plottable_samples = len(all_plottable)
    unplottable_samples = total_samples - plottable_samples

    partial_view = plottable_samples > cap

    # y_data_max_bytes: highest RSS in the full evidence
    y_data_max_bytes: int | None = None
    for r in all_plottable:
        if r.rss is not None and (y_data_max_bytes is None or r.rss > y_data_max_bytes):
            y_data_max_bytes = r.rss

    # ----- Selection -----
    selected_indices = select_points(all_plottable_ordered, cap)
    selected_rows = [all_plottable_ordered[i] for i in selected_indices]

    # ----- Actual time axis mode (B-mode correction; fallback if overflow) -----
    actual_mode = mode
    actual_offset = offset_ms
    correction_unavailable_reason: str | None = None

    if mode == "corrected" and offset_ms is not None:
        # Validate that the correction does not overflow datetime for the first row
        from coroner.viewer.timefmt import epoch_ms_to_text as _fmt, EPOCH
        from datetime import timedelta
        try:
            # Quick check using the first and last selected rows
            for r in selected_rows[:1] + (selected_rows[-1:] if len(selected_rows) > 1 else []):
                if r.ts_ms is not None:
                    corrected = r.ts_ms - offset_ms
                    _fmt(corrected)  # will raise ValueError/OverflowError if out of range
        except (OverflowError, ValueError, OSError):
            actual_mode = "raw_uncorrected"
            actual_offset = None
            correction_unavailable_reason = "correction_out_of_range"
            # The SIGKILL marker is not drawn on a raw axis, so its x position
            # must not widen the x-axis domain or add ticks.
            sigkill_x_ms = None

    # ----- Build corrected_capture_time and x_ms per point -----
    def _corrected_text(ts_ms: int | None) -> str | None:
        if actual_mode != "corrected" or actual_offset is None or ts_ms is None:
            return None
        try:
            return epoch_ms_to_text(ts_ms - actual_offset)
        except (OverflowError, ValueError, OSError):
            return None

    def _x_ms_for(ts_ms: int | None) -> int | None:
        if ts_ms is None:
            return None
        if actual_mode == "corrected" and actual_offset is not None:
            return ts_ms - actual_offset
        return ts_ms

    # ----- Full sequence for segment styles (all valid Samples in Capture order) -----
    # "All valid Samples with a parseable Capture_Time" = rows with ts_ms is not None
    full_sequence = capture_order([r for r in rows if r.ts_ms is not None])

    # ----- Segment styles -----
    seg_data = segment_styles(selected_rows, full_sequence, gaps)

    # ----- Build chart points -----
    points: list[ChartPoint] = []
    for idx, (r, (seg_before, omitted_before, skipped_before)) in enumerate(
        zip(selected_rows, seg_data)
    ):
        x_ms = _x_ms_for(r.ts_ms)
        corrected_ct = _corrected_text(r.ts_ms)
        gap_b = idx > 0 and seg_before == "none" and _gap_before_pair(
            selected_rows[idx - 1], r, gaps
        )
        # For gap_before: re-compute from the gap index directly for accuracy
        # (segment_styles returns "none" both for gap_before and for index 0)
        if idx == 0:
            gap_b = False
        else:
            gap_b = _gap_before_pair(selected_rows[idx - 1], r, gaps)

        rss_mib = (r.rss / (1024.0 * 1024.0)) if r.rss is not None else None

        points.append(ChartPoint(
            file_pos=r.file_pos,
            seq=r.seq,
            ts=r.ts,
            ts_ms=r.ts_ms,
            recv=r.recv,
            rss_mib=rss_mib,
            x_ms=x_ms,
            corrected_capture_time=corrected_ct,
            gap_before=gap_b,
            segment_before=seg_before,
            skipped_before=skipped_before,
            omitted_before=omitted_before,
        ))

    # ----- X axis -----
    x_ms_values = [p.x_ms for p in points if p.x_ms is not None]
    x_axis_data: XAxis | None = None
    if x_ms_values or sigkill_x_ms is not None:
        x_axis_data = x_axis(x_ms_values, sigkill_x_ms)

    # ----- Gap list (from gaps; for the gaps block in the API) -----
    gap_records = _build_gap_list(gaps)

    return Chart(
        total_samples=total_samples,
        plottable_samples=plottable_samples,
        unplottable_samples=unplottable_samples,
        plotted_samples=len(points),
        partial_view=partial_view,
        chart_cap=cap,
        time_axis=actual_mode,
        correction_unavailable_reason=correction_unavailable_reason,
        x_axis_data=x_axis_data,
        y_data_max_bytes=y_data_max_bytes,
        unknown_range_drop_present=gaps.unknown_range_drop_present,
        points=points,
        gaps=gap_records,
    )


def _build_gap_list(gaps: GapIndex) -> list[dict[str, Any]]:
    """Build the ``gaps`` list for the API response from a :class:`GapIndex`."""
    result: list[dict[str, Any]] = []
    for lo, hi in gaps.seq_intervals:
        result.append({
            "gap_type": "sequence_discontinuity",
            "first_missing_seq": lo,
            "last_missing_seq": hi,
            "range_status": "known",
        })
    for _ in gaps.silence_positions:
        result.append({
            "gap_type": "silence_timeout",
            "first_missing_seq": None,
            "last_missing_seq": None,
            "range_status": None,
        })
    return result
