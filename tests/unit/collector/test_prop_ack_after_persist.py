"""Property 7: Persistence precedes acknowledgement at the claimed level (Task 3.8).

**Validates: Requirements 4.3, 4.8, 4.13, 4.14, 4.15, 4.16, 5.7, 13.14**

Contract tested through the real `IngestService`, `RunRegistry` and `EvidenceWriter` on real files in a temporary
directory. A recording double sees every operation on the run's evidence file: open (`O`), write (`W`, with the bytes),
flush (`F`), `fsync` of the file (`S`, with the file size at that moment), `fsync` of the directory (`D`) and a
raised injected failure (`X`). The test adds the acknowledgement (`A`) when `ingest` returns. Probe files are not used.

Independent oracle (written here, never calling the production append decision logic). The tokens of ONE write
group, from the `open` of the file until the next `open`, are fixed by a hand-written table:

    level    file     tokens of a clean write group
    process  any      O W F            (no fsync, ever)
    crash    existing O W F S          (file fsynced after the whole batch was written and flushed)
    crash    new, or  O W F S D        (directory fsynced after the file, when the file was created or an earlier
             dir sync pending                creation did not finish its directory sync)
    crash    sync     O F S D          (recovery's explicit fsync of file and directory; never in process level)

A request's groups must be, in this order and nothing else: `R` repair (iff the file's tail is unterminated or not a
JSON object), `Sy` recovery sync (iff crash level and the run is dirty), optionally one `G` (regenerated gaps),
`M` run_metadata (iff none was completed in this process), and `B` the batch (the last group, whose bytes are the
Sample or anomaly record plus its 0 to 2 gap records). A 200 is returned only when every group is complete and
matches the table, so the acknowledgement follows write, flush and (crash) fsync at the batch's final size. A 503 is
returned only when the injected failure ended the request in its last group; then no state moved, the run is dirty,
the probe was invalidated and earlier bytes are an unchanged prefix of the file. `durability` in the
acknowledgement is `crash` iff the batch group contained the fsync.
"""
from __future__ import annotations

import builtins
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from coroner.collector import state as state_module
from coroner.collector.config import CollectorConfig
from coroner.collector.service import create_ingest_service
from coroner.collector.service import IngestService
from coroner.collector.state import NewSample, Outcome, RunRegistry, RunState
from coroner.evidence import writer as writer_module
from coroner.evidence.reader import read_evidence, summarize
from coroner.evidence.schema import validate_record
from coroner.evidence.writer import AppendResult, Durability
from tests.unit.collector.test_prop_ingest_stored import line_of
from tests.unit.collector.test_service import Clock, FakeProbe, Rec, body, sample

