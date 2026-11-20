"""Unit tests for coroner.evidence.gaps (task 2.6; requirements 6.1, 6.3, 6.4, 6.8 to 6.13).

Example and exhaustive small-domain tests. The Hypothesis properties (10 to 12) and the five
worked examples are tasks 2.7 to 2.9 and are not repeated here.
"""
from __future__ import annotations

import copy
import itertools
import json

import pytest

from coroner.evidence import gaps
from coroner.evidence.gaps import (
    DropEvent,
    GapRecordError,
    IntervalSet,
    SeqEvents,
    SeqState,
    classify,
    derive_drop_range,
    merge_ranges,
    missing_sample_count,
    overlap_size,
    reconcile,
    unreconciled_drops,
    union_size,
)


# --- IntervalSet ---------------------------------------------------------------------


def test_empty_interval_set() -> None:
    s = IntervalSet()
    assert not s and s.size() == 0 and s.ranges() == () and 0 not in s
    assert not s.intersects(0, 100)


def test_add_single_numbers_and_ranges() -> None:
    s = IntervalSet()
    s.add(5)
    s.add(10, 12)
    assert s.ranges() == ((5, 5), (10, 12)) and s.size() == 4
    assert 5 in s and 10 in s and 11 in s and 12 in s
    assert 4 not in s and 6 not in s and 9 not in s and 13 not in s


def test_touching_and_overlapping_intervals_merge() -> None:
    s = IntervalSet()
    for lo, hi in [(1, 2), (3, 4)]:  # adjacent: 2 and 3 touch
        s.add(lo, hi)
    assert s.ranges() == ((1, 4),)
    s.add(10, 12)
    s.add(6, 8)
    assert s.ranges() == ((1, 4), (6, 8), (10, 12))
    s.add(9)  # bridges (6,8) and (10,12)
    assert s.ranges() == ((1, 4), (6, 12))
    s.add(5)  # bridges (1,4) and (6,12)
    assert s.ranges() == ((1, 12),)
    s.add(3, 7)  # contained
    s.add(0, 20)  # swallows everything
    assert s.ranges() == ((0, 20),) and s.size() == 21


def test_add_in_any_order_gives_the_same_set() -> None:
    items = [(0, 0), (2, 3), (3, 6), (9, 9), (7, 7), (12, 15)]
    results = {IntervalSet(order).ranges() for order in itertools.permutations(items)}
    assert results == {((0, 0), (2, 7), (9, 9), (12, 15))}


def test_membership_boundaries_and_non_integers() -> None:
    s = IntervalSet([(10, 20)])
    assert 9 not in s and 10 in s and 20 in s and 21 not in s
    for value in (True, 10.0, "10", None, (10,)):
        assert value not in s


def test_intersects_boundaries() -> None:
    s = IntervalSet([(10, 20)])
    assert s.intersects(20, 25) and s.intersects(5, 10) and s.intersects(12, 13) and s.intersects(0, 100)
    assert not s.intersects(21, 30) and not s.intersects(0, 9)  # touching is not intersecting
    assert s.intersects(10, 10) and s.intersects(20, 20)


def test_copy_is_independent_and_equality_is_by_content() -> None:
    a = IntervalSet([(1, 3)])
    b = a.copy()
    b.add(10)
    assert a.ranges() == ((1, 3),) and b.ranges() == ((1, 3), (10, 10))
    assert IntervalSet([(1, 2), (3, 3)]) == IntervalSet([(1, 3)])
    assert IntervalSet([(1, 3)]) != IntervalSet([(1, 4)]) and IntervalSet() != "x"
    assert repr(IntervalSet([(1, 3)])) == "IntervalSet([(1, 3)])"
    with pytest.raises(TypeError):
        hash(a)


@pytest.mark.parametrize("lo, hi", [(5, 4), (1, 0), (0, -1)])
def test_add_rejects_a_reversed_range(lo: int, hi: int) -> None:
    with pytest.raises(ValueError, match="greater than"):
        IntervalSet().add(lo, hi)


