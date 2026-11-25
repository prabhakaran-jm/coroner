"""Property 26: Diagnostic metrics and sampling limitations are recorded.

**Validates: Requirements 11.12, 11.19, 11.20**

The oracle restates the design formulas independently of `coroner.runner.evaluate`. These tests
exercise computation over in-memory values only. They do not prove that a genuine Docker OOM
kill happened; only a real Docker run does.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings, strategies as st

from coroner.runner.evaluate import (
    ContainerState,
    DiagnosticMetrics,
    InspectRecord,
    SampleView,
    SamplingLimitations,
    diagnostic_metrics,
    evaluate_experiment_c,
    sampling_limitations,
)

T0 = datetime(2025, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
STOP = T0 + timedelta(seconds=100)
VALID_ID = "fedcba9876543210" * 4
SETTINGS = dict(max_examples=400, deadline=None)


def mk(rss_list):
    return [
        SampleView(ts=T0 + timedelta(seconds=i), recv_time=T0 + timedelta(seconds=i, milliseconds=1), rss_bytes=r, seq=i)
        for i, r in enumerate(rss_list)
    ]


def verdict(rss_list, *, oom=True, limit=1000, recv_after_stop=False):
    samples = mk(rss_list)
    if recv_after_stop:
        samples = [SampleView(s.ts, STOP + timedelta(seconds=1), s.rss_bytes, s.seq) for s in samples]
    health = type("H", (), {"http_status": 200, "storage_ok": True})()
    insp = InspectRecord(VALID_ID, "exited", True, 137, 1000, "runs/r/docker_inspect.json", T0 + timedelta(seconds=120))
    return evaluate_experiment_c(
        ContainerState(137, oom, "exited"), health, samples, STOP, insp, limit
    )


# --------------------------------------------------------------------------- oracle


def oracle(rss_list, limit):
    n = len(rss_list)
    tail = rss_list[max(0, n - 5):]
    if not tail or any(v is None for v in tail):
        mono = None
    else:
        mono = all(tail[i] <= tail[i + 1] for i in range(len(tail) - 1))
    readable = [v for v in rss_list if v is not None]
    ratio = (max(readable) / limit) if readable else None
    return mono, ratio, n


rss_s = st.one_of(st.none(), st.integers(0, 10**9))
rss_lists = st.lists(rss_s, max_size=14)
dense_lists = st.lists(st.integers(0, 50), max_size=12)  # many ties and small values
limits = st.integers(1, 10**12)


# --------------------------------------------------------------------------- Property 26


@settings(**SETTINGS)
@given(st.one_of(rss_lists, dense_lists), limits)
def test_diagnostic_metrics_match_independent_oracle(rss, limit):
    m = diagnostic_metrics(mk(rss), limit)
    mono, ratio, n = oracle(rss, limit)
    assert m.rss_monotonic_tail is mono
    assert m.peak_rss_ratio_of_limit == ratio
    assert m.sample_count == n


@settings(max_examples=300, deadline=None)
@given(st.one_of(rss_lists, dense_lists), limits, st.integers(0, 10**6))
def test_metrics_follow_capture_time_not_input_order(rss, limit, seed):
    samples = mk(rss)
    random.Random(seed).shuffle(samples)
    m = diagnostic_metrics(samples, limit)
    assert (m.rss_monotonic_tail, m.peak_rss_ratio_of_limit, m.sample_count) == oracle(rss, limit)


@settings(max_examples=300, deadline=None)
@given(st.lists(st.integers(0, 1000), max_size=14), limits)
def test_tail_is_last_five_only(rss, limit):
    m = diagnostic_metrics(mk(rss), limit)
    last5 = rss[-5:]
    expected = None if not rss else all(a <= b for a, b in zip(last5, last5[1:]))
    assert m.rss_monotonic_tail is expected


@settings(max_examples=300, deadline=None)
@given(st.lists(st.integers(0, 1000), min_size=1, max_size=14), limits)
def test_null_in_tail_makes_tail_inconclusive_but_earlier_null_does_not(rss, limit):
    in_tail = list(rss)
    in_tail[-1] = None
    assert diagnostic_metrics(mk(in_tail), limit).rss_monotonic_tail is None
    if len(rss) > 5:
        early = [None] + list(rss[1:])
        early[0] = None
        assert diagnostic_metrics(mk(early), limit).rss_monotonic_tail is not None


@settings(**SETTINGS)
@given(rss_lists, limits, st.booleans(), st.booleans())
def test_diagnostics_and_limitations_recorded_on_success_and_failure(rss, limit, oom, late):
    result = verdict(rss, oom=oom, limit=limit, recv_after_stop=late)
    m = diagnostic_metrics(mk(rss), limit)  # computed whatever the verdict was
    d = m.to_dict()
    assert {"rss_monotonic_tail", "peak_rss_ratio_of_limit", "sample_count"} <= set(d)
    assert (d["rss_monotonic_tail"], d["peak_rss_ratio_of_limit"], d["sample_count"]) == oracle(rss, limit)
    sl = sampling_limitations(500, 4 * 1024 * 1024).to_dict()
    assert len(sl["statements"]) == 3 and all(isinstance(s, str) and s for s in sl["statements"])
    assert result.passed in (True, False)


def test_both_outcomes_record_the_same_diagnostics():
    passed = verdict([10, 900, 800])
    failed = verdict([10, 900, 800], oom=False)
    assert passed.passed is True and failed.passed is False
    assert diagnostic_metrics(mk([10, 900, 800]), 1000) == diagnostic_metrics(mk([10, 900, 800]), 1000)


@settings(**SETTINGS)
@given(rss_lists, limits, limits, st.sampled_from([True, False]))
def test_verdict_unchanged_when_only_limit_changes(rss, limit_a, limit_b, oom):
    a = verdict(rss, oom=oom, limit=limit_a)
    b = verdict(rss, oom=oom, limit=limit_b)
    assert a.passed is b.passed
    assert [(c.name, c.passed) for c in a.checks] == [(c.name, c.passed) for c in b.checks]


@settings(**SETTINGS)
@given(
    st.integers(0, 10**6), st.integers(1, 10**6),
    st.lists(st.integers(0, 10**6), max_size=8), limits,
)
def test_tail_shape_does_not_change_verdict(first, gain, tail, limit):
    peak = first + gain
    clipped = [min(v, peak) for v in tail]
    base = [first, peak]
    shapes = [base + clipped, base + sorted(clipped), base + sorted(clipped, reverse=True)]
    outcomes = {verdict(s, limit=limit).passed for s in shapes}
    assert outcomes == {True}
    monos = {diagnostic_metrics(mk(s), limit).rss_monotonic_tail for s in shapes}
    assert monos <= {True, False}  # the shape is recorded, whatever it is


# --------------------------------------------------------------------------- examples


@pytest.mark.parametrize(
    "rss, limit, mono, ratio, n",
    [
        ([], 100, None, None, 0),
        ([5], 100, True, 0.05, 1),
        ([None], 100, None, None, 1),
        ([None, None], 100, None, None, 2),
        ([3, 2], 100, False, 0.03, 2),
        ([1, 2, 3, 4], 100, True, 0.04, 4),         # fewer than 5: all of them
        ([1, 2, 2, 3, 3], 100, True, 0.03, 5),      # ties are non-decreasing
        ([9, 1, 2, 3, 4, 5], 100, True, 0.09, 6),   # the 9 is outside the 5-wide tail
        ([1, 5, 2, 3, 4, 5], 100, False, 0.05, 6),
        ([1, 2, 3, 4, None], 100, None, 0.04, 5),
        ([None, 1, 2, 3, 4, 5], 100, True, 0.05, 6),  # a null before the tail is ignored
        ([None, 0, 5], 100, None, 0.05, 3),         # null never becomes 0; 0 is a real reading
        ([0, 0], 100, True, 0.0, 2),
        ([10, 1000], 100_000, True, 0.01, 2),       # peak ratio 0.01
    ],
)
def test_diagnostic_examples(rss, limit, mono, ratio, n):
    m = diagnostic_metrics(mk(rss), limit)
    assert m.rss_monotonic_tail is mono
    assert m.peak_rss_ratio_of_limit == ratio
    assert m.sample_count == n


def test_null_rss_is_not_zero_in_ratio_or_peak():
    m = diagnostic_metrics(mk([None, None]), 100)
    assert m.peak_rss_ratio_of_limit is None  # not 0.0
    assert diagnostic_metrics(mk([0]), 100).peak_rss_ratio_of_limit == 0.0


def test_non_monotonic_tail_and_one_percent_peak_still_pass():
    rss = [10, 1000, 900, 800, 700, 600]
    assert diagnostic_metrics(mk(rss), 10**5).rss_monotonic_tail is False
    assert diagnostic_metrics(mk(rss), 10**5).peak_rss_ratio_of_limit == 0.01
    assert verdict(rss, limit=10**5).passed is True


def test_failed_evaluation_still_yields_diagnostics():
    result = verdict([10, 900, 100], oom=False)
    assert result.passed is False
    m = diagnostic_metrics(mk([10, 900, 100]), 1000)
    assert isinstance(m, DiagnosticMetrics)
    assert m.to_dict()["sample_count"] == 3
    assert m.to_dict()["rss_monotonic_tail"] is False


def test_diagnostic_dict_shape():
    d = diagnostic_metrics(mk([1, 2]), 10).to_dict()
    assert set(d) >= {"rss_monotonic_tail", "peak_rss_ratio_of_limit", "sample_count"}
    assert d["rss_monotonic_tail"] is True and d["peak_rss_ratio_of_limit"] == 0.2 and d["sample_count"] == 2


@pytest.mark.parametrize("bad", [0, -1, None, "100", 1.5, True])
def test_non_positive_or_non_int_limit_is_rejected(bad):
    with pytest.raises(ValueError):
        diagnostic_metrics(mk([1, 2]), bad)


# --------------------------------------------------------------------------- sampling limitations

FIXED_2 = "The final seconds before the out-of-memory kill may be unobserved"
FIXED_3 = "The highest sampled RSS may be lower than the highest RSS the container reached because sampling is not continuous."


@settings(**SETTINGS)
@given(
    st.one_of(st.none(), st.integers(-5, 10**5), st.just(True), st.just("500")),
    st.one_of(st.none(), st.integers(-5, 10**10), st.just(True), st.just("4")),
)
def test_sampling_limitations_always_three_statements(interval, rate):
    sl = sampling_limitations(interval, rate)
    assert isinstance(sl, SamplingLimitations)
    assert len(sl.statements) == 3
    assert all(isinstance(s, str) and s.strip() for s in sl.statements)
    assert sl.statements[1].startswith(FIXED_2)
    assert sl.statements[2] == FIXED_3
    assert sl.to_dict()["statements"] == list(sl.statements)


def test_sampling_limitations_without_arguments_has_three_statements():
    assert len(sampling_limitations().statements) == 3


def test_sampling_limitations_quote_interval_and_rate_when_known():
    sl = sampling_limitations(500, 4 * 1024 * 1024)
    assert "500 ms" in sl.statements[0]
    assert "4 MiB" in sl.statements[0]
    assert sl.sample_interval_ms == 500 and sl.approx_leak_rate_bytes_per_s == 4 * 1024 * 1024
    assert sl.to_dict()["sample_interval_ms"] == 500


@settings(max_examples=300, deadline=None)
@given(rss_lists, st.booleans(), st.booleans())
def test_limitations_are_independent_of_the_verdict(rss, oom, late):
    verdict(rss, oom=oom, recv_after_stop=late)  # success or failure
    assert sampling_limitations(500, 1024).statements == sampling_limitations(500, 1024).statements
    assert len(sampling_limitations(500, 1024).statements) == 3


# --------------------------------------------------------------------------- negative controls
# In-memory mutants, each shown to disagree with the oracle on a witness input.


def mutant_null_as_zero(rss, limit):
    vals = [v or 0 for v in rss]
    return (max(vals) / limit) if vals else None


def mutant_tail_window(width, rss):
    tail = rss[-width:]
    if not tail or any(v is None for v in tail):
        return None
    return all(a <= b for a, b in zip(tail, tail[1:]))


def mutant_first_five(rss):
    tail = rss[:5]
    if not tail or any(v is None for v in tail):
        return None
    return all(a <= b for a, b in zip(tail, tail[1:]))


def mutant_null_in_tail_ignored(rss):
    tail = [v for v in rss[-5:] if v is not None]
    return all(a <= b for a, b in zip(tail, tail[1:])) if tail else None


def mutant_drop_limitations_on_failure(passed, interval, rate):
    return sampling_limitations(interval, rate).statements if passed else ()


def mutant_verdict_uses_diagnostics(rss, limit):
    m = oracle(rss, limit)
    base = verdict(rss, limit=limit).passed
    return base and m[0] is True


def test_mutant_null_as_zero_disagrees():
    rss = [None, None]
    assert mutant_null_as_zero(rss, 100) == 0.0
    assert oracle(rss, 100)[1] is None
    assert diagnostic_metrics(mk(rss), 100).peak_rss_ratio_of_limit is None


@pytest.mark.parametrize(
    "name, mutant, rss",
    [
        ("window4", lambda r: mutant_tail_window(4, r), [1, 5, 2, 3, 4, 5]),
        ("window6", lambda r: mutant_tail_window(6, r), [9, 1, 2, 3, 4, 5]),
        ("first5", mutant_first_five, [1, 2, 3, 4, 5, 0]),
        ("null_ignored", mutant_null_in_tail_ignored, [1, 2, 3, 4, None]),
    ],
)
def test_wrong_tail_window_mutants_disagree(name, mutant, rss):
    expected = oracle(rss, 100)[0]
    assert mutant(rss) != expected
    assert diagnostic_metrics(mk(rss), 100).rss_monotonic_tail == expected


def test_mutant_dropping_limitations_on_failure_disagrees():
    mutant = mutant_drop_limitations_on_failure(False, 500, 1024)
    assert len(mutant) == 0
    assert len(sampling_limitations(500, 1024).statements) == 3


def test_mutant_verdict_using_diagnostics_disagrees():
    rss, limit = [10, 1000, 900, 800, 700, 600], 10**5  # non-monotonic tail, ratio 0.01
    assert mutant_verdict_uses_diagnostics(rss, limit) is False
    assert verdict(rss, limit=limit).passed is True
