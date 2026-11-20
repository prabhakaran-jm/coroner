"""Property 27 and Experiment B timing examples (task 6.6).

**Validates: Requirements 10.8, 10.10, 10.11, 10.12, 10.13**

Pure tests: no Docker, network, signals or clock. The production classifier is compared with an
independent oracle that works on integer microseconds derived from the datetime fields
(proleptic ordinal, wall clock and UTC offset), and never calls production code or uses
datetime subtraction or timedelta comparison.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from coroner.runner.evaluate import (
    CLOCK_TOLERANCE_MS,
    ContainerState,
    SampleView,
    classify_b_samples,
    evaluate_experiment_b,
)

PRE, WITHIN, BEYOND = "pre_kill", "within_tolerance", "post_kill_beyond_tolerance"
UTC = timezone.utc
BASE_US = 1_750_000_000_000_000  # 2025-06-15 in UTC microseconds since 1970 (arbitrary)

PROP = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


# ---------------------------------------------------------------------------------------
# Independent oracle (integer microseconds)
# ---------------------------------------------------------------------------------------

_EPOCH_ORDINAL = 719163  # date(1970, 1, 1).toordinal(), written out so the oracle stays literal


def us_of(dt: datetime) -> int:
    """UTC microseconds since 1970 computed from the wall-clock fields and the UTC offset."""
    off = dt.utcoffset()
    off_us = (off.days * 86_400 + off.seconds) * 1_000_000 + off.microseconds
    days = dt.toordinal() - _EPOCH_ORDINAL
    wall = (((days * 24 + dt.hour) * 60 + dt.minute) * 60 + dt.second) * 1_000_000 + dt.microsecond
    return wall - off_us


@dataclass(frozen=True)
class Spec:
    """One generated Sample in integer microseconds (UTC) plus the display UTC offset."""

    idx: int
    ts_us: int
    recv_us: int | None
    tz_min: int = 0


def to_dt(us: int, tz_min: int = 0) -> datetime:
    tz = timezone(timedelta(minutes=tz_min))
    return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=us)).astimezone(tz)


def to_view(sp: Spec) -> SampleView:
    return SampleView(
        ts=to_dt(sp.ts_us, sp.tz_min),
        recv_time=None if sp.recv_us is None else to_dt(sp.recv_us, -sp.tz_min),
        seq=sp.idx,
    )


def oracle_one(ts_us, recv_us, kill_us, fin_us, tol_ms, off_ms):
    """-> (primary, late_received, after_stop)."""
    capture = ts_us - off_ms * 1000
    limit = kill_us + tol_ms * 1000
    if capture < kill_us:
        primary = PRE
    elif capture <= limit:
        primary = WITHIN
    else:
        primary = BEYOND
    late = primary == PRE and recv_us is not None and recv_us > kill_us
    stopped = fin_us is not None and capture > fin_us
    return primary, late, stopped


def oracle(specs, kill_us, fin_us, tol_ms, off_ms):
    return {s.idx: oracle_one(s.ts_us, s.recv_us, kill_us, fin_us, tol_ms, off_ms) for s in specs}


def oracle_timing_ok(table) -> bool:
    cats = [v[0] for v in table.values()]
    return PRE in cats and BEYOND not in cats


def run_production(specs, kill_us, fin_us, tol_ms, off_ms, kill_tz=0, fin_tz=0):
    views = [to_view(s) for s in specs]
    t = classify_b_samples(
        views,
        to_dt(kill_us, kill_tz),
        None if fin_us is None else to_dt(fin_us, fin_tz),
        tol_ms,
        off_ms,
    )
    return t, views


def production_table(t):
    """Per-sample (primary, late, stopped) from a BTiming, keyed by seq."""
    out: dict[int, list] = {}
    for name, group in ((PRE, t.pre_kill), (WITHIN, t.within_tolerance), (BEYOND, t.post_kill_beyond_tolerance)):
        for s in group:
            assert s.seq not in out, f"sample {s.seq} has more than one primary category"
            out[s.seq] = [name, False, False]
    for s in t.pre_kill_late_received:
        out[s.seq][1] = True
    for s in t.after_container_stop:
        out[s.seq][2] = True
    return {k: tuple(v) for k, v in out.items()}


# ---------------------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------------------

tol_st = st.integers(0, CLOCK_TOLERANCE_MS)
off_st = st.integers(-5_000, 5_000)
tz_st = st.sampled_from([0, 330, -480, 60, -300, 545, 840, -720])
kill_st = st.integers(BASE_US, BASE_US + 10**12)


def delta_st(tol_ms):
    edges = [-1, 0, 1, tol_ms * 1000 - 1, tol_ms * 1000, tol_ms * 1000 + 1]
    return st.one_of(st.sampled_from(edges), st.integers(-1_000_000, 1_000_000), st.integers(-10**10, 10**10))


@st.composite
def scenario(draw, min_size=0, max_size=12):
    kill = draw(kill_st)
    tol = draw(tol_st)
    off = draw(off_st)
    n = draw(st.integers(min_size, max_size))
    fin = draw(st.one_of(st.none(), st.integers(-500_000, 3_000_000).map(lambda d: kill + d)))
    specs = []
    for i in range(n):
        d = draw(delta_st(tol))
        recv = draw(st.one_of(st.none(), st.integers(-2_000_000, 60_000_000).map(lambda r: kill + r)))
        specs.append(Spec(i, kill + d + off * 1000, recv, draw(tz_st)))
    order = draw(st.permutations(specs))  # arrival order
    return kill, fin, tol, off, list(order)


# ---------------------------------------------------------------------------------------
# Property 27
# ---------------------------------------------------------------------------------------


@PROP
@given(scenario(), tz_st, tz_st)
def test_p27_classification_matches_oracle(sc, kill_tz, fin_tz):
    kill, fin, tol, off, specs = sc
    t, _ = run_production(specs, kill, fin, tol, off, kill_tz, fin_tz)
    assert production_table(t) == oracle(specs, kill, fin, tol, off)
    assert t.total_count == len(specs)  # exactly one primary category each
    assert set(t.pre_kill_late_received) <= set(t.pre_kill)
    assert t.tolerance_ms == tol and t.clock_offset_ms == off


@PROP
@given(scenario(min_size=1), st.data())
def test_p27_receive_time_never_changes_classification(sc, data):
    kill, fin, tol, off, specs = sc
    base, _ = run_production(specs, kill, fin, tol, off)
    other = [
        Spec(s.idx, s.ts_us, data.draw(st.one_of(st.none(), st.integers(-10**9, 10**12).map(lambda r: kill + r))), s.tz_min)
        for s in specs
    ]
    changed, _ = run_production(other, kill, fin, tol, off)
    p1, p2 = production_table(base), production_table(changed)
    assert {k: v[0] for k, v in p1.items()} == {k: v[0] for k, v in p2.items()}
    assert {k: v[2] for k, v in p1.items()} == {k: v[2] for k, v in p2.items()}
    assert p2 == oracle(other, kill, fin, tol, off)


@PROP
@given(scenario(), st.randoms(use_true_random=False))
def test_p27_arrival_order_irrelevant(sc, rnd):
    kill, fin, tol, off, specs = sc
    shuffled = list(specs)
    rnd.shuffle(shuffled)
    a = production_table(run_production(specs, kill, fin, tol, off)[0])
    b = production_table(run_production(shuffled, kill, fin, tol, off)[0])
    assert a == b


@PROP
@given(scenario(), st.booleans(), st.booleans())
def test_p27_verdict_iff_pre_kill_and_no_beyond(sc, stopped_ok, handler_ok):
    kill, fin, tol, off, specs = sc
    views = [to_view(s) for s in specs]
    ev = evaluate_experiment_b(
        ContainerState(137, False, "exited"),
        SimpleNamespace(http_status=200, storage_ok=True),
        views,
        to_dt(kill),
        None if fin is None else to_dt(fin),
        off,
        tol,
        stopped_within_10s=stopped_ok,
        no_shutdown_handler_records=handler_ok,
    )
    by_name = {c.name: c.passed for c in ev.checks}
    table = oracle(specs, kill, fin, tol, off)
    cats = [v[0] for v in table.values()]
    assert by_name["at_least_one_sample_captured_before_kill"] == (PRE in cats)
    assert by_name["no_sample_captured_after_kill_beyond_tolerance"] == (BEYOND not in cats)
    # late receipt and after-stop flags are never a reason to fail on their own
    assert ev.passed == (oracle_timing_ok(table) and stopped_ok and handler_ok)


@PROP
@given(scenario(), st.integers(1, 10**9))
def test_p27_late_received_is_valid_evidence(sc, extra_us):
    """Adding a pre-kill sample received long after the kill never makes a passing run fail."""
    kill, fin, tol, off, specs = sc
    before = oracle_timing_ok(oracle(specs, kill, fin, tol, off))
    added = Spec(len(specs), kill - 1 + off * 1000, kill + extra_us, 0)
    after_table = oracle(specs + [added], kill, fin, tol, off)
    t, _ = run_production(specs + [added], kill, fin, tol, off)
    assert production_table(t) == after_table
    assert after_table[added.idx] == (PRE, True, fin is not None and kill - 1 > fin)
    # the added late-received sample supplies the pre-kill evidence; only beyond-tolerance samples can fail
    assert oracle_timing_ok(after_table) == (not any(v[0] == BEYOND for v in after_table.values()))
    assert t.pre_kill_count >= 1
    assert before <= oracle_timing_ok(after_table)


# ---------------------------------------------------------------------------------------
# Oracle self-checks (so the oracle itself is anchored)
# ---------------------------------------------------------------------------------------


def test_oracle_us_of_matches_known_instants():
    assert us_of(datetime(1970, 1, 1, tzinfo=UTC)) == 0
    assert us_of(datetime(1970, 1, 2, 0, 0, 0, 5, tzinfo=UTC)) == 86_400_000_005
    ist = timezone(timedelta(hours=5, minutes=30))
    assert us_of(datetime(1970, 1, 1, 5, 30, tzinfo=ist)) == 0
    pst = timezone(timedelta(hours=-8))
    assert us_of(datetime(1969, 12, 31, 16, 0, tzinfo=pst)) == 0
    sp = Spec(0, BASE_US + 123_456, BASE_US + 7, 330)
    assert us_of(to_view(sp).ts) == sp.ts_us


# ---------------------------------------------------------------------------------------
# Concrete examples
# ---------------------------------------------------------------------------------------

KILL = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
KILL_US = us_of(KILL)


def sv(delta_ms: float, recv_delta_ms: float | None = None, seq: int = 0, tz=UTC) -> SampleView:
    ts = (KILL + timedelta(milliseconds=delta_ms)).astimezone(tz)
    rv = None if recv_delta_ms is None else KILL + timedelta(milliseconds=recv_delta_ms)
    return SampleView(ts=ts, recv_time=rv, seq=seq)


def cls(samples, finished_at=None, tol=250, off=0):
    return classify_b_samples(samples, KILL, finished_at, tol, off)


def verdict(samples, finished_at=None, off=0, tol=250):
    return evaluate_experiment_b(
        ContainerState(137, False, "exited"),
        SimpleNamespace(http_status=200, storage_ok=True),
        samples,
        KILL,
        finished_at,
        off,
        tol,
        stopped_within_10s=True,
        no_shutdown_handler_records=True,
    )


def check(ev, name):
    return next(c for c in ev.checks if c.name == name).passed


def test_pre_kill_capture_received_after_kill_is_accepted_and_counted():
    t = cls([sv(-100, recv_delta_ms=500)])
    assert (t.pre_kill_count, t.pre_kill_late_received_count) == (1, 1)
    assert not t.post_kill_beyond_tolerance
    assert verdict([sv(-100, recv_delta_ms=500)]).passed


def test_pre_kill_received_before_kill_is_not_late():
    t = cls([sv(-100, recv_delta_ms=-50)])
    assert (t.pre_kill_count, t.pre_kill_late_received_count) == (1, 0)


def test_no_pre_kill_sample_fails():
    ev = verdict([sv(10), sv(100)])
    assert not ev.passed
    assert not check(ev, "at_least_one_sample_captured_before_kill")
    assert check(ev, "no_sample_captured_after_kill_beyond_tolerance")


def test_empty_evidence():
    t = cls([])
    assert (t.pre_kill_count, t.within_tolerance_count, len(t.post_kill_beyond_tolerance)) == (0, 0, 0)
    ev = verdict([])
    assert not ev.passed and not check(ev, "at_least_one_sample_captured_before_kill")
    assert check(ev, "no_sample_captured_after_kill_beyond_tolerance")


def test_capture_exactly_at_kill_is_within_tolerance_not_pre_kill():
    t = cls([sv(0)])
    assert (t.pre_kill_count, t.within_tolerance_count) == (0, 1)


def test_capture_one_microsecond_before_kill_is_pre_kill():
    t = cls([SampleView(ts=KILL - timedelta(microseconds=1))])
    assert t.pre_kill_count == 1


def test_capture_at_kill_plus_250ms_is_within_tolerance():
    t = cls([sv(-1), sv(250)])
    assert t.within_tolerance_count == 1 and not t.post_kill_beyond_tolerance
    assert verdict([sv(-1), sv(250)]).passed


def test_capture_at_kill_plus_251ms_fails_and_is_listed():
    s = sv(251, seq=7)
    ev = verdict([sv(-1), s])
    assert not ev.passed
    assert not check(ev, "no_sample_captured_after_kill_beyond_tolerance")
    assert cls([sv(-1), s]).post_kill_beyond_tolerance == (s,)
    assert cls([sv(-1), s]).to_dict()["post_kill_beyond_tolerance"] == ["2025-01-01T12:00:00.251Z"]


def test_one_microsecond_past_tolerance_is_beyond():
    s = SampleView(ts=KILL + timedelta(milliseconds=250, microseconds=1))
    assert cls([s]).post_kill_beyond_tolerance == (s,)


def test_smaller_tolerance_is_honored():
    assert cls([sv(101)], tol=100).post_kill_beyond_tolerance
    assert cls([sv(100)], tol=100).within_tolerance_count == 1


@pytest.mark.parametrize("off_ms, delta_ms, expected", [
    (3, 253, WITHIN),    # raw kill+253, corrected kill+250
    (3, 254, BEYOND),    # corrected kill+251
    (3, 3, WITHIN),      # corrected exactly the kill
    (3, 2, PRE),         # corrected kill-1
    (-40, -39, WITHIN),  # raw kill-39, corrected kill+1
    (-40, -41, PRE),     # corrected kill-1
    (-40, 210, WITHIN),  # corrected kill+250
])
def test_clock_offset_sign(off_ms, delta_ms, expected):
    t = cls([sv(delta_ms, seq=1)], off=off_ms)
    got = (PRE if t.pre_kill else WITHIN if t.within_tolerance else BEYOND)
    assert got == expected


def test_negative_offset_pushes_sample_beyond_tolerance():
    assert cls([sv(211)], off=-40).post_kill_beyond_tolerance  # corrected kill+251


def test_sample_received_long_after_kill_keeps_primary_category():
    for delta, cat in ((-1, PRE), (0, WITHIN), (250, WITHIN), (251, BEYOND)):
        t_near = cls([sv(delta, recv_delta_ms=delta)])
        t_far = cls([sv(delta, recv_delta_ms=3_600_000)])
        for t in (t_near, t_far):
            got = PRE if t.pre_kill else WITHIN if t.within_tolerance else BEYOND
            assert got == cat


def test_after_container_stop_is_flagged_but_does_not_fail_by_itself():
    fin = KILL + timedelta(milliseconds=100)
    late = sv(200, seq=5)  # within tolerance, after FinishedAt
    t = cls([sv(-5), late], finished_at=fin)
    assert t.after_container_stop == (late,)
    assert t.within_tolerance == (late,)
    assert verdict([sv(-5), late], finished_at=fin).passed


def test_after_container_stop_and_beyond_tolerance_fails():
    fin = KILL + timedelta(milliseconds=100)
    late = sv(400, seq=5)
    t = cls([sv(-5), late], finished_at=fin)
    assert t.after_container_stop == (late,) and t.post_kill_beyond_tolerance == (late,)
    assert not verdict([sv(-5), late], finished_at=fin).passed


def test_pre_kill_sample_after_finished_at_is_flagged_and_still_pre_kill():
    fin = KILL - timedelta(milliseconds=50)  # stop recorded before the kill instant
    s = sv(-10)
    t = cls([s], finished_at=fin)
    assert t.pre_kill == (s,) and t.after_container_stop == (s,)
    assert verdict([s], finished_at=fin).passed


def test_finished_at_none_flags_nothing():
    assert cls([sv(10_000)]).after_container_stop == ()


def test_capture_equal_to_finished_at_is_not_after_stop():
    fin = KILL + timedelta(milliseconds=100)
    assert cls([sv(100)], finished_at=fin).after_container_stop == ()
    assert len(cls([sv(101)], finished_at=fin).after_container_stop) == 1


def test_unsorted_arrival_order():
    a, b, c, d = sv(300, seq=1), sv(-20, seq=2), sv(100, seq=3), sv(-90, seq=4)
    t = cls([a, b, c, d])
    assert [s.seq for s in t.pre_kill] == [4, 2]
    assert [s.seq for s in t.within_tolerance] == [3]
    assert [s.seq for s in t.post_kill_beyond_tolerance] == [1]
    assert production_table(t) == production_table(cls([d, c, b, a]))


@pytest.mark.parametrize("tz", [timezone(timedelta(hours=5, minutes=30)), timezone(timedelta(hours=-8)),
                                timezone(timedelta(hours=14)), timezone(timedelta(hours=-12))])
def test_utc_offset_timestamps_compare_as_instants(tz):
    samples = [sv(-1, seq=1, tz=tz), sv(0, seq=2, tz=tz), sv(250, seq=3, tz=tz), sv(251, seq=4, tz=tz)]
    t = classify_b_samples(samples, KILL.astimezone(tz), (KILL + timedelta(seconds=1)).astimezone(tz), 250, 0)
    assert [s.seq for s in t.pre_kill] == [1]
    assert [s.seq for s in t.within_tolerance] == [2, 3]
    assert [s.seq for s in t.post_kill_beyond_tolerance] == [4]
    assert t.after_container_stop == ()


def test_receive_time_with_other_utc_offset_is_compared_as_instant():
    ist = timezone(timedelta(hours=5, minutes=30))
    s = SampleView(ts=KILL - timedelta(milliseconds=10), recv_time=(KILL + timedelta(milliseconds=1)).astimezone(ist), seq=1)
    assert cls([s]).pre_kill_late_received == (s,)
    s2 = SampleView(ts=KILL - timedelta(milliseconds=10), recv_time=KILL.astimezone(ist), seq=2)  # == kill, not after
    assert cls([s2]).pre_kill_late_received == ()


def test_every_sample_has_exactly_one_primary_category():
    samples = [sv(d, seq=i) for i, d in enumerate([-500, -1, 0, 1, 249, 250, 251, 1000])]
    t = cls(samples)
    seen = [s.seq for s in (*t.pre_kill, *t.within_tolerance, *t.post_kill_beyond_tolerance)]
    assert sorted(seen) == list(range(len(samples)))
    assert t.total_count == len(samples)


def test_tolerance_constant_is_250():
    assert CLOCK_TOLERANCE_MS == 250
    with pytest.raises(ValueError):
        cls([sv(0)], tol=251)


# ---------------------------------------------------------------------------------------
# Negative controls: in-memory mutant classifiers must disagree with the oracle
# ---------------------------------------------------------------------------------------


def mutant_boundary_swap(ts, recv, kill, fin, tol, off):
    """capture <= kill is pre_kill; kill+tol is already beyond."""
    c = ts - off * 1000
    p = PRE if c <= kill else WITHIN if c < kill + tol * 1000 else BEYOND
    return p, p == PRE and recv is not None and recv > kill, fin is not None and c > fin


def mutant_tolerance_251(ts, recv, kill, fin, tol, off):
    return oracle_one(ts, recv, kill, fin, 251, off)


def mutant_offset_sign_flipped(ts, recv, kill, fin, tol, off):
    return oracle_one(ts, recv, kill, fin, tol, -off)


def mutant_offset_ignored(ts, recv, kill, fin, tol, off):
    return oracle_one(ts, recv, kill, fin, tol, 0)


def mutant_receive_time_based(ts, recv, kill, fin, tol, off):
    """Uses Receive_Time (when present) instead of Capture_Time for the primary category."""
    basis = recv if recv is not None else ts - off * 1000
    p = PRE if basis < kill else WITHIN if basis <= kill + tol * 1000 else BEYOND
    return p, p == PRE and recv is not None and recv > kill, fin is not None and ts - off * 1000 > fin


def mutant_late_changes_category(ts, recv, kill, fin, tol, off):
    """A late-received pre-kill sample is demoted to within_tolerance."""
    p, late, stopped = oracle_one(ts, recv, kill, fin, tol, off)
    if late:
        return WITHIN, False, stopped
    return p, late, stopped


def mutant_after_stop_overrides_category(ts, recv, kill, fin, tol, off):
    """A sample captured after FinishedAt becomes beyond_tolerance."""
    p, late, stopped = oracle_one(ts, recv, kill, fin, tol, off)
    return (BEYOND if stopped else p), late, stopped


def mutant_late_flag_ignores_primary(ts, recv, kill, fin, tol, off):
    """late flag set for any sample received after the kill, not only pre_kill ones."""
    p, _, stopped = oracle_one(ts, recv, kill, fin, tol, off)
    return p, recv is not None and recv > kill, stopped


def mutant_stop_flag_inclusive(ts, recv, kill, fin, tol, off):
    p, late, _ = oracle_one(ts, recv, kill, fin, tol, off)
    return p, late, fin is not None and ts - off * 1000 >= fin


MUTANTS = {
    "boundary_swap": (mutant_boundary_swap, (KILL_US, None, 250, 0), [Spec(0, KILL_US, None), Spec(1, KILL_US + 250_000, None)]),
    "tolerance_251": (mutant_tolerance_251, (KILL_US, None, 250, 0), [Spec(0, KILL_US + 251_000, None)]),
    "offset_sign_flipped": (mutant_offset_sign_flipped, (KILL_US, None, 250, 40), [Spec(0, KILL_US + 230_000, None)]),
    "offset_ignored": (mutant_offset_ignored, (KILL_US, None, 250, 40), [Spec(0, KILL_US + 20_000, None)]),
    "receive_time_based": (mutant_receive_time_based, (KILL_US, None, 250, 0), [Spec(0, KILL_US - 10_000, KILL_US + 5_000_000)]),
    "late_changes_category": (mutant_late_changes_category, (KILL_US, None, 250, 0), [Spec(0, KILL_US - 1, KILL_US + 1)]),
    "after_stop_overrides_category": (mutant_after_stop_overrides_category, (KILL_US, KILL_US + 100_000, 250, 0), [Spec(0, KILL_US + 200_000, None)]),
    "late_flag_ignores_primary": (mutant_late_flag_ignores_primary, (KILL_US, None, 250, 0), [Spec(0, KILL_US + 10, KILL_US + 20)]),
    "stop_flag_inclusive": (mutant_stop_flag_inclusive, (KILL_US, KILL_US + 100_000, 250, 0), [Spec(0, KILL_US + 100_000, None)]),
}


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_mutant_disagrees_with_oracle_but_production_agrees(name):
    mutant, (kill, fin, tol, off), specs = MUTANTS[name]
    expected = oracle(specs, kill, fin, tol, off)
    mutated = {s.idx: mutant(s.ts_us, s.recv_us, kill, fin, tol, off) for s in specs}
    assert mutated != expected, f"mutant {name} is not caught by its crafted input"
    t, _ = run_production(specs, kill, fin, tol, off)
    assert production_table(t) == expected


def mutant_verdict_after_stop_fails(table) -> bool:
    """Verdict mutant: any after_container_stop sample fails timing."""
    return oracle_timing_ok(table) and not any(v[2] for v in table.values())


def test_mutant_after_stop_failing_verdict_is_caught():
    fin = KILL_US + 100_000
    specs = [Spec(0, KILL_US - 5_000, None), Spec(1, KILL_US + 200_000, None)]  # second: within, after stop
    table = oracle(specs, KILL_US, fin, 250, 0)
    assert oracle_timing_ok(table) is True
    assert mutant_verdict_after_stop_fails(table) is False
    ev = verdict([to_view(s) for s in specs], finished_at=to_dt(fin))
    assert ev.passed is True


def test_mutant_receive_time_verdict_is_caught():
    """A verdict that required receipt before the kill would reject the in-flight case."""
    specs = [Spec(0, KILL_US - 5_000, KILL_US + 1_000)]
    table = oracle(specs, KILL_US, None, 250, 0)
    mutant_ok = all(s.recv_us is None or s.recv_us <= KILL_US for s in specs) and PRE in [v[0] for v in table.values()]
    assert mutant_ok is False and oracle_timing_ok(table) is True
    assert verdict([to_view(s) for s in specs]).passed is True


def test_oracle_vs_production_would_detect_a_swapped_production_boundary():
    """Sanity that the property comparison is sensitive: a mutant table differs on the edge inputs."""
    specs = [Spec(i, KILL_US + d, None) for i, d in enumerate([-1, 0, 250_000, 250_001])]
    good = oracle(specs, KILL_US, None, 250, 0)
    assert [good[i][0] for i in range(4)] == [PRE, WITHIN, WITHIN, BEYOND]
    bad = {s.idx: mutant_boundary_swap(s.ts_us, s.recv_us, KILL_US, None, 250, 0) for s in specs}
    assert bad != good
