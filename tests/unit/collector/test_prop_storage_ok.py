"""Task 3.4: Property 28, Collector half (Requirements 4.4, 4.17, 4.18; design D21, D32).

**Property 28: Health readiness requires a successful storage probe**

Tested here, with `storage_ok` taken to be `ProbeResult.ok` of `StorageProbe`:
  1. every probe step succeeds            => ok is True;
  2. any required step fails              => ok is False;
  3. the failure names the first failing step in the probe's step order;
  4. `error` is a bounded readable string (non-empty, at most 200 characters, one line, printable);
  5. a probe never modifies a run file, `_rejections.jsonl` or `.collector.lock` (sha256, size and mtime), and every
     write-mode open is `.coroner_probe/probe.tmp`;
  6. `invalidate()` (what a failed append must trigger) drops a previously successful cached result;
  7. the next check after an invalidation performs a real verification again;
  8. a probe in flight when `invalidate()` is called never repopulates the cache.

NOT tested here (pending integration verification in Task 3.11): the `/healthz` endpoint, that it answers HTTP 200
whatever `storage_ok` is, and the `storage_error` body field. Also owed to Tasks 3.5 and 3.11: calling `invalidate()`
from the ingest path after a failed append (no ingest code exists yet; `RunRegistry.append` has no hook). Here the
Collector-side contract is tested through `StorageProbe.invalidate()` itself. The Runner half is a later task.

Independent oracle: `oracle_probe` below holds its own fixed step-order table and its own model of the directory and
pending-directory-fsync state. It never calls `check()` and imports nothing private from `storage_probe`. Cleanup
modelled explicitly (this is the implementation's documented behaviour):
  * after a failing write, short write, flush or fsync, the write handle is still closed (event `close`), and a close
    failure there is masked: the reported step stays the earlier one;
  * a close failure with no earlier failure is fatal and reports `close`; no directory fsync follows;
  * `open:wb` failure leaves no handle, so no close event;
  * the read handle is always closed after `open:rb` succeeded; a read or close failure there reports `read`;
  * a directory whose fsync failed (or was never reached) stays pending and is synced again by the next probe, even
    though the directory already exists; `.coroner_probe` is synced before the evidence directory.
Both durability levels are exercised (`load_config`), and the probe must behave identically: it has no durability input.
"""
from __future__ import annotations

import hashlib
import inspect
import io
import itertools
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from coroner.collector.config import load_config
from coroner.collector.storage_probe import StorageProbe

PROBE_DIR = ".coroner_probe"
PROBE_FILE = "probe.tmp"
START = 100.0
WAIT = 10.0  # seconds; only an upper bound for a deterministic hand-off, never a pacing sleep

FRESH = [
    "mkdir", "open:wb", "write", "flush", "fsync", "close",
    "fsync_dir:probe", "fsync_dir:evidence", "open:rb", "read", "close",
]
EXISTING = ["open:wb", "write", "flush", "fsync", "close", "open:rb", "read", "close"]

HYP = dict(derandomize=True, database=None, deadline=None, suppress_health_check=[HealthCheck.too_slow])


# --- failure injection ----------------------------------------------------------------------


@dataclass(frozen=True)
class Flags:
    """Outcome of each injectable step. True (or a non-"ok" string) means that step fails."""

    mkdir: bool = False
    open_w: bool = False
    write: str = "ok"  # ok | raise | short
    flush: bool = False
    fsync: bool = False
    close_w: bool = False
    dirfsync_probe: bool = False
    dirfsync_evidence: bool = False
    open_r: bool = False
    read: bool = False
    close_r: bool = False
    compare: str = "ok"  # ok | mismatch | prefix | extra


