"""Property 19: the Demo_App leak endpoint accumulates exactly or changes nothing (Task 4.12).

**Validates: Requirements 1.2, 1.3, 1.4, 1.7, 1.8**

Method. Every example builds a fresh app (flag "1", FakeProbe, recording exit_fn that never exits
the process) with a fresh LeakStore. An independent reference model (expected total, expected chunk
lengths, expected status/body) is advanced per action; the model never reads LeakStore.total. The
store is observed white-box (``_chunks``) and wrapped with call spies on ``retain`` and on the
``_allocate`` seam. MemoryError is injected through that seam; an append failure is injected by
replacing ``_chunks`` with a list subclass whose next append raises MemoryError.

Memory safety: successful generated allocations are 1..1024 bytes (at most 15 per example). The
104,857,600 boundary is checked only with an ``_allocate`` recorder that returns b"" (validation
only, no retention, no real allocation).
"""
from __future__ import annotations

import re
import sys
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st

from coroner.demo_app.app import MAX_LEAK_BYTES, create_app
from coroner.demo_app.leak import LeakStore

FLAG = "CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS"
FILL = 0xA5
MISSING = None


class FakeProbe:
    def __init__(self) -> None:
        self.config = SimpleNamespace(run_id="run-1")
        self.requests = 0
        self._lock = threading.Lock()

    def count_request(self) -> None:
        with self._lock:
            self.requests += 1

    @property
    def state(self) -> dict:
        return {"next_seq": 0, "dropped": 0}

    def start(self, loop=None) -> None:
        pass

    async def stop(self) -> None:
        pass


class FailOnceList(list):
    """White-box: the next append raises MemoryError once when armed."""

    armed = False

    def append(self, item) -> None:
        if self.armed:
            self.armed = False
            raise MemoryError("injected append failure")
        super().append(item)


def parse_decimal(text):
    """Independent model of the accepted syntax and range; returns the int or None."""
    if text is MISSING or re.fullmatch(r"[0-9]{1,9}", text) is None:
        return None
    n = int(text)
    return n if 1 <= n <= MAX_LEAK_BYTES else None


class Harness:
    """Fresh app + store + spies + reference model."""

    def __init__(self) -> None:
        self.exits: list = []
        self.probe = FakeProbe()
        self.app = create_app({FLAG: "1"}, probe=self.probe, exit_fn=lambda code: self.exits.append(code))
        self.client = TestClient(self.app)
        self.store: LeakStore = self.app.state.leak_store
        self.retain_calls = 0
        self.alloc_calls: list[int] = []
        self.fail_alloc = 0
        orig_alloc = LeakStore._allocate
        orig_retain = self.store.retain

        def alloc(n):
            self.alloc_calls.append(n)
            if self.fail_alloc:
                self.fail_alloc -= 1
                raise MemoryError("injected allocation failure")
            return orig_alloc(n)

        def retain(n):
            self.retain_calls += 1
            return orig_retain(n)

        self.store._allocate = alloc
        self.store.retain = retain
        self.store._chunks = FailOnceList()
        self.expected_total = 0
        self.expected_lens: list[int] = []
        self.seen_chunks: list[bytes] = []
        self.last_returned_total = 0
        self.prev_kind = None

    def post(self, text):
        params = None if text is MISSING else {"bytes": text}
        return self.client.post("/leak", params=params)

    def step(self, action):
        """action = (kind, text); kind in valid, invalid, memerr, appendfail. Returns the response."""
        kind, text = action
        n = parse_decimal(text)
        retains, allocs = self.retain_calls, len(self.alloc_calls)
        if kind in ("memerr", "appendfail"):
            assert n is not None and n <= 1024
            if kind == "memerr":
                self.fail_alloc = 1
            else:
                self.store._chunks.armed = True
        else:
            assert n is None or n <= 1024  # strategies never request a big real allocation
        resp = self.post(text)
        if kind in ("memerr", "appendfail"):
            assert resp.status_code == 503
            assert resp.json() == {"error": "allocation_failed", "retained_bytes_total": self.expected_total}
            assert self.retain_calls == retains + 1 and len(self.alloc_calls) == allocs + 1
            assert self.alloc_calls[-1] == n
            self.fail_alloc = 0
            self.store._chunks.armed = False
        elif n is None:
            assert resp.status_code == 422
            assert self.retain_calls == retains and len(self.alloc_calls) == allocs  # store untouched
        else:
            assert resp.status_code == 200
            self.expected_total += n
            self.expected_lens.append(n)
            assert resp.json() == {"retained_bytes_total": self.expected_total, "requested_bytes": n}
            assert self.retain_calls == retains + 1 and self.alloc_calls[-1] == n
            assert self.expected_total >= self.last_returned_total
            self.last_returned_total = self.expected_total
            newest = self.store._chunks[-1]
            assert len(newest) == n and newest == bytes([FILL]) * n and n > 0
            self.seen_chunks.append(newest)
        self.check_store()
        return resp

    def check_store(self) -> None:
        chunks = self.store._chunks
        assert self.store.chunk_count == len(self.expected_lens)
        assert [len(c) for c in chunks] == self.expected_lens
        assert self.store.total == self.expected_total
        assert len(chunks) == len(self.seen_chunks)
        for current, remembered in zip(chunks, self.seen_chunks):
            assert current is remembered  # earlier chunks are the same objects
            assert current.count(FILL) == len(current)  # and still unchanged and non-zero
        assert self.exits == []