@pytest.mark.parametrize("lo, hi", [(1.0, 2), (1, 2.5), (True, 3), ("1", 2), (None, 2), (1, "2")])
def test_add_rejects_non_integer_bounds(lo: object, hi: object) -> None:
    with pytest.raises(TypeError):
        IntervalSet().add(lo, hi)  # type: ignore[arg-type]


def test_interval_set_matches_a_plain_set_on_every_small_sequence_of_adds() -> None:
    domain = range(0, 6)
    ranges = [(lo, hi) for lo in domain for hi in domain if lo <= hi]
    checked = 0
    for count in (1, 2, 3):
        for adds in itertools.product(ranges, repeat=count):
            s, model = IntervalSet(), set()
            for lo, hi in adds:
                s.add(lo, hi)
                model |= set(range(lo, hi + 1))
            assert s.size() == len(model)
            assert all((n in s) == (n in model) for n in range(-1, 8))
            expected = []
            for n in sorted(model):  # intervals of a plain set, merging adjacent numbers
                if expected and expected[-1][1] == n - 1:
                    expected[-1] = (expected[-1][0], n)
                else:
                    expected.append((n, n))
            assert list(s.ranges()) == expected
            for lo, hi in ranges:
                assert s.intersects(lo, hi) == bool(model & set(range(lo, hi + 1)))
            checked += 1
    assert checked > 9000


# --- merge_ranges, union_size, overlap_size ------------------------------------------


def test_merge_ranges_examples() -> None:
    assert merge_ranges([]) == []
    assert merge_ranges([(3, 4), (1, 1)]) == [(1, 1), (3, 4)]
    assert merge_ranges([(1, 2), (3, 4)]) == [(1, 4)]  # touching merges
    assert merge_ranges([(1, 5), (2, 3), (4, 9)]) == [(1, 9)]
    assert merge_ranges([(1, 5), (1, 5), (1, 5)]) == [(1, 5)]
    assert merge_ranges(iter([(8, 9), (1, 2)])) == [(1, 2), (8, 9)]
    assert merge_ranges([[1, 2], [4, 5]]) == [(1, 2), (4, 5)]  # JSON arrays are accepted


def test_merge_ranges_does_not_modify_its_input() -> None:
    source = [(5, 6), (1, 2)]
    merge_ranges(source)
    assert source == [(5, 6), (1, 2)]


@pytest.mark.parametrize("bad", [[(3, 1)], [(1, 2, 3)], [5], [None], [("a", "b")], [(1.5, 2)], [(True, 2)]])
def test_merge_ranges_rejects_malformed_ranges(bad: list) -> None:
    with pytest.raises((ValueError, TypeError)):
        merge_ranges(bad)


def test_union_size_counts_each_number_once() -> None:
    assert union_size([]) == 0
    assert union_size([(0, 0)]) == 1
    assert union_size([(0, 9)]) == 10
    assert union_size([(0, 9), (0, 9)]) == 10
    assert union_size([(0, 4), (3, 9)]) == 10
    assert union_size([(0, 4), (5, 9)]) == 10
    assert union_size([(0, 4), (6, 9)]) == 9
    assert union_size([(0, 2**40)]) == 2**40 + 1


def test_overlap_size() -> None:
    assert overlap_size((5, 10), []) == 0
    assert overlap_size((5, 10), [(0, 4)]) == 0
    assert overlap_size((5, 10), [(0, 5)]) == 1
    assert overlap_size((5, 10), [(10, 20)]) == 1
    assert overlap_size((5, 10), [(0, 20)]) == 6
    assert overlap_size((5, 10), [(6, 7), (9, 9)]) == 3
    assert overlap_size((5, 10), [(7, 8), (6, 7)]) == 3  # unmerged input is merged first
    assert overlap_size((0, 0), [(0, 0)]) == 1


