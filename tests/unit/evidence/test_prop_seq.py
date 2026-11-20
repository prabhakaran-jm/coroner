"""Property 10: Sequence anomalies are classified exactly and separately (task 2.7).

*For any* sequence of arriving sequence numbers for one Run_Identifier, each Sample is classified
as exactly one of `new`, `duplicate` (its sequence number equals that of a previously received
Sample), or `reset` (lower than the highest accepted number and not previously received). Each
`new` Sample whose number exceeds the previous highest plus 1 yields exactly one sequence
discontinuity gap whose first and last missing numbers are `previous + 1` and `received - 1` and
whose count equals `received - previous - 1`. Duplicates and resets never change previously
stored Samples and never change the highest accepted number.

**Validates: Requirements 6.1, 6.4, 13.3**

The oracle below is the property's own statement written with plain Python sets, independent of
`IntervalSet`. For the first Sample the "previous highest" is -1, so a first number above 0 reports
`[0, seq - 1]` missing (decision D6). "Previously received Sample" means a Sample classified `new`:
anomalies are stored as `sequence_anomaly` records, not as Samples.
"""
from __future__ import annotations

from collections import Counter

from hypothesis import example, given, settings
from hypothesis import strategies as st

from coroner.evidence.gaps import SeqState, classify

SETTINGS = dict(max_examples=500, deadline=None, derandomize=True, database=None)

KINDS = ("new", "duplicate", "reset")

# Small domains force many collisions, which is where duplicates and resets come from.
SMALL = st.lists(st.integers(min_value=0, max_value=9), max_size=30)
TINY = st.lists(st.integers(min_value=0, max_value=4), max_size=12)
WIDE = st.lists(st.integers(min_value=0, max_value=10**6), max_size=12)
MONOTONE = st.lists(st.integers(min_value=0, max_value=60), max_size=20).map(sorted)
PERMUTATION = st.integers(min_value=0, max_value=8).flatmap(lambda n: st.permutations(list(range(n))))
ARRIVALS = st.one_of(SMALL, TINY, WIDE, MONOTONE, PERMUTATION)
ENUMERABLE = st.one_of(SMALL, TINY, MONOTONE, PERMUTATION)  # small numbers, so sets of numbers are cheap to build


def model(arrivals: list[int]):
    """The specification as an oracle: one (seq, kind, previous_highest, gap) tuple per arrival."""
    accepted: set[int] = set()
    highest: int | None = None
    steps = []
    for seq in arrivals:
        if seq in accepted:
            kind = "duplicate"
        elif highest is not None and seq < highest:
            kind = "reset"
        else:
            kind = "new"
        gap = None
        if kind == "new":
            previous = -1 if highest is None else highest
            if seq > previous + 1:
                gap = (previous + 1, seq - 1)
        steps.append((seq, kind, highest, gap))
        if kind == "new":
            accepted.add(seq)
            highest = seq
    return steps, accepted, highest