# ---- strategies -----------------------------------------------------------------------

valid_text = st.tuples(st.integers(1, 1024), st.integers(0, 5)).map(lambda t: str(t[0]).zfill(len(str(t[0])) + t[1]))

INVALID_KINDS = {
    "missing": st.just(MISSING),
    "empty": st.just(""),
    "zero": st.sampled_from(["0", "00", "000000000"]),
    "negative": st.one_of(st.sampled_from(["-1", "-0"]), st.integers(1, 10**6).map(lambda i: f"-{i}")),
    "text": st.text(alphabet="abcxyzXYZ!#$ /-_", min_size=1, max_size=12),
    "decimal": st.sampled_from(["1.5", "1e3", "5.0", "0x10", "1,5"]),
    "plus": st.integers(1, 1024).map(lambda i: f"+{i}"),
    "underscore": st.sampled_from(["1_000", "1_0", "_1"]),
    "space": st.sampled_from([" 5", "5 ", "%20", "5\n", " ", "5\t"]),
    "toolong": st.tuples(st.integers(1, 1024), st.integers(10, 12)).map(lambda t: str(t[0]).zfill(t[1])),
    "over": st.one_of(st.just("104857601"), st.integers(MAX_LEAK_BYTES + 1, 999_999_999).map(str)),
    "huge": st.integers(10, 400).map(lambda k: "9" * k),
    "nonascii": st.sampled_from(["\u0665", "\uff15", "\u0663\u0664", "5\u0665", "\u00b2"]),
}
invalid_text = st.one_of(*INVALID_KINDS.values())
invalid_labeled = st.one_of(*(v.map(lambda t, k=k: (k, t)) for k, v in INVALID_KINDS.items()))

action = st.one_of(
    st.tuples(st.just("valid"), valid_text),
    st.tuples(st.just("invalid"), invalid_text),
    st.tuples(st.just("memerr"), valid_text),
    st.tuples(st.just("appendfail"), valid_text),
)


def classify(act) -> str:
    kind, text = act
    if kind == "invalid":
        return "invalid"
    return "memerr" if kind in ("memerr", "appendfail") else "valid"


def run(actions) -> Harness:
    h = Harness()
    prev = None
    for act in actions:
        kind = classify(act)
        event(f"kind:{act[0]}")
        if act[0] == "invalid":
            event(f"invalid:{invalid_kind_of(act[1])}")
        if prev is not None and prev != kind:
            event(f"transition:{prev}->{kind}")
        elif prev == kind and kind == "memerr":
            event("transition:memerr->memerr")
        prev = kind
        h.step(act)
    return h


def invalid_kind_of(text) -> str:
    if text is MISSING:
        return "missing"
    if text == "":
        return "empty"
    if re.fullmatch(r"-.*", text):
        return "negative"
    if re.fullmatch(r"[0-9]{10,}", text):
        return "toolong_or_huge"
    if re.fullmatch(r"[0-9]{1,9}", text):
        return "zero_or_over"
    return "syntax"


V = lambda t: ("valid", t)  # noqa: E731
I = lambda t: ("invalid", t)  # noqa: E731
M = lambda t: ("memerr", t)  # noqa: E731
A = lambda t: ("appendfail", t)  # noqa: E731


@settings(max_examples=300, derandomize=True, database=None, deadline=None,
          suppress_health_check=list(HealthCheck))
