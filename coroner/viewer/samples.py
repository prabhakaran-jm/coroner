"""Sample extraction and statistics for the Coroner Viewer.

Reads ``sample`` records from a ``ReadResult``, derives plottability,
defines capture order, and computes the first/highest/last RSS statistics.

Import constraints: stdlib only, plus coroner.evidence.reader (ReadResult),
coroner.common.timeutil (parse_utc_ms).  Never imports
coroner.runner.experiments.base, coroner.runner.evaluate or any heavy M0 module.

IMPORTANT: ``summarize().last_sample_time`` is a Receive_Time string and is
returned separately; it is NEVER used as either the Last_Persisted_Sample or
the Latest_Capture_Sample.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple, Sequence

from coroner.common.timeutil import parse_utc_ms

__all__ = [
    "SampleRow",
    "RssStats",
    "extract_samples",
    "is_plottable",
    "capture_order",
    "last_persisted",
    "latest_capture",
    "rss_stats",
]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SampleRow:
    """One ``sample`` record extracted from a ``ReadResult``.

    ``file_pos`` is the index of this record in ``ReadResult.records``
    (file order, 0-based).  ``ts_ms`` is ``None`` when ``ts`` cannot be
    parsed by ``parse_utc_ms`` (defensive; the M0 reader should have
    quarantined such a line, but the code guards it anyway).  ``rss`` is
    ``None`` when the recorded ``rss_bytes`` is ``null`` or not a valid
    non-negative integer.
    """
    file_pos: int           # index in ReadResult.records (file order)
    seq: int
    ts: str                 # Original_Timestamp (Capture_Time, Container_Clock)
    ts_ms: int | None       # epoch ms from parse_utc_ms; None if unparseable
    recv: str               # Original_Timestamp (Receive_Time, Collector_Clock)
    rss: int | None         # None when null or invalid


@dataclass(frozen=True)
class RssStats:
    """First, highest and last RSS computed from the plottable samples."""
    first: "SampleRow | None"           # first in Capture order
    highest: "SampleRow | None"         # maximum rss_bytes; earliest Capture order on ties
    last: "SampleRow | None"            # last_persisted when plottable, else last plottable in file order
    basis: "str | None"                 # "last_persisted" | "last_plottable_in_file_order" | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _epoch_ms(ts: str) -> int | None:
    """Parse a strict UTC ms timestamp to epoch milliseconds; ``None`` on failure."""
    try:
        from coroner.viewer.timefmt import EPOCH  # local import to avoid circular
        from datetime import timedelta
        dt = parse_utc_ms(ts)
        return int((dt - EPOCH) / timedelta(milliseconds=1))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_samples(result: object) -> list[SampleRow]:
    """Return all ``sample`` records from *result* as :class:`SampleRow` objects.

    Records are returned in **file order** (the order they appear in
    ``ReadResult.records``, which mirrors the line order of ``evidence.jsonl``).
    ``file_pos`` is the 0-based index into ``ReadResult.records``.

    Only records whose ``record_type`` is ``"sample"`` are included.
    """
    rows: list[SampleRow] = []
    for file_pos, record in enumerate(result.records):  # type: ignore[attr-defined]
        if record.get("record_type") != "sample":
            continue
        sample = record.get("sample") or {}
        seq_raw = sample.get("seq")
        # seq must be a non-negative int (not bool)
        if not isinstance(seq_raw, int) or isinstance(seq_raw, bool) or seq_raw < 0:
            continue

        ts = sample.get("ts", "")
        if not isinstance(ts, str):
            ts = ""
        ts_ms = _epoch_ms(ts) if ts else None

        recv_raw = record.get("recv_time", "")
        recv = recv_raw if isinstance(recv_raw, str) else ""

        rss_raw = sample.get("rss_bytes")
        # rss must be a non-negative int (not bool), or None
        if isinstance(rss_raw, int) and not isinstance(rss_raw, bool) and rss_raw >= 0:
            rss: int | None = rss_raw
        else:
            rss = None

        rows.append(SampleRow(
            file_pos=file_pos,
            seq=seq_raw,
            ts=ts,
            ts_ms=ts_ms,
            recv=recv,
            rss=rss,
        ))
    return rows


def is_plottable(row: SampleRow) -> bool:
    """Return ``True`` when *row* has a valid integer ``ts_ms`` and a valid
    non-negative integer ``rss``.

    Both conditions must hold: a sample with a valid RSS but an unparseable
    Capture_Time is counted as unplottable (and never plotted), and vice versa.
    """
    return row.ts_ms is not None and row.rss is not None and row.rss >= 0


def capture_order(rows: Sequence[SampleRow]) -> list[SampleRow]:
    """Return *rows* sorted by ``(ts_ms, seq, file_pos)`` ascending.

    ``ts_ms`` is the primary key (earlier capture time first).  ``seq`` breaks
    ties within the same millisecond (the Probe increments it monotonically).
    ``file_pos`` is the final tie-break so the order is total even for
    hand-edited files.

    Rows with ``ts_ms is None`` sort **last** (Python sorts ``None > int``
    would raise, so ``None`` is replaced with a large sentinel).
    """
    _LARGE = 2 ** 53  # larger than any valid epoch_ms
    return sorted(rows, key=lambda r: (r.ts_ms if r.ts_ms is not None else _LARGE, r.seq, r.file_pos))


def last_persisted(rows: Sequence[SampleRow]) -> SampleRow | None:
    """Return the last ``sample`` record in **file order**.

    ``rows`` is assumed to already be in file order (as returned by
    :func:`extract_samples`).  Returns ``None`` when *rows* is empty.

    This is the Last_Persisted_Sample as defined in the glossary.  It is NOT
    ``summarize().last_sample_time``, which is a Receive_Time string.
    """
    if not rows:
        return None
    return rows[-1]


def latest_capture(rows: Sequence[SampleRow]) -> SampleRow | None:
    """Return the sample with the **maximum** ``ts_ms`` (Latest_Capture_Sample).

    Ties (equal ``ts_ms``) are broken by **earliest file_pos** (the first
    record with that timestamp in file order wins).  Returns ``None`` when
    *rows* is empty or all rows have ``ts_ms is None``.
    """
    best: SampleRow | None = None
    for row in rows:
        if row.ts_ms is None:
            continue
        if best is None:
            best = row
            continue
        if row.ts_ms > best.ts_ms:
            best = row
        elif row.ts_ms == best.ts_ms and row.file_pos < best.file_pos:
            best = row
    return best


def rss_stats(rows: Sequence[SampleRow]) -> RssStats:
    """Compute first, highest and last RSS from *rows*.

    *rows* must be in **file order** (as returned by :func:`extract_samples`).

    Rules (Requirement 12 criterion 8, design "Evidence assembly"):

    - **first**: the first plottable sample in Capture order.
    - **highest**: the plottable sample with the maximum ``rss``; on a tie,
      the one that appears first in Capture order.
    - **last**: the Last_Persisted_Sample when it is plottable; otherwise the
      last plottable sample in file order.  ``basis`` records which rule was
      used: ``"last_persisted"`` or ``"last_plottable_in_file_order"``.

    Returns an :class:`RssStats` with all fields ``None`` and ``basis``
    ``None`` when there are no plottable samples.
    """
    plottable = [r for r in rows if is_plottable(r)]
    if not plottable:
        return RssStats(first=None, highest=None, last=None, basis=None)

    # Capture order for first and highest
    ordered = capture_order(plottable)

    # first: first in Capture order
    first = ordered[0]

    # highest: maximum rss, earliest in Capture order on ties
    highest = ordered[0]
    for r in ordered[1:]:
        if r.rss is not None and highest.rss is not None:
            if r.rss > highest.rss:
                highest = r
            # tie: keep the one with the smaller (earlier) Capture-order position
            # since `ordered` is already in Capture order, the first one wins

    # last: last_persisted when plottable, else last plottable in file order
    last_p = last_persisted(list(rows))
    if last_p is not None and is_plottable(last_p):
        last = last_p
        basis = "last_persisted"
    else:
        # last plottable in file order: `plottable` is in file order already
        # because extract_samples preserves file order and filter preserves order
        last = plottable[-1]
        basis = "last_plottable_in_file_order"

    return RssStats(first=first, highest=highest, last=last, basis=basis)