# --- reconcile -----------------------------------------------------------------------


def test_reconcile_identical_ranges_are_reconciled() -> None:
    assert reconcile((13, 15), [("gap-0001", (13, 15))]) == ("reconciled", ["gap-0001"])


@pytest.mark.parametrize("other", [(13, 16), (12, 15), (14, 14), (10, 20), (15, 20), (10, 13)])
def test_reconcile_any_other_overlap_is_partial(other: tuple[int, int]) -> None:
    assert reconcile((13, 15), [("g", other)]) == ("partial_overlap", ["g"])


@pytest.mark.parametrize("other", [(16, 20), (0, 12), (1, 1), (100, 200)])
def test_reconcile_disjoint_or_merely_touching_ranges_do_not_overlap(other: tuple[int, int]) -> None:
    assert reconcile((13, 15), [("g", other)]) == ("none", [])


def test_reconcile_with_no_range_or_no_others() -> None:
    assert reconcile(None, [("g", (1, 2))]) == ("none", [])
    assert reconcile((1, 2), []) == ("none", [])
    assert reconcile((1, 2), [("g", None)]) == ("none", [])


def test_reconcile_with_several_others() -> None:
    others = [("a", (13, 15)), ("b", None), ("c", (30, 40)), ("d", (13, 15))]
    assert reconcile((13, 15), others) == ("reconciled", ["a", "d"])
    assert reconcile((13, 15), others + [("e", (14, 20))]) == ("partial_overlap", ["a", "d", "e"])
    assert reconcile((13, 15), [("a", (13, 15)), ("a", (13, 15))]) == ("reconciled", ["a"])


# --- derive_drop_range ---------------------------------------------------------------


def test_drop_range_is_the_numbers_just_below_seq() -> None:
    assert derive_drop_range(16, 3, IntervalSet([(0, 12)])) == (13, 15)
    assert derive_drop_range(5, 1, IntervalSet()) == (4, 4)
    assert derive_drop_range(5, 5, IntervalSet()) == (0, 4)  # starts exactly at 0


@pytest.mark.parametrize("seq, dropped", [(0, 1), (3, 4), (5, 6), (1, 4294967295)])
def test_drop_range_starting_below_zero_is_unknown(seq: int, dropped: int) -> None:
    assert derive_drop_range(seq, dropped, IntervalSet()) is None


def test_drop_range_touching_the_received_set_is_still_known() -> None:
    assert derive_drop_range(10, 3, IntervalSet([(0, 6)])) == (7, 9)  # 6 is received, 7..9 are not


@pytest.mark.parametrize("seen", [[(9, 9)], [(7, 7)], [(8, 8)], [(0, 20)], [(5, 7)]])
def test_drop_range_containing_a_received_number_is_unknown(seen: list) -> None:
    assert derive_drop_range(10, 3, IntervalSet(seen)) is None


@pytest.mark.parametrize("dropped", [0, -1, 1.0, True, None, "1"])
def test_derive_drop_range_requires_a_real_drop(dropped: object) -> None:
    with pytest.raises(ValueError):
        derive_drop_range(5, dropped, IntervalSet())  # type: ignore[arg-type]


# --- classify: the design's table ----------------------------------------------------


def run(seqs: list[int | tuple[int, int]]) -> tuple[SeqState, list[SeqEvents]]:
    """Feed (seq or (seq, dropped)) values through classify and apply, as the Collector will."""
    state, events = SeqState(), []
    for item in seqs:
        seq, dropped = item if isinstance(item, tuple) else (item, 0)
        event = classify(state, seq, dropped)
        state.apply(event)
        events.append(event)
    return state, events


def kinds(events: list[SeqEvents]) -> list[str]:
    return [e.kind for e in events]


def test_first_sample_at_zero_is_new_with_no_gap() -> None:
    e = classify(SeqState(), 0, 0)
    assert (e.kind, e.previous_highest, e.discontinuity, e.drop, e.missing_count) == ("new", None, None, None, 0)


