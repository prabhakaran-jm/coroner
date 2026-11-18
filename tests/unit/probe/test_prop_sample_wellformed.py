"""Property 15: Probe Samples are well formed and never fabricated (Requirements 2.2-2.7, 2.9, 7.6; D35, D36).

Every reader and the clock are injected and deterministic. `Probe.build_sample` is called directly: no
sampler loop, no sender thread, no network, no sleeps. An independent oracle (`_expected`) rebuilds the
whole Sample from the injected inputs, using datetime arithmetic and literal reason texts instead of
the Probe's helpers, and every field is compared with it.

`dropped` is carried by the Probe and changes only through the sender, so these tests seed it white-box
through `Probe._dropped`.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import event, example, given, settings
from hypothesis import strategies as st

from coroner.evidence.schema import validate_sample
from coroner.probe.probe import MAX_DROPPED, NO_CONTAINER_ID_REASON, Probe, ProbeConfig

RUN_IDS = ["a", "run-1", "A.b_c-9", "x" * 64]
NULLABLE = ("rss_bytes", "request_count", "loop_delay_ms", "container_id")
HEX64 = "0123456789abcdef" * 4
HEX12 = "0123456789ab"
OFFSETS_MIN = [0, 330, -480, 840, -720, 345]
RSS_MAX = 2**63 - 1

SETTINGS = dict(derandomize=True, database=None, deadline=None)


class _Transport:
    def send(self, body: bytes, timeout_s: float) -> bool:  # never called: the Probe is not started
        raise AssertionError("transport must not be used")


# ---- strategies (every outcome is a plain tuple so it can be replayed by @example) ---------------------------
def _aware(naive: datetime, minutes: int) -> datetime:
    return naive.replace(tzinfo=timezone(timedelta(minutes=minutes)))


_naive_dt = st.datetimes(min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1))
_aware_dt = st.builds(_aware, _naive_dt, st.sampled_from(OFFSETS_MIN))


def _weighted(good, other, good_of_10: int):
    """`good` with probability good_of_10/10, else `other` (one_of would merge repeated branches)."""
    return st.tuples(st.integers(0, 9), good, other).map(lambda t: t[1] if t[0] < good_of_10 else t[2])


clocks = _weighted(_aware_dt, _naive_dt, 8)
rss_specs = _weighted(
    st.tuples(st.just("ok"), st.one_of(st.sampled_from([0, 1, 4096, RSS_MAX, 2**64]), st.integers(0, RSS_MAX))),
    st.one_of(
        st.tuples(st.just("exc"), st.sampled_from([OSError, ValueError, PermissionError, RuntimeError])),
        st.tuples(st.just("bad"), st.sampled_from([True, -1, 1.5, "7", None])),
    ),
    6,
)
pid_specs = _weighted(
    st.tuples(st.just("ok"), st.integers(1, 2**31 - 1)),
    st.one_of(
        st.tuples(st.just("exc"), st.sampled_from([OSError, RuntimeError])),
        st.tuples(st.just("bad"), st.sampled_from([True, "5", None, -1, 2.0])),
    ),
    8,
)
_hex64 = st.text("0123456789abcdef", min_size=64, max_size=64)
_hex12 = st.text("0123456789abcdef", min_size=12, max_size=12)
cid_specs = _weighted(
    st.one_of(
        st.tuples(st.just("ok"), st.tuples(_hex64, st.sampled_from(["mountinfo", "cgroup"]))),
        st.tuples(st.just("ok"), st.tuples(_hex12, st.just("hostname"))),
    ),
    st.one_of(
        st.tuples(st.just("none")),
        st.tuples(st.just("exc"), st.sampled_from([OSError, ValueError, PermissionError])),
    ),
    6,
)
loop_specs = _weighted(
    st.one_of(
        st.tuples(st.just("val"), st.sampled_from([0.0, -0.0, 5e-324, 1e-12, 1e12, sys.float_info.max, 0, 7])),
        st.tuples(st.just("val"), st.floats(min_value=0, allow_nan=False, allow_infinity=False)),
    ),
    st.one_of(
        st.tuples(st.just("none"), st.text(min_size=1, max_size=200)),
        st.tuples(st.just("badreason"), st.sampled_from([None, "", "r" * 201, 5])),
        st.tuples(st.just("badloop"), st.sampled_from([float("nan"), float("inf"), -1.0, True, "1", -5e-324])),
    ),
    8,
)
outcomes = st.fixed_dictionaries(
    {"rss": rss_specs, "pid": pid_specs, "cid": cid_specs, "clock": clocks, "loop": loop_specs}
)
ops = st.one_of(st.tuples(st.just("count"), st.integers(0, 5)), st.tuples(st.just("build"), outcomes))


# ---- independent oracle --------------------------------------------------------------------------------------
def _ts_text(dt: datetime) -> str:
    u = dt.replace(tzinfo=None) - dt.utcoffset()
    return (
        f"{u.year:04d}-{u.month:02d}-{u.day:02d}T{u.hour:02d}:{u.minute:02d}:{u.second:02d}"
        f".{u.microsecond // 1000:03d}Z"
    )


def _expected(o: dict, run_id: str, seq: int, requests: int, interval: int, dropped: int):
    """Return ("err", exception type) or ("ok", Sample dict) for the injected outcome `o`."""
    reasons: dict[str, str] = {}
    lk, lv = o["loop"][0], o["loop"][1]
    if lk in ("badreason", "badloop"):
        return ("err", ValueError)
    loop_delay = None
    if lk == "none":
        reasons["loop_delay_ms"] = lv
    else:
        loop_delay = lv
    if o["pid"][0] == "exc":
        return ("err", o["pid"][1])
    if o["pid"][0] == "bad":
        return ("err", ValueError)
    if o["clock"].tzinfo is None:
        return ("err", ValueError)
    rk = o["rss"][0]
    if rk == "ok":
        rss = o["rss"][1]
    else:
        rss = None
        reasons["rss_bytes"] = "rss unavailable: " + (o["rss"][1].__name__ if rk == "exc" else "ValueError")
    ck = o["cid"][0]
    cid, source = (o["cid"][1] if ck == "ok" else (None, "none"))
    if ck == "none":
        reasons["container_id"] = NO_CONTAINER_ID_REASON
    elif ck == "exc":
        reasons["container_id"] = "container id unavailable: " + o["cid"][1].__name__
    sample = {
        "run_id": run_id, "seq": seq, "ts": _ts_text(o["clock"]), "rss_bytes": rss, "request_count": requests,
        "loop_delay_ms": loop_delay, "pid": o["pid"][1], "container_id": cid, "container_id_source": source,
        "interval_ms": interval, "dropped": dropped,
    }
    if reasons:
        sample["null_reasons"] = reasons
    return ("ok", sample)


def _check(sample: dict, exp: dict) -> None:
    assert validate_sample(sample) == []
    assert set(sample) == set(exp)
    assert sample == exp
    for key in ("rss_bytes", "pid", "loop_delay_ms", "request_count", "seq", "dropped"):
        assert type(sample[key]) is type(exp[key]), key
    nulls = {k for k in NULLABLE if sample[k] is None}
    assert set(sample.get("null_reasons", {})) == nulls
    assert ("null_reasons" in sample) == bool(nulls)
    assert all(1 <= len(r) <= 200 for r in sample.get("null_reasons", {}).values())


# ---- driver ----------------------------------------------------------------------------------------------------
def _play(spec):
    if spec[0] == "exc":
        raise spec[1]("injected")
    return spec[1]


def _make_probe(run_id: str, interval: int, dropped: int):
    cur: dict = {}

    def cid():
        if cur["o"]["cid"][0] == "none":
            return (None, "none")
        return _play(cur["o"]["cid"])

    probe = Probe(
        ProbeConfig(run_id=run_id, collector_url="http://unused.invalid:1", interval_ms=interval),
        transport=_Transport(),
        rss_reader=lambda: _play(cur["o"]["rss"]),
        pid_reader=lambda: _play(cur["o"]["pid"]),
        container_id_reader=cid,
        wall_clock=lambda: cur["o"]["clock"],
    )
    probe._dropped = dropped  # white-box: dropped is only changed by the sender
    return probe, cur


def _loop_args(o: dict):
    k, v = o["loop"]
    return {"val": (v, None), "none": (None, v), "badreason": (None, v), "badloop": (v, None)}[k]


def _flags(o: dict) -> list[str]:
    out = []
    if o["pid"][0] != "ok":
        out.append("pid-fail")
    if o["clock"].tzinfo is None:
        out.append("naive-clock")
    if o["rss"][0] != "ok":
        out.append("rss-null")
    if o["cid"][0] != "ok":
        out.append("cid-null")
    if o["loop"][0] == "none":
        out.append("loop-null")
    return out


def _run(op_list, run_id: str, interval: int, dropped: int) -> None:
    probe, cur = _make_probe(run_id, interval, dropped)
    seq = requests = 0
    last_ok: bool | None = None
    for kind, arg in op_list:
        if kind == "count":
            for _ in range(arg):
                probe.count_request()
            requests += arg
            continue
        cur["o"] = arg
        status, exp = _expected(arg, run_id, seq, requests, interval, dropped)
        for flag in _flags(arg):
            event(flag)
        if len([f for f in _flags(arg) if f.endswith("-null")]) > 1:
            event("multi-null")
        if status == "err":
            with pytest.raises(Exception) as info:
                probe.build_sample(*_loop_args(arg))
            assert type(info.value) is exp
        else:
            sample = probe.build_sample(*_loop_args(arg))
            _check(sample, exp)
            seq += 1
        if last_ok is not None:
            event(f"{'success' if last_ok else 'failure'}->{'success' if status == 'ok' else 'failure'}")
        last_ok = status == "ok"
        assert probe.state == {"next_seq": seq, "dropped": dropped}


_OK = {
    "rss": ("ok", 123456), "pid": ("ok", 42), "cid": ("ok", (HEX64, "mountinfo")),
    "clock": datetime(2024, 5, 6, 7, 8, 9, 999999, tzinfo=timezone.utc), "loop": ("val", 1.5),
}


def _with(**kw):
    return {**_OK, **kw}


_ARGS = dict(run_id=st.sampled_from(RUN_IDS), interval=st.integers(100, 60000))


# ---- Property 15: one Sample after a number of counted requests ------------------------------------------------
@settings(max_examples=300, **SETTINGS)
@given(counts=st.integers(0, 4), o=outcomes, dropped=st.sampled_from([0, 1, MAX_DROPPED]), **_ARGS)
@example(counts=0, o=_with(rss=("ok", 0)), dropped=0, run_id="a", interval=100)
@example(counts=3, o=_with(rss=("ok", RSS_MAX)), dropped=MAX_DROPPED, run_id="x" * 64, interval=60000)
@example(counts=0, o=_with(loop=("val", 0.0)), dropped=0, run_id="a", interval=500)
@example(counts=0, o=_with(loop=("val", 5e-324)), dropped=0, run_id="a", interval=500)
@example(counts=0, o=_with(loop=("val", sys.float_info.max)), dropped=0, run_id="a", interval=500)
@example(counts=0, o=_with(clock=datetime(2024, 1, 1, 0, 0, 0, 999999, tzinfo=timezone(timedelta(hours=-8)))), dropped=0, run_id="a", interval=500)
@example(counts=0, o=_with(clock=datetime(2024, 12, 31, 23, 59, 59, 999999, tzinfo=timezone(timedelta(hours=5, minutes=30)))), dropped=0, run_id="a", interval=500)
@example(counts=0, o=_with(cid=("ok", (HEX12, "hostname"))), dropped=0, run_id="a", interval=500)
@example(counts=0, o=_with(cid=("none",)), dropped=0, run_id="a", interval=500)
@example(counts=0, o=_with(pid=("exc", OSError)), dropped=0, run_id="a", interval=500)
@example(counts=0, o=_with(clock=datetime(2024, 1, 1, 12, 0, 0)), dropped=0, run_id="a", interval=500)
@example(counts=1, o=_with(rss=("exc", PermissionError), cid=("exc", OSError), loop=("none", "ü" * 200)), dropped=0, run_id="a", interval=500)
def test_property_15_single_sample_matches_oracle(counts, o, dropped, run_id, interval):
    _run([("count", counts), ("build", o)], run_id, interval, dropped)


# ---- Property 15: sequences of counted requests and builds (success/failure transitions) ----------------------
_S1 = _with()
_F_PID = _with(pid=("exc", OSError))
_F_RSS = _with(rss=("exc", OSError), cid=("none",))


@settings(max_examples=200, **SETTINGS)
@given(op_list=st.lists(ops, min_size=1, max_size=20), dropped=st.sampled_from([0, 7]), **_ARGS)
@example(
    op_list=[("build", _S1), ("build", _F_RSS), ("build", _with(rss=("ok", 9), cid=("ok", (HEX12, "hostname")))),
             ("build", _F_PID), ("build", _F_PID), ("count", 2), ("build", _S1)],
    dropped=0, run_id="a", interval=500,
)
@example(op_list=[("build", _with(clock=datetime(2024, 1, 1))), ("build", _S1)], dropped=0, run_id="a", interval=500)
def test_property_15_sequences_have_no_gaps_or_stale_values(op_list, dropped, run_id, interval):
    _run(op_list, run_id, interval, dropped)


# ---- explicit examples -----------------------------------------------------------------------------------------
def test_oracle_rejects_a_valid_but_fabricated_measurement():
    probe, cur = _make_probe("a", 500, 0)
    cur["o"] = _with()
    sample = probe.build_sample(1.5)
    exp = _expected(_with(), "a", 0, 0, 500, 0)[1]
    _check(sample, exp)
    for key, fake in (("rss_bytes", sample["rss_bytes"] + 4096), ("request_count", 1), ("pid", 43)):
        forged = {**sample, key: fake}
        assert validate_sample(forged) == []
        with pytest.raises(AssertionError):
            _check(forged, exp)


def test_failure_after_success_is_null_not_stale_and_seq_has_no_gap():
    probe, cur = _make_probe("a", 500, 0)
    cur["o"] = _with()
    assert probe.build_sample(1.0)["seq"] == 0
    cur["o"] = _with(rss=("exc", OSError), cid=("exc", OSError))
    failed = probe.build_sample(1.0)
    assert failed["rss_bytes"] is None and failed["container_id"] is None
    assert failed["container_id_source"] == "none"
    assert failed["null_reasons"] == {
        "rss_bytes": "rss unavailable: OSError", "container_id": "container id unavailable: OSError"}
    cur["o"] = _with(pid=("exc", OSError))
    with pytest.raises(OSError):
        probe.build_sample(1.0)
    cur["o"] = _with(clock=datetime(2024, 1, 1))
    with pytest.raises(ValueError):
        probe.build_sample(1.0)
    with pytest.raises(ValueError):
        probe.build_sample(None)
    assert probe.state["next_seq"] == 2
    cur["o"] = _with()
    assert probe.build_sample(2.0)["seq"] == 2