# failure kinds: name -> (Flags kwargs, step the probe must report, rank in the fixed step order)
KINDS: dict[str, tuple[dict, str, int]] = {
    "mkdir": ({"mkdir": True}, "mkdir", 0),
    "open": ({"open_w": True}, "open", 1),
    "write": ({"write": "raise"}, "write", 2),
    "short_write": ({"write": "short"}, "write", 2),
    "flush": ({"flush": True}, "flush", 3),
    "fsync": ({"fsync": True}, "fsync", 4),
    "close": ({"close_w": True}, "close", 5),
    "dirfsync_probe": ({"dirfsync_probe": True}, "dir_fsync", 6),
    "dirfsync_evidence": ({"dirfsync_evidence": True}, "dir_fsync", 7),
    "open_r": ({"open_r": True}, "read", 8),
    "read": ({"read": True}, "read", 9),
    "close_r": ({"close_r": True}, "read", 10),
    "mismatch": ({"compare": "mismatch"}, "compare", 11),
    "prefix": ({"compare": "prefix"}, "compare", 11),
    "extra": ({"compare": "extra"}, "compare", 11),
}
# kinds that are only applicable when the probe creates something (fresh state) / only after mkdir
FRESH_ONLY = {"mkdir", "dirfsync_probe", "dirfsync_evidence"}


def flags_of(*names: str) -> Flags:
    merged: dict = {}
    for name in names:
        merged.update(KINDS[name][0])
    return Flags(**merged)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now


class _WriteHandle:
    def __init__(self, st_: "Storage", real) -> None:
        self.s, self.real = st_, real

    def write(self, data):
        self.s.rec("write")
        mode = self.s.flags.write
        if self.s.inject and mode == "raise":
            raise OSError(self.s.msg)
        if self.s.inject and mode == "short":
            half = len(data) // 2
            self.real.write(data[:half])
            return half
        return self.real.write(data)

    def flush(self):
        self.s.rec("flush")
        if self.s.inject and self.s.flags.flush:
            raise OSError(self.s.msg)
        self.real.flush()

    def fileno(self):
        return self.real.fileno()

    def close(self):
        self.s.rec("close")
        self.real.close()
        if self.s.inject and self.s.flags.close_w:
            raise OSError(self.s.msg)


class _ReadHandle:
    def __init__(self, st_: "Storage", real) -> None:
        self.s, self.real = st_, real

    def read(self):
        self.s.rec("read")
        if self.s.inject and self.s.flags.read:
            raise OSError(self.s.msg)
        data = self.real.read()
        mode = self.s.flags.compare if self.s.inject else "ok"
        if mode == "mismatch":
            return bytes([data[0] ^ 1]) + data[1:]
        if mode == "prefix":
            return data[:-1]
        if mode == "extra":
            return data + b"x"
        return data

    def close(self):
        self.s.rec("close")
        self.real.close()
        if self.s.inject and self.s.flags.close_r:
            raise OSError(self.s.msg)


class Storage:
    """Recording doubles around the real filesystem. Everything the probe is allowed to inject is routed here."""

    def __init__(self, root: Path, *, inject: bool = True, msg: str = "boom") -> None:
        self.root = root
        self.events: list[str] = []
        self.write_paths: list[Path] = []
        self.flags = Flags()
        self.inject = inject
        self.msg = msg
        self.hook = None  # called with each event name; may block (deterministic interleaving)
        self.clock = Clock()
        self.probe = StorageProbe(
            root, fsync=self._fsync, fsync_dir=self._fsync_dir, clock=self.clock, opener=self._open, mkdir=self._mkdir
        )

    def rec(self, name: str) -> None:
        self.events.append(name)
        hook = self.hook
        if hook is not None:
            hook(name)

    def _fsync(self, fd: int) -> None:
        self.rec("fsync")
        if self.inject and self.flags.fsync:
            raise OSError(self.msg)

    def _fsync_dir(self, d: Path) -> None:
        name = "evidence" if Path(d) == self.root else Path(d).name
        assert name in ("evidence", PROBE_DIR), name
        self.rec("fsync_dir:" + ("probe" if name == PROBE_DIR else "evidence"))
        if self.inject and (self.flags.dirfsync_evidence if name == "evidence" else self.flags.dirfsync_probe):
            raise OSError(self.msg)

    def _mkdir(self, p: Path) -> None:
        self.rec("mkdir")
        if self.inject and self.flags.mkdir:
            raise PermissionError(self.msg)
        os.mkdir(p)

    def _open(self, p: Path, mode: str):
        self.rec("open:" + mode)
        if mode != "rb":
            self.write_paths.append(Path(p))
        if self.inject and ((mode == "wb" and self.flags.open_w) or (mode == "rb" and self.flags.open_r)):
            raise PermissionError(self.msg)
        real = io.open(p, mode)
        return _WriteHandle(self, real) if mode == "wb" else _ReadHandle(self, real)