@pytest.mark.parametrize("seq", [1, 2, 7, 1000])
def test_first_sample_above_zero_reports_everything_before_it_missing(seq: int) -> None:
    e = classify(SeqState(), seq, 0)
    assert e.kind == "new" and e.discontinuity == (0, seq - 1) and e.missing_count == seq


def test_the_next_number_is_new_with_no_gap() -> None:
    _, events = run([0, 1, 2, 3])
    assert kinds(events) == ["new"] * 4 and all(e.discontinuity is None for e in events)
    assert [e.previous_highest for e in events] == [None, 0, 1, 2]


@pytest.mark.parametrize("highest, seq", [(12, 14), (12, 16), (0, 2), (0, 1000), (99, 101)])
def test_a_jump_reports_exactly_the_numbers_in_between(highest: int, seq: int) -> None:
    state = SeqState(highest, IntervalSet([(0, highest)]))
    e = classify(state, seq, 0)
    assert e.kind == "new" and e.discontinuity == (highest + 1, seq - 1) and e.missing_count == seq - highest - 1


def test_a_gap_of_exactly_one() -> None:
    _, events = run([0, 2])
    assert events[1].discontinuity == (1, 1) and events[1].missing_count == 1


def test_duplicate_of_the_highest_and_of_an_earlier_number() -> None:
    state, events = run([0, 1, 2, 3, 3, 1, 0])
    assert kinds(events) == ["new", "new", "new", "new", "duplicate", "duplicate", "duplicate"]
    assert [e.previous_highest for e in events[4:]] == [3, 3, 3]
    assert all(e.discontinuity is None and e.drop is None for e in events[4:])
    assert state.highest == 3


def test_a_number_below_the_highest_that_was_never_received_is_a_reset() -> None:
    state, events = run([0, 5, 2])
    assert kinds(events) == ["new", "new", "reset"] and events[2].previous_highest == 5
    assert events[2].discontinuity is None and state.highest == 5
    assert 2 not in state.seen  # an anomaly is not a received Sample


def test_a_late_sample_that_fills_a_discontinuity_is_a_reset_and_the_gap_stays_recorded() -> None:
    state, events = run([0, 1, 4, 2, 3])  # 2 and 3 arrive late, after 4
    assert events[2].discontinuity == (2, 3)
    assert kinds(events) == ["new", "new", "new", "reset", "reset"]
    assert state.seen.ranges() == ((0, 1), (4, 4)) and state.highest == 4


def test_the_same_reset_twice_is_reset_both_times_because_resets_are_not_received_samples() -> None:
    _, events = run([0, 5, 2, 2])
    assert kinds(events) == ["new", "new", "reset", "reset"]


def test_a_process_restart_inside_a_run_is_flagged_as_resets_and_duplicates_not_as_a_new_incarnation() -> None:
    _, events = run([0, 1, 2, 3, 4, 0, 1, 2, 3, 4, 5, 6])  # restarted at 0 (decision D7)
    assert kinds(events) == ["new"] * 5 + ["duplicate"] * 5 + ["new", "new"]
    assert events[10].discontinuity is None and events[10].seq == 5


def test_anomalies_do_not_move_the_baseline() -> None:
    state, _ = run([0, 1, 2])
    before = (state.highest, state.seen.copy())
    for seq in (0, 1, 2):  # duplicates
        state.apply(classify(state, seq, 0))
    assert (state.highest, state.seen) == before
    state2, _ = run([0, 9])
    state2.apply(classify(state2, 4, 0))  # a reset
    assert state2.highest == 9 and state2.seen.ranges() == ((0, 0), (9, 9))
    e = classify(state2, 10, 0)  # the next new number is judged against the old baseline
    assert e.kind == "new" and e.discontinuity is None


