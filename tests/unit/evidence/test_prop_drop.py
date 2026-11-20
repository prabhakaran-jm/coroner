"""Property 11: Probe-reported drop ranges and reconciliation fields agree with the ranges (task 2.8).

*For any* Sample sequence number, dropped counter, and set of already received numbers, the derived
drop range is `[seq - dropped, seq - 1]` with `range_status` `known` when that range is non-negative
and disjoint from received numbers, and null with `range_status` `unknown` otherwise, and the drop
record always carries `dropped_count` and never carries a `missing_count`. For any set of gap
records created by one ingest, a record's `reconciliation` is `reconciled` when another gap in the
set has an identical range, `partial_overlap` when ranges overlap but are not identical, `none`
otherwise, and `not_applicable` for silence timeouts, and `reconciles_with` lists exactly the
overlapping gap identifiers.

**Validates: Requirements 6.3, 6.8, 6.9, 6.10, 6.11**

Scope. These tests exercise the existing gap algorithms: `derive_drop_range`, `classify` (its
`DropEvent`), and `reconcile`. Two clauses of the property concern the Collector's record builder
(task 3.5), which does not exist yet and is not invented here: the stored drop record never contains
a `missing_count` key, and a silence timeout gets `not_applicable`. What can be tested now is that
the algorithm outputs carry `dropped_count` and no `missing_count`, and that `reconcile` is never
asked about, and never invents, a silence status.

Oracles are plain Python sets (a set of received numbers; each range as a set of integers). They
share no code with `IntervalSet` or `gaps.py`.
"""
from __future__ import annotations

import dataclasses

from hypothesis import example, given, settings
from hypothesis import strategies as st

from coroner.evidence.gaps import (
    DropEvent,
    IntervalSet,
    SeqState,
    classify,
    derive_drop_range,
    reconcile,
    unreconciled_drops,
)

SETTINGS = dict(max_examples=500, deadline=None, derandomize=True, database=None)
DESIGN_OVERLAP_NOTE = "range overlaps received samples"  # quoted in the design, "Gap and sequence handling"

# --- independent oracles ------------------------------------------------------------


def oracle_drop_range(seq: int, dropped: int, received: set[int]):
    """(range or None, why) straight from the property text, using sets."""
    first = seq - dropped
    if first < 0:
        return None, "below_zero"
    if set(range(first, seq)) & received:
        return None, "overlap"
    return (first, seq - 1), None


def span(rng: tuple[int, int]) -> set[int]:
    return set(range(rng[0], rng[1] + 1))


def oracle_overlapping_ids(mine, others):
    return [gap_id for gap_id, other in others if mine is not None and other is not None and span(mine) & span(other)]


# --- strategies ---------------------------------------------------------------------

RANGE = st.tuples(st.integers(0, 9), st.integers(0, 9)).map(lambda p: (min(p), max(p)))
RECEIVED_ADDS = st.lists(st.tuples(st.integers(0, 40), st.integers(0, 40)).map(lambda p: (min(p), max(p))), max_size=6)
DROPPED = st.one_of(st.integers(1, 35), st.sampled_from([4294967295]))


def make_seen(adds: list[tuple[int, int]]) -> tuple[IntervalSet, set[int]]:
    seen, model = IntervalSet(), set()
    for lo, hi in adds:
        seen.add(lo, hi)
        model |= set(range(lo, hi + 1))
    return seen, model


# --- Property 11, part 1: the derived drop range -------------------------------------


@settings(**SETTINGS)
@given(st.integers(0, 30), DROPPED, RECEIVED_ADDS)
@example(5, 5, [])  # starts exactly at 0: known
@example(5, 6, [])  # one below zero: unknown
@example(0, 1, [])  # seq 0 can never have a drop
@example(6, 3, [(2, 2)])  # received number adjacent below the range: still known
@example(6, 3, [(5, 5)])  # received number is the last number of the range: unknown
@example(6, 3, [(3, 3)])  # received number is the first number of the range: unknown
@example(6, 3, [(0, 2)])  # received numbers end just below the range: known
@example(6, 3, [(6, 40)])  # received numbers at or above seq never intersect: known
@example(12, 3, [(0, 12)])  # ack-lost style overlap
@example(3, 4294967295, [])  # the largest counter value
def test_property_11_derived_drop_range_matches_the_oracle(seq: int, dropped: int, adds: list) -> None:
    seen, received = make_seen(adds)
    before = seen.ranges()
    got = derive_drop_range(seq, dropped, seen)
    want, _why = oracle_drop_range(seq, dropped, received)
    assert got == want
    if got is not None:
        assert got == (seq - dropped, seq - 1) and got[1] - got[0] + 1 == dropped
        assert got[0] >= 0 and not (set(range(got[0], got[1] + 1)) & received)
    assert seen.ranges() == before  # the received set is not modified
    assert derive_drop_range(seq, dropped, seen) == got  # deterministic


