"""Property 12: Gap accounting counts each number once, from discontinuities only (task 2.9).

*For any* collection of gap records (sequence discontinuity, Probe-reported drop with known,
uncertain, or unknown range, silence timeout), in any order and with any record duplicated, the
reported Missing_Sample_Count equals the size of the set union of the integer ranges of the
sequence discontinuity records only, computed independently by brute-force set union. Fully and
partially overlapping discontinuity ranges count each number once, Probe-reported drops and silence
timeouts contribute zero to the Missing_Sample_Count, and a known-range drop that lies entirely
inside the discontinuity union adds zero to the Unreconciled_Drop_Count. The Unreconciled_Drop_Count
equals, summed over Probe-reported drops, the `dropped_count` of each drop with an unknown or
uncertain range plus the number of numbers in each known range that are outside the discontinuity
union, and the uncertainty flag is set whenever any such drop exists. In all cases, the numbers the
Missing_Sample_Count covers and the numbers the Unreconciled_Drop_Count covers from known ranges
are disjoint (no sequence number is counted twice).

**Validates: Requirements 6.11, 6.12, 6.13, 13.12**

Oracles are plain Python sets, so they share no code with `IntervalSet`, `merge_ranges`,
`union_size`, or `overlap_size`. Wide numbers (up to 10**12) are never enumerated: those tests use a
translation check (shifting every number by a constant must not change any count) against the same
set oracle on the small domain, plus a hand-written sweep oracle.

Unreconciled_Drop_Count is the sum of the `count` values of `unreconciled_drops` (design pseudocode);
the uncertainty flag is "any entry exists", because every entry carries `uncertain: True`.
"""
from __future__ import annotations

import copy

import pytest
from hypothesis import assume, example, given, settings
from hypothesis import strategies as st

from coroner.evidence.gaps import (
    GapRecordError,
    merge_ranges,
    missing_sample_count,
    overlap_size,
    unreconciled_drops,
    union_size,
)

SETTINGS = dict(max_examples=500, deadline=None, derandomize=True, database=None)
NOTE_NOT_COVERED = "range not covered by a sequence discontinuity"  # design pseudocode text


# --- record constructors (test-local; the Collector's record builder is task 3.5) -------


def disc(first: int, last: int, gap_id: str = "d", **extra: object) -> dict:
    return {"record_type": "gap", "gap_id": gap_id, "gap_type": "sequence_discontinuity",
            "first_missing_seq": first, "last_missing_seq": last, **extra}


def drop(first: int | None, last: int | None, count: int, status: str = "known", gap_id: str = "p", **extra: object) -> dict:
    return {"record_type": "gap", "gap_id": gap_id, "gap_type": "probe_reported_drop", "dropped_count": count,
            "range_status": status, "first_missing_seq": first, "last_missing_seq": last, **extra}


def silence(gap_id: str = "s", **extra: object) -> dict:
    return {"record_type": "gap", "gap_id": gap_id, "gap_type": "silence_timeout", "sample_count": "unknown", **extra}


# --- the independent oracle ---------------------------------------------------------------


def span(first: int, last: int) -> set[int]:
    return set(range(first, last + 1))


def oracle(records: list[dict]):
    """(missing_count, union, [(gap_id, count, reason)...], flag, per-drop uncovered sets) by brute force."""
    union: set[int] = set()
    for r in records:
        if r.get("gap_type") == "sequence_discontinuity":
            union |= span(r["first_missing_seq"], r["last_missing_seq"])
    entries, uncovered_sets = [], []
    for r in records:
        if r.get("gap_type") != "probe_reported_drop":
            continue
        if r["range_status"] in ("unknown", "uncertain"):
            entries.append((r["gap_id"], r["dropped_count"], r.get("range_note") or "range unknown"))
        else:
            left = span(r["first_missing_seq"], r["last_missing_seq"]) - union
            uncovered_sets.append(left)
            if left:
                entries.append((r["gap_id"], len(left), NOTE_NOT_COVERED))
    return len(union), union, entries, bool(entries), uncovered_sets


def check_against_oracle(records: list[dict]) -> None:
    want_missing, union, want_entries, want_flag, uncovered_sets = oracle(records)
    assert missing_sample_count(records) == want_missing
    got = unreconciled_drops(records)
    assert [(e["gap_id"], e["count"], e["reason"]) for e in got] == want_entries
    assert all(e["uncertain"] is True and set(e) == {"gap_id", "count", "uncertain", "reason"} for e in got)
    assert bool(got) == want_flag  # the uncertainty flag is set exactly when some drop contributes
    assert all(e["uncertain"] for e in got)
    total = sum(e["count"] for e in got)
    assert total == sum(c for _, c, _ in want_entries)
    # no number is counted twice: what Unreconciled counts from known ranges is outside what Missing counts
    for left in uncovered_sets:
        assert not (left & union)