def test_classify_is_pure_and_deterministic() -> None:
    state = SeqState(5, IntervalSet([(0, 5)]))
    snapshot = (state.highest, state.seen.copy())
    first = classify(state, 9, 2)
    assert classify(state, 9, 2) == first
    assert (state.highest, state.seen) == snapshot


@pytest.mark.parametrize("seq", [-1, 1.0, True, None, "1", [1]])
def test_classify_rejects_an_invalid_seq(seq: object) -> None:
    with pytest.raises(ValueError):
        classify(SeqState(), seq, 0)  # type: ignore[arg-type]


@pytest.mark.parametrize("dropped", [-1, 4294967296, 1.0, True, None, "0"])
def test_classify_rejects_an_invalid_dropped(dropped: object) -> None:
    with pytest.raises(ValueError):
        classify(SeqState(), 0, dropped)  # type: ignore[arg-type]


def test_dropped_at_its_limits_is_accepted() -> None:
    assert classify(SeqState(), 3, 0).drop is None
    e = classify(SeqState(), 3, 4294967295)
    assert e.drop == DropEvent(4294967295, None, "unknown", gaps.NOTE_BELOW_ZERO)


# --- classify: Probe-reported drops --------------------------------------------------


def test_a_drop_whose_range_equals_the_discontinuity() -> None:
    state = SeqState(12, IntervalSet([(0, 12)]))
    e = classify(state, 16, 3)
    assert e.discontinuity == (13, 15)
    assert e.drop == DropEvent(3, (13, 15), "known", None)


def test_first_sample_with_drops_equal_to_its_seq() -> None:
    e = classify(SeqState(), 5, 5)
    assert e.discontinuity == (0, 4) and e.drop == DropEvent(5, (0, 4), "known", None)


def test_a_drop_larger_than_the_numbers_before_seq_has_an_unknown_range() -> None:
    e = classify(SeqState(), 2, 5)
    assert e.discontinuity == (0, 1) and e.drop == DropEvent(5, None, "unknown", gaps.NOTE_BELOW_ZERO)


def test_a_drop_on_a_consecutive_sample_whose_range_was_received_is_unknown() -> None:
    """The acknowledgement was lost: the Collector stored numbers the Probe believes it dropped."""
    state = SeqState(10, IntervalSet([(0, 10)]))
    e = classify(state, 11, 2)
    assert e.discontinuity is None
    assert e.drop == DropEvent(2, None, "unknown", gaps.NOTE_OVERLAP)


def test_a_drop_larger_than_the_discontinuity_is_unknown_when_it_reaches_received_numbers() -> None:
    state = SeqState(10, IntervalSet([(0, 10)]))
    e = classify(state, 14, 6)  # candidate 8..13 includes received 8, 9, 10
    assert e.discontinuity == (11, 13) and e.drop is not None and e.drop.range_status == "unknown"


def test_drops_on_duplicates_and_resets_are_ignored() -> None:
    state, _ = run([0, 1, 5])
    assert classify(state, 1, 4).drop is None  # duplicate
    assert classify(state, 3, 4).drop is None  # reset


def test_the_drop_range_never_contains_the_current_sample_or_a_received_number() -> None:
    for highest in range(0, 6):
        for seq in range(highest + 1, highest + 6):
            for dropped in range(1, 8):
                state = SeqState(highest, IntervalSet([(0, highest)]))
                e = classify(state, seq, dropped)
                assert e.drop is not None
                if e.drop.range is not None:
                    lo, hi = e.drop.range
                    assert hi == seq - 1 and hi - lo + 1 == dropped
                    assert not state.seen.intersects(lo, hi) and lo >= 0
                else:
                    assert e.drop.range_note in (gaps.NOTE_BELOW_ZERO, gaps.NOTE_OVERLAP)


# --- SeqState and restart-related assumptions ----------------------------------------


