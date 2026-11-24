"""Clock offset resolution, axis mode, correction, ordering and derived intervals
for the Coroner Viewer.

Design rules enforced here:
- VIEWER_CLOCK_TOLERANCE_MAX_MS = 250 mirrors M0 CLOCK_TOLERANCE_MS and is
  NEVER imported from M0.  A parity test (test_tolerance_parity.py) pins them.
- tolerance_status: 'valid' only for a Python int (not bool) in [0, 250].
  The recorded value is returned UNCHANGED -- never clamped, rounded or replaced.
- ordering: returns None when tolerance_status != 'valid', when uncertainty_ms
  is None, or when abs(diff) <= max(uncertainty_ms, tolerance_ms).
- derived_intervals: Experiment A, C and unknown -> empty list, suppressed False.
  Experiment B with conflict -> suppressed True, empty list.
  Experiment B with usable offset -> sample_to_sigkill, sample_to_finished_at.
  Experiment B without correction -> finished_at_to_sigkill only (when both
  times are recorded); no sample_to_* intervals.
- Docker-target interval kinds always have ordering None, bounded False,
  reason 'no_recorded_bound_for_docker_clock'.
- Receive_Time is NEVER shifted.

Import constraints: stdlib only, plus coroner.viewer.samples (SampleRow),
coroner.viewer.timefmt (epoch_ms_to_text), coroner.viewer.model (safe_int,
safe_number).  Never imports coroner.runner.experiments.base,
coroner.runner.evaluate, or any other forbidden module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from coroner.viewer.model import safe_int, safe_number
from coroner.viewer.timefmt import epoch_ms_to_text
from coroner.viewer.samples import SampleRow

__all__ = [
    "VIEWER_CLOCK_TOLERANCE_MAX_MS",
    "ToleranceStatus",
    "OffsetResolution",
    "tolerance_status",
    "resolve_offset",
    "axis_mode",
    "corrected_ms",
    "ordering",
    "derived_intervals",
    "DerivedBlock",
    "AxisMode",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Mirrors M0 CLOCK_TOLERANCE_MS (coroner/runner/evaluate.py).
# Never imported from M0. The parity test test_tolerance_parity.py asserts
# that VIEWER_CLOCK_TOLERANCE_MAX_MS == CLOCK_TOLERANCE_MS so this copy
# cannot drift silently.
VIEWER_CLOCK_TOLERANCE_MAX_MS: int = 250

# Type aliases
ToleranceStatus = str   # "valid" | "not_recorded" | "invalid_type" | "out_of_range"
AxisMode = str          # "raw_capture" | "corrected" | "raw_uncorrected"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OffsetResolution:
    """Result of resolve_offset.

    ``offset_ms`` is the usable integer offset, or None when not recorded or
    when there is a conflict.
    ``source`` names which manifest key supplied the value: ``"sample_timing"``
    or ``"manifest"``, or None when no value is usable.
    ``conflict`` holds (sample_timing_value, manifest_value) when both keys are
    present and they disagree; None otherwise.
    """
    offset_ms: int | None
    source: str | None          # "sample_timing" | "manifest" | None
    conflict: tuple[int, int] | None   # (st_value, top_value) when conflict; else None


@dataclass
class DerivedInterval:
    """One derived time interval."""
    kind: str               # "sample_to_sigkill" | "sample_to_finished_at" | "finished_at_to_sigkill"
    sample: str | None      # "last_persisted" | "latest_capture" | None
    sample_seq: int | None
    from_time: str | None   # original timestamp string
    from_time_display: str | None
    from_clock: str         # clock code
    from_basis: str         # "corrected" | "docker_finished_at"
    from_corrected_time: str | None     # corrected display, non-None for sample_to_* kinds
    to_kind: str            # "sigkill_sent" | "docker_finished_at"
    to_time: str | None
    to_time_display: str | None
    to_clock: str           # clock code
    difference_ms: int | None
    ordering: str | None    # "before_sigkill" | "after_sigkill" | "not_established" | None
    uncertainty_ms: float | None
    tolerance_ms: Any       # the recorded value (never clamped)
    threshold_ms: float | None
    bounded: bool
    reason: str | None


@dataclass
class DerivedBlock:
    """The ``timing.derived`` block."""
    suppressed: bool
    suppressed_reason: str | None
    finished_at_source: str | None   # "manifest" | "raw_inspect" | None
    intervals: list[DerivedInterval] = field(default_factory=list)


# ---------------------------------------------------------------------------
# tolerance_status
# ---------------------------------------------------------------------------

def tolerance_status(recorded: object) -> ToleranceStatus:
    """Classify the recorded ``sample_timing.tolerance_ms`` value.

    Returns:
      ``"valid"``        – a Python int (NOT bool) in [0, VIEWER_CLOCK_TOLERANCE_MAX_MS].
      ``"not_recorded"`` – the key is absent (caller passes None) or the value is None.
      ``"invalid_type"`` – a float, bool, str, list, dict, or any non-int type.
      ``"out_of_range"`` – an int (not bool) outside [0, 250].

    The recorded value is NEVER clamped or replaced.
    """
    if recorded is None:
        return "not_recorded"
    # bool subclasses int in Python; must be excluded explicitly
    if isinstance(recorded, bool):
        return "invalid_type"
    if isinstance(recorded, int):
        if 0 <= recorded <= VIEWER_CLOCK_TOLERANCE_MAX_MS:
            return "valid"
        return "out_of_range"
    # float, str, list, dict, etc.
    return "invalid_type"


# ---------------------------------------------------------------------------
# resolve_offset (Experiment B only)
# ---------------------------------------------------------------------------

def resolve_offset(exp_type: str | None, extra: Mapping[str, Any]) -> OffsetResolution:
    """Resolve the clock offset from the manifest ``extra`` block.

    Only meaningful for Experiment B.  For A, C and unknown types returns a
    resolution with offset_ms None, source None and conflict None.

    Rules (design "Offset resolution"):
    - Read ``extra.sample_timing.clock_offset_ms`` and ``extra.clock_offset_ms``
      each through safe_int (a float or bool is "not recorded").
    - Both present and different -> conflict.
    - Both present and equal -> use the sample_timing value.
    - Exactly one present -> use it.
    - Neither present -> None.
    """
    if exp_type != "B":
        return OffsetResolution(offset_ms=None, source=None, conflict=None)

    sample_timing = extra.get("sample_timing") or {}
    if not isinstance(sample_timing, dict):
        sample_timing = {}

    st_raw = sample_timing.get("clock_offset_ms")
    top_raw = extra.get("clock_offset_ms")

    st_val = safe_int(st_raw)
    top_val = safe_int(top_raw)

    have_st = st_val is not None
    have_top = top_val is not None

    if have_st and have_top:
        if st_val != top_val:
            # Conflict: both present and different
            return OffsetResolution(
                offset_ms=None,
                source=None,
                conflict=(st_val, top_val),  # type: ignore[arg-type]
            )
        else:
            # Both present and equal: use sample_timing value
            return OffsetResolution(offset_ms=st_val, source="sample_timing", conflict=None)
    elif have_st:
        return OffsetResolution(offset_ms=st_val, source="sample_timing", conflict=None)
    elif have_top:
        return OffsetResolution(offset_ms=top_val, source="manifest", conflict=None)
    else:
        return OffsetResolution(offset_ms=None, source=None, conflict=None)


# ---------------------------------------------------------------------------
# axis_mode
# ---------------------------------------------------------------------------

def axis_mode(
    exp_type: str | None,
    res: OffsetResolution,
    tolerance_st: ToleranceStatus = "valid",  # unused here; kept for interface symmetry
) -> tuple[AxisMode, str | None]:
    """Return (axis_mode_code, correction_unavailable_reason).

    Rules (design "Per-experiment rules"):
      B with usable offset         -> "corrected",           reason None
      B with conflict              -> "raw_uncorrected",     reason "clock_offsets_conflict"
      B with no offset             -> "raw_uncorrected",     reason "offset_not_recorded"
      A, C, unknown                -> "raw_capture",         reason None
    """
    if exp_type == "B":
        if res.offset_ms is not None:
            return ("corrected", None)
        if res.conflict is not None:
            return ("raw_uncorrected", "clock_offsets_conflict")
        return ("raw_uncorrected", "offset_not_recorded")
    # A, C, None
    return ("raw_capture", None)


# ---------------------------------------------------------------------------
# corrected_ms
# ---------------------------------------------------------------------------

def corrected_ms(ts_ms: int, offset_ms: int) -> int:
    """Return ts_ms - offset_ms (the Corrected_Capture_Time in epoch ms).

    Receive_Time is NEVER shifted; this is only applied to Capture_Time.
    """
    return ts_ms - offset_ms


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------

def ordering(
    diff_ms: int | None,
    uncertainty_ms: float | None,
    tolerance_ms: int | None,
    tolerance_st: ToleranceStatus,
) -> tuple[str | None, str | None]:
    """Compute (ordering_code, reason) for a sample-to-SIGKILL interval.

    Returns (None, reason) when ordering cannot be stated.
    Returns ("not_established" | "before_sigkill" | "after_sigkill", reason).

    Rules (design pseudocode, Requirement 15):
      tolerance_st != "valid"  -> (None, "tolerance_<status>")
      uncertainty_ms is None   -> if diff_ms is also None: (None, "uncertainty_not_recorded")
                                   else: return difference but no ordering
      diff_ms is None          -> (None, "uncertainty_not_recorded")  [shouldn't happen]
      abs(diff_ms) <= max(uncertainty_ms, tolerance_ms) -> ("not_established", ...)
      diff_ms < 0              -> ("before_sigkill", None)
      diff_ms > 0              -> ("after_sigkill", None)
    """
    if tolerance_st != "valid":
        return (None, f"tolerance_{tolerance_st}")

    if uncertainty_ms is None:
        return (None, "uncertainty_not_recorded")

    if diff_ms is None:
        return (None, "uncertainty_not_recorded")

    tol = tolerance_ms if (tolerance_ms is not None and tolerance_st == "valid") else 0
    threshold = max(float(uncertainty_ms), float(tol))

    if abs(diff_ms) <= threshold:
        return ("not_established", "within_uncertainty_and_tolerance")

    if diff_ms < 0:
        return ("before_sigkill", None)
    return ("after_sigkill", None)


def _threshold_ms(
    uncertainty_ms: float | None,
    tolerance_ms: int | None,
    tolerance_st: ToleranceStatus,
) -> float | None:
    """Return max(uncertainty_ms, tolerance_ms) or None when inputs are unavailable."""
    if uncertainty_ms is None or tolerance_st != "valid" or tolerance_ms is None:
        return None
    return max(float(uncertainty_ms), float(tolerance_ms))


# ---------------------------------------------------------------------------
# derived_intervals
# ---------------------------------------------------------------------------

def derived_intervals(
    exp_type: str | None,
    res: OffsetResolution,
    last_persisted_row: "SampleRow | None",
    latest_capture_row: "SampleRow | None",
    sigkill_ms: int | None,
    finished_at_ms: int | None,
    uncertainty_ms: float | None,
    tolerance_ms_val: Any,          # the recorded tolerance value (never clamped)
    tolerance_st: ToleranceStatus,
    finished_at_source: str | None,
) -> DerivedBlock:
    """Compute the DerivedBlock for the ``timing.derived`` API block.

    Rules (R-C, R-E resolved; design "Derived_Intervals"):
    - Experiment A, C and unknown -> intervals empty, suppressed False.
    - Experiment B with conflict  -> suppressed True, intervals empty.
    - Experiment B with usable offset ->
        * sample_to_sigkill for last_persisted and latest_capture
          (when each sample and sigkill_ms are recorded)
        * sample_to_finished_at for last_persisted and latest_capture
          (when each sample and finished_at_ms are recorded; correction required)
    - Experiment B without offset (no conflict, offset None) ->
        * finished_at_to_sigkill only (when both times are recorded)
        * NO sample_to_* intervals
    - Docker-target interval kinds always have ordering None, bounded False,
      reason 'no_recorded_bound_for_docker_clock'.
    """
    # R-C: A, C, unknown -> empty block
    if exp_type != "B":
        return DerivedBlock(
            suppressed=False,
            suppressed_reason=None,
            finished_at_source=finished_at_source,
            intervals=[],
        )

    # B with conflict -> suppressed
    if res.conflict is not None:
        return DerivedBlock(
            suppressed=True,
            suppressed_reason="clock_offsets_conflict",
            finished_at_source=finished_at_source,
            intervals=[],
        )

    intervals: list[DerivedInterval] = []

    # tolerance_ms for the interval is the recorded value (never clamped)
    # We pass it through for display but use safe_int for the threshold calculation
    tol_int = safe_int(tolerance_ms_val)  # None if not valid

    if res.offset_ms is not None:
        # B with usable offset: sample_to_sigkill and sample_to_finished_at
        for sample_label, sample_row in [
            ("last_persisted", last_persisted_row),
            ("latest_capture", latest_capture_row),
        ]:
            if sample_row is None or sample_row.ts_ms is None:
                continue

            corrected = corrected_ms(sample_row.ts_ms, res.offset_ms)
            try:
                corrected_display = epoch_ms_to_text(corrected)
            except (OverflowError, ValueError, OSError):
                corrected_display = None

            # sample_to_sigkill
            if sigkill_ms is not None:
                # Req 15.19 / Property 10: no uncertainty recorded -> no difference, no ordering
                diff = corrected - sigkill_ms if uncertainty_ms is not None else None
                ord_code, ord_reason = ordering(diff, uncertainty_ms, tol_int, tolerance_st)
                if uncertainty_ms is None:
                    ord_code, ord_reason = None, "uncertainty_not_recorded"
                thr = _threshold_ms(uncertainty_ms, tol_int, tolerance_st)
                intervals.append(DerivedInterval(
                    kind="sample_to_sigkill",
                    sample=sample_label,
                    sample_seq=sample_row.seq,
                    from_time=sample_row.ts,
                    from_time_display=epoch_ms_to_text(sample_row.ts_ms) if sample_row.ts_ms is not None else None,
                    from_clock="container",
                    from_basis="corrected",
                    from_corrected_time=corrected_display,
                    to_kind="sigkill_sent",
                    to_time=None,       # caller supplies the display from timing block
                    to_time_display=None,
                    to_clock="host",
                    difference_ms=diff,
                    ordering=ord_code,
                    uncertainty_ms=uncertainty_ms,
                    tolerance_ms=tolerance_ms_val,
                    threshold_ms=thr,
                    bounded=(ord_code is not None and ord_code != "not_established"),
                    reason=ord_reason,
                ))

            # sample_to_finished_at (Docker-target; ordering always None)
            if finished_at_ms is not None:
                diff_fat = corrected - finished_at_ms
                try:
                    fat_display = epoch_ms_to_text(finished_at_ms)
                except (OverflowError, ValueError, OSError):
                    fat_display = None
                intervals.append(DerivedInterval(
                    kind="sample_to_finished_at",
                    sample=sample_label,
                    sample_seq=sample_row.seq,
                    from_time=sample_row.ts,
                    from_time_display=epoch_ms_to_text(sample_row.ts_ms) if sample_row.ts_ms is not None else None,
                    from_clock="container",
                    from_basis="corrected",
                    from_corrected_time=corrected_display,
                    to_kind="docker_finished_at",
                    to_time=None,
                    to_time_display=fat_display,
                    to_clock="docker",
                    difference_ms=diff_fat,
                    ordering=None,
                    uncertainty_ms=uncertainty_ms,
                    tolerance_ms=tolerance_ms_val,
                    threshold_ms=None,
                    bounded=False,
                    reason="no_recorded_bound_for_docker_clock",
                ))

    else:
        # B without correction: only finished_at_to_sigkill (when both times recorded)
        if finished_at_ms is not None and sigkill_ms is not None:
            diff_fts = finished_at_ms - sigkill_ms
            try:
                fat_display = epoch_ms_to_text(finished_at_ms)
            except (OverflowError, ValueError, OSError):
                fat_display = None
            intervals.append(DerivedInterval(
                kind="finished_at_to_sigkill",
                sample=None,
                sample_seq=None,
                from_time=None,
                from_time_display=fat_display,
                from_clock="docker",
                from_basis="docker_finished_at",
                from_corrected_time=None,
                to_kind="sigkill_sent",
                to_time=None,
                to_time_display=None,
                to_clock="host",
                difference_ms=diff_fts,
                ordering=None,
                uncertainty_ms=uncertainty_ms,
                tolerance_ms=tolerance_ms_val,
                threshold_ms=None,
                bounded=False,
                reason="no_recorded_bound_for_docker_clock",
            ))

    return DerivedBlock(
        suppressed=False,
        suppressed_reason=None,
        finished_at_source=finished_at_source,
        intervals=intervals,
    )