# --- strategies ---------------------------------------------------------------------------

RANGE = st.tuples(st.integers(0, 14), st.integers(0, 14)).map(lambda p: (min(p), max(p)))
POOL = st.lists(RANGE, min_size=1, max_size=4)  # a small pool makes overlap, containment, and duplicates common


@st.composite
def gap_records(draw, max_size: int = 8):
    pool = draw(POOL)
    count = draw(st.integers(0, max_size))
    out = []
    for n in range(count):
        kind = draw(st.sampled_from(["disc", "disc", "known", "known", "unknown", "uncertain", "silence", "other"]))
        first, last = draw(st.sampled_from(pool))
        gap_id = f"gap-{n + 1:04d}"
        if kind == "disc":
            extra = draw(st.sampled_from([{}, {"missing_count": 0}, {"missing_count": 999}, {"missing_count": last - first + 1}]))
            out.append(disc(first, last, gap_id, **extra))
        elif kind == "known":
            out.append(drop(first, last, draw(st.integers(1, 20)), "known", gap_id))
        elif kind in ("unknown", "uncertain"):
            note = draw(st.sampled_from([{}, {"range_note": "range overlaps received samples"}]))
            out.append(drop(None, None, draw(st.integers(1, 9)), kind, gap_id, **note))
        elif kind == "silence":
            out.append(silence(gap_id, last_sample_time="x", detected_time=draw(st.sampled_from(["y", 5, None]))))
        else:
            out.append({"record_type": draw(st.sampled_from(["sample", "repair", "rejection"])), "seq": first, "gap_type": None})
    return out


@st.composite
def duplicated_and_shuffled(draw):
    records = draw(gap_records())
    extra = [copy.deepcopy(draw(st.sampled_from(records))) for _ in range(draw(st.integers(0, 3)))] if records else []
    combined = records + extra
    return draw(st.permutations(combined)) if combined else []


# --- Property 12 ---------------------------------------------------------------------------


@settings(**SETTINGS)
@given(gap_records())
@example([])
@example([disc(13, 15), drop(13, 15, 3)])  # (a)
@example([disc(13, 15), drop(14, 17, 4)])  # (b)
@example([disc(13, 15), drop(None, None, 4, "unknown")])  # (c)
@example([silence()])  # (d)
@example([disc(13, 15), disc(13, 15)])  # (e)
@example([drop(0, 0, 1)])
@example([disc(0, 0), drop(0, 0, 1), disc(1, 1), drop(1, 2, 2)])
def test_property_12_counts_agree_with_the_brute_force_oracle(records: list) -> None:
    snapshot = copy.deepcopy(records)
    check_against_oracle(records)
    assert records == snapshot  # non-mutation


@settings(**SETTINGS)
@given(duplicated_and_shuffled())
def test_property_12_any_order_and_any_duplication(records: list) -> None:
    """Missing_Sample_Count is the union, so order and duplicated records cannot change it; the sums follow the oracle."""
    check_against_oracle(records)
    assert missing_sample_count(records) == missing_sample_count(sorted(records, key=lambda r: repr(sorted(r.items(), key=str))))
    unique_discs = {(r["first_missing_seq"], r["last_missing_seq"]) for r in records if r.get("gap_type") == "sequence_discontinuity"}
    brute: set[int] = set()
    for first, last in unique_discs:
        brute |= span(first, last)
    assert missing_sample_count(records) == len(brute)


@settings(**SETTINGS)
@given(gap_records(), st.data())
def test_property_12_results_do_not_depend_on_record_order(records: list, data: st.DataObject) -> None:
    shuffled = data.draw(st.permutations(records)) if records else []
    assert missing_sample_count(shuffled) == missing_sample_count(records)
    key = lambda e: (e["gap_id"], e["count"], e["reason"])  # noqa: E731
    assert sorted(unreconciled_drops(shuffled), key=key) == sorted(unreconciled_drops(records), key=key)