def test_initial_state_and_apply() -> None:
    s = SeqState()
    assert s.highest is None and not s.seen
    s.apply(classify(s, 3, 0))
    assert s.highest == 3 and s.seen.ranges() == ((3, 3),)
    s.apply(classify(s, 4, 0))
    assert s.highest == 4 and s.seen.ranges() == ((3, 4),)


def test_state_copy_is_independent() -> None:
    a, _ = run([0, 1, 2])
    b = a.copy()
    b.apply(classify(b, 3, 0))
    assert a.highest == 2 and b.highest == 3 and a.seen.size() == 3


STREAMS = [
    [0, 1, 2, 3],
    [0, 1, 4, 5, 9],
    [3, 4, 5],
    [0, 2, 1, 3, 3, 0, 7, 5, 7],
    [0, 1, 2, 3, 4, 0, 1, 2, 3, 4, 5],
    [0, (3, 2), (4, 1), 4, 2, (9, 3)],
    [(5, 5), 5, 3, (8, 2)],
]


@pytest.mark.parametrize("stream", STREAMS)
def test_rebuilding_the_state_from_the_received_samples_alone_reproduces_it_exactly(stream: list) -> None:
    """After a restart the state is rebuilt by scanning the file. Only `sample` records are needed:
    anomalies never move the baseline, so the replayed state and the live state are identical, and a
    replay of the received Samples regenerates the same discontinuities."""
    live_state, live_events = run(stream)
    received = [e for e in live_events if e.kind == "new"]
    replay_state, replay_events = SeqState(), []
    for e in received:
        event = classify(replay_state, e.seq, 0)
        replay_state.apply(event)
        replay_events.append(event)
    assert (replay_state.highest, replay_state.seen) == (live_state.highest, live_state.seen)
    assert [(e.seq, e.discontinuity) for e in replay_events] == [(e.seq, e.discontinuity) for e in received]


def test_a_replayed_state_continues_classification_like_the_original() -> None:
    live, _ = run([0, 1, 5, 3, 5, 7])
    replay = SeqState()
    for seq in (0, 1, 5, 7):  # the received Samples only
        replay.apply(classify(replay, seq, 0))
    for seq in (8, 7, 6, 12, 2):
        assert classify(live, seq, 0) == classify(replay, seq, 0)


def test_replaying_a_file_that_starts_above_zero_reports_the_leading_numbers_missing() -> None:
    """Decision D6 also holds after a restart: a Collector that sees only seq 10 first marks 0..9 missing."""
    e = classify(SeqState(), 10, 0)
    assert e.discontinuity == (0, 9)


# --- missing_sample_count and unreconciled_drops -------------------------------------

GAP_EXAMPLES = [
    json.loads(line) for line in (
        '{"record_type":"gap","gap_id":"gap-0001","gap_type":"sequence_discontinuity","first_missing_seq":13,"last_missing_seq":15,"missing_count":3,"reconciliation":"reconciled","reconciles_with":["gap-0002"],"detected_at_seq":16}',
        '{"record_type":"gap","gap_id":"gap-0002","gap_type":"probe_reported_drop","dropped_count":3,"range_status":"known","first_missing_seq":13,"last_missing_seq":15,"reconciliation":"reconciled","reconciles_with":["gap-0001"],"detected_at_seq":16}',
        '{"record_type":"gap","gap_id":"gap-0003","gap_type":"probe_reported_drop","dropped_count":1,"range_status":"unknown","first_missing_seq":null,"last_missing_seq":null,"range_note":"range overlaps received samples","reconciliation":"none","reconciles_with":[],"detected_at_seq":20}',
        '{"record_type":"gap","gap_id":"gap-0004","gap_type":"silence_timeout","last_sample_time":"2025-01-01T12:00:10.530Z","detected_time":"2025-01-01T12:00:12.081Z","threshold_ms":1500,"interval_ms":500,"sample_count":"unknown","reconciliation":"not_applicable","reconciles_with":[]}',
    )
]