@given(st.lists(action, min_size=1, max_size=15))
@example([V("1")])
@example([V("7")])
@example([V("007")])
@example([V("1024")])
@example([I("0")])
@example([I("-1")])
@example([I("104857601")])
@example([I(MISSING)])
@example([I("")])
@example([I("abc")])
@example([I("1.5")])
@example([I("1e3")])
@example([I("+5")])
@example([I("1_000")])
@example([I(" 5")])
@example([I("5 ")])
@example([I("0000000001")])
@example([V("5"), I("abc"), V("6")])
@example([M("5")])
@example([V("1"), V("2"), V("3"), M("4")])
@example([M("5"), V("6")])
@example([M("5"), M("6"), M("7")])
@example([V("9"), A("5"), V("2")])
def test_property_19_leak_accumulates_exactly_or_changes_nothing(actions) -> None:
    h = run(actions)
    ok = [t for k, t in actions if k == "valid"]
    assert h.expected_lens == [int(t) for t in ok]


@settings(max_examples=60, derandomize=True, database=None, deadline=None,
          suppress_health_check=list(HealthCheck))
@given(st.lists(invalid_labeled, min_size=1, max_size=10))
def test_invalid_only_sequences_never_touch_the_store(items) -> None:
    h = Harness()
    for kind, text in items:
        event(f"invalid:{kind}")
        h.step(("invalid", text))
    assert h.retain_calls == 0 and h.alloc_calls == [] and h.store.chunk_count == 0


# ---- MemoryError atomicity (explicit A-D + append failure) --------------------------------

def test_a_memoryerror_on_first_request_changes_nothing_and_later_success_works() -> None:
    h = Harness()
    r = h.step(M("100"))
    assert r.status_code == 503 and r.json() == {"error": "allocation_failed", "retained_bytes_total": 0}
    assert h.store.chunk_count == 0
    r = h.step(V("100"))
    assert r.json() == {"retained_bytes_total": 100, "requested_bytes": 100}


def test_b_memoryerror_after_successes_keeps_total_and_previous_chunks() -> None:
    h = Harness()
    for t in ("10", "20", "30"):
        h.step(V(t))
    before = list(h.store._chunks)
    r = h.step(M("40"))
    assert r.status_code == 503 and r.json()["retained_bytes_total"] == 60
    assert h.store.chunk_count == 3 and all(a is b for a, b in zip(h.store._chunks, before))


def test_c_consecutive_memoryerrors_then_d_success_continues_cumulative_total() -> None:
    h = Harness()
    h.step(V("5"))
    for _ in range(3):
        assert h.step(M("9")).json() == {"error": "allocation_failed", "retained_bytes_total": 5}
    assert h.step(V("7")).json() == {"retained_bytes_total": 12, "requested_bytes": 7}
    assert h.store.chunk_count == 2


def test_append_failure_after_allocation_changes_nothing() -> None:
    h = Harness()
    h.step(V("3"))
    r = h.step(A("8"))
    assert r.status_code == 503 and r.json() == {"error": "allocation_failed", "retained_bytes_total": 3}
    assert h.alloc_calls == [3, 8]  # the chunk was allocated, but never retained
    assert h.step(V("2")).json() == {"retained_bytes_total": 5, "requested_bytes": 2}


# ---- upper bound: validation only, via recorder (no real allocation) ------------------------

def recording_harness():
    h = Harness()
    calls: list[int] = []

    def rec(n):
        calls.append(n)
        return b""  # never allocates; the store then only counts the number

    h.store._allocate = rec
    return h, calls


def test_upper_bound_accepted_and_just_above_rejected_validation_only() -> None:
    h, calls = recording_harness()
    r = h.post("104857600")
    assert r.status_code == 200
    assert r.json() == {"retained_bytes_total": MAX_LEAK_BYTES, "requested_bytes": MAX_LEAK_BYTES}
    assert calls == [MAX_LEAK_BYTES]
    for bad in ("104857601", "999999999", "9" * 400, "0000000001", "0", "1" * 10):
        assert h.post(bad).status_code == 422
    assert h.post(MISSING).status_code == 422
    assert calls == [MAX_LEAK_BYTES] and h.retain_calls == 1  # nothing else reached the store


def test_nine_character_leading_zero_decimal_is_accepted_as_decimal() -> None:
    h = Harness()
    assert h.step(V("000000012")).json() == {"retained_bytes_total": 12, "requested_bytes": 12}