# --- independent oracle ------------------------------------------------------------------------


@dataclass
class Model:
    dir_exists: bool
    file_exists: bool
    pending: set


def oracle_probe(m: Model, f: Flags) -> tuple[bool, str | None, list[str]]:
    """Expected (ok, failing step, executed events) of one real probe; updates the model. Fixed order:
    mkdir, open, write, flush, fsync, close, dir fsync (probe dir, evidence dir), open for read, read, compare."""
    ev: list[str] = []
    if not m.dir_exists:
        ev.append("mkdir")
        if f.mkdir:
            return False, "mkdir", ev
        m.dir_exists = True
        m.pending.add("evidence")
    if not m.file_exists:
        m.pending.add("probe")
    ev.append("open:wb")
    if f.open_w:
        return False, "open", ev
    m.file_exists = True
    failing = None
    ev.append("write")
    if f.write != "ok":
        failing = "write"
    if failing is None:
        ev.append("flush")
        if f.flush:
            failing = "flush"
    if failing is None:
        ev.append("fsync")
        if f.fsync:
            failing = "fsync"
    ev.append("close")  # cleanup: always attempted once the write handle exists
    if failing is not None:
        return False, failing, ev  # a close failure here is masked
    if f.close_w:
        return False, "close", ev
    for name, flag in (("probe", f.dirfsync_probe), ("evidence", f.dirfsync_evidence)):
        if name in m.pending:
            ev.append("fsync_dir:" + name)
            if flag:
                return False, "dir_fsync", ev
            m.pending.discard(name)
    ev.append("open:rb")
    if f.open_r:
        return False, "read", ev
    ev.append("read")
    ev.append("close")  # the read handle is closed even when read fails
    if f.read or f.close_r:
        return False, "read", ev
    if f.compare != "ok":
        return False, "compare", ev
    return True, None, ev


def expected_step(names: tuple[str, ...], fresh: bool) -> str | None:
    """Hard-coded ranking for explicit cases (independent of `oracle_probe`)."""
    applicable = [n for n in names if fresh or n not in FRESH_ONLY]
    if not applicable:
        return None
    return KINDS[min(applicable, key=lambda n: KINDS[n][2])][1]


# --- harness --------------------------------------------------------------------------------


def assert_error_ok(result) -> None:
    if result.ok:
        assert result.step is None and result.error is None
        return
    err = result.error
    assert isinstance(err, str) and 1 <= len(err) <= 200, err
    assert "\n" not in err and "\r" not in err and err.isprintable(), repr(err)
    assert err.startswith(f"{result.step}:"), (result.step, err)


def populate(root: Path) -> dict[str, tuple[str, int, int]]:
    (root / "run-a.jsonl").write_bytes(b'{"record_type":"sample"}\n{"partial')
    (root / "_rejections.jsonl").write_bytes(b'{"record_type":"rejection"}\n')
    (root / ".collector.lock").write_bytes(b"")
    return snapshot(root)


def snapshot(root: Path) -> dict[str, tuple[str, int, int]]:
    out = {}
    for name in ("run-a.jsonl", "_rejections.jsonl", ".collector.lock"):
        p = root / name
        out[name] = (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_size, p.stat().st_mtime_ns)
    return out