@settings(**SETTINGS)
@given(gap_records())
def test_property_12_only_discontinuities_feed_missing_and_only_drops_feed_unreconciled(records: list) -> None:
    discs = [r for r in records if r.get("gap_type") == "sequence_discontinuity"]
    drops = [r for r in records if r.get("gap_type") == "probe_reported_drop"]
    rest = [r for r in records if r not in discs and r not in drops]
    assert missing_sample_count(discs) == missing_sample_count(records)
    assert missing_sample_count(drops + rest) == 0  # drops, silence, and non-gap records add nothing
    assert unreconciled_drops(discs + rest) == []
    # dropping everything that is not a drop or a discontinuity changes nothing
    assert unreconciled_drops(drops + discs) == unreconciled_drops(records)
    # removing the discontinuities can only raise the unreconciled total (they explain numbers, never add)
    assert sum(e["count"] for e in unreconciled_drops(drops)) >= sum(e["count"] for e in unreconciled_drops(drops + discs))


@settings(**SETTINGS)
@given(gap_records())
def test_property_12_the_stored_missing_count_is_never_read(records: list) -> None:
    changed = copy.deepcopy(records)
    for r in changed:
        r["missing_count"] = -7  # a wrong value on every record, including drops (which must never carry one)
    assert missing_sample_count(changed) == missing_sample_count(records)
    assert unreconciled_drops(changed) == unreconciled_drops(records)
    for r in changed:
        r.pop("missing_count")
    assert missing_sample_count(changed) == missing_sample_count(records)


@settings(**SETTINGS)
@given(gap_records())
def test_property_12_helper_functions_agree_with_brute_force_and_with_the_counts(records: list) -> None:
    """The relationship between missing_sample_count, unreconciled_drops, union_size, and overlap_size."""
    _, union, _, _, _ = oracle(records)
    disc_ranges = [(r["first_missing_seq"], r["last_missing_seq"]) for r in records if r.get("gap_type") == "sequence_discontinuity"]
    assert union_size(disc_ranges) == len(union) == missing_sample_count(records)
    merged = merge_ranges(disc_ranges)
    assert sum(hi - lo + 1 for lo, hi in merged) == len(union)
    assert all(a[1] + 1 < b[0] for a, b in zip(merged, merged[1:]))  # sorted, disjoint, not even touching
    entries = {e["gap_id"]: e for e in unreconciled_drops(records)}
    for r in records:
        if r.get("gap_type") == "probe_reported_drop" and r["range_status"] == "known":
            lo, hi = r["first_missing_seq"], r["last_missing_seq"]
            assert overlap_size((lo, hi), disc_ranges) == len(span(lo, hi) & union)
            left = (hi - lo + 1) - overlap_size((lo, hi), disc_ranges)
            assert (entries[r["gap_id"]]["count"] if r["gap_id"] in entries else 0) == left
            if span(lo, hi) <= union:
                assert r["gap_id"] not in entries  # entirely inside the discontinuity union: adds nothing


@settings(**SETTINGS)
@given(gap_records())
def test_property_12_known_drop_numbers_outside_the_union_never_overlap_the_missing_numbers(records: list) -> None:
    _, union, _, _, uncovered_sets = oracle(records)
    counted_by_unreconciled = set().union(*uncovered_sets) if uncovered_sets else set()
    assert not (counted_by_unreconciled & union)
    # together they cover exactly the union of everything the sequence discontinuities and known drops name
    named = set(union)
    for r in records:
        if r.get("gap_type") == "probe_reported_drop" and r["range_status"] == "known":
            named |= span(r["first_missing_seq"], r["last_missing_seq"])
    assert len(union) + len(counted_by_unreconciled) == len(named)


@settings(**SETTINGS)
@given(gap_records(), st.sampled_from([1, 10**6, 10**12, 2**62]))
def test_property_12_results_survive_translation_so_wide_numbers_need_no_enumeration(records: list, offset: int) -> None:
    def shift(r: dict) -> dict:
        r = dict(r)
        for key in ("first_missing_seq", "last_missing_seq"):
            if isinstance(r.get(key), int):
                r[key] += offset
        return r

    moved = [shift(r) for r in records]
    assert missing_sample_count(moved) == missing_sample_count(records)
    key = lambda e: (e["gap_id"], e["count"], e["reason"])  # noqa: E731
    assert [key(e) for e in unreconciled_drops(moved)] == [key(e) for e in unreconciled_drops(records)]


def sweep_union_size(ranges: list[tuple[int, int]]) -> int:
    """A hand-written sweep, independent of IntervalSet, for ranges too wide to enumerate."""
    total, reach = 0, None
    for lo, hi in sorted(ranges):
        if reach is None or lo > reach:
            total += hi - lo + 1
            reach = hi
        elif hi > reach:
            total += hi - reach
            reach = hi
    return total