# --- Property 11, part 1 through classify: range_status, dropped_count, no missing_count ---


@settings(**SETTINGS)
@given(st.lists(st.tuples(st.integers(0, 9), st.sampled_from([0, 0, 0, 1, 2, 3, 6, 10])), max_size=25))
@example([(0, 0)])
@example([(5, 5)])
@example([(5, 6)])
@example([(0, 0), (1, 0), (4, 3)])  # discontinuity 2..3 with a drop 1..3 that touches the received 1
@example([(0, 0), (1, 0), (4, 2)])  # discontinuity 2..3, drop 2..3 identical
def test_property_11_classified_drop_events_agree_with_the_oracle(stream: list) -> None:
    state, accepted, highest = SeqState(), set(), None
    for seq, dropped in stream:
        snapshot = (state.highest, state.seen.ranges())
        event = classify(state, seq, dropped)
        assert (state.highest, state.seen.ranges()) == snapshot  # classify does not mutate the state
        is_new = seq not in accepted and (highest is None or seq > highest)
        if is_new and dropped > 0:
            want, why = oracle_drop_range(seq, dropped, accepted)
            drop = event.drop
            assert drop is not None
            assert drop.dropped_count == dropped  # always carried, known or unknown
            assert drop.range == want
            assert drop.range_status == ("known" if want is not None else "unknown")
            if want is not None:
                assert drop.range_note is None
            elif why == "overlap":
                assert drop.range_note == DESIGN_OVERLAP_NOTE
            else:
                assert isinstance(drop.range_note, str) and "below" in drop.range_note and "0" in drop.range_note
        else:
            assert event.drop is None  # no drop without a `new` Sample reporting one
        state.apply(event)
        if is_new:
            accepted.add(seq)
            highest = seq


def test_a_drop_event_carries_dropped_count_and_has_no_missing_count_field() -> None:
    """The existing algorithms only. The stored drop record's key set is the record builder's (task 3.5)."""
    fields = {f.name for f in dataclasses.fields(DropEvent)}
    assert fields == {"dropped_count", "range", "range_status", "range_note"}
    for event in (classify(SeqState(), 5, 5), classify(SeqState(), 2, 5)):  # known and unknown
        assert event.drop is not None and not hasattr(event.drop, "missing_count")
        assert event.drop.dropped_count in (5,)
    # missing_count belongs to the discontinuity (SeqEvents), never to the drop
    assert hasattr(classify(SeqState(), 5, 5), "missing_count")


def test_unreconciled_drop_entries_use_count_and_never_missing_count() -> None:
    entry = unreconciled_drops(
        [{"gap_id": "p", "gap_type": "probe_reported_drop", "dropped_count": 4, "range_status": "unknown"}]
    )[0]
    assert set(entry) == {"gap_id", "count", "uncertain", "reason"}


# --- Property 11, part 2: reconciliation marking --------------------------------------

GAP_SETS = st.lists(st.one_of(st.none(), RANGE), min_size=1, max_size=4)


def gap_set(ranges: list) -> list[tuple[str, tuple[int, int] | None]]:
    return [(f"gap-{n:04d}", rng) for n, rng in enumerate(ranges, start=1)]