def run_scenario(durability, initial, ops, *, probe_cls=None, inject=True, msg="boom") -> int:
    """Drive `ops` against a probe and compare every step with the oracle. Returns the number of real probes."""
    probes = 0
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "evidence"
        root.mkdir()
        cfg = load_config({"CORONER_DURABILITY": durability}, evidence_dir=root)
        before = populate(cfg.evidence_dir)
        dir_exists, file_exists = initial
        if dir_exists:
            os.mkdir(root / PROBE_DIR)
        if file_exists:
            (root / PROBE_DIR / PROBE_FILE).write_bytes(b"old contents\n")
        s = Storage(cfg.evidence_dir, inject=inject, msg=msg)
        if probe_cls is not None:
            s.probe = probe_cls(
                root, fsync=s._fsync, fsync_dir=s._fsync_dir, clock=s.clock, opener=s._open, mkdir=s._mkdir
            )
        m = Model(dir_exists, file_exists, set())
        now = START
        cache = None  # (ok, step, checked_at)
        for op in ops:
            kind = op[0]
            if kind == "advance":
                s.clock.now += op[1]
                now += op[1]
            elif kind == "invalidate":
                s.probe.invalidate()
                cache = None
            else:
                f = op[1]
                s.flags = f
                cached = kind == "check" and cache is not None and 0.0 <= now - cache[2] < 1.0
                n0 = len(s.events)
                res = s.probe.run_startup() if kind == "startup" else s.probe.check(force=(kind == "force"))
                new = s.events[n0:]
                assert_error_ok(res)
                if cached:
                    assert new == [], new
                    assert (res.ok, res.step, res.checked_at) == cache
                else:
                    probes += 1
                    ok, step, ev = oracle_probe(m, f)
                    assert new == ev, (new, ev)
                    assert (res.ok, res.step) == (ok, step), (res, ok, step)
                    assert res.checked_at == now
                    cache = (ok, step, now)
                    assert (root / PROBE_DIR).is_dir() == m.dir_exists
                    assert (root / PROBE_DIR / PROBE_FILE).exists() == m.file_exists
        # isolation: no evidence file changed, nothing but the reserved directory was added, writes stayed inside it
        assert snapshot(root) == before
        assert all(p == root / PROBE_DIR / PROBE_FILE for p in s.write_paths), s.write_paths
        assert set(os.listdir(root)) <= set(before) | {PROBE_DIR}
        if (root / PROBE_DIR).is_dir():
            assert set(os.listdir(root / PROBE_DIR)) <= {PROBE_FILE}
    return probes


# --- strategies -----------------------------------------------------------------------------

_sparse = st.sampled_from([False] * 6 + [True])
_dense = st.booleans()


def _flags(flag):
    return st.builds(
        Flags,
        mkdir=flag, open_w=flag, flush=flag, fsync=flag, close_w=flag, dirfsync_probe=flag, dirfsync_evidence=flag,
        open_r=flag, read=flag, close_r=flag,
        write=st.sampled_from(["ok"] * 5 + ["raise", "short"]),
        compare=st.sampled_from(["ok"] * 5 + ["mismatch", "prefix", "extra"]),
    )


flags_st = st.one_of(_flags(_sparse), _flags(_dense))
initial_st = st.sampled_from([(False, False), (True, False), (True, True)])
durability_st = st.sampled_from(["process", "crash"])
dt_st = st.sampled_from([0.0, 0.5, 0.75, 0.9375, 1.0, 1.25, 2.0, -0.5])
op_st = st.one_of(
    st.tuples(st.just("check"), flags_st),
    st.tuples(st.just("force"), flags_st),
    st.tuples(st.just("startup"), flags_st),
    st.tuples(st.just("invalidate")),
    st.tuples(st.just("advance"), dt_st),
)


# --- Hypothesis properties ------------------------------------------------------------------


@settings(max_examples=300, **HYP)
@given(durability=durability_st, initial=initial_st, flags=flags_st)
def test_single_probe_matches_oracle(durability, initial, flags):
    """Properties 1-5: one forced probe over any combination of step outcomes agrees with the oracle."""
    assert run_scenario(durability, initial, [("force", flags)]) == 1


@settings(max_examples=300, **HYP)
@given(durability=durability_st, initial=initial_st, ops=st.lists(op_st, min_size=1, max_size=9))
def test_probe_sequences_cache_invalidation_and_pending_dirs(durability, initial, ops):
    """Properties 1-7: sequences of checks, forced probes, invalidations and clock moves (cache, expiry, recovery,
    pending directory fsync) agree with the oracle and the cache model."""
    run_scenario(durability, initial, ops)


_text_msgs = st.one_of(
    st.text(max_size=700),
    st.just("line one\nline two\r\n" * 60),
    st.just("x" * 1000),
    st.just("\x00\x1b[31mred\x07"),
    st.just(""),
)


