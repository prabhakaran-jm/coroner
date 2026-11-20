"""Persistence across a Collector restart (Task 3.13; Requirements 4.4, 4.16, 5.3, 5.5, 5.7; design D26, D30 to D34).

Restart methodology: Collector A is `create_app` over a temporary evidence directory, driven through HTTP inside
`with TestClient(app)`. Leaving the context is a clean shutdown. Collector B is a NEW `create_app` (new registry,
new in-memory state, new probe, new watchdog) over the same directory. A's objects are never reused. Evidence
bytes are checked against expectations built from the test's own inputs and the acknowledgements A returned, not
from `read_evidence` alone. The volume lock is a recording double (the real `VolumeLock` is a no-op on win32).

Scope limit: these tests show that evidence that reached the operating system (Process) or was fsynced (Crash)
survives a clean process restart and the repair paths. They make no claim about power loss; the fsync calls are
observed through doubles, not through a host crash.
"""
from __future__ import annotations

import base64
import builtins
import hashlib
import io
import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from starlette.testclient import TestClient

from coroner.collector.app import create_app
from coroner.collector.config import load_config
from coroner.collector.state import CollectorAlreadyRunning
from coroner.collector.storage_probe import StorageProbe
from coroner.common.timeutil import format_utc_ms
from coroner.evidence import writer as writer_module
from coroner.evidence.reader import read_evidence, summarize
from coroner.evidence.repair import repair_file
from coroner.evidence.schema import validate_record
from coroner.evidence.writer import Durability, EvidenceWriter

BASE = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
SAMPLE = {
    "run_id": "r1", "seq": 0, "ts": "2025-01-01T12:00:06.512Z", "rss_bytes": 1000, "request_count": 1,
    "loop_delay_ms": 0.5, "pid": 1, "container_id": "3f1c0e0a9d7b", "container_id_source": "mountinfo",
    "interval_ms": 500, "dropped": 0,
}
TERMINATION = {
    "source": "experiment_runner", "observed_time": "2025-01-01T12:17:03.221Z",
    "container_state": {"status": "exited", "exit_code": 137, "oom_killed": True},
    "cause_label": "docker_oom_kill", "note": "Observed after the fact via docker inspect.",
}
SEQS_A = (0, 1, 2, 5)  # seq 5 after 2 makes a sequence_discontinuity gap (gap-0001) for 3..4
STORAGE_FAILURE = {"error": "storage_failure"}


def sample(seq: int = 0, run_id: str = "r1") -> dict[str, Any]:
    return {**SAMPLE, "run_id": run_id, "seq": seq}


def post(client: TestClient, seq: int = 0, run_id: str = "r1") -> Any:
    return client.post("/v1/samples", content=json.dumps(sample(seq, run_id)).encode())


def terminate(client: TestClient, run_id: str) -> Any:
    return client.post(f"/v1/runs/{run_id}/termination", content=json.dumps(TERMINATION).encode())


# --- doubles ---------------------------------------------------------------------------------------------------


class Ops:
    """An fsync double: records the size of the file at each call and can be made to fail."""

    def __init__(self) -> None:
        self.fail = False
        self.sizes: list[int] = []

    def fsync(self, fd: int) -> None:
        if self.fail:
            raise OSError("fsync failed")
        import os

        self.sizes.append(os.fstat(fd).st_size)

    @property
    def calls(self) -> int:
        return len(self.sizes)


class DirOps:
    def __init__(self) -> None:
        self.fail = False
        self.calls: list[str] = []

    def __call__(self, path: Path) -> None:
        if self.fail:
            raise OSError("directory fsync failed")
        self.calls.append(Path(path).name)


class Tick:
    def __init__(self, value: float) -> None:
        self.t = value

    def __call__(self) -> float:
        return self.t


class Wall:
    """A wall clock that advances 10 ms per reading, starting at `BASE + offset`."""

    def __init__(self, offset_s: int) -> None:
        self.start = BASE + timedelta(seconds=offset_s)
        self.ticks = 0

    def now(self) -> str:
        self.ticks += 1
        return format_utc_ms(self.start + timedelta(milliseconds=10 * self.ticks))


class Locks:
    """Volume-lock doubles that share one owner slot, like the flock on a real volume."""

    def __init__(self) -> None:
        self.holder: str | None = None
        self.log: list[str] = []

    def make(self, tag: str, directory: Path) -> "LockDouble":
        return LockDouble(self, tag, directory)


class LockDouble:
    def __init__(self, owner: Locks, tag: str, directory: Path) -> None:
        self.owner, self.tag, self.path, self.held = owner, tag, directory / ".collector.lock", False

    def acquire(self) -> None:
        if self.owner.holder is not None:
            raise CollectorAlreadyRunning(f"{self.owner.holder} holds the volume")
        self.owner.holder, self.held = self.tag, True
        self.owner.log.append(f"acquire:{self.tag}")

    def release(self) -> None:
        if self.held:
            self.owner.holder, self.held = None, False
            self.owner.log.append(f"release:{self.tag}")