@settings(**SETTINGS)
@given(GAP_SETS, st.randoms(use_true_random=False))
@example([(13, 15), (13, 15)], None)  # the design's reconciled pair
@example([(13, 15), (16, 20)], None)  # touching, not overlapping
@example([(13, 15), (14, 14)], None)  # contained
@example([(13, 15), (15, 20)], None)  # share one number
@example([(13, 15), None], None)  # a gap with no range
@example([None, None], None)
@example([(0, 0), (0, 0)], None)
@example([(3, 3)], None)
def test_property_11_reconciliation_fields_agree_with_the_ranges(ranges: list, rnd) -> None:
    gaps_ = gap_set(ranges)
    for index, (gap_id, mine) in enumerate(gaps_):
        others = [g for g in gaps_ if g[0] != gap_id]
        if rnd is not None:
            rnd.shuffle(others)  # the order the caller passes the others in
        snapshot = list(others)
        status, with_ids = reconcile(mine, others)
        assert others == snapshot  # inputs are not modified
        assert (status, with_ids) == reconcile(mine, others)  # deterministic

        overlapping = oracle_overlapping_ids(mine, others)
        assert with_ids == overlapping  # exactly the overlapping gap identifiers, in the caller's order
        assert gap_id not in with_ids and len(set(with_ids)) == len(with_ids)
        assert status in ("reconciled", "partial_overlap", "none")  # silence's not_applicable is never produced here
        if not overlapping:
            assert status == "none"
        else:
            identical = [oid for oid, orng in others if orng is not None and mine is not None and orng == mine]
            if set(overlapping) == set(identical):
                assert status == "reconciled"  # every overlapping range is identical
            elif not identical:
                assert status == "partial_overlap"  # overlap, but never identical
            # a mix of identical and non-identical overlaps is the documented ambiguity: see the test below


@settings(**SETTINGS)
@given(GAP_SETS)
def test_property_11_overlap_is_symmetric_and_a_gap_without_a_range_has_none(ranges: list) -> None:
    gaps_ = gap_set(ranges)
    results = {gid: reconcile(rng, [g for g in gaps_ if g[0] != gid]) for gid, rng in gaps_}
    for gid, rng in gaps_:
        status, with_ids = results[gid]
        for other_id in with_ids:
            assert gid in results[other_id][1]  # overlap is mutual
        if rng is None:
            assert (status, with_ids) == ("none", [])
        # nobody lists a gap that has no range
        assert all(dict(gaps_)[other_id] is not None for other_id in with_ids)


@settings(**SETTINGS)
@given(RANGE, RANGE)
def test_property_11_a_pair_of_gaps_from_one_ingest_is_reconciled_exactly_when_identical(a: tuple, b: tuple) -> None:
    """One ingest creates at most two gaps (a discontinuity and a drop): the property text applies literally."""
    sa, sb = reconcile(a, [("b", b)]), reconcile(b, [("a", a)])
    if a == b:
        assert sa == ("reconciled", ["b"]) and sb == ("reconciled", ["a"])
    elif span(a) & span(b):
        assert sa == ("partial_overlap", ["b"]) and sb == ("partial_overlap", ["a"])
    else:
        assert sa == ("none", []) and sb == ("none", [])


# --- Property 11 across classify and reconcile: what a real ingest produces -------------


@settings(**SETTINGS)
@given(st.lists(st.tuples(st.integers(0, 9), st.sampled_from([0, 0, 1, 2, 3, 5])), max_size=25))
@example([(0, 0), (5, 3)])  # discontinuity 1..4 and drop 2..4: partial overlap
@example([(0, 0), (4, 3)])  # discontinuity 1..3 and drop 1..3: reconciled
@example([(6, 6)])  # first Sample: discontinuity 0..5 and drop 0..5: reconciled
@example([(0, 0), (1, 0), (2, 1)])  # drop range received: unknown, nothing to reconcile
def test_property_11_the_two_gaps_of_one_real_ingest_are_marked_per_their_ranges(stream: list) -> None:
    state = SeqState()
    for seq, dropped in stream:
        event = classify(state, seq, dropped)
        state.apply(event)
        if event.kind != "new":
            continue
        disc = event.discontinuity
        drop_range = event.drop.range if event.drop is not None else None
        disc_mark = reconcile(disc, [("drop", drop_range)])
        drop_mark = reconcile(drop_range, [("disc", disc)])
        if disc is not None and drop_range is not None and disc == drop_range:
            assert disc_mark == ("reconciled", ["drop"]) and drop_mark == ("reconciled", ["disc"])
        elif disc is not None and drop_range is not None and span(disc) & span(drop_range):
            assert disc_mark == ("partial_overlap", ["drop"]) and drop_mark == ("partial_overlap", ["disc"])
        else:
            assert disc_mark == ("none", []) and drop_mark == ("none", [])
        if event.drop is not None and event.drop.range is None:
            assert drop_mark == ("none", [])  # an unknown range is never reconciled