@settings(max_examples=150, **HYP)
@given(kind=st.sampled_from(sorted(KINDS)), msg=_text_msgs, initial=initial_st)
def test_error_is_bounded_readable_single_line(kind, msg, initial):
    """Property 4: whatever the injected exception says, `error` is non-empty, <= 200 chars, one line, printable."""
    flags = flags_of(kind)
    with tempfile.TemporaryDirectory() as tmp:
        s = Storage(Path(tmp), msg=msg)
        if initial[0]:
            os.mkdir(Path(tmp) / PROBE_DIR)
        if initial[1]:
            (Path(tmp) / PROBE_DIR / PROBE_FILE).write_bytes(b"old\n")
        s.flags = flags
        res = s.probe.check(force=True)
        ok, step, _events = oracle_probe(Model(initial[0], initial[1], set()), flags)
        assert (res.ok, res.step) == (ok, step)
        assert_error_ok(res)


# --- explicit single failures and combinations ------------------------------------------------


@pytest.mark.parametrize("durability", ["process", "crash"])
def test_all_steps_succeed_gives_ok(durability, tmp_path):
    cfg = load_config({"CORONER_DURABILITY": durability}, evidence_dir=tmp_path)
    s = Storage(cfg.evidence_dir)
    res = s.probe.check()
    assert res.ok is True and res.step is None and res.error is None and res.checked_at == START
    assert s.events == FRESH
    s.events.clear()
    assert s.probe.check(force=True).ok and s.events == EXISTING


@pytest.mark.parametrize("fresh", [True, False])
@pytest.mark.parametrize("durability", ["process", "crash"])
@pytest.mark.parametrize("kind", sorted(KINDS))
def test_each_required_step_failing_alone(kind, durability, fresh, tmp_path):
    cfg = load_config({"CORONER_DURABILITY": durability}, evidence_dir=tmp_path)
    s = Storage(cfg.evidence_dir)
    if not fresh:
        os.mkdir(tmp_path / PROBE_DIR)
        (tmp_path / PROBE_DIR / PROBE_FILE).write_bytes(b"old\n")
    s.flags = flags_of(kind)
    res = s.probe.check()
    step = expected_step((kind,), fresh)
    assert res.ok is (step is None)
    assert res.step == step
    assert_error_ok(res)


@pytest.mark.parametrize("fresh", [True, False])
@pytest.mark.parametrize("pair", list(itertools.combinations(sorted(KINDS), 2)), ids="-".join)
def test_pairs_of_failures_report_the_first_in_step_order(pair, fresh, tmp_path):
    s = Storage(tmp_path)
    if not fresh:
        os.mkdir(tmp_path / PROBE_DIR)
        (tmp_path / PROBE_DIR / PROBE_FILE).write_bytes(b"old\n")
    s.flags = flags_of(*pair)
    res = s.probe.check()
    step = expected_step(pair, fresh)
    assert (res.ok, res.step) == (step is None, step)


def test_explicit_event_prefixes_and_cleanup(tmp_path):
    expected = {
        "mkdir": ["mkdir"],
        "open": ["mkdir", "open:wb"],
        "write": ["mkdir", "open:wb", "write", "close"],
        "short_write": ["mkdir", "open:wb", "write", "close"],
        "flush": ["mkdir", "open:wb", "write", "flush", "close"],
        "fsync": ["mkdir", "open:wb", "write", "flush", "fsync", "close"],
        "close": ["mkdir", "open:wb", "write", "flush", "fsync", "close"],
        "dirfsync_probe": FRESH[:7],
        "dirfsync_evidence": FRESH[:8],
        "open_r": FRESH[:9],
        "read": FRESH,
        "close_r": FRESH,
        "mismatch": FRESH,
        "prefix": FRESH,
        "extra": FRESH,
    }
    for kind, events in expected.items():
        root = tmp_path / kind
        root.mkdir()
        s = Storage(root)
        s.flags = flags_of(kind)
        assert not s.probe.check().ok
        assert s.events == events, kind