WIDE_RANGE = st.tuples(st.integers(0, 10**12), st.integers(0, 10**12)).map(lambda p: (min(p), max(p)))


@settings(**SETTINGS)
@given(st.lists(WIDE_RANGE, max_size=8), WIDE_RANGE)
def test_property_12_wide_ranges_match_an_independent_sweep(ranges: list, probe: tuple) -> None:
    records = [disc(lo, hi, f"d{n}") for n, (lo, hi) in enumerate(ranges)]
    assert missing_sample_count(records) == union_size(ranges) == sweep_union_size(ranges)
    inside = sweep_union_size(ranges + [probe]) - sweep_union_size(ranges)  # numbers of `probe` outside the union
    entries = unreconciled_drops(records + [drop(probe[0], probe[1], probe[1] - probe[0] + 1, "known", "p")])
    assert sum(e["count"] for e in entries) == inside
    assert (probe[1] - probe[0] + 1) - overlap_size(probe, ranges) == inside


# --- malformed records raise instead of being ignored --------------------------------------

BREAKERS = [
    ("first after last", lambda r: r.update(first_missing_seq=r["last_missing_seq"] + 1)),
    ("negative first", lambda r: r.update(first_missing_seq=-1)),
    ("float first", lambda r: r.update(first_missing_seq=1.5)),
    ("string last", lambda r: r.update(last_missing_seq="9")),
    ("boolean first", lambda r: r.update(first_missing_seq=True)),
    ("none last", lambda r: r.update(last_missing_seq=None)),
    ("missing first", lambda r: r.pop("first_missing_seq")),
    ("missing last", lambda r: r.pop("last_missing_seq")),
]
DROP_BREAKERS = [
    ("status not allowed", lambda r: r.update(range_status="maybe")),
    ("status missing", lambda r: r.pop("range_status")),
    ("status none", lambda r: r.update(range_status=None)),
]
UNKNOWN_DROP_BREAKERS = [
    ("negative count", lambda r: r.update(dropped_count=-1)),
    ("string count", lambda r: r.update(dropped_count="4")),
    ("boolean count", lambda r: r.update(dropped_count=True)),
    ("missing count", lambda r: r.pop("dropped_count")),
]


@settings(**SETTINGS)
@given(gap_records(), st.data())
def test_property_12_a_malformed_record_raises_gap_record_error_naming_it(records: list, data: st.DataObject) -> None:
    candidates = [
        (i, r) for i, r in enumerate(records)
        if r.get("gap_type") == "sequence_discontinuity" or (r.get("gap_type") == "probe_reported_drop")
    ]
    assume(bool(candidates))  # nothing to break in a collection without discontinuities or drops
    index, target = data.draw(st.sampled_from(candidates))
    if target["gap_type"] == "sequence_discontinuity" or target["range_status"] == "known":
        options = BREAKERS + (DROP_BREAKERS if target["gap_type"] == "probe_reported_drop" else [])
    else:
        options = DROP_BREAKERS + UNKNOWN_DROP_BREAKERS
    _name, breaker = data.draw(st.sampled_from(options))
    broken = copy.deepcopy(records)
    breaker(broken[index])
    expected_id = broken[index]["gap_id"]
    # a discontinuity record is read by both functions; a drop record only by unreconciled_drops
    raised = False
    for call in (missing_sample_count, unreconciled_drops):
        try:
            call(broken)
        except GapRecordError as exc:
            raised = True
            assert f"gap {expected_id!r}" in str(exc)
    assert raised, "a malformed gap record was silently accepted"


@settings(**SETTINGS)
@given(gap_records(), st.lists(st.dictionaries(st.sampled_from(["gap_type", "x", "first_missing_seq", "range_status"]), st.one_of(st.none(), st.integers(), st.text(max_size=5)), max_size=3), max_size=4))
def test_property_12_records_that_are_not_discontinuities_or_drops_are_ignored_whatever_they_contain(records: list, junk: list) -> None:
    safe = [j for j in junk if j.get("gap_type") not in ("sequence_discontinuity", "probe_reported_drop")]
    assert missing_sample_count(records + safe) == missing_sample_count(records)
    assert unreconciled_drops(records + safe) == unreconciled_drops(records)


@pytest.mark.parametrize("call", [missing_sample_count, unreconciled_drops])
def test_property_12_empty_input(call) -> None:
    assert call([]) in (0, [])
    assert call(iter([])) in (0, [])


# --- the five worked examples, expected results exactly as in the design -------------------