SETTINGS = settings(
    max_examples=200, derandomize=True, database=None, deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
RUN = "r1"
FILE = "r1.jsonl"
FAULT_KINDS = ("write", "partial", "flush", "fsync", "dir")
CUTS = ("zero", "one", "half", "last", "all")


# --- recording doubles -----------------------------------------------------------------------------------


class Fault:
    """Fail the (skip+1)th matching operation, then `fires - 1` more matching operations (one failure per firing)."""

    def __init__(self, kind: str, skip: int = 0, fires: int = 1, cut: str = "half") -> None:
        assert kind in FAULT_KINDS and (isinstance(cut, int) or cut in CUTS)
        self.kind, self.skip, self.fires, self.cut = kind, skip, fires, cut

    def hit(self, op: str) -> bool:
        want = "write" if self.kind == "partial" else self.kind
        if op != want or self.fires <= 0:
            return False
        if self.skip > 0:
            self.skip -= 1
            return False
        self.fires -= 1
        return True


def cut_length(cut: Any, size: int) -> int:
    if isinstance(cut, int):
        return max(0, min(cut, size))
    return {"zero": 0, "one": min(1, size), "half": size // 2, "last": max(size - 1, 0), "all": size}[cut]


class Tape:
    def __init__(self, directory: Path, fault: Fault | None = None) -> None:
        self.directory = directory
        self.fault = fault
        self.events: list[tuple[Any, ...]] = []

    def open(self, path: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        target = Path(path)
        existed = target.exists()
        real = builtins.open(path, mode, *args, **kwargs)
        if target.parent != self.directory or target.suffix != ".jsonl" or mode != "ab":
            return real
        self.events.append(("O", existed, target.stat().st_size if existed else 0))
        return RecFile(self, real)

    def hit(self, op: str) -> bool:
        return self.fault is not None and self.fault.hit(op)

    def fsync(self, fd: int) -> None:
        if self.hit("fsync"):
            self.events.append(("X", "fsync"))
            raise OSError("injected fsync failure")
        import os

        self.events.append(("S", os.fstat(fd).st_size))

    def fsync_dir(self, path: Path) -> None:
        if self.hit("dir"):
            self.events.append(("X", "dir"))
            raise OSError("injected directory fsync failure")
        self.events.append(("D",))


class RecFile:
    def __init__(self, tape: Tape, real: Any) -> None:
        self._tape, self._real = tape, real

    def __enter__(self) -> "RecFile":
        return self

    def __exit__(self, *exc: object) -> None:
        self._real.close()

    def fileno(self) -> int:
        return self._real.fileno()

    def write(self, data: bytes) -> int:
        tape = self._tape
        if tape.hit("write"):
            assert tape.fault is not None
            if tape.fault.kind == "partial":
                part = bytes(data[: cut_length(tape.fault.cut, len(data))])
                self._real.write(part)
                tape.events.append(("W", part))
            tape.events.append(("X", "write"))
            raise OSError("injected write failure")
        self._real.write(data)
        tape.events.append(("W", bytes(data)))
        return len(data)

    def flush(self) -> None:
        self._real.flush()
        if self._tape.hit("flush"):
            self._tape.events.append(("X", "flush"))
            raise OSError("injected flush failure")
        self._tape.events.append(("F",))


# --- the oracle ------------------------------------------------------------------------------------------


@dataclass
class Group:
    toks: list[str]
    existed: bool
    size0: int
    data: bytes = b""
    s_size: int | None = None

    @property
    def faulted(self) -> bool:
        return self.toks[-1] == "X"


def parse_groups(events: list[tuple[Any, ...]]) -> list[Group]:
    groups: list[Group] = []
    for ev in events:
        if ev[0] == "O":
            groups.append(Group(["O"], ev[1], ev[2]))
            continue
        group = groups[-1]
        group.toks.append(ev[0])
        if ev[0] == "W":
            group.data += ev[1]
        elif ev[0] == "S":
            group.s_size = ev[1]
    return groups


def expected_tokens(crash: bool, sync: bool, created_or_pending: bool) -> list[str]:
    if sync:
        return ["O", "F", "S", "D"]
    tokens = ["O", "W", "F"]
    if crash:
        tokens.append("S")
        if created_or_pending:
            tokens.append("D")
    return tokens


def matches(toks: list[str], exp: list[str]) -> bool:
    """A complete group equals the table row; a failed group is the row's prefix followed by the failure."""
    if toks[-1] == "X":
        return toks[:-1] == exp[: len(toks) - 1] and len(toks) - 1 < len(exp)
    return toks == exp


def kind_of(data: bytes) -> str:
    if not data:
        return "Sy"
    if data.startswith(b"\x00\n"):
        return "R"
    first = json.loads(data.split(b"\n")[0])
    return {"repair": "R", "run_metadata": "M", "gap": "G", "sample": "B", "sequence_anomaly": "B"}[first["record_type"]]


def tail_bad(data: bytes) -> bool:
    if not data:
        return False
    if not data.endswith(b"\n"):
        return True
    last = data[:-1].rsplit(b"\n", 1)[-1]
    try:
        return not isinstance(json.loads(last.decode("utf-8")), dict)
    except ValueError:
        return True


def ranges_of(values: set[int]) -> tuple[tuple[int, int], ...]:
    out: list[list[int]] = []
    for v in sorted(values):
        if out and v == out[-1][1] + 1:
            out[-1][1] = v
        else:
            out.append([v, v])
    return tuple((a, b) for a, b in out)


def scan_bytes(data: bytes) -> tuple[Any, ...]:
    """What the state must be after reading these bytes: terminated, valid lines only (a fragment never counts)."""
    seqs: set[int] = set()
    recv = interval = None
    gap = 0
    for line in data.split(b"\n")[:-1]:
        try:
            record = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("record_type") == "sample":
            seqs.add(record["sample"]["seq"])
            recv, interval = record["recv_time"], record["sample"]["interval_ms"]
        elif record.get("record_type") == "gap":
            match = re.fullmatch(r"gap-([0-9]+)", str(record.get("gap_id")))
            gap = max(gap, int(match.group(1)) if match else 0)
    return (max(seqs) if seqs else None, ranges_of(seqs), recv, interval, gap + 1)


def snapshot(state: Any) -> tuple[Any, ...]:
    return (
        state.highest_seq, tuple((a, b) for a, b in state.seen.ranges()), state.last_sample_recv_time,
        state.interval_ms, state.next_gap_number,
    )


@dataclass
class Model:
    level: Durability
    dirty: bool = False
    meta_done: bool = False
    pending: bool = False

    @property
    def crash(self) -> bool:
        return self.level is Durability.CRASH


@dataclass
class Op:
    skip: int = 0
    dropped: int = 0
    repeat: bool = False


@dataclass
class Scenario:
    level: Durability
    first_seq: int
    ops: list[Op]
    existing: bool
    fault: tuple[str, int, int, str] | None  # kind, skip, fires, cut


def gap_count(prev: int | None, seq: int, dropped: int) -> int:
    discontinuity = seq > 0 if prev is None else seq > prev + 1
    return int(discontinuity) + int(dropped > 0)


def check_segment(
    m: Model, seg: list[tuple[Any, ...]], result: Any, pre: bytes | None, post: bytes, pre_snap: tuple[Any, ...],
    post_snap: tuple[Any, ...], dirty_flag: bool, probe_delta: int, seq: int, attempt: int, op: Op, prev: int | None,
) -> None:
    groups = parse_groups(seg)
    exists = pre is not None
    expected = []
    if exists and tail_bad(pre or b""):
        expected.append("R")
    if exists and m.crash and m.dirty:
        expected.append("Sy")
    gap_slot = len(expected)
    if not m.meta_done:
        expected.append("M")
    expected.append("B")
    candidates = [expected] + ([expected[:gap_slot] + ["G"] + expected[gap_slot:]] if exists else [])
    pending = m.pending
    complete: list[str] = []
    for index, g in enumerate(groups):
        assert not g.faulted or index == len(groups) - 1, "a failure did not end the request"
        wrote = "W" in g.toks
        options = [expected_tokens(m.crash, False, (not g.existed) or pending)]
        if m.crash and not wrote:
            options.append(expected_tokens(True, True, False))
        assert any(matches(g.toks, o) for o in options), f"group tokens {g.toks} match no table row {options}"
        if m.crash and ((not g.existed) or (wrote and pending)):
            pending = not ("D" in g.toks and not g.faulted)
        if not g.faulted:
            kind = kind_of(g.data)
            complete.append(kind)
            if kind == "G":
                assert all(json.loads(l)["record_type"] == "gap" for l in g.data.split(b"\n")[:-1])
            if kind == "M":
                m.meta_done = True
    m.pending = pending
    if pre is not None:
        assert post.startswith(pre), "earlier evidence bytes changed"
    if result.http_status == 200:
        assert groups and not groups[-1].faulted and complete in candidates, f"groups {complete}, expected one of {candidates}"
        batch = groups[-1]
        assert complete[-1] == "B"
        _check_ack(m, result, batch, post, seq, attempt, op, prev)
        assert post.endswith(batch.data)
        assert post_snap[:4] == scan_bytes(post)[:4], "state differs from the persisted evidence"
        assert not dirty_flag and probe_delta == 0
        m.dirty = False
        return
    assert (result.http_status, result.body, result.kind, result.durability) == (503, {"error": "storage_failure"}, "storage_failure", None)
    assert groups and groups[-1].faulted, "503 without an injected failure"
    nxt = [c for c in candidates if len(c) > len(complete) and c[: len(complete)] == complete]
    assert nxt, f"complete groups {complete} are not a prefix of {candidates}"
    failed = groups[-1]
    if nxt[0][len(complete)] == "Sy":
        assert "W" not in failed.toks
    assert dirty_flag, "the run was not marked dirty"
    assert probe_delta == 1, "the storage probe was not invalidated exactly once"
    at_batch = len(complete) == len(nxt[0]) - 1
    want = scan_bytes(post[: failed.size0]) if at_batch else pre_snap
    assert post_snap[:4] == (want if at_batch else want)[:4], "state moved although the append failed"
    assert (post_snap[4] >= want[4]) if at_batch else post_snap[4] == want[4]
    m.dirty = True


def _check_ack(m: Model, result: Any, batch: Group, post: bytes, seq: int, attempt: int, op: Op, prev: int | None) -> None:
    data = batch.data
    assert data.endswith(b"\n") and b"\r" not in data
    records = [json.loads(line) for line in data.split(b"\n")[:-1]]
    assert all(validate_record(r) == [] for r in records)
    status = result.body["status"]
    if attempt == 0:
        assert status == ("duplicate" if op.repeat else "stored")
    if status == "stored":
        assert [r["record_type"] for r in records] == ["sample"] + ["gap"] * gap_count(prev, seq, op.dropped)
        assert records[0]["sample"]["seq"] == seq
    else:
        assert status == "duplicate" and [r["record_type"] for r in records] == ["sequence_anomaly"] and records[0]["seq"] == seq
    claimed = "crash" if "S" in batch.toks else "process"
    assert result.body["durability"] == claimed == m.level.value == result.durability
    if m.crash:
        assert batch.s_size == batch.size0 + len(data), "fsync did not cover the whole batch"
    else:
        assert "S" not in batch.toks and "D" not in batch.toks


# --- the runner ------------------------------------------------------------------------------------------


def run_scenario(sc: Scenario, root: Path, *, mutate: Any = None) -> dict[str, Any]:
    directory = root / "evidence"
    path = directory / FILE
    config = CollectorConfig(durability=sc.level, max_body_bytes=65536, watchdog_tick_ms=50, evidence_dir=directory)
    clock = Clock()
    prev: int | None = None
    sent: list[int] = []
    if sc.existing:  # an earlier Collector process left the run file behind
        rec = Rec()
        first = create_ingest_service(config, fsync=rec.fsync, fsync_dir=rec.fsync_dir, now=clock.now)
        first.registry.start()
        assert first.ingest(body(sample(RUN, 0))).http_status == 200
        first.registry.close()
        prev = 0
        sent.append(0)
    tape = Tape(directory)
    probe = FakeProbe()
    model = Model(sc.level)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(writer_module, "open", tape.open, raising=False)
        mp.setattr(state_module, "open", tape.open, raising=False)
        service = create_ingest_service(config, fsync=tape.fsync, fsync_dir=tape.fsync_dir, now=clock.now, storage_probe=probe)
        service.registry.start()
        tape.events.clear()
        if mutate is not None:
            mutate(mp, service)
        if sc.fault is not None:
            tape.fault = Fault(*sc.fault)
        state = service.registry.get(RUN)
        total_gaps = {"sequence_discontinuity": 0, "probe_reported_drop": 0}
        for index, op in enumerate(sc.ops):
            repeat = op.repeat and prev is not None
            op = Op(op.skip, op.dropped, repeat)
            seq = prev if repeat else (sc.first_seq if prev is None else prev + 1 + op.skip)
            if not repeat:
                total_gaps["sequence_discontinuity"] += int(seq > 0 if prev is None else seq > prev + 1)
                total_gaps["probe_reported_drop"] += int(op.dropped > 0)
            raw = body(sample(RUN, seq, op.dropped))
            for attempt in range(6):
                pre = path.read_bytes() if path.exists() else None
                pre_snap = snapshot(state)
                invalidations = probe.invalidations
                mark = len(tape.events)
                result = service.ingest(raw)
                tape.events.append(("A", result.http_status))
                segment = tape.events[mark:-1]
                check_segment(
                    model, segment, result, pre, path.read_bytes() if path.exists() else b"", pre_snap, snapshot(state),
                    state.dirty, probe.invalidations - invalidations, seq, attempt, op, prev,
                )
                if result.http_status == 200:
                    break
            else:
                pytest.fail("the request never succeeded after 6 attempts")
            if not repeat:
                sent.append(seq)
                prev = seq
        service.registry.close()
    final = read_evidence(path)
    assert all(validate_record(r) == [] for r in final.records)
    seqs = sorted(r["sample"]["seq"] for r in final.records if r["record_type"] == "sample")
    assert seqs == sorted(sent), "a Sample is missing, duplicated or came from a fragment"
    for gap_type, count in total_gaps.items():
        assert len([r for r in final.records if r["record_type"] == "gap" and r["gap_type"] == gap_type]) == count, gap_type
    return {"final": final, "tape": tape, "probe": probe, "model": model}


# --- Hypothesis ------------------------------------------------------------------------------------------


@st.composite
def scenarios(draw: Any, always_fault: bool = False) -> Scenario:
    level = draw(st.sampled_from(list(Durability)))
    ops = draw(st.lists(
        st.builds(Op, skip=st.sampled_from([0, 0, 0, 1, 3]), dropped=st.sampled_from([0, 0, 0, 1, 2]), repeat=st.sampled_from([False, False, False, True])),
        min_size=1, max_size=4,
    ))
    one_fault = st.tuples(st.sampled_from(FAULT_KINDS), st.integers(0, 4), st.sampled_from([1, 1, 2]), st.sampled_from(CUTS))
    fault = draw(one_fault if always_fault else st.one_of(st.none(), one_fault))
    return Scenario(level, draw(st.sampled_from([0, 0, 1, 3])), ops, draw(st.booleans()), fault)


@SETTINGS
@given(st.one_of(scenarios(), scenarios(always_fault=True)))
def test_property_7_persistence_precedes_acknowledgement_at_the_claimed_level(sc: Scenario) -> None:
    event(f"level={sc.level.value}")
    event(f"fault={sc.fault[0] if sc.fault else 'none'}")
    event(f"file={'existing' if sc.existing else 'new'}")
    event(f"fires={sc.fault[2] if sc.fault else 0}")
    with tempfile.TemporaryDirectory() as tmp:
        out = run_scenario(sc, Path(tmp))
    if sc.fault is not None and any(e[0] == "X" for e in out["tape"].events):
        event("failure_fired")


# --- boundaries: the exact event order, per level and file ------------------------------------------------


def stream(out: dict[str, Any]) -> str:
    return "".join(e[0] for e in out["tape"].events)


@pytest.mark.parametrize(
    ("level", "existing", "want"),
    [
        (Durability.CRASH, False, "OWFSD" "OWFS" "A"),  # created: file fsync then directory fsync; then the batch, fsynced
        (Durability.CRASH, True, "OWFS" "OWFS" "A"),  # resumed run_metadata, then the batch; no directory fsync
        (Durability.PROCESS, False, "OWF" "OWF" "A"),  # write and flush only; never an fsync
        (Durability.PROCESS, True, "OWF" "OWF" "A"),
    ],
)
def test_the_event_order_of_a_first_ingest_is_exactly_the_table(level: Durability, existing: bool, want: str, tmp_path: Path) -> None:
    out = run_scenario(Scenario(level, 0, [Op(skip=0)] if not existing else [Op(skip=0)], existing, None), tmp_path)
    assert stream(out) == want
    assert out["final"].errors == [] and out["probe"].invalidations == 0


def test_a_batch_of_one_and_a_batch_of_three_records_are_one_write_each(tmp_path: Path) -> None:
    out = run_scenario(Scenario(Durability.CRASH, 0, [Op(), Op(skip=1, dropped=1)], False, None), tmp_path)
    writes = [e[1] for e in out["tape"].events if e[0] == "W"]
    assert [w.count(b"\n") for w in writes] == [1, 1, 3]  # run_metadata, the first Sample, then Sample + 2 gaps in ONE write
    assert stream(out) == "OWFSD" "OWFS" "A" "OWFS" "A"


@pytest.mark.parametrize("cut", ["zero", "one", "last", "all"])
@pytest.mark.parametrize("kind", ["write", "partial", "flush", "fsync"])
def test_failures_at_the_first_byte_and_the_last_byte_of_a_three_record_batch_are_never_acknowledged(kind: str, cut: str, tmp_path: Path) -> None:
    sc = Scenario(Durability.CRASH, 0, [Op(), Op(skip=1, dropped=1)], False, (kind, 2 if kind in ("write", "partial") else (0 if kind == "dir" else 1), 1, cut))
    out = run_scenario(sc, tmp_path)
    assert any(e[0] == "X" for e in out["tape"].events) and out["probe"].invalidations == 1


@pytest.mark.parametrize("existing", [False, True])
def test_a_failed_directory_fsync_when_the_file_is_created_is_never_acknowledged_and_the_retry_syncs_the_directory(existing: bool, tmp_path: Path) -> None:
    out = run_scenario(Scenario(Durability.CRASH, 0, [Op()], existing, ("dir", 0, 1, "zero")), tmp_path)
    if existing:
        assert not any(e[0] == "X" for e in out["tape"].events)  # the file exists, so there is no creation sync to fail
    else:
        assert stream(out).startswith("OWFSX")  # file synced, then the directory sync failed: no acknowledgement yet
        assert "OWFSD" in stream(out)  # the retry's first write still owes the directory sync
        assert out["probe"].invalidations == 1


# --- the oracle itself ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [list("OWSF"), list("OSWF"), list("OWF"), list("OWFD"), list("OWFSDD"), list("OFWS"), list("OWFDS")],
)
def test_the_table_rejects_a_clean_group_in_a_wrong_order_or_with_a_step_missing(bad: list[str]) -> None:
    assert not matches(bad, expected_tokens(True, False, True)) or bad == list("OWFSD")
    assert not matches(bad, expected_tokens(True, False, False))


def test_the_table_for_process_level_has_no_fsync_and_a_failed_group_is_a_prefix_plus_the_failure() -> None:
    assert matches(list("OWF"), expected_tokens(False, False, True)) and not matches(list("OWFS"), expected_tokens(False, False, True))
    assert matches(["O", "W", "F", "X"], expected_tokens(True, False, False)) and not matches(["O", "W", "F", "S", "X"], expected_tokens(True, False, False))
    assert matches(["O", "X"], expected_tokens(True, True, False)) and not matches(["O", "S", "X"], expected_tokens(True, False, False))


# --- negative controls: broken Collectors are caught -------------------------------------------------------


def _wrap_append(mp: pytest.MonkeyPatch, replacement: Any) -> None:
    real = RunRegistry.append
    mp.setattr(RunRegistry, "append", lambda self, state, records: replacement(real, self, state, records))


def _ack_after_failed_append(real: Any, self: RunRegistry, state: RunState, records: Any) -> AppendResult:
    try:
        return real(self, state, records)
    except OSError:
        return AppendResult(self.writer.path_for(state.run_id), len(records), 0, False, True, True)  # acknowledges what failed


def _apply_before_append(real: Any, self: RunRegistry, state: RunState, records: Any) -> AppendResult:
    if records[0]["record_type"] == "sample":
        state.apply(Outcome(new_sample=NewSample(records[0]["sample"]["seq"], records[0]["recv_time"], 500, 0.0)))
    return real(self, state, records)


def _mutants() -> dict[str, tuple[Any, Scenario]]:
    fail_batch = ("write", 1, 1, "zero")
    crash_fault = Scenario(Durability.CRASH, 0, [Op()], False, fail_batch)
    return {
        "ack after a failed append": (lambda mp, s: _wrap_append(mp, _ack_after_failed_append), Scenario(Durability.CRASH, 0, [Op()], False, ("fsync", 1, 1, "zero"))),
        "state applied before the append": (lambda mp, s: _wrap_append(mp, _apply_before_append), crash_fault),
        "mark_dirty skipped": (lambda mp, s: mp.setattr(RunState, "mark_dirty", lambda self: None), crash_fault),
        "probe invalidation skipped": (lambda mp, s: mp.setattr(IngestService, "_invalidate", lambda self: None), crash_fault),
        "crash reported without an fsync": (lambda mp, s: mp.setattr(AppendResult, "durability", property(lambda self: Durability.CRASH)), Scenario(Durability.PROCESS, 0, [Op()], False, None)),
        "file fsync skipped at crash level": (lambda mp, s: setattr(s.registry.writer, "_fsync", lambda fd: None), Scenario(Durability.CRASH, 0, [Op()], False, None)),
        "directory fsync skipped at crash level": (lambda mp, s: setattr(s.registry.writer, "_fsync_dir", lambda p: None), Scenario(Durability.CRASH, 0, [Op()], False, None)),
        "recovery sync skipped": (lambda mp, s: setattr(s.registry, "_fsync", lambda fd: None), Scenario(Durability.CRASH, 0, [Op()], False, ("fsync", 1, 1, "zero"))),
    }


@pytest.mark.parametrize("name", list(_mutants()))
def test_the_property_catches_a_broken_collector(name: str, tmp_path: Path) -> None:
    mutate, sc = _mutants()[name]
    with pytest.raises(Exception):  # the oracle assertion, or the broken Collector crashing on its own inconsistency
        run_scenario(sc, tmp_path, mutate=mutate)


def test_the_mutation_scenarios_pass_on_the_real_collector(tmp_path: Path) -> None:
    for index, (mutate, sc) in enumerate(_mutants().values()):
        run_scenario(sc, tmp_path / str(index))


# --- recovery of a partial append: what the reader sees ----------------------------------------------------


def _sample_line_length(seq: int, dropped: int = 0) -> int:
    record = {"record_type": "sample", "schema_version": 1, "run_id": RUN, "recv_time": "2025-01-01T12:00:00.000Z", "sample": sample(RUN, seq, dropped)}
    return len(line_of(record))


def _types(final: Any) -> list[str]:
    return [r["record_type"] for r in final.records]


def _errors(final: Any) -> list[tuple[Any, ...]]:
    return [(e.file, e.line_no, e.kind, e.has_repair_record, e.byte_length) for e in final.errors]


def _partial_gap_run(cut: int, tmp_path: Path) -> tuple[dict[str, Any], bytes]:
    """Sample 0, then a 3-record batch (Sample 2 + two gaps) cut after `cut` bytes, then a successful retry."""
    out = run_scenario(Scenario(Durability.CRASH, 0, [Op(), Op(skip=1, dropped=1)], False, ("partial", 2, 1, cut)), tmp_path)
    return out, (tmp_path / "evidence" / FILE).read_bytes()


def test_a_cut_inside_the_sample_line_is_isolated_by_repair_and_the_fragment_is_never_counted(tmp_path: Path) -> None:
    length = _sample_line_length(2, 1)
    out, data = _partial_gap_run(length // 2, tmp_path)
    lines = data.split(b"\n")
    fragment = lines[2]
    assert fragment.endswith(b"\x00") and len(fragment) == length // 2 + 1  # the original bytes are an unchanged prefix, then the D30 marker
    assert _errors(out["final"]) == [(FILE, 3, "isolated_by_repair", True, length // 2 + 1)]
    assert _types(out["final"]) == ["run_metadata", "sample", "repair", "sample", "gap", "gap"]  # retry stored it once
    assert summarize(out["final"]).sample_count == 2


def test_a_complete_sample_json_missing_only_its_newline_is_isolated_not_counted_and_stored_again(tmp_path: Path) -> None:
    length = _sample_line_length(2, 1)
    out, data = _partial_gap_run(length - 1, tmp_path)  # cut before the newline: the bytes parse as a Sample
    fragment = data.split(b"\n")[2]
    assert json.loads(fragment[:-1])["sample"]["seq"] == 2 and fragment.endswith(b"\x00")
    assert _errors(out["final"]) == [(FILE, 3, "isolated_by_repair", True, length)]
    assert [r["sample"]["seq"] for r in out["final"].records if r["record_type"] == "sample"] == [0, 2]  # once, from the retry
    assert _types(out["final"]) == ["run_metadata", "sample", "repair", "sample", "gap", "gap"]


def test_a_cut_right_after_the_sample_line_leaves_no_fragment_and_the_gaps_are_regenerated(tmp_path: Path) -> None:
    out, data = _partial_gap_run(_sample_line_length(2, 1), tmp_path)
    assert out["final"].errors == [] and data.endswith(b"\n")
    assert sorted(_types(out["final"])) == sorted(["run_metadata", "sample", "sample", "gap", "gap", "sequence_anomaly"])
    assert [r["sample"]["seq"] for r in out["final"].records if r["record_type"] == "sample"] == [0, 2]


def test_a_partial_gap_line_after_a_complete_sample_line_is_isolated_and_the_sample_stays_countable(tmp_path: Path) -> None:
    out, data = _partial_gap_run(_sample_line_length(2, 1) + 10, tmp_path)
    assert _errors(out["final"]) == [(FILE, 4, "isolated_by_repair", True, 11)]
    assert data.split(b"\n")[3].endswith(b"\x00") and len(data.split(b"\n")[3]) == 11
    assert [r["sample"]["seq"] for r in out["final"].records if r["record_type"] == "sample"] == [0, 2]
    assert sorted(_types(out["final"])) == sorted(["run_metadata", "sample", "sample", "repair", "gap", "gap", "sequence_anomaly"])


def test_an_interrupted_repair_never_makes_an_unacknowledged_sample_countable(tmp_path: Path) -> None:
    directory = tmp_path / "evidence"
    path = directory / FILE
    config = CollectorConfig(durability=Durability.CRASH, max_body_bytes=65536, watchdog_tick_ms=50, evidence_dir=directory)
    clock, tape = Clock(), Tape(directory)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(writer_module, "open", tape.open, raising=False)
        mp.setattr(state_module, "open", tape.open, raising=False)
        service = create_ingest_service(config, fsync=tape.fsync, fsync_dir=tape.fsync_dir, now=clock.now, storage_probe=FakeProbe())
        service.registry.start()
        assert service.ingest(body(sample(RUN, 0))).http_status == 200
        length = _sample_line_length(1)

        def attempt(fault: Fault | None) -> int:
            tape.fault = fault
            return service.ingest(body(sample(RUN, 1))).http_status

        def seqs() -> list[int]:
            return [r["sample"]["seq"] for r in read_evidence(path).records if r["record_type"] == "sample"]

        original = path.read_bytes()
        assert attempt(Fault("partial", 0, 1, length - 1)) == 503  # a complete Sample JSON without its newline
        sample_json = path.read_bytes()[len(original):]
        assert json.loads(sample_json)["sample"]["seq"] == 1
        assert seqs() == [0]
        assert [(e.line_no, e.kind, e.has_repair_record) for e in read_evidence(path).errors] == [(3, "incomplete_last_line", False)]
        assert attempt(Fault("partial", 0, 1, 1)) == 503  # the repair persisted only its NUL marker
        assert path.read_bytes() == original + sample_json + b"\x00" and seqs() == [0]
        assert attempt(Fault("partial", 0, 1, 10)) == 503  # the repair buffer is cut after 10 bytes, inside its record
        assert path.read_bytes().startswith(original + sample_json + b"\x00\x00\n") and seqs() == [0]
        assert [(e.line_no, e.kind, e.has_repair_record) for e in read_evidence(path).errors] == [(3, "invalid_json", False), (4, "incomplete_last_line", False)]
        assert attempt(None) == 200
        service.registry.close()
    final = read_evidence(path)
    assert seqs_of(final) == [0, 1]  # the acknowledged retry is the only countable copy of Sample 1
    assert [(e.line_no, e.kind, e.has_repair_record) for e in final.errors] == [(3, "invalid_json", False), (4, "isolated_by_repair", True)]
    assert path.read_bytes().startswith(original + sample_json + b"\x00\x00\n")  # nothing earlier was changed
    assert all(validate_record(r) == [] for r in final.records)


def seqs_of(final: Any) -> list[int]:
    return [r["sample"]["seq"] for r in final.records if r["record_type"] == "sample"]