def test_failed_dir_fsync_stays_pending_and_is_retried(tmp_path):
    s = Storage(tmp_path)
    s.flags = flags_of("dirfsync_evidence")
    res = s.probe.check(force=True)
    assert (res.ok, res.step) == (False, "dir_fsync") and s.events == FRESH[:8]
    s.events.clear()
    res = s.probe.check(force=True)  # directory and file exist now, the evidence directory entry is still unsynced
    assert (res.ok, res.step) == (False, "dir_fsync")
    assert s.events == ["open:wb", "write", "flush", "fsync", "close", "fsync_dir:evidence"]
    s.flags = Flags()
    s.events.clear()
    assert s.probe.check(force=True).ok
    assert s.events == ["open:wb", "write", "flush", "fsync", "close", "fsync_dir:evidence", "open:rb", "read", "close"]
    s.events.clear()
    assert s.probe.check(force=True).ok and s.events == EXISTING


@pytest.mark.parametrize("durability", ["process", "crash"])
def test_file_and_directory_fsync_are_mandatory_at_both_levels(durability, tmp_path):
    cfg = load_config({"CORONER_DURABILITY": durability}, evidence_dir=tmp_path)
    assert "durability" not in inspect.signature(StorageProbe.__init__).parameters
    s = Storage(cfg.evidence_dir)
    assert s.probe.check().ok
    assert "fsync" in s.events and "fsync_dir:probe" in s.events and "fsync_dir:evidence" in s.events
    for kind in ("fsync", "dirfsync_probe", "dirfsync_evidence"):
        root = tmp_path / kind
        root.mkdir()
        t = Storage(root)
        t.flags = flags_of(kind)
        assert t.probe.check().ok is False


# --- cache, invalidation, recovery -----------------------------------------------------------


@pytest.mark.parametrize("durability", ["process", "crash"])
def test_cached_success_reused_under_one_second_then_expires(durability, tmp_path):
    cfg = load_config({"CORONER_DURABILITY": durability}, evidence_dir=tmp_path)
    s = Storage(cfg.evidence_dir)
    first = s.probe.check()
    s.events.clear()
    s.clock.now = START + 0.999
    assert s.probe.check() is first and s.events == []
    s.clock.now = START + 1.0
    second = s.probe.check()
    assert second is not first and s.events == EXISTING and second.checked_at == START + 1.0


def test_cached_failure_is_reused_even_if_storage_recovered(tmp_path):
    s = Storage(tmp_path)
    s.flags = flags_of("fsync")
    bad = s.probe.check()
    assert bad.ok is False
    s.flags = Flags()
    s.events.clear()
    s.clock.now = START + 0.5
    assert s.probe.check() is bad and s.events == []  # within the 1 s cache bound
    s.clock.now = START + 1.0
    good = s.probe.check()  # recovery after a failed probe
    assert good.ok and s.events != []


def test_invalidation_after_failed_append_forces_real_verification(tmp_path):
    s = Storage(tmp_path)
    assert s.probe.check().ok
    s.events.clear()
    assert s.probe.check().ok and s.events == []  # reused before expiry
    s.probe.invalidate()  # what the ingest path must do after a failed append
    s.flags = flags_of("flush")
    res = s.probe.check()  # same instant: not reused, real storage verification happens again
    assert s.events == ["open:wb", "write", "flush", "close"]
    assert (res.ok, res.step) == (False, "flush")


def test_repeated_forced_probes_and_startup_always_verify(tmp_path):
    s = Storage(tmp_path)
    assert s.probe.run_startup().ok
    for _ in range(4):
        s.events.clear()
        assert s.probe.check(force=True).ok and s.events == EXISTING
    s.events.clear()
    assert s.probe.run_startup().ok and s.events == EXISTING  # startup forces even when the cache is fresh


# --- deterministic interleavings ----------------------------------------------------------------


class Gate:
    """Block the first `fsync` of the probe in flight until released."""

    def __init__(self, s: Storage) -> None:
        self.entered, self.release = threading.Event(), threading.Event()
        self.armed = True
        s.hook = self

    def __call__(self, name: str) -> None:
        if name == "fsync" and self.armed:
            self.armed = False
            self.entered.set()
            assert self.release.wait(WAIT)


def _in_flight(s: Storage, between) -> list:
    gate = Gate(s)
    results: list = []
    t = threading.Thread(target=lambda: results.append(s.probe.check()))
    t.start()
    assert gate.entered.wait(WAIT)
    between(results)
    gate.release.set()
    t.join(WAIT)
    assert not t.is_alive()
    return results


