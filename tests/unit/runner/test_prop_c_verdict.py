"""Property 22: Experiment C verdict depends only on mandatory conditions.

**Validates: Requirements 11.5, 11.6, 11.10, 11.11, 11.13, 11.14, 11.17, 11.18**

The oracle below is an independent restatement of the six mandatory conditions. It shares no
code with `coroner.runner.evaluate`. These tests exercise decision logic over in-memory
values only. They do not prove that a genuine Docker OOM kill happened; only a real Docker
run does.
"""
from __future__ import annotations

import random
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from hypothesis import given, settings, strategies as st

from coroner.runner import evaluate as ev
from coroner.runner.collector_client import HealthResult
from coroner.runner.evaluate import (
    ContainerState,
    InspectRecord,
    SampleView,
    evaluate_experiment_c,
)

T0 = datetime(2025, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
STOP = T0 + timedelta(seconds=100)
VALID_ID = "0123456789abcdef" * 4
LIMIT = 128 * 1024 * 1024
MISSING = object()
NO_OBJECT = object()
CHECK_NAMES = (
    "oom_killed_true",
    "exit_code_137",
    "collector_survival",
    "preserved_precrash_evidence",
    "measurable_memory_growth",
    "docker_inspect_recorded",
)
SETTINGS = dict(max_examples=400, deadline=None)


# --------------------------------------------------------------------------- builders


def mk_samples(rss_list, recv_kinds=None):
    """Samples in capture order (ts strictly increasing). recv kind: pre, post, equal, none."""
    out = []
    for i, rss in enumerate(rss_list):
        kind = "pre" if recv_kinds is None else recv_kinds[i]
        ts = T0 + timedelta(seconds=i)
        recv = {
            "pre": ts + timedelta(milliseconds=1),
            "post": STOP + timedelta(seconds=1),
            "equal": STOP,
            "none": None,
        }[kind]
        out.append(SampleView(ts=ts, recv_time=recv, rss_bytes=rss, seq=i))
    return out


def mk_inspect(**over):
    f = dict(
        id=VALID_ID,
        status="exited",
        oom_killed=True,
        exit_code=137,
        memory=LIMIT,
        raw_inspect_path="runs/r1/docker_inspect.json",
        inspect_time=T0 + timedelta(seconds=120),
    )
    f.update(over)
    return InspectRecord(**f)


def mk_health(status=200, storage_ok=True):
    if status is NO_OBJECT:
        return None
    h = SimpleNamespace(http_status=status)
    if storage_ok is not MISSING:
        h.storage_ok = storage_ok
    return h


def mk_state(oom=True, code=137):
    if oom is NO_OBJECT:
        return None
    return ContainerState(exit_code=code, oom_killed=oom, status="exited")


def good_args():
    return dict(
        state=mk_state(),
        health=mk_health(),
        samples=mk_samples([100, 200, 300]),
        stop_time=STOP,
        inspect_record=mk_inspect(),
        limit_bytes=LIMIT,
    )


def run(args):
    return evaluate_experiment_c(
        args["state"], args["health"], args["samples"], args["stop_time"],
        args["inspect_record"], args["limit_bytes"],
    )


def failed_names(evaluation):
    return {c.name for c in evaluation.checks if not c.passed}


# --------------------------------------------------------------------------- oracle


def _is_int(v):
    return type(v) is int


def oracle_checks(args):
    """Return the set of failing mandatory check names, from the design pseudocode."""
    failing = set()
    st_ = args["state"]
    if not (st_ is not None and getattr(st_, "oom_killed", None) is True):
        failing.add("oom_killed_true")
    if not (st_ is not None and _is_int(getattr(st_, "exit_code", None)) and st_.exit_code == 137):
        failing.add("exit_code_137")
    h = args["health"]
    if not (
        h is not None
        and _is_int(getattr(h, "http_status", None))
        and h.http_status == 200
        and getattr(h, "storage_ok", None) is True
    ):
        failing.add("collector_survival")
    stop = args["stop_time"]
    pre = [
        s for s in args["samples"]
        if stop is not None and s.recv_time is not None and s.recv_time < stop
    ]
    if not pre:
        failing.add("preserved_precrash_evidence")
    ordered = sorted(args["samples"], key=lambda s: s.ts)
    readable = [s.rss_bytes for s in ordered if s.rss_bytes is not None]
    if not (readable and max(readable) > readable[0]):
        failing.add("measurable_memory_growth")
    if not inspect_complete(args["inspect_record"]):
        failing.add("docker_inspect_recorded")
    return failing


def inspect_complete(r):
    if r is None:
        return False
    return (
        isinstance(r.id, str) and re.fullmatch(r"[0-9a-f]{64}", r.id) is not None
        and isinstance(r.status, str) and r.status != ""
        and type(r.oom_killed) is bool
        and _is_int(r.exit_code)
        and _is_int(r.memory) and r.memory > 0
        and isinstance(r.raw_inspect_path, str) and r.raw_inspect_path != ""
        and isinstance(r.inspect_time, datetime) and r.inspect_time.utcoffset() is not None
    )


def oracle_pass(args):
    return not oracle_checks(args)


# --------------------------------------------------------------------------- strategies


def pick(valid, invalid, weight=4):
    return st.sampled_from([valid] * weight + list(invalid))


oom_s = pick(True, [False, NO_OBJECT, 1, "true", 0, None])
code_s = pick(137, [138, 0, 1, 136, True, "137", None, 137.0, -137])
status_s = pick(200, [500, 503, 201, 204, NO_OBJECT, None, "200", True])
storage_s = pick(True, [False, None, 1, "true", 0, MISSING, "", {}])
limit_s = st.sampled_from([1, 1000, LIMIT, 10**12])

rss_s = st.one_of(st.none(), st.integers(0, 10**9))
recv_s = st.sampled_from(["pre", "pre", "post", "equal", "none"])


@st.composite
def series(draw):
    if draw(st.booleans()):  # constructed: leading nulls, first value, mids, strictly larger last
        nulls = draw(st.integers(0, 3))
        first = draw(st.integers(0, 1000))
        mids = draw(st.lists(rss_s, max_size=6))
        last = first + draw(st.integers(1, 1000))
        rss = [None] * nulls + [first] + mids + [last]
        kinds = [draw(recv_s) for _ in rss]
        kinds[draw(st.integers(0, len(rss) - 1))] = "pre" if draw(st.integers(0, 9)) else "post"
    else:
        rss = draw(st.lists(rss_s, max_size=10))
        kinds = [draw(recv_s) for _ in rss]
    return rss, kinds


inspect_fields = st.fixed_dictionaries(
    {
        "id": pick(VALID_ID, ["a" * 63, "a" * 65, "g" * 64, "", None, 123, VALID_ID[:12]]),
        "status": pick("exited", ["", None, 5]),
        "oom_killed": pick(True, [False, None, "true", 1]),
        "exit_code": pick(137, [138, 0, None, "137", True]),
        "memory": pick(LIMIT, [None, 0, -1, "128m", True, 1.5]),
        "raw_inspect_path": pick("runs/r1/docker_inspect.json", ["", None, 5]),
        "inspect_time": pick(
            T0 + timedelta(seconds=120),
            [datetime(2025, 3, 1, 12, 2), None, "2025-03-01T12:02:00Z"],
        ),
    }
)
inspect_s = st.one_of(st.just(NO_OBJECT), inspect_fields.map(lambda d: InspectRecord(**d)))


@st.composite
def scenarios(draw):
    rss, kinds = draw(series())
    stop = draw(st.sampled_from([STOP] * 6 + [None]))
    insp = draw(inspect_s)
    return dict(
        state=mk_state(draw(oom_s), draw(code_s)),
        health=mk_health(draw(status_s), draw(storage_s)),
        samples=mk_samples(rss, kinds),
        stop_time=stop,
        inspect_record=None if insp is NO_OBJECT else insp,
        limit_bytes=draw(limit_s),
    )


# --------------------------------------------------------------------------- Property 22


@settings(**SETTINGS)
@given(scenarios())
def test_verdict_passes_iff_all_six_mandatory_conditions_hold(args):
    result = run(args)
    assert result.passed is oracle_pass(args)
    assert failed_names(result) == oracle_checks(args)
    assert tuple(c.name for c in result.checks) == CHECK_NAMES


@settings(**SETTINGS)
@given(series(), limit_s)
def test_all_valid_non_series_inputs_pass_exactly_when_series_is_good(ser, limit):
    rss, kinds = ser
    args = good_args() | dict(samples=mk_samples(rss, kinds), limit_bytes=limit)
    assert run(args).passed is oracle_pass(args)


@settings(max_examples=300, deadline=None)
@given(scenarios(), st.data())
def test_shuffled_input_order_uses_capture_order(args, data):
    shuffled = list(args["samples"])
    random.Random(data.draw(st.integers(0, 10**6))).shuffle(shuffled)
    assert run(args | dict(samples=shuffled)).passed is oracle_pass(args)


# one mandatory condition broken at a time, from an otherwise passing scenario
BREAKS = {
    "oom_false": (dict(state=mk_state(False, 137)), "oom_killed_true"),
    "oom_none": (dict(state=mk_state(None, 137)), "oom_killed_true"),
    "oom_truthy_int": (dict(state=mk_state(1, 137)), "oom_killed_true"),
    "oom_truthy_str": (dict(state=mk_state("true", 137)), "oom_killed_true"),
    "exit_138": (dict(state=mk_state(True, 138)), "exit_code_137"),
    "exit_0": (dict(state=mk_state(True, 0)), "exit_code_137"),
    "exit_str": (dict(state=mk_state(True, "137")), "exit_code_137"),
    "exit_none": (dict(state=mk_state(True, None)), "exit_code_137"),
    "state_none": (dict(state=None), "oom_killed_true|exit_code_137"),
    "health_none": (dict(health=None), "collector_survival"),
    "storage_false": (dict(health=mk_health(200, False)), "collector_survival"),
    "storage_missing": (dict(health=mk_health(200, MISSING)), "collector_survival"),
    "storage_null": (dict(health=mk_health(200, None)), "collector_survival"),
    "storage_int_1": (dict(health=mk_health(200, 1)), "collector_survival"),
    "storage_str_true": (dict(health=mk_health(200, "true")), "collector_survival"),
    "status_500": (dict(health=mk_health(500, True)), "collector_survival"),
    "status_503": (dict(health=mk_health(503, True)), "collector_survival"),
    "status_none": (dict(health=mk_health(None, True)), "collector_survival"),
    "no_samples": (dict(samples=[]), "preserved_precrash_evidence|measurable_memory_growth"),
    "all_received_after_stop": (
        dict(samples=mk_samples([100, 200, 300], ["post"] * 3)), "preserved_precrash_evidence"),
    "received_exactly_at_stop": (
        dict(samples=mk_samples([100, 200, 300], ["equal"] * 3)), "preserved_precrash_evidence"),
    "recv_time_absent": (
        dict(samples=mk_samples([100, 200, 300], ["none"] * 3)), "preserved_precrash_evidence"),
    "stop_time_none": (dict(stop_time=None), "preserved_precrash_evidence"),
    "flat_rss": (dict(samples=mk_samples([500, 500, 500])), "measurable_memory_growth"),
    "decreasing_rss": (dict(samples=mk_samples([500, 400, 300])), "measurable_memory_growth"),
    "single_sample": (dict(samples=mk_samples([500])), "measurable_memory_growth"),
    "all_null_rss": (dict(samples=mk_samples([None, None, None])), "measurable_memory_growth"),
    "peak_is_first": (dict(samples=mk_samples([900, 100, 200])), "measurable_memory_growth"),
    "inspect_none": (dict(inspect_record=None), "docker_inspect_recorded"),
    "inspect_no_id": (dict(inspect_record=mk_inspect(id=None)), "docker_inspect_recorded"),
    "inspect_short_id": (dict(inspect_record=mk_inspect(id=VALID_ID[:12])), "docker_inspect_recorded"),
    "inspect_63_id": (dict(inspect_record=mk_inspect(id=VALID_ID[:63])), "docker_inspect_recorded"),
    "inspect_65_id": (dict(inspect_record=mk_inspect(id=VALID_ID + "a")), "docker_inspect_recorded"),
    "inspect_nonhex_id": (dict(inspect_record=mk_inspect(id="z" * 64)), "docker_inspect_recorded"),
    "inspect_no_status": (dict(inspect_record=mk_inspect(status=None)), "docker_inspect_recorded"),
    "inspect_no_oom": (dict(inspect_record=mk_inspect(oom_killed=None)), "docker_inspect_recorded"),
    "inspect_no_exit": (dict(inspect_record=mk_inspect(exit_code=None)), "docker_inspect_recorded"),
    "inspect_no_memory": (dict(inspect_record=mk_inspect(memory=None)), "docker_inspect_recorded"),
    "inspect_no_path": (dict(inspect_record=mk_inspect(raw_inspect_path=None)), "docker_inspect_recorded"),
    "inspect_empty_path": (dict(inspect_record=mk_inspect(raw_inspect_path="")), "docker_inspect_recorded"),
    "inspect_no_time": (dict(inspect_record=mk_inspect(inspect_time=None)), "docker_inspect_recorded"),
}


@pytest.mark.parametrize("name", sorted(BREAKS))
def test_each_mandatory_condition_failing_alone_fails_the_experiment(name):
    change, expected = BREAKS[name]
    args = good_args() | change
    result = run(args)
    assert result.passed is False
    assert failed_names(result) == set(expected.split("|"))
    assert failed_names(result) == oracle_checks(args)


def test_uppercase_container_id_is_not_a_valid_64_hex_docker_id():
    # Docker prints lowercase IDs; every other module in the project accepts lowercase only.
    args = good_args() | dict(inspect_record=mk_inspect(id=VALID_ID.upper()))
    result = run(args)
    assert result.passed is False
    assert failed_names(result) == {"docker_inspect_recorded"}


def test_good_baseline_passes_all_six_checks():
    result = run(good_args())
    assert result.passed is True
    assert [c.name for c in result.checks] == list(CHECK_NAMES)
    assert all(c.passed for c in result.checks)


def test_real_health_result_requires_storage_ok_true():
    def hr(status, storage, present=True):
        return HealthResult(status, storage, present, status == 200 and storage is True, T0, 1.0)

    assert run(good_args() | dict(health=hr(200, True))).passed is True
    for bad in (hr(200, False), hr(200, None, False), hr(200, 1), hr(200, "true"), hr(503, True), hr(None, None, False)):
        assert run(good_args() | dict(health=bad)).passed is False


@pytest.mark.parametrize(
    "rss, expected",
    [
        ([None, None, 5, 9], True),    # leading nulls skipped; first readable is 5
        ([None, 5, 5], False),         # a null is not an invented 0 (0 < 5 would pass)
        ([None, 0, 5], True),          # 0 is a real reading
        ([0, 0, 0], False),
        ([None, None], False),         # no measurement
        ([None], False),
        ([], False),
        ([7], False),
        ([5, 9, 2], True),             # peak above first; the last value is irrelevant
        ([5, None, 5], False),
        ([5, None, 6], True),
    ],
)
def test_growth_check_examples(rss, expected):
    result = run(good_args() | dict(samples=mk_samples(rss)))
    assert ("measurable_memory_growth" not in failed_names(result)) is expected
    assert result.passed is expected


def test_first_sample_is_by_capture_time_not_list_position():
    forward = mk_samples([100, 900, 50])
    assert run(good_args() | dict(samples=forward)).passed is True
    assert run(good_args() | dict(samples=list(reversed(forward)))).passed is True  # same ordering by ts
    flat_when_sorted = mk_samples([500, 500, 500])
    assert run(good_args() | dict(samples=list(reversed(flat_when_sorted)))).passed is False


# --------------------------------------------------------------------------- diagnostics never decide


@settings(**SETTINGS)
@given(
    first=st.integers(0, 10**6),
    gain=st.integers(1, 10**6),
    tail_a=st.lists(st.integers(0, 10**6), max_size=8),
    tail_b=st.lists(st.integers(0, 10**6), max_size=8),
    limit_a=st.integers(1, 10**12),
    limit_b=st.integers(1, 10**12),
)
def test_changing_only_diagnostic_inputs_never_changes_the_outcome(first, gain, tail_a, tail_b, limit_a, limit_b):
    peak = first + gain
    clip = lambda xs: [min(x, peak) for x in xs]  # keeps first and peak fixed
    a = [first, peak] + clip(tail_a)
    b = [first, peak] + sorted(clip(tail_b), reverse=True)  # decreasing tail shape
    results = {
        run(good_args() | dict(samples=mk_samples(rss), limit_bytes=lim)).passed
        for rss in (a, b)
        for lim in (limit_a, limit_b)
    }
    assert results == {True}


@pytest.mark.parametrize("limit", [100_000, 10**9, 10**15])
def test_peak_ratio_of_one_percent_or_less_still_passes(limit):
    peak = limit // 100
    result = run(good_args() | dict(samples=mk_samples([peak // 2, peak]), limit_bytes=limit))
    assert result.passed is True


def test_non_monotonic_tail_still_passes():
    rss = [10, 900, 800, 700, 600, 500, 100]
    assert run(good_args() | dict(samples=mk_samples(rss))).passed is True


def test_verdict_never_calls_diagnostic_metrics(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("verdict must not read Diagnostic_Metrics")

    monkeypatch.setattr(ev, "diagnostic_metrics", boom)
    assert run(good_args()).passed is True
    assert run(good_args() | dict(state=mk_state(False, 137))).passed is False


def test_missing_inputs_fail_without_raising():
    result = evaluate_experiment_c(None, None, [], None, None, LIMIT)
    assert result.passed is False
    assert failed_names(result) == set(CHECK_NAMES)


# --------------------------------------------------------------------------- negative controls
# In-memory mutants of the oracle. Each is shown to disagree with the oracle (and so with the
# production verdict, which equals the oracle) on a witness input.


def _ordered_rss(args):
    return [s.rss_bytes for s in sorted(args["samples"], key=lambda s: s.ts)]


def mutant_uses_diagnostics(args):
    base = oracle_pass(args)
    rss = [r for r in _ordered_rss(args) if r is not None]
    tail = _ordered_rss(args)[-5:]
    monotonic = bool(tail) and None not in tail and all(a <= b for a, b in zip(tail, tail[1:]))
    ratio_ok = bool(rss) and max(rss) / args["limit_bytes"] >= 0.8
    return base and monotonic and ratio_ok


def mutant_null_as_zero(args):
    failing = oracle_checks(args) - {"measurable_memory_growth"}
    rss = [r or 0 for r in _ordered_rss(args)]
    if not (rss and max(rss) > rss[0]):
        failing.add("measurable_memory_growth")
    return not failing


def mutant_truthy_storage_ok(args):
    failing = oracle_checks(args) - {"collector_survival"}
    h = args["health"]
    if not (h is not None and getattr(h, "http_status", None) == 200 and getattr(h, "storage_ok", None)):
        failing.add("collector_survival")
    return not failing


def mutant_last_instead_of_peak(args):
    failing = oracle_checks(args) - {"measurable_memory_growth"}
    rss = [r for r in _ordered_rss(args) if r is not None]
    if not (rss and rss[-1] > rss[0]):
        failing.add("measurable_memory_growth")
    return not failing


def mutant_received_after_stop_counts(args):
    failing = oracle_checks(args) - {"preserved_precrash_evidence"}
    if not args["samples"]:
        failing.add("preserved_precrash_evidence")
    return not failing


WITNESSES = {
    "uses_diagnostics": (
        mutant_uses_diagnostics, good_args() | dict(samples=mk_samples([10, 900, 800, 700, 600, 500, 100]))),
    "uses_diagnostics_small_peak": (
        mutant_uses_diagnostics, good_args() | dict(samples=mk_samples([10, 20, 30]))),
    "null_as_zero": (
        mutant_null_as_zero, good_args() | dict(samples=mk_samples([None, 5, 5]))),
    "truthy_storage_ok": (
        mutant_truthy_storage_ok, good_args() | dict(health=mk_health(200, 1))),
    "last_instead_of_peak": (
        mutant_last_instead_of_peak, good_args() | dict(samples=mk_samples([5, 9, 2]))),
    "late_received_counts": (
        mutant_received_after_stop_counts,
        good_args() | dict(samples=mk_samples([100, 200, 300], ["post"] * 3))),
}


@pytest.mark.parametrize("name", sorted(WITNESSES))
def test_mutant_disagrees_with_oracle_and_production_follows_oracle(name):
    mutant, args = WITNESSES[name]
    assert mutant(args) is not oracle_pass(args)
    assert run(args).passed is oracle_pass(args)