def discontinuity(first: int, last: int, gap_id: str = "d", **extra: object) -> dict:
    return {"gap_id": gap_id, "gap_type": "sequence_discontinuity", "first_missing_seq": first, "last_missing_seq": last, **extra}


def drop(first: int | None, last: int | None, count: int, status: str = "known", gap_id: str = "p", **extra: object) -> dict:
    return {"gap_id": gap_id, "gap_type": "probe_reported_drop", "dropped_count": count, "range_status": status,
            "first_missing_seq": first, "last_missing_seq": last, **extra}


SILENCE = {"gap_id": "s", "gap_type": "silence_timeout", "sample_count": "unknown"}


def test_the_design_examples() -> None:
    assert missing_sample_count(GAP_EXAMPLES) == 3  # gap-0001 only
    assert unreconciled_drops(GAP_EXAMPLES[:2]) == []  # fully reconciled
    assert unreconciled_drops(GAP_EXAMPLES) == [
        {"gap_id": "gap-0003", "count": 1, "uncertain": True, "reason": "range overlaps received samples"}
    ]
    assert missing_sample_count(GAP_EXAMPLES[1:]) == 0  # drops and silence never count


def test_missing_count_counts_discontinuities_only() -> None:
    records = [discontinuity(3, 5), drop(10, 20, 11), SILENCE, {"record_type": "sample"}, {"gap_type": "other"}]
    assert missing_sample_count(records) == 3
    assert missing_sample_count([]) == 0 and missing_sample_count([SILENCE, drop(1, 2, 2)]) == 0


def test_missing_count_counts_each_number_once() -> None:
    assert missing_sample_count([discontinuity(3, 5), discontinuity(3, 5)]) == 3  # duplicated record
    assert missing_sample_count([discontinuity(3, 5), discontinuity(5, 8)]) == 6  # overlap
    assert missing_sample_count([discontinuity(3, 5), discontinuity(6, 8)]) == 6  # adjacent
    assert missing_sample_count([discontinuity(3, 5), discontinuity(10, 10)]) == 4
    assert missing_sample_count([discontinuity(0, 0)]) == 1


def test_missing_count_ignores_the_stored_count_field() -> None:
    assert missing_sample_count([discontinuity(3, 5, missing_count=999)]) == 3
    assert missing_sample_count([discontinuity(3, 5, missing_count=0)]) == 3


def test_missing_count_matches_a_brute_force_union() -> None:
    domain = range(0, 7)
    ranges = [(lo, hi) for lo in domain for hi in domain if lo <= hi]
    for pair in itertools.product(ranges, repeat=3):
        brute = set()
        for lo, hi in pair:
            brute |= set(range(lo, hi + 1))
        assert missing_sample_count([discontinuity(lo, hi) for lo, hi in pair]) == len(brute)


@pytest.mark.parametrize(
    "bad",
    [
        discontinuity(5, 3), discontinuity(-1, 3), discontinuity(1.5, 3), discontinuity(True, 3),
        {"gap_id": "d", "gap_type": "sequence_discontinuity"},
        discontinuity(None, None), discontinuity("1", "2"),
    ],
)
def test_a_malformed_discontinuity_record_raises_and_names_the_gap(bad: dict) -> None:
    with pytest.raises(GapRecordError, match="gap 'd'"):
        missing_sample_count([discontinuity(1, 2, "ok"), bad])
    assert issubclass(GapRecordError, ValueError)


def test_unreconciled_known_range_fully_covered_by_a_discontinuity() -> None:
    assert unreconciled_drops([discontinuity(13, 15), drop(13, 15, 3)]) == []
    assert unreconciled_drops([discontinuity(10, 20), drop(13, 15, 3)]) == []  # contained