@pytest.mark.parametrize("fails", [False, True])
def test_probe_in_flight_during_invalidate_does_not_repopulate_cache(fails, tmp_path):
    s = Storage(tmp_path)
    s.flags = flags_of("fsync") if fails else Flags()
    results = _in_flight(s, lambda _r: s.probe.invalidate())
    assert results[0].ok is (not fails)  # the in-flight caller still gets its own result
    s.hook = None
    s.flags = Flags()
    n = len(s.events)
    res = s.probe.check()  # same instant, but the invalidated in-flight result must not be served
    assert s.events[n:] != [] and res.ok and res.checked_at == START
    n = len(s.events)
    assert s.probe.check() is res and len(s.events) == n  # and the fresh result is cached normally


def test_probe_in_flight_without_invalidate_is_cached(tmp_path):
    s = Storage(tmp_path)
    results = _in_flight(s, lambda _r: None)
    s.hook = None
    n = len(s.events)
    assert s.probe.check() is results[0] and len(s.events) == n


@pytest.mark.parametrize("invalidated", [False, True])
def test_waiting_caller_shares_cached_result_or_probes_itself(invalidated, tmp_path):
    s = Storage(tmp_path)
    waiter: list = []

    def between(_r):
        w = threading.Thread(target=lambda: waiter.append(s.probe.check()))
        w.start()
        between.w = w
        if invalidated:
            s.probe.invalidate()

    results = _in_flight(s, between)
    between.w.join(WAIT)
    assert not between.w.is_alive() and results[0].ok and waiter[0].ok
    probes = s.events.count("open:wb")
    assert probes == (2 if invalidated else 1)
    if not invalidated:
        assert waiter[0] is results[0]


def test_concurrent_checks_serialise_real_probes(tmp_path):
    s = Storage(tmp_path)
    out: list = []
    barrier = threading.Barrier(8)

    def work(i: int) -> None:
        barrier.wait(WAIT)
        for j in range(10):
            out.append(s.probe.check(force=(i + j) % 2 == 0))

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(WAIT * 3)
    assert not any(t.is_alive() for t in threads)
    assert len(out) == 80 and all(r.ok for r in out)
    n = s.events.count("open:wb")
    assert n >= 1
    assert s.events == FRESH + EXISTING * (n - 1)  # whole probes only: never two interleaved


# --- isolation with the real default operations ----------------------------------------------


@pytest.mark.parametrize("durability", ["process", "crash"])
def test_real_operations_leave_evidence_files_untouched(durability, tmp_path):
    cfg = load_config({"CORONER_DURABILITY": durability}, evidence_dir=tmp_path)
    before = populate(cfg.evidence_dir)
    probe = StorageProbe(cfg.evidence_dir)
    for _ in range(3):
        assert probe.check(force=True).ok
    assert snapshot(tmp_path) == before
    assert set(os.listdir(tmp_path)) == set(before) | {PROBE_DIR}
    assert os.listdir(tmp_path / PROBE_DIR) == [PROBE_FILE]


# --- negative controls: the harness must reject broken implementations --------------------------


class _IgnoresDirFsync(StorageProbe):
    def _sync_pending_dirs(self) -> None:
        try:
            super()._sync_pending_dirs()
        except Exception:
            pass


class _AlwaysOk(StorageProbe):
    def check(self, *, force: bool = False):
        from coroner.collector.storage_probe import ProbeResult

        return ProbeResult(True, None, None, 0.0)


def test_negative_control_probe_that_ignores_dir_fsync_failure_is_detected():
    with pytest.raises(AssertionError):
        run_scenario("crash", (False, False), [("force", flags_of("dirfsync_probe"))], probe_cls=_IgnoresDirFsync)


def test_negative_control_constant_storage_ok_is_detected():
    with pytest.raises(AssertionError):
        run_scenario("process", (False, False), [("force", flags_of("fsync"))], probe_cls=_AlwaysOk)


def test_negative_control_disabled_injection_disagrees_with_oracle():
    with pytest.raises(AssertionError):
        run_scenario("crash", (False, False), [("force", flags_of("fsync"))], inject=False)


def test_negative_control_oracle_agrees_when_nothing_fails():
    assert run_scenario("crash", (False, False), [("force", Flags())], inject=False) == 1