class FailingFile:
    def __init__(self, real: Any, mode: str) -> None:
        self._real, self._mode = real, mode

    def __enter__(self) -> "FailingFile":
        return self

    def __exit__(self, *exc: object) -> None:
        self._real.close()

    def write(self, data: bytes) -> int:
        if self._mode == "write":
            raise OSError("disk full")
        if self._mode == "partial":
            self._real.write(data[: len(data) // 2])
            raise OSError("disk full")
        return self._real.write(data)

    def flush(self) -> None:
        self._real.flush()
        if self._mode == "flush":
            raise OSError("flush failed")

    def fileno(self) -> int:
        return self._real.fileno()


class Faults:
    """Fail the next open of `<name>` through the writer's `open` seam (one failure per `arm`)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.mode: str | None = None
        self.name = ""
        monkeypatch.setattr(writer_module, "open", self._open, raising=False)

    def arm(self, mode: str, name: str) -> None:
        self.mode, self.name = mode, name

    def _open(self, path: Any, mode: str) -> Any:
        real = builtins.open(path, mode)
        if self.mode is not None and Path(path).name == self.name:
            fault, self.mode = self.mode, None
            return FailingFile(real, fault)
        return real


class Node:
    """One Collector process: its own app, registry, clocks, fsync doubles and probe."""

    def __init__(self, env: "Env", tag: str, durability: str, offset_s: int) -> None:
        self.env, self.tag = env, tag
        self.directory = env.directory
        self.config = load_config({"CORONER_DURABILITY": durability}, evidence_dir=self.directory)
        self.ops, self.dirs, self.probe_ops = Ops(), DirOps(), Ops()
        self.mono, self.pclock, self.wall = Tick(1000.0), Tick(0.0), Wall(offset_s)
        self.probe_opens = 0

        def opener(path: Path, mode: str) -> Any:
            if mode == "wb":
                self.probe_opens += 1
            return io.open(path, mode)

        self.probe = StorageProbe(self.directory, fsync=self.probe_ops.fsync, fsync_dir=lambda p: None, clock=self.pclock, opener=opener)
        self.lock = env.locks.make(tag, self.directory)
        self.app = create_app(
            self.config, fsync=self.ops.fsync, fsync_dir=self.dirs, now=self.wall.now, monotonic=self.mono,
            probe=self.probe, volume_lock=self.lock,  # type: ignore[arg-type]
        )
        self.registry = self.app.state.registry

    @contextmanager
    def up(self) -> Iterator[TestClient]:
        """Run the lifespan. The watchdog thread is stopped so tests drive `check_once` themselves."""
        with TestClient(self.app) as client:
            self.app.state.watchdog.stop()
            yield client

    def check(self) -> list[str]:
        return self.app.state.watchdog.check_once()


class Env:
    def __init__(self, tmp: Path) -> None:
        self.tmp, self.directory, self.locks, self.count = tmp, tmp / "evidence", Locks(), 0

    def node(self, durability: str = "crash") -> Node:
        node = Node(self, "ABCDEFGH"[self.count], durability, 3600 * self.count)
        self.count += 1
        return node


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# --- helpers ---------------------------------------------------------------------------------------------------


def snap(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.glob("*.jsonl"))}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parsed(data: bytes) -> list[dict[str, Any]]:
    """Every line of `data` that is a JSON object, found without the Evidence_Reader."""
    out = []
    for line in data.split(b"\n"):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def of_type(path: Path, kind: str) -> list[dict[str, Any]]:
    return [r for r in parsed(path.read_bytes()) if r.get("record_type") == kind]


def sample_seqs(path: Path) -> list[int]:
    return [r["sample"]["seq"] for r in of_type(path, "sample")]


def line_of(data: bytes, kind: str) -> bytes:
    found = [ln for ln in data.split(b"\n") if ln and f'"record_type":"{kind}"'.encode() in ln]
    return found[-1]


def assert_all_valid(path: Path) -> None:
    for record in read_evidence(path).records:
        assert validate_record(record) == [], record


def run_a(env: Env, durability: str = "crash", run_id: str = "r1", seqs: tuple[int, ...] = SEQS_A) -> tuple[Node, list[dict[str, Any]]]:
    a = env.node(durability)
    acks = []
    with a.up() as client:
        for seq in seqs:
            response = post(client, seq, run_id)
            assert response.status_code == 200 and response.json()["status"] == "stored"
            acks.append(response.json())
    assert env.locks.holder is None  # clean shutdown released the volume lock
    return a, acks


def expect_samples(path: Path, seqs: tuple[int, ...], acks: list[dict[str, Any]], run_id: str = "r1") -> None:
    """Oracle: the Sample records equal what A was sent, with the recv_time A acknowledged."""
    actual = [(r["sample"], r["recv_time"]) for r in of_type(path, "sample")]
    assert actual == [(sample(s, run_id), a["recv_time"]) for s, a in zip(seqs, acks)]


# --- Step 1 and 2: restart methodology and evidence preservation -----------------------------------------------


def test_restart_uses_a_new_registry_and_the_volume_lock_is_released_between_collectors(env: Env) -> None:
    a, _ = run_a(env)
    assert env.locks.log == ["acquire:A", "release:A"] and not a.registry.started
    b = env.node()
    with b.up():
        assert env.locks.holder == "B" and b.registry is not a.registry and b.app is not a.app
        assert b.registry.get("r1") is not a.registry.get("r1") and not b.registry.get("r1").loaded
    assert env.locks.log == ["acquire:A", "release:A", "acquire:B", "release:B"]


def test_a_second_collector_is_refused_while_the_first_is_running(env: Env) -> None:
    a = env.node()
    with a.up():
        b = env.node()
        with pytest.raises(CollectorAlreadyRunning):
            with TestClient(b.app):
                pass  # pragma: no cover
        assert not b.app.state.ready
    with env.node().up():  # after A stopped, a new Collector starts
        pass


def test_acknowledged_samples_survive_a_restart_unchanged(env: Env) -> None:
    _, acks = run_a(env)
    path = env.directory / "r1.jsonl"
    assert [a["seq"] for a in acks] == list(SEQS_A) and {a["durability"] for a in acks} == {"crash"}
    expect_samples(path, SEQS_A, acks)
    b = env.node()
    with b.up() as client:
        evidence = client.get("/v1/runs/r1/evidence")
    assert evidence.status_code == 200 and evidence.content == path.read_bytes()
    expect_samples(path, SEQS_A, acks)
    assert_all_valid(path)
    assert summarize(read_evidence(path)).sample_count == len(SEQS_A)


def test_earlier_bytes_are_an_unchanged_prefix_after_b_appends(env: Env) -> None:
    run_a(env)
    path = env.directory / "r1.jsonl"
    before = path.read_bytes()
    sizes = [len(before)]
    with env.node().up() as client:
        assert post(client, 6).json()["status"] == "stored"
        sizes.append(path.stat().st_size)
        assert post(client, 7).json()["status"] == "stored"
        sizes.append(path.stat().st_size)
    after = path.read_bytes()
    assert after.startswith(before) and sha(after[: len(before)]) == sha(before)
    assert sizes == sorted(sizes) and sizes[0] < sizes[-1] and len(after) == path.stat().st_size


def test_resending_every_acknowledged_sample_after_restart_stores_no_duplicate(env: Env) -> None:
    run_a(env)
    path = env.directory / "r1.jsonl"
    with env.node().up() as client:
        statuses = [post(client, s).json()["status"] for s in SEQS_A]
    assert statuses == ["duplicate"] * len(SEQS_A)
    assert sample_seqs(path) == list(SEQS_A)


def test_gap_ids_stay_unique_and_continue_after_a_restart(env: Env) -> None:
    run_a(env)
    path = env.directory / "r1.jsonl"
    (first,) = of_type(path, "gap")
    assert (first["gap_id"], first["gap_type"], first["first_missing_seq"], first["last_missing_seq"]) == ("gap-0001", "sequence_discontinuity", 3, 4)
    with env.node().up() as client:
        assert post(client, 9).json()["status"] == "stored"  # 6..8 missing
    gaps = of_type(path, "gap")
    assert [g["gap_id"] for g in gaps] == ["gap-0001", "gap-0002"] and gaps[0] == first
    assert (gaps[1]["first_missing_seq"], gaps[1]["last_missing_seq"]) == (6, 8)
    assert_all_valid(path)


def test_sequence_state_is_rebuilt_so_the_next_sample_is_classified_correctly(env: Env) -> None:
    run_a(env)
    path = env.directory / "r1.jsonl"
    with env.node().up() as client:
        assert post(client, 5).json()["status"] == "duplicate"  # A's last
        assert post(client, 1).json()["status"] == "duplicate"  # an earlier one
        assert post(client, 3).json()["status"] == "reset"  # below the highest and never seen: an anomaly, not stored
        assert post(client, 6).json()["status"] == "stored"  # the next expected seq: no new gap
    assert sample_seqs(path) == [0, 1, 2, 5, 6] and len(of_type(path, "gap")) == 1
    assert [r["record_type"] for r in of_type(path, "sequence_anomaly")] != []


def test_rejection_records_from_a_remain_readable_after_a_restart(env: Env) -> None:
    a = env.node()
    with a.up() as client:
        assert client.post("/v1/samples", content=b"{").status_code == 422
        assert post(client, -1).status_code == 422
    rejections, run_file = env.directory / "_rejections.jsonl", env.directory / "r1.jsonl"
    before = snap(env.directory)
    with env.node().up() as client:
        assert client.get("/v1/runs/r1/evidence").content == before["r1.jsonl"]
    assert snap(env.directory) == before
    assert [r["reason_code"] for r in of_type(rejections, "rejection")] == ["invalid_json"]
    assert [r["http_status"] for r in of_type(run_file, "rejection")] == [422]
    assert read_evidence(rejections).errors == [] and len(read_evidence(run_file).records) == 2 and read_evidence(run_file).errors == []


def test_nothing_is_truncated_or_overwritten_across_two_restarts(env: Env) -> None:
    run_a(env)
    path = env.directory / "r1.jsonl"
    history = [path.read_bytes()]
    for seq in (6, 7):
        with env.node().up() as client:
            assert post(client, seq).json()["status"] == "stored"
        history.append(path.read_bytes())
    assert all(later.startswith(earlier) and len(later) > len(earlier) for earlier, later in zip(history, history[1:]))
    assert_all_valid(path)
    assert sample_seqs(path) == [0, 1, 2, 5, 6, 7]


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.integers(min_value=0, max_value=12), min_size=1, max_size=7))
def test_property_restart_keeps_exactly_the_acknowledged_samples(seqs: list[int]) -> None:
    """For any request order, the stored Samples after a restart are exactly the ones A acknowledged as stored."""
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(Path(tmp))
        a = env.node()
        stored: list[int] = []
        with a.up() as client:
            for seq in seqs:
                body = post(client, seq).json()
                if body["status"] == "stored":
                    stored.append(seq)
        path = env.directory / "r1.jsonl"
        before = path.read_bytes()
        assert sample_seqs(path) == stored
        with env.node().up() as client:
            replies = [post(client, s).json()["status"] for s in stored]
        assert replies == ["duplicate"] * len(stored)
        assert sample_seqs(path) == stored and path.read_bytes().startswith(before)


# --- Step 3: startup repair -------------------------------------------------------------------------------------


def build_base(env: Env) -> dict[str, bytes]:
    a = env.node()
    with a.up() as client:
        for seq in SEQS_A:
            assert post(client, seq).status_code == 200
        assert post(client, -1).status_code == 422  # a rejection record inside r1.jsonl
        assert client.post("/v1/samples", content=b"{").status_code == 422  # _rejections.jsonl
        assert post(client, 0, "r2").status_code == 200
    return snap(env.directory)


def real_repair_line(env: Env, fragment: bytes) -> bytes:
    scratch = env.tmp / "scratch"
    scratch.mkdir(exist_ok=True)
    path = scratch / "r1.jsonl"
    path.write_bytes(fragment)
    repair_file(path, EvidenceWriter(scratch, Durability.PROCESS), lambda: "2025-01-01T11:00:00.000Z")
    return path.read_bytes().split(b"\n")[-2]


def fragments(env: Env, base: dict[str, bytes]) -> dict[str, tuple[str, bytes]]:
    r1, rej = base["r1.jsonl"], base["_rejections.jsonl"]
    partial = line_of(r1, "sample")[:40]
    return {
        "incomplete_sample": ("r1", partial),
        "complete_sample_missing_newline": ("r1", line_of(r1, "sample")),
        "partial_gap": ("r1", line_of(r1, "gap")[:30]),
        "partial_rejection_in_run_file": ("r1", line_of(r1, "rejection")[:25]),
        "partial_rejection_in_rejections_file": ("_rejections", line_of(rej, "rejection")[:25]),
        "interrupted_repair_marker_only": ("r1", partial + b"\x00"),
        "interrupted_repair_partial_record": ("r1", partial + b"\x00\n" + real_repair_line(env, partial)[:30]),
    }


CASES = [
    "incomplete_sample", "complete_sample_missing_newline", "partial_gap", "partial_rejection_in_run_file",
    "partial_rejection_in_rejections_file", "interrupted_repair_marker_only", "interrupted_repair_partial_record",
]


@pytest.mark.parametrize("case", CASES)
def test_startup_repair_completes_before_the_first_request_and_keeps_every_byte(env: Env, case: str) -> None:
    base = build_base(env)
    target, fragment = fragments(env, base)[case]
    name = f"{target}.jsonl"
    damaged = base[name] + fragment
    (env.directory / name).write_bytes(damaged)
    path = env.directory / name
    b = env.node()
    with b.up() as client:
        now = snap(env.directory)  # no request has been made yet
        data = now[name]
        assert data.startswith(damaged) and data[len(damaged):].startswith(b"\x00\n")  # D30 marker, then the isolating newline
        repair = json.loads(data.split(b"\n")[-2])
        found = damaged.rsplit(b"\n", 1)[-1]
        expected_reason = "missing_trailing_newline" if case == "complete_sample_missing_newline" else "incomplete_last_line"
        assert (repair["record_type"], repair["file"], repair["action"], repair["reason"]) == ("repair", name, "appended_newline", expected_reason)
        assert repair["fragment_len"] == len(found) and base64.b64decode(repair["fragment_b64"]) == found
        assert repair["line_no"] == damaged.count(b"\n") + 1
        assert repair["run_id"] == (None if target == "_rejections" else target)
        assert validate_record(repair) == [] and data.count(b'"record_type":"repair"') == damaged.count(b'"record_type":"repair"') + 1
        for other, content in base.items():
            if other != name:
                assert now[other] == content  # unrelated files byte-identical
        result = read_evidence(path)
        isolated = next(e for e in result.errors if e.line_no == repair["line_no"])
        assert isolated.has_repair_record and isolated.kind == "isolated_by_repair"
        assert not [e for e in result.errors if e.kind == "incomplete_last_line"]
        assert_all_valid(path)
        if target == "r1":
            assert summarize(result).sample_count == len(SEQS_A)  # the fragment is never counted as a Sample (D30)
            assert post(client, 6).json()["status"] == "stored"
            assert sample_seqs(path)[-1] == 6 and len(sample_seqs(path)) == len(SEQS_A) + 1
        else:
            assert len(of_type(path, "rejection")) == 1
            assert client.post("/v1/samples", content=b"{").status_code == 422
            assert len(of_type(path, "rejection")) == 2
    assert path.read_bytes().startswith(data)


def test_a_clean_file_gets_no_repair_record_and_no_marker(env: Env) -> None:
    base = build_base(env)
    with env.node().up():
        assert snap(env.directory) == base
    assert all(b"\x00" not in data and b'"record_type":"repair"' not in data for data in base.values())


def test_only_the_damaged_file_among_several_run_files_is_repaired(env: Env) -> None:
    base = build_base(env)
    partial = line_of(base["r2.jsonl"], "sample")[:30]
    (env.directory / "r2.jsonl").write_bytes(base["r2.jsonl"] + partial)
    with env.node().up():
        now = snap(env.directory)
    assert {n for n in now if now[n] != base[n]} == {"r2.jsonl"}
    assert now["r2.jsonl"].startswith(base["r2.jsonl"] + partial + b"\x00\n")
    assert summarize(read_evidence(env.directory / "r2.jsonl")).sample_count == 1


# --- Step 4: resumed metadata ------------------------------------------------------------------------------------


def metadata(path: Path) -> list[dict[str, Any]]:
    return of_type(path, "run_metadata")


def test_passive_startup_of_a_clean_file_appends_no_metadata(env: Env) -> None:
    run_a(env)
    before = snap(env.directory)
    with env.node().up() as client:
        assert snap(env.directory) == before  # lifespan start (repair scan, startup probe)
        assert client.get("/healthz").status_code == 200
        assert client.get("/v1/runs/r1/evidence").status_code == 200
        assert snap(env.directory) == before
    assert snap(env.directory) == before and len(metadata(env.directory / "r1.jsonl")) == 1


def test_first_write_after_a_restart_appends_exactly_one_resumed_metadata_record(env: Env) -> None:
    run_a(env)
    path = env.directory / "r1.jsonl"
    b = env.node()
    with b.up() as client:
        assert post(client, 6).json()["status"] == "stored"
        assert post(client, 7).json()["status"] == "stored"  # same process: no further metadata
        assert terminate(client, "r1").status_code == 200
        assert post(client, 5).json()["status"] == "duplicate"
    created, resumed = metadata(path)
    assert created["event"] == "created" and resumed["event"] == "resumed"
    assert (resumed["run_id"], resumed["durability"], resumed["fsync_before_ack"], resumed["evidence_file"]) == ("r1", "crash", True, "r1.jsonl")
    assert resumed["recv_time"] > created["recv_time"] and validate_record(resumed) == []
    assert [r["event"] for r in metadata(path)] == ["created", "resumed"]


def test_a_new_run_in_b_gets_a_created_event_and_an_existing_one_never_does(env: Env) -> None:
    run_a(env)
    with env.node().up() as client:
        assert post(client, 0, "fresh").status_code == 200 and post(client, 6).status_code == 200
    assert [m["event"] for m in metadata(env.directory / "fresh.jsonl")] == ["created"]
    assert [m["event"] for m in metadata(env.directory / "r1.jsonl")] == ["created", "resumed"]


def test_every_restart_that_writes_adds_one_resumed_record(env: Env) -> None:
    run_a(env)
    for seq in (6, 7):
        with env.node().up() as client:
            post(client, seq)
    assert [m["event"] for m in metadata(env.directory / "r1.jsonl")] == ["created", "resumed", "resumed"]


def test_startup_repair_writes_a_repair_record_but_no_metadata_until_the_first_write(env: Env) -> None:
    run_a(env)
    path = env.directory / "r1.jsonl"
    path.write_bytes(path.read_bytes() + b'{"record_type":"sa')
    with env.node().up() as client:
        assert len(of_type(path, "repair")) == 1 and len(metadata(path)) == 1
        assert client.get("/v1/runs/r1/evidence").status_code == 200 and len(metadata(path)) == 1
        post(client, 6)
        assert [m["event"] for m in metadata(path)] == ["created", "resumed"]


def test_metadata_reports_the_durability_each_collector_was_configured_with(env: Env) -> None:
    a = env.node("process")
    with a.up() as client:
        post(client, 0)
    with env.node("crash").up() as client:
        post(client, 1)
    first, second = metadata(env.directory / "r1.jsonl")
    assert (first["durability"], first["fsync_before_ack"]) == ("process", False)
    assert (second["durability"], second["fsync_before_ack"]) == ("crash", True)
    assert summarize(read_evidence(env.directory / "r1.jsonl")).durability_levels == ["process", "crash"]


# --- Step 5: process versus crash --------------------------------------------------------------------------------


def test_process_durability_never_fsyncs_evidence_and_still_survives_a_restart(env: Env) -> None:
    a, acks = run_a(env, "process")
    assert a.ops.calls == 0 and a.dirs.calls == [] and {x["durability"] for x in acks} == {"process"}
    path = env.directory / "r1.jsonl"
    before = path.read_bytes()
    b = env.node("process")
    with b.up() as client:
        assert client.get("/healthz").json()["durability"] == "process"
        again = post(client, 5).json()
        assert (again["status"], again["durability"]) == ("duplicate", "process")
    assert b.ops.calls == 0 and b.dirs.calls == []  # no fsync at startup or on ingest (probe fsyncs are separate, D32)
    assert path.read_bytes().startswith(before)
    expect_samples(path, SEQS_A, acks)


def test_crash_durability_fsyncs_the_file_and_the_directory_before_acknowledging(env: Env) -> None:
    a = env.node("crash")
    with a.up() as client:
        startup_calls = a.ops.calls
        assert a.dirs.calls == [env.directory.name]  # startup synced the directory
        path = env.directory / "r1.jsonl"
        r = post(client, 0)
        assert r.json()["durability"] == "crash"
        # creation: metadata append (file fsync + directory fsync), then the Sample append (file fsync)
        assert a.ops.calls == startup_calls + 2 and a.dirs.calls == [env.directory.name, env.directory.name]
        assert a.ops.sizes[-1] == path.stat().st_size  # the Sample line was already in the file when it was fsynced
        assert post(client, 1).json()["durability"] == "crash"
        assert a.ops.calls == startup_calls + 3 and len(a.dirs.calls) == 2  # no directory fsync for an existing file
    with env.node("crash").up() as client:  # restart: the acknowledged Samples are still there, byte for byte
        assert client.get("/v1/runs/r1/evidence").content == path.read_bytes()
    assert sample_seqs(path) == [0, 1]


def test_crash_restart_fsyncs_every_existing_file_and_the_directory(env: Env) -> None:
    build_base(env)
    b = env.node("crash")
    with b.up():
        assert b.ops.calls == 3 and b.dirs.calls == [env.directory.name]  # r1, r2, _rejections
    assert env.locks.holder is None


# --- Step 6: dirty files and failed writes ----------------------------------------------------------------------


@pytest.fixture
def faults(monkeypatch: pytest.MonkeyPatch) -> Faults:
    return Faults(monkeypatch)


def test_partial_write_is_503_unacknowledged_and_repaired_before_the_next_append(env: Env, faults: Faults) -> None:
    a = env.node()
    path = env.directory / "r1.jsonl"
    with a.up() as client:
        assert post(client, 0).status_code == 200
        client.get("/healthz")
        opens, before = a.probe_opens, path.read_bytes()
        faults.arm("partial", "r1.jsonl")
        failed = post(client, 1)
        assert failed.status_code == 503 and failed.json() == STORAGE_FAILURE
        torn = path.read_bytes()
        assert torn.startswith(before) and len(torn) > len(before) and not torn.endswith(b"\n")
        assert sample_seqs(path) == [0]  # nothing was acknowledged and the fragment is not a Sample
        client.get("/healthz")
        assert a.probe_opens == opens + 1  # the failure invalidated the probe cache
        retry = post(client, 1)
        assert retry.status_code == 200 and retry.json()["status"] == "stored"
    data = path.read_bytes()
    assert data.startswith(torn + b"\x00\n")  # the fragment stays in the file for audit
    (repair,) = of_type(path, "repair")
    assert repair["reason"] == "incomplete_last_line" and base64.b64decode(repair["fragment_b64"]) == torn[len(before):]
    assert sample_seqs(path) == [0, 1] and summarize(read_evidence(path)).sample_count == 2 and len(read_evidence(path).errors) == 1


def test_a_write_that_fails_before_any_byte_leaves_nothing_to_repair(env: Env, faults: Faults) -> None:
    a = env.node()
    path = env.directory / "r1.jsonl"
    with a.up() as client:
        post(client, 0)
        before = path.read_bytes()
        faults.arm("write", "r1.jsonl")
        assert post(client, 1).status_code == 503 and path.read_bytes() == before
        assert post(client, 1).json()["status"] == "stored"
    assert of_type(path, "repair") == [] and sample_seqs(path) == [0, 1]


@pytest.mark.parametrize("mode", ["flush", "fsync"])
def test_a_complete_but_unacknowledged_sample_may_survive_and_the_retry_is_a_duplicate(env: Env, faults: Faults, mode: str) -> None:
    a = env.node()
    path = env.directory / "r1.jsonl"
    with a.up() as client:
        post(client, 0)
        if mode == "flush":
            faults.arm("flush", "r1.jsonl")
        else:
            a.ops.fail = True
        failed = post(client, 1)
        a.ops.fail = False
        assert failed.status_code == 503 and failed.json() == STORAGE_FAILURE  # never acknowledged
        assert sample_seqs(path) == [0, 1]  # but the complete line reached the file
        retry = post(client, 1)
        assert retry.status_code == 200 and retry.json()["status"] == "duplicate"
    assert sample_seqs(path) == [0, 1] and of_type(path, "repair") == []  # not stored twice, nothing to repair
    assert_all_valid(path)


def test_directory_fsync_failure_on_creation_is_503_then_recovers_and_syncs_the_directory(env: Env) -> None:
    a = env.node()
    path = env.directory / "n1.jsonl"
    with a.up() as client:
        syncs = len(a.dirs.calls)
        a.dirs.fail = True
        assert post(client, 0, "n1").status_code == 503
        assert path.is_file() and sample_seqs(path) == []  # created, never acknowledged
        a.dirs.fail = False
        ok = post(client, 0, "n1")
        assert ok.status_code == 200 and ok.json() == {**ok.json(), "status": "stored", "durability": "crash"}
        assert len(a.dirs.calls) > syncs  # the directory was synced on recovery
    assert [m["event"] for m in metadata(path)] == ["created", "resumed"] and sample_seqs(path) == [0]


def test_an_interrupted_repair_leaves_a_partial_record_that_the_next_repair_isolates(env: Env, faults: Faults) -> None:
    a = env.node()
    path = env.directory / "r1.jsonl"
    with a.up() as client:
        post(client, 0)
        before = path.read_bytes()
        faults.arm("partial", "r1.jsonl")
        assert post(client, 1).status_code == 503
        torn = path.read_bytes()
        faults.arm("partial", "r1.jsonl")  # the repair append itself is torn
        assert post(client, 1).status_code == 503
        interrupted = path.read_bytes()
        assert interrupted.startswith(torn + b"\x00\n") and not interrupted.endswith(b"\n")
        assert sample_seqs(path) == [0]
        assert post(client, 1).json()["status"] == "stored"
    data = path.read_bytes()
    assert data.startswith(interrupted + b"\x00\n") and data.startswith(before)
    assert len(of_type(path, "repair")) == 1 and sample_seqs(path) == [0, 1]
    assert summarize(read_evidence(path)).sample_count == 2 and len(read_evidence(path).errors) == 2
    assert_all_valid(path)


def test_a_failed_recovery_is_503_and_a_later_request_succeeds(env: Env, faults: Faults) -> None:
    a = env.node()
    path = env.directory / "r1.jsonl"
    with a.up() as client:
        post(client, 0)
        faults.arm("partial", "r1.jsonl")
        post(client, 1)
        torn = path.read_bytes()
        faults.arm("write", "r1.jsonl")  # recovery cannot write its repair record
        assert post(client, 1).status_code == 503 and path.read_bytes() == torn
        assert client.get("/healthz").status_code == 200
        assert post(client, 2, "other").status_code == 200  # another run is unaffected
        assert post(client, 1).json()["status"] == "stored"
    assert sample_seqs(path) == [0, 1] and len(of_type(path, "repair")) == 1


@pytest.mark.parametrize("mode", ["partial", "flush", "fsync"])
def test_a_failure_in_a_is_recovered_by_b_after_a_restart(env: Env, faults: Faults, mode: str) -> None:
    a = env.node()
    path = env.directory / "r1.jsonl"
    with a.up() as client:
        post(client, 0)
        before = path.read_bytes()
        if mode == "fsync":
            a.ops.fail = True
        else:
            faults.arm(mode, "r1.jsonl")
        assert post(client, 1).status_code == 503
    failed = path.read_bytes()
    b = env.node()
    with b.up() as client:
        after_start = path.read_bytes()
        retry = post(client, 1)
    assert retry.status_code == 200
    if mode == "partial":
        assert after_start.startswith(failed + b"\x00\n") and len(of_type(path, "repair")) == 1  # repaired at startup
        assert retry.json()["status"] == "stored"
    else:
        assert after_start == failed and of_type(path, "repair") == []  # a complete line needs no repair
        assert retry.json()["status"] == "duplicate"  # the unacknowledged Sample survived
    assert sample_seqs(path) == [0, 1] and path.read_bytes().startswith(before)
    assert summarize(read_evidence(path)).sample_count == 2


def test_gap_replay_is_idempotent_across_two_restarts(env: Env) -> None:
    run_a(env)
    path = env.directory / "r1.jsonl"
    lines = path.read_bytes().split(b"\n")
    kept = [ln for ln in lines if b'"record_type":"gap"' not in ln]
    path.write_bytes(b"\n".join(kept))  # the gap record is missing, as if its write never happened
    assert of_type(path, "gap") == []
    with env.node().up() as client:
        assert post(client, 5).json()["status"] == "duplicate"
    regenerated = of_type(path, "gap")
    assert [g["gap_id"] for g in regenerated] == ["gap-0001"] and validate_record(regenerated[0]) == []
    with env.node().up() as client:
        assert post(client, 5).json()["status"] == "duplicate"
    with env.node().up() as client:
        assert post(client, 5).json()["status"] == "duplicate"
    assert of_type(path, "gap") == regenerated  # same record, not rewritten, not repeated


# --- Step 7: termination and the watchdog after a restart ------------------------------------------------------


def run_with_termination_and_silence(env: Env) -> None:
    a = env.node()
    with a.up() as client:
        for seq in (0, 1):
            post(client, seq, "t1")
            post(client, seq, "s1")
        assert terminate(client, "t1").status_code == 200
        a.mono.t += 10.0
        assert a.check() == ["s1:gap-0001"]  # only the run that is not terminated goes silent


def test_a_termination_recorded_in_a_survives_and_b_reconstructs_it(env: Env) -> None:
    run_with_termination_and_silence(env)
    path = env.directory / "t1.jsonl"
    assert len(of_type(path, "termination")) == 1
    b = env.node()
    with b.up() as client:
        assert b.registry.get("t1").terminated is False  # nothing touched it yet
        assert client.get("/v1/runs/t1/evidence").status_code == 200
        assert post(client, 2, "t1").json()["status"] == "stored"  # first touch for writing: state is rebuilt
        state = b.registry.get("t1")
        assert state.terminated is True and state.loaded
        b.mono.t += 1000.0
        assert b.check() == []  # a late Sample after termination does not start silence monitoring
    assert len(of_type(path, "termination")) == 1 and of_type(path, "gap") == []


def test_historical_runs_are_not_monitored_and_restart_creates_no_gap(env: Env) -> None:
    run_with_termination_and_silence(env)
    before = snap(env.directory)
    b = env.node()
    with b.up() as client:
        client.get("/v1/runs/s1/evidence")
        client.get("/v1/runs/t1/evidence")
        assert all(s.last_sample_monotonic is None for s in b.registry.states())
        b.mono.t += 100000.0
        assert b.check() == [] and b.check() == []
    assert snap(env.directory) == before  # no synthetic silence gap


def test_a_silence_gap_recorded_in_a_is_not_duplicated_in_b(env: Env) -> None:
    run_with_termination_and_silence(env)
    path = env.directory / "s1.jsonl"
    (original,) = [g for g in of_type(path, "gap") if g["gap_type"] == "silence_timeout"]
    b = env.node()
    with b.up() as client:
        client.get("/v1/runs/s1/evidence")
        b.mono.t += 100.0
        assert b.check() == []
        assert b.registry.get("s1").silence_open is False  # not loaded yet: the file is only read on a write
        assert post(client, 2, "s1").json()["status"] == "stored"
        assert b.registry.get("s1").silence_open is False  # a new Sample ended the silence
        b.mono.t += 100.0
        assert b.check() == ["s1:gap-0002"]  # a new silence, a new id
    silences = [g for g in of_type(path, "gap") if g["gap_type"] == "silence_timeout"]
    assert silences[0] == original and [g["gap_id"] for g in silences] == ["gap-0001", "gap-0002"]


def test_a_loaded_run_that_received_no_new_sample_in_b_is_still_not_monitored(env: Env) -> None:
    run_a(env)
    before = snap(env.directory)
    b = env.node()
    with b.up() as client:
        assert post(client, 5).json()["status"] == "duplicate"  # loads the run (interval_ms known) but is no new Sample
        assert post(client, -1).status_code == 422  # a rejection loads it as well
        state = b.registry.get("r1")
        assert state.loaded and state.interval_ms == 500 and state.last_sample_monotonic is None
        b.mono.t += 100000.0
        assert b.check() == []
    assert [r["gap_type"] for r in of_type(env.directory / "r1.jsonl", "gap")] == ["sequence_discontinuity"]
    assert snap(env.directory)["r1.jsonl"].startswith(before["r1.jsonl"])


# --- Step 8: the Demo App is stopped, no Samples after the restart ---------------------------------------------


@pytest.mark.parametrize("durability", ["crash", "process"])
def test_a_restart_with_no_samples_leaves_every_record_untouched(env: Env, durability: str) -> None:
    run_a(env, durability)
    before = snap(env.directory)
    b = env.node(durability)
    with b.up() as client:
        health = client.get("/healthz")
        assert health.status_code == 200 and health.json()["storage_ok"] is True and health.json()["durability"] == durability
        b.mono.t += 1_000_000.0
        assert b.check() == []  # no false silence gap for a run that sent nothing in this process
        assert client.get("/v1/runs/r1/evidence").content == before["r1.jsonl"]
        assert (env.directory / ".coroner_probe").is_dir()  # the real probe ran in its own directory
    assert snap(env.directory) == before
    assert sample_seqs(env.directory / "r1.jsonl") == list(SEQS_A) and len(metadata(env.directory / "r1.jsonl")) == 1


# --- Step 9: several run files stay independent ----------------------------------------------------------------


def test_recovery_and_writes_to_one_run_never_modify_the_others(env: Env) -> None:
    a = env.node()
    with a.up() as client:
        for run_id in ("clean", "damaged", "done"):
            for seq in (0, 1):
                post(client, seq, run_id)
        terminate(client, "done")
    (env.directory / "damaged.jsonl").write_bytes((env.directory / "damaged.jsonl").read_bytes() + b'{"record_type":"sam')
    h0 = {n: sha(d) for n, d in snap(env.directory).items()}
    b = env.node()
    with b.up() as client:
        h1 = {n: sha(d) for n, d in snap(env.directory).items()}
        assert {n for n in h1 if h1[n] != h0[n]} == {"damaged.jsonl"}  # startup repair touched only the damaged file
        assert post(client, 2, "damaged").json()["status"] == "stored"
        h2 = {n: sha(d) for n, d in snap(env.directory).items()}
        assert {n for n in h2 if h2[n] != h1[n]} == {"damaged.jsonl"}
        assert post(client, 2, "clean").json()["status"] == "stored"
        h3 = {n: sha(d) for n, d in snap(env.directory).items()}
        assert {n for n in h3 if h3[n] != h2[n]} == {"clean.jsonl"}
        assert post(client, 2, "done").json()["status"] == "stored"
        h4 = {n: sha(d) for n, d in snap(env.directory).items()}
        assert {n for n in h4 if h4[n] != h3[n]} == {"done.jsonl"}
        states = {r: b.registry.get(r) for r in ("clean", "damaged", "done")}
    assert [states[r].terminated for r in ("clean", "damaged", "done")] == [False, False, True]
    assert [states[r].highest_seq for r in ("clean", "damaged", "done")] == [2, 2, 2]
    assert [len(of_type(env.directory / f"{r}.jsonl", "repair")) for r in ("clean", "damaged", "done")] == [0, 1, 0]
    assert [summarize(read_evidence(env.directory / f"{r}.jsonl")).sample_count for r in ("clean", "damaged", "done")] == [3, 3, 3]
    assert summarize(read_evidence(env.directory / "done.jsonl")).termination is not None
    assert summarize(read_evidence(env.directory / "clean.jsonl")).termination is None