def counts(records: list[dict]) -> tuple[int, int, bool]:
    entries = unreconciled_drops(records)
    return missing_sample_count(records), sum(e["count"] for e in entries), bool(entries) and all(e["uncertain"] for e in entries)


def test_example_a_overlap_discontinuity_and_identical_drop() -> None:
    """(a) discontinuity [13,15] plus drop [13,15]: Missing=3, Unreconciled=0."""
    assert counts([disc(13, 15), drop(13, 15, 3)]) == (3, 0, False)
    assert unreconciled_drops([disc(13, 15), drop(13, 15, 3)]) == []


def test_example_b_partial_overlap() -> None:
    """(b) discontinuity [13,15] plus drop [14,17]: Missing=3, Unreconciled=2 (numbers 16 and 17), flag set."""
    records = [disc(13, 15), drop(14, 17, 4, gap_id="p")]
    assert counts(records) == (3, 2, True)
    assert unreconciled_drops(records) == [{"gap_id": "p", "count": 2, "uncertain": True, "reason": NOTE_NOT_COVERED}]
    assert span(14, 17) - span(13, 15) == {16, 17}


def test_example_c_unknown_range_counts_the_whole_reported_drop() -> None:
    """(c) a drop with range_status unknown and dropped_count 4: Missing unchanged, Unreconciled=4, flag set."""
    base = [disc(13, 15)]
    with_drop = base + [drop(None, None, 4, "unknown", "p", range_note="range overlaps received samples")]
    assert missing_sample_count(with_drop) == missing_sample_count(base) == 3
    assert counts(with_drop) == (3, 4, True)
    assert counts([drop(None, None, 4, "unknown")]) == (0, 4, True)
    assert counts([drop(None, None, 4, "uncertain")]) == (0, 4, True)


def test_example_d_silence_timeout_adds_nothing_to_either_count() -> None:
    """(d) a silence timeout adds nothing to either count. Its sample_count stays the string `unknown` (the
    reader prints the interval; that is task 2.11)."""
    gap = silence("s", last_sample_time="2025-01-01T12:00:10.530Z", detected_time="2025-01-01T12:00:12.081Z", threshold_ms=1500, interval_ms=500)
    assert counts([gap]) == (0, 0, False)
    assert counts([disc(13, 15), gap, drop(13, 15, 3)]) == counts([disc(13, 15), drop(13, 15, 3)]) == (3, 0, False)
    assert gap["sample_count"] == "unknown"


def test_example_e_a_duplicated_discontinuity_counts_once() -> None:
    """(e) the same discontinuity stored twice counts once."""
    assert counts([disc(13, 15, "a"), disc(13, 15, "b")]) == (3, 0, False)
    assert missing_sample_count([disc(13, 15, "a")] * 3) == 3


# --- accounting properties that are also stated, not left implicit --------------------------


def test_a_duplicated_drop_record_is_counted_once_per_record() -> None:
    """Unreconciled_Drop_Count sums over drop records, so a stored duplicate would double count; the counting
    function does not deduplicate (the Collector's recovery matches drops by semantic identity, task 3.5)."""
    one = [drop(14, 17, 4, "known", "p1")]
    two = one + [drop(14, 17, 4, "known", "p2")]
    assert sum(e["count"] for e in unreconciled_drops(one)) == 4
    assert sum(e["count"] for e in unreconciled_drops(two)) == 8
    assert missing_sample_count(one) == missing_sample_count(two) == 0


def test_an_unknown_drop_with_a_zero_count_still_sets_the_flag() -> None:
    entries = unreconciled_drops([drop(None, None, 0, "unknown")])
    assert [(e["count"], e["uncertain"]) for e in entries] == [(0, True)]


def test_boundaries_single_numbers_and_adjacent_ranges() -> None:
    assert counts([disc(0, 0)]) == (1, 0, False)
    assert counts([disc(0, 0), disc(1, 1)]) == (2, 0, False)  # adjacent
    assert counts([disc(0, 0), disc(2, 2)]) == (2, 0, False)  # a gap between them
    assert counts([disc(5, 9), drop(0, 4, 5)]) == (5, 5, True)  # a drop just below the union
    assert counts([disc(5, 9), drop(10, 14, 5)]) == (5, 5, True)  # just above
    assert counts([disc(5, 9), drop(9, 9, 1)]) == (5, 0, False)  # the last number of the union
    assert counts([disc(5, 9), drop(4, 5, 2)]) == (5, 1, True)
    assert counts([disc(2**62, 2**62 + 9), drop(2**62 + 5, 2**62 + 12, 8)]) == (10, 3, True)