# --- explicit regression examples from the approved specification ----------------------


def test_design_example_discontinuity_and_known_drop_are_reconciled() -> None:
    """Design, Gap_Record examples gap-0001 and gap-0002: received 0..12, then seq 16 reporting 3 dropped."""
    state = SeqState(12, IntervalSet([(0, 12)]))
    event = classify(state, 16, 3)
    assert event.discontinuity == (13, 15) and event.missing_count == 3
    assert event.drop == DropEvent(3, (13, 15), "known", None)
    assert reconcile(event.discontinuity, [("gap-0002", event.drop.range)]) == ("reconciled", ["gap-0002"])
    assert reconcile(event.drop.range, [("gap-0001", event.discontinuity)]) == ("reconciled", ["gap-0001"])


def test_design_example_drop_with_an_unknown_range_is_not_reconciled() -> None:
    """Design example gap-0003: dropped 1, range overlaps received samples, reconciliation none."""
    state = SeqState(19, IntervalSet([(0, 19)]))
    event = classify(state, 20, 1)
    assert event.discontinuity is None
    assert event.drop == DropEvent(1, None, "unknown", DESIGN_OVERLAP_NOTE)
    assert reconcile(event.drop.range, []) == ("none", [])


def test_design_rule_partial_overlap_when_the_drop_is_smaller_than_the_discontinuity() -> None:
    state = SeqState(10, IntervalSet([(0, 10)]))
    event = classify(state, 14, 2)  # discontinuity 11..13, drop 12..13
    assert event.discontinuity == (11, 13) and event.drop.range == (12, 13)
    assert reconcile(event.discontinuity, [("drop", event.drop.range)]) == ("partial_overlap", ["drop"])
    assert reconcile(event.drop.range, [("disc", event.discontinuity)]) == ("partial_overlap", ["disc"])


def test_design_rule_a_drop_reaching_received_numbers_is_unknown() -> None:
    state = SeqState(10, IntervalSet([(0, 10)]))
    event = classify(state, 14, 6)  # candidate 8..13 includes received 8, 9, 10
    assert event.drop is not None and event.drop.range is None and event.drop.range_status == "unknown"
    assert event.drop.dropped_count == 6  # the count is kept even when the range is not


def test_design_rule_first_sample_with_a_drop_equal_to_its_number_is_reconciled() -> None:
    event = classify(SeqState(), 6, 6)
    assert event.discontinuity == (0, 5) and event.drop.range == (0, 5)
    assert reconcile(event.discontinuity, [("drop", event.drop.range)])[0] == "reconciled"


def test_adjacent_ranges_do_not_overlap() -> None:
    assert reconcile((13, 15), [("g", (16, 18))]) == ("none", [])
    assert reconcile((13, 15), [("g", (10, 12))]) == ("none", [])


def test_documented_ambiguity_mixed_identical_and_partial_overlaps_in_a_set_of_three() -> None:
    """The property text lists `reconciled` (another gap is identical) before `partial_overlap`; with three or
    more gaps a record can have both. `reconcile` answers `partial_overlap` (reconciled needs every overlapping
    range to be identical). Approved interpretation (14 November 2026): `partial_overlap` is kept whenever any overlapping
    comparison range is non-identical. One ingest creates at most two gaps, but persisted evidence can hold many gaps
    for a run, so this case is not claimed to be impossible. Pinned as current behaviour."""
    status, ids = reconcile((13, 15), [("a", (13, 15)), ("b", (14, 20))])
    assert (status, ids) == ("partial_overlap", ["a", "b"])


def test_reconcile_never_returns_not_applicable_so_the_record_builder_must_set_it_for_silence() -> None:
    """Uncovered builder requirement (task 3.5): `not_applicable` for silence timeouts is the builder's constant."""
    for mine in (None, (1, 2)):
        status, _ = reconcile(mine, [("g", (1, 2)), ("h", None)])
        assert status != "not_applicable"