def test_unreconciled_known_range_partly_covered() -> None:
    result = unreconciled_drops([discontinuity(13, 15), drop(13, 17, 5, gap_id="p1")])
    assert result == [{"gap_id": "p1", "count": 2, "uncertain": True, "reason": gaps.NOTE_NOT_COVERED}]
    result = unreconciled_drops([discontinuity(10, 11), discontinuity(14, 14), drop(10, 14, 5, gap_id="p2")])
    assert [r["count"] for r in result] == [2]  # 12 and 13 are not explained


def test_unreconciled_known_range_with_no_discontinuity() -> None:
    assert [r["count"] for r in unreconciled_drops([drop(4, 8, 5)])] == [5]
    assert unreconciled_drops([drop(4, 8, 5)])[0]["reason"] == gaps.NOTE_NOT_COVERED


@pytest.mark.parametrize("status", ["unknown", "uncertain"])
def test_unreconciled_unknown_or_uncertain_range_counts_the_whole_reported_drop(status: str) -> None:
    covering = discontinuity(0, 100)  # a covering discontinuity changes nothing for an unknown range
    result = unreconciled_drops([covering, drop(None, None, 4, status, range_note="range overlaps received samples")])
    assert result == [{"gap_id": "p", "count": 4, "uncertain": True, "reason": "range overlaps received samples"}]


def test_unreconciled_unknown_range_without_a_note_gets_a_default_reason() -> None:
    assert unreconciled_drops([drop(None, None, 2, "unknown")])[0]["reason"] == gaps.NOTE_UNKNOWN


def test_unreconciled_total_is_the_sum_of_the_entries() -> None:
    records = [discontinuity(0, 4), drop(0, 4, 5, gap_id="a"), drop(3, 9, 7, gap_id="b"), drop(None, None, 3, "unknown", gap_id="c"), SILENCE]
    entries = unreconciled_drops(records)
    assert [(e["gap_id"], e["count"]) for e in entries] == [("b", 5), ("c", 3)]
    assert sum(e["count"] for e in entries) == 8


def test_unreconciled_ignores_silence_and_discontinuity_records_themselves() -> None:
    assert unreconciled_drops([SILENCE, discontinuity(1, 9)]) == []
    assert unreconciled_drops([]) == []


def test_no_number_is_counted_in_both_totals() -> None:
    """A number is in Missing_Sample_Count (from a discontinuity) or in the unreconciled drops, never both."""
    records = [discontinuity(5, 9), drop(7, 12, 6, gap_id="p")]
    missing = set(range(5, 10))  # the discontinuity 5..9
    unreconciled_numbers = set(range(7, 13)) - missing
    assert missing_sample_count(records) == 5
    assert sum(e["count"] for e in unreconciled_drops(records)) == len(unreconciled_numbers) == 3
    assert not (missing & unreconciled_numbers)


@pytest.mark.parametrize(
    "bad",
    [
        drop(1, 2, 2, "maybe"), drop(1, 2, 2, None), drop(None, None, 2, "known"), drop(5, 3, 3, "known"),
        drop(None, None, -1, "unknown"), drop(None, None, "3", "unknown"), drop(None, None, True, "unknown"),
    ],
)
def test_a_malformed_drop_record_raises_and_names_the_gap(bad: dict) -> None:
    with pytest.raises(GapRecordError, match="gap 'p'"):
        unreconciled_drops([bad])


def test_counting_functions_do_not_modify_their_input() -> None:
    records = [discontinuity(3, 5), drop(3, 8, 6), SILENCE]
    snapshot = copy.deepcopy(records)
    missing_sample_count(records)
    unreconciled_drops(records)
    assert records == snapshot


def test_counting_functions_accept_any_iterable_once() -> None:
    records = [discontinuity(3, 5), drop(3, 8, 6)]
    assert missing_sample_count(iter(records)) == 3
    assert [e["count"] for e in unreconciled_drops(iter(records))] == [3]


# --- the module has no hidden dependencies -------------------------------------------


def test_gaps_module_is_standard_library_only() -> None:
    import ast
    import pathlib
    import sys

    tree = ast.parse(pathlib.Path(gaps.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names)