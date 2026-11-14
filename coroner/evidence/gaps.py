"""Sequence tracking and gap accounting (Requirements 6.1, 6.3, 6.4, 6.8 to 6.13).

Pure, deterministic functions over integers and already-parsed records; standard library only.
Nothing here reads or writes files, builds Gap_Records, assigns `gap_id` values, or repairs
anything: the Collector builds the records (task 3.5) and recovery lives elsewhere.

Rules (design, "Gap and sequence handling"):
* The Probe starts every process at sequence number 0, so a first received `seq > 0` implies the
  numbers `[0, seq - 1]` are missing (decision D6).
* A `new` Sample has a number above the highest accepted number, or is the first Sample. Only
  `new` Samples move the baseline. `duplicate` (the number was received as a Sample before) and
  `reset` (below the highest number and never received as a Sample) are anomalies: they do not
  move the baseline, are not added to the received set, and ignore the `dropped` counter. A late
  Sample that fills an earlier discontinuity is therefore a `reset` (decision D7); the
  discontinuity it fills stays recorded.
* Missing_Sample_Count is the size of the union of the ranges of sequence discontinuity gaps only.
  Probe-reported drops and silence gaps never add to it, so no number is counted twice.
* A Probe-reported drop is counted as unreconciled only for what discontinuities do not cover, or
  for its whole `dropped_count` when its range is unknown.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DROPPED_MAX", "DropEvent", "GapRecordError", "IntervalSet", "SeqEvents", "SeqRange", "SeqState",
    "classify", "derive_drop_range", "merge_ranges", "missing_sample_count", "overlap_size",
    "reconcile", "unreconciled_drops", "union_size",
]

SeqRange = tuple[int, int]  # inclusive on both ends

DROPPED_MAX = 4_294_967_295
SEQUENCE_DISCONTINUITY = "sequence_discontinuity"
PROBE_REPORTED_DROP = "probe_reported_drop"
NOTE_BELOW_ZERO = "range starts below sequence number 0"
NOTE_OVERLAP = "range overlaps received samples"
NOTE_UNKNOWN = "range unknown"
NOTE_NOT_COVERED = "range not covered by a sequence discontinuity"


class GapRecordError(ValueError):
    """A gap record that the counting functions need is malformed. Never silently skipped."""


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_range(lo: object, hi: object) -> None:
    if not (_is_int(lo) and _is_int(hi)):
        raise TypeError(f"range bounds must be integers, got {lo!r} and {hi!r}")
    if lo > hi:  # type: ignore[operator]
        raise ValueError(f"range start {lo} is greater than its end {hi}")


def _as_range(item: object) -> SeqRange:
    try:
        lo, hi = item  # type: ignore[misc]
    except (TypeError, ValueError):
        raise ValueError(f"a range must be a (first, last) pair, got {item!r}") from None
    _check_range(lo, hi)
    return (lo, hi)


class IntervalSet:
    """A set of integers kept as sorted, disjoint, non-adjacent inclusive intervals."""

    __slots__ = ("_los", "_his")
    __hash__ = None  # type: ignore[assignment]

    def __init__(self, ranges: Iterable[SeqRange] = ()) -> None:
        self._los: list[int] = []
        self._his: list[int] = []
        for lo, hi in (_as_range(r) for r in ranges):
            self.add(lo, hi)

    def add(self, lo: int, hi: int | None = None) -> None:
        """Add one number (`add(5)`) or the inclusive range `lo..hi`, merging touching intervals."""
        hi = lo if hi is None else hi
        _check_range(lo, hi)
        first = bisect_left(self._his, lo - 1)  # first interval that overlaps or touches [lo, hi]
        last = first
        new_lo, new_hi = lo, hi
        while last < len(self._los) and self._los[last] <= hi + 1:
            new_lo = min(new_lo, self._los[last])
            new_hi = max(new_hi, self._his[last])
            last += 1
        self._los[first:last] = [new_lo]
        self._his[first:last] = [new_hi]

    def __contains__(self, number: object) -> bool:
        if not _is_int(number):
            return False
        index = bisect_right(self._los, number) - 1  # type: ignore[arg-type]
        return index >= 0 and self._his[index] >= number  # type: ignore[operator]

    def intersects(self, lo: int, hi: int) -> bool:
        """True if any number in `lo..hi` is in the set. Touching without overlapping is False."""
        _check_range(lo, hi)
        index = bisect_left(self._his, lo)
        return index < len(self._los) and self._los[index] <= hi

    def ranges(self) -> tuple[SeqRange, ...]:
        return tuple(zip(self._los, self._his))

    def size(self) -> int:
        return sum(hi - lo + 1 for lo, hi in zip(self._los, self._his))

    def copy(self) -> "IntervalSet":
        other = IntervalSet()
        other._los, other._his = list(self._los), list(self._his)
        return other

    def __bool__(self) -> bool:
        return bool(self._los)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, IntervalSet) and self.ranges() == other.ranges()

    def __repr__(self) -> str:
        return f"IntervalSet({list(self.ranges())!r})"


def merge_ranges(ranges: Iterable[SeqRange]) -> list[SeqRange]:
    """Sort and merge overlapping or touching inclusive ranges. Raises on a malformed range."""
    return list(IntervalSet(ranges).ranges())


def union_size(ranges: Iterable[SeqRange]) -> int:
    """How many distinct numbers the ranges cover, each counted once."""
    return IntervalSet(ranges).size()


def overlap_size(rng: SeqRange, ranges: Iterable[SeqRange]) -> int:
    """How many numbers of `rng` are covered by `ranges`."""
    lo, hi = _as_range(rng)
    return sum(
        max(0, min(hi, mhi) - max(lo, mlo) + 1) for mlo, mhi in merge_ranges(ranges)
    )


def reconcile(new: SeqRange | None, others: Sequence[tuple[str, SeqRange | None]]) -> tuple[str, list[str]]:
    """Mark a new gap's range against other gaps from the same ingest: (reconciliation, reconciles_with).

    `reconciled`: every overlapping range is identical to `new`. `partial_overlap`: some overlapping
    range is not identical. `none`: no range, or no overlap (touching ranges do not overlap). The
    ids are those of the overlapping gaps, in input order, each once. The reader recomputes
    everything from the ranges; this marking is advisory.
    """
    if new is None:
        return "none", []
    lo, hi = _as_range(new)
    ids: list[str] = []
    identical = True
    for gap_id, other in others:
        if other is None:
            continue
        olo, ohi = _as_range(other)
        if olo > hi or ohi < lo:
            continue
        if gap_id not in ids:
            ids.append(gap_id)
        identical = identical and (olo, ohi) == (lo, hi)
    if not ids:
        return "none", []
    return ("reconciled" if identical else "partial_overlap"), ids


def _drop_range_and_note(seq: int, dropped: int, seen: IntervalSet) -> tuple[SeqRange | None, str | None]:
    if not (_is_int(dropped) and dropped >= 1):
        raise ValueError(f"dropped must be an integer of at least 1, got {dropped!r}")
    if not (_is_int(seq) and seq >= 0):
        raise ValueError(f"seq must be an integer of at least 0, got {seq!r}")
    first = seq - dropped
    if first < 0:
        return None, NOTE_BELOW_ZERO
    if seen.intersects(first, seq - 1):
        return None, NOTE_OVERLAP
    return (first, seq - 1), None


def derive_drop_range(seq: int, dropped: int, seen: IntervalSet) -> SeqRange | None:
    """The candidate range `[seq - dropped, seq - 1]`, or None when it cannot be right.

    None when it would start below 0 or contain a number that was received as a Sample (which
    happens when an acknowledgement was lost). Raises ValueError for `dropped < 1`: callers ask
    only when the Probe reported a drop.
    """
    return _drop_range_and_note(seq, dropped, seen)[0]


@dataclass
class SeqState:
    """What the tracker needs to know about one run: the highest accepted number and the received set."""

    highest: int | None = None
    seen: IntervalSet = field(default_factory=IntervalSet)

    def copy(self) -> "SeqState":
        return SeqState(self.highest, self.seen.copy())

    def apply(self, events: "SeqEvents") -> None:
        """Record a classified Sample. Call it only after the records are durable. Anomalies change nothing."""
        if events.kind != "new":
            return
        self.seen.add(events.seq)
        self.highest = events.seq if self.highest is None else max(self.highest, events.seq)


@dataclass(frozen=True)
class DropEvent:
    dropped_count: int
    range: SeqRange | None
    range_status: str  # "known" or "unknown" (M0 never derives "uncertain")
    range_note: str | None


@dataclass(frozen=True)
class SeqEvents:
    seq: int
    kind: str  # "new", "duplicate", or "reset"
    previous_highest: int | None  # the highest accepted number before this Sample (the anomaly's previous_seq)
    discontinuity: SeqRange | None  # the numbers found missing, for a `new` Sample only
    drop: DropEvent | None  # a Probe-reported drop, for a `new` Sample with dropped > 0 only

    @property
    def missing_count(self) -> int:
        return 0 if self.discontinuity is None else self.discontinuity[1] - self.discontinuity[0] + 1


def classify(state: SeqState, seq: int, dropped: int) -> SeqEvents:
    """Classify one validated Sample against `state`. Pure: `state` is not changed (see SeqState.apply)."""
    if not (_is_int(seq) and seq >= 0):
        raise ValueError(f"seq must be an integer of at least 0, got {seq!r}")
    if not (_is_int(dropped) and 0 <= dropped <= DROPPED_MAX):
        raise ValueError(f"dropped must be an integer from 0 to {DROPPED_MAX}, got {dropped!r}")
    highest = state.highest
    if highest is not None and (seq in state.seen or seq == highest):
        return SeqEvents(seq, "duplicate", highest, None, None)
    if highest is not None and seq < highest:
        return SeqEvents(seq, "reset", highest, None, None)
    expected = 0 if highest is None else highest + 1
    discontinuity = (expected, seq - 1) if seq > expected else None
    drop = None
    if dropped > 0:
        rng, note = _drop_range_and_note(seq, dropped, state.seen)
        drop = DropEvent(dropped, rng, "known" if rng is not None else "unknown", note)
    return SeqEvents(seq, "new", highest, discontinuity, drop)


def _gap_id(record: Mapping[str, Any]) -> object:
    return record.get("gap_id")


def _record_range(record: Mapping[str, Any]) -> SeqRange:
    first, last = record.get("first_missing_seq"), record.get("last_missing_seq")
    if not (_is_int(first) and _is_int(last) and 0 <= first <= last):  # type: ignore[operator]
        raise GapRecordError(
            f"gap {_gap_id(record)!r}: first_missing_seq={first!r} and last_missing_seq={last!r} "
            "are not integers with 0 <= first <= last"
        )
    return (first, last)


def _discontinuity_ranges(gap_records: Iterable[Mapping[str, Any]]) -> list[SeqRange]:
    return [
        _record_range(g)
        for g in gap_records
        if isinstance(g, Mapping) and g.get("gap_type") == SEQUENCE_DISCONTINUITY
    ]


def missing_sample_count(gap_records: Iterable[Mapping[str, Any]]) -> int:
    """Missing_Sample_Count: the union size of sequence discontinuity ranges, each number once.

    Only `sequence_discontinuity` records count; every other record (drops, silence, anything that
    is not a gap) is ignored, and a stored `missing_count` is never read. A malformed discontinuity
    record raises GapRecordError.
    """
    return union_size(_discontinuity_ranges(gap_records))


def unreconciled_drops(gap_records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The Probe-reported drops that discontinuities do not explain: one entry per contributing drop.

    Unknown or uncertain range: `dropped_count`. Known range: the numbers of the range that no
    discontinuity covers (nothing is returned when it is fully covered). Every entry has
    `uncertain: True`. Unreconciled_Drop_Count is the sum of the `count` values. Raises
    GapRecordError for a malformed drop record.
    """
    records = [g for g in gap_records if isinstance(g, Mapping)]
    covered = merge_ranges(_discontinuity_ranges(records))
    out: list[dict[str, Any]] = []
    for g in records:
        if g.get("gap_type") != PROBE_REPORTED_DROP:
            continue
        status = g.get("range_status")
        if status not in ("known", "unknown", "uncertain"):
            raise GapRecordError(f"gap {_gap_id(g)!r}: range_status {status!r} is not known, unknown, or uncertain")
        if status != "known":
            count = g.get("dropped_count")
            if not (_is_int(count) and count >= 0):
                raise GapRecordError(f"gap {_gap_id(g)!r}: dropped_count {count!r} is not an integer of at least 0")
            out.append({"gap_id": _gap_id(g), "count": count, "uncertain": True, "reason": g.get("range_note") or NOTE_UNKNOWN})
            continue
        lo, hi = _record_range(g)
        left = (hi - lo + 1) - overlap_size((lo, hi), covered)
        if left > 0:
            out.append({"gap_id": _gap_id(g), "count": left, "uncertain": True, "reason": NOTE_NOT_COVERED})
    return out