# ---- concurrency ----------------------------------------------------------------------------

def _install_failing_alloc(store: LeakStore, every: int):
    lock = threading.Lock()
    state = {"count": 0, "failed": 0}
    orig = LeakStore._allocate

    def alloc(n):
        with lock:
            state["count"] += 1
            fail = state["count"] % every == 0
            if fail:
                state["failed"] += 1
        if fail:
            raise MemoryError("injected")
        return orig(n)

    store._allocate = alloc
    return state


def _run_threads(target, threads: int) -> None:
    barrier = threading.Barrier(threads)
    errors: list = []

    def wrap(idx):
        try:
            barrier.wait()
            target(idx)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=wrap, args=(i,)) for i in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(120)
    assert not any(t.is_alive() for t in ts)
    assert errors == []


def test_concurrent_http_leaks_with_injected_failures_keep_exact_accounting() -> None:
    app = create_app({FLAG: "1"}, probe=FakeProbe(), exit_fn=lambda code: None)
    store: LeakStore = app.state.leak_store
    state = _install_failing_alloc(store, 7)
    ledger: list[tuple[int, int, int]] = []  # status, requested, returned total (0 if 503)
    failures: list[int] = []
    lock = threading.Lock()

    def worker(idx):
        client = TestClient(app)  # one client per thread
        for i in range(30):
            n = 1 + (idx * 31 + i * 7) % 64
            r = client.post("/leak", params={"bytes": str(n)})
            body = r.json()
            with lock:
                if r.status_code == 200:
                    ledger.append((n, body["retained_bytes_total"], body["requested_bytes"]))
                else:
                    assert r.status_code == 503 and body["error"] == "allocation_failed"
                    failures.append(n)

    _run_threads(worker, 8)
    ok_sizes = sorted(n for n, _, _ in ledger)
    assert len(ledger) + len(failures) == 240
    assert len(failures) == state["failed"]
    assert store.total == sum(ok_sizes)
    assert store.chunk_count == len(ledger)
    assert sorted(len(c) for c in store._chunks) == ok_sizes
    assert all(c.count(FILL) == len(c) for c in store._chunks)
    totals = [t for _, t, _ in ledger]
    assert len(set(totals)) == len(totals) and max(totals) == store.total  # no lost update


def test_concurrent_direct_retain_has_no_lost_updates() -> None:
    store = LeakStore()
    state = _install_failing_alloc(store, 11)
    results: list[int] = []
    sizes: list[int] = []
    lock = threading.Lock()
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # force frequent thread switches
    try:
        def worker(idx):
            mine: list[int] = []
            ok: list[int] = []
            for i in range(300):
                n = 1 + (idx + i) % 8
                try:
                    mine.append(store.retain(n))
                    ok.append(n)
                except MemoryError:
                    pass
            assert mine == sorted(mine) and len(set(mine)) == len(mine)
            with lock:
                results.extend(mine)
                sizes.extend(ok)

        _run_threads(worker, 8)
    finally:
        sys.setswitchinterval(old)
    assert state["failed"] > 0
    assert store.total == sum(sizes) and store.chunk_count == len(sizes)
    assert len(set(results)) == len(results) and max(results) == store.total
    assert sorted(len(c) for c in store._chunks) == sorted(sizes)


class WideRaceStore(LeakStore):
    """White-box: reading `_total` yields the GIL, so an unlocked `_total += n` loses updates."""

    @property
    def _total(self) -> int:
        value = self.__dict__["_t"]
        time.sleep(0.0002)
        return value

    @_total.setter
    def _total(self, value: int) -> None:
        self.__dict__["_t"] = value


def test_concurrent_retain_with_widened_race_window_has_no_lost_updates() -> None:
    store = WideRaceStore()
    results: list[int] = []
    lock = threading.Lock()

    def worker(idx):
        mine = [store.retain(1 + (idx + i) % 5) for i in range(40)]
        with lock:
            results.extend(mine)

    _run_threads(worker, 8)
    expected = sum(1 + (idx + i) % 5 for idx in range(8) for i in range(40))
    assert store.total == expected and store.chunk_count == 320
    assert len(set(results)) == 320 and max(results) == expected


def test_no_real_exit_was_called() -> None:
    h = Harness()
    h.step(V("1"))
    assert h.exits == []