def intervals(numbers: set[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for n in sorted(numbers):
        if out and out[-1][1] == n - 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


def run(arrivals: list[int], dropped: list[int] | None = None):
    state, events, before_after = SeqState(), [], []
    for index, seq in enumerate(arrivals):
        before = (state.highest, state.seen.copy())
        event = classify(state, seq, 0 if dropped is None else dropped[index])
        after_classify = (state.highest, state.seen.copy())
        state.apply(event)
        events.append(event)
        before_after.append((before, after_classify, (state.highest, state.seen.copy())))
    return state, events, before_after


@settings(**SETTINGS)
@given(ARRIVALS)
@example([])
@example([0])
@example([5])
@example([0, 0])
@example([0, 1, 2, 3])
@example([3, 2, 1, 0])
@example([0, 2, 1])
@example([0, 1, 4, 4, 2])  # Requirement 13.3: one missing range, one duplicate, one reset
@example([4, 4, 4])
@example([0, 5, 2, 2, 5, 7, 6])
@example([10**6, 0, 10**6])
def test_property_10_classification_gaps_and_baseline(arrivals: list[int]) -> None:
    expected, accepted, highest = model(arrivals)
    state, events, snapshots = run(arrivals)

    assert len(events) == len(arrivals)
    for event, (seq, kind, previous, gap), (before, after_classify, after_apply) in zip(events, expected, snapshots):
        # exactly one of the three classes, and it is the specified one
        assert event.seq == seq and event.kind == kind and event.kind in KINDS
        # the anomaly's previous_seq is the highest accepted number so far
        assert event.previous_highest == previous
        # a discontinuity exists exactly for a `new` Sample above previous + 1 and is previous+1 .. received-1
        assert event.discontinuity == gap
        if gap is None:
            assert event.missing_count == 0
        else:
            base = -1 if previous is None else previous  # decision D6: the Probe starts at 0
            assert event.missing_count == seq - base - 1
            assert event.missing_count == gap[1] - gap[0] + 1
        # anomalies carry no gap and never drops
        if kind != "new":
            assert event.discontinuity is None and event.drop is None
        # classify is pure: it never changes the state it is given
        assert after_classify[0] == before[0] and after_classify[1] == before[1]
        # duplicates and resets never change the stored set or the highest accepted number
        if kind != "new":
            assert after_apply[0] == before[0] and after_apply[1] == before[1]
        else:
            assert after_apply[0] == seq and seq in after_apply[1] and seq not in before[1]

    assert state.highest == highest
    assert list(state.seen.ranges()) == intervals(accepted)
    assert state.seen.size() == len(accepted)


@settings(**SETTINGS)
@given(ARRIVALS)
def test_property_10_every_arrival_is_counted_in_exactly_one_class(arrivals: list[int]) -> None:
    _, events, _ = run(arrivals)
    counts = Counter(event.kind for event in events)
    assert set(counts) <= set(KINDS)
    assert sum(counts.values()) == len(arrivals)
    expected, accepted, _ = model(arrivals)
    assert counts["new"] == len(accepted)  # only `new` arrivals are received Samples, each number once
    assert counts["new"] + counts["duplicate"] + counts["reset"] == len(arrivals)


@settings(**SETTINGS)
@given(ENUMERABLE)
def test_property_10_every_number_up_to_the_highest_is_received_or_reported_missing_but_never_both(arrivals: list[int]) -> None:
    _, events, _ = run(arrivals)
    expected, accepted, highest = model(arrivals)
    missing: set[int] = set()
    total = 0
    for event in events:
        if event.discontinuity is not None:
            lo, hi = event.discontinuity
            numbers = set(range(lo, hi + 1))
            assert not (numbers & missing)  # discontinuities never overlap each other
            assert not (numbers & accepted - {event.seq})  # nor contain a received number
            missing |= numbers
            total += event.missing_count
    assert total == len(missing)
    assert not (missing & accepted)
    if highest is None:
        assert not accepted and not missing
    else:
        assert accepted | missing == set(range(0, highest + 1))


@settings(**SETTINGS)
@given(WIDE)
def test_property_10_missing_counts_add_up_without_enumerating_huge_ranges(arrivals: list[int]) -> None:
    """Same invariant as above in arithmetic form, so numbers up to 10**6 stay cheap."""
    _, events, _ = run(arrivals)
    _, accepted, highest = model(arrivals)
    total_missing = sum(event.missing_count for event in events)
    assert total_missing == (0 if highest is None else highest + 1 - len(accepted))


@settings(**SETTINGS)
@given(ARRIVALS, st.data())
def test_property_10_the_probe_drop_counter_never_changes_classification_or_gaps(arrivals: list[int], data: st.DataObject) -> None:
    dropped = [data.draw(st.sampled_from([0, 0, 1, 2, 5, 4294967295])) for _ in arrivals]
    _, with_drops, _ = run(arrivals, dropped)
    _, without, _ = run(arrivals)
    assert [(e.seq, e.kind, e.previous_highest, e.discontinuity) for e in with_drops] == [
        (e.seq, e.kind, e.previous_highest, e.discontinuity) for e in without
    ]
    for event in with_drops:
        if event.kind != "new":
            assert event.drop is None  # a duplicate or reset never reports a drop


@settings(**SETTINGS)
@given(ARRIVALS)
def test_property_10_rebuilding_from_the_received_samples_alone_gives_the_same_state_and_gaps(arrivals: list[int]) -> None:
    """Restart assumption: the file holds only `new` Samples as sample records; anomalies are not needed."""
    live_state, live_events, _ = run(arrivals)
    received = [e.seq for e in live_events if e.kind == "new"]
    replay_state, replay_events, _ = run(received)
    assert (replay_state.highest, replay_state.seen) == (live_state.highest, live_state.seen)
    assert [e.discontinuity for e in replay_events] == [e.discontinuity for e in live_events if e.kind == "new"]
    assert all(e.kind == "new" for e in replay_events)


def test_requirement_13_3_one_missing_one_duplicate_one_reset_are_reported_separately() -> None:
    """The worked example: 0, 1, 4 (2 and 3 missing), 4 again (duplicate), then 2 (reset, below the highest)."""
    _, events, _ = run([0, 1, 4, 4, 2])
    assert [e.kind for e in events] == ["new", "new", "new", "duplicate", "reset"]
    assert [e.discontinuity for e in events] == [None, None, (2, 3), None, None]
    assert [e.missing_count for e in events] == [0, 0, 2, 0, 0]
    assert [e.previous_highest for e in events] == [None, 0, 1, 4, 4]
    missing = [e for e in events if e.discontinuity is not None]
    duplicates = [e for e in events if e.kind == "duplicate"]
    resets = [e for e in events if e.kind == "reset"]
    assert (len(missing), len(duplicates), len(resets)) == (1, 1, 1)  # each anomaly appears once, in its own class
    assert {id(e) for e in missing}.isdisjoint({id(e) for e in duplicates} | {id(e) for e in resets})