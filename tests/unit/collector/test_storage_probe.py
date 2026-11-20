"""Task 3.3: StorageProbe (Requirements 4.4, 4.17, 4.18). Property 28 itself is Task 3.4."""
from __future__ import annotations

import hashlib
import io
import os
import threading
from pathlib import Path

import pytest

from coroner.collector.storage_probe import PROBE_DIR_NAME, PROBE_FILE_NAME, StorageProbe

SUCCESS_FRESH = [
    "mkdir", "open:wb", "write", "flush", "fsync", "close",
    "fsync_dir:.coroner_probe", "fsync_dir:evidence", "open:rb", "read", "close",
]
SUCCESS_EXISTING = ["open:wb", "write", "flush", "fsync", "close", "open:rb", "read", "close"]


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class Handle:
    def __init__(self, real, rec, fail: str | None) -> None:
        self.real, self.rec, self.fail = real, rec, fail

    def write(self, data):
        self.rec("write")
        if self.fail == "write":
            raise OSError("disk full")
        if self.fail == "partial":
            half = len(data) // 2
            self.real.write(data[:half])
            return half
        return self.real.write(data)

    def flush(self):
        self.rec("flush")
        if self.fail == "flush":
            raise OSError("flush failed")
        self.real.flush()

    def fileno(self):
        return self.real.fileno()

    def read(self):
        self.rec("read")
        if self.fail == "read":
            raise OSError("read failed")
        data = self.real.read()
        if self.fail == "mismatch":
            return data[:-2] + b"zz"
        if self.fail == "prefix":
            return data[:-1]
        if self.fail == "suffix":
            return data + b"x"
        return data

    def close(self):
        self.rec("close")
        self.real.close()


class Env:
    """Recording doubles around the real filesystem in tmp_path."""

    def __init__(self, root: Path, fail: dict[str, str] | None = None) -> None:
        self.root = root / "evidence"
        self.root.mkdir(exist_ok=True)
        self.events: list[str] = []
        self.fail = fail or {}
        self.write_paths: list[Path] = []
        self.clock = Clock()
        self.fsync_calls = 0
        self.probe = StorageProbe(
            self.root, fsync=self._fsync, fsync_dir=self._fsync_dir, clock=self.clock,
            opener=self._open, mkdir=self._mkdir,
        )

    def _name(self, p: Path) -> str:
        return "evidence" if p == self.root else p.name

    def _fsync(self, fd: int) -> None:
        self.events.append("fsync")
        self.fsync_calls += 1
        if self.fail.get("fsync"):
            raise OSError("fsync failed")
        os.fsync(fd)

    def _fsync_dir(self, d: Path) -> None:
        self.events.append(f"fsync_dir:{self._name(Path(d))}")
        if self.fail.get("dir_fsync"):
            raise OSError("dir fsync failed")

    def _mkdir(self, p: Path) -> None:
        self.events.append("mkdir")
        if self.fail.get("mkdir"):
            raise PermissionError("read-only volume")
        os.mkdir(p)

    def _open(self, p: Path, mode: str):
        self.events.append(f"open:{mode}")
        if mode != "rb":
            self.write_paths.append(Path(p))
        if self.fail.get("open") and mode == "wb":
            raise PermissionError("denied")
        real = io.open(p, mode)
        if mode == "wb":
            fail = next((k for k in ("write", "flush", "partial") if self.fail.get(k)), None)
        else:
            fail = "read" if self.fail.get("read") else None
        return Handle(real, self.events.append, fail)


def make(tmp_path: Path, **fail: str) -> Env:
    return Env(tmp_path, fail)


def test_success_sequence_on_first_probe(tmp_path: Path) -> None:
    env = make(tmp_path)
    result = env.probe.check()
    assert result.ok and result.step is None and result.error is None
    assert env.events == SUCCESS_FRESH
    assert (env.root / PROBE_DIR_NAME / PROBE_FILE_NAME).read_bytes().startswith(b"coroner-storage-probe ")


def test_existing_directory_and_file_need_no_dir_fsync(tmp_path: Path) -> None:
    env = make(tmp_path)
    assert env.probe.check().ok
    env.events.clear()
    assert env.probe.check(force=True).ok
    assert env.events == SUCCESS_EXISTING


def test_existing_directory_new_file_syncs_only_probe_dir(tmp_path: Path) -> None:
    env = make(tmp_path)
    (env.root / PROBE_DIR_NAME).mkdir()
    assert env.probe.check().ok
    assert env.events == ["open:wb", "write", "flush", "fsync", "close", "fsync_dir:.coroner_probe", "open:rb", "read", "close"]


@pytest.mark.parametrize(
    ("fail", "step", "events"),
    [
        ({"mkdir": "1"}, "mkdir", ["mkdir"]),
        ({"open": "1"}, "open", ["mkdir", "open:wb"]),
        ({"write": "1"}, "write", ["mkdir", "open:wb", "write", "close"]),
        ({"partial": "1"}, "write", ["mkdir", "open:wb", "write", "close"]),
        ({"flush": "1"}, "flush", ["mkdir", "open:wb", "write", "flush", "close"]),
        ({"fsync": "1"}, "fsync", ["mkdir", "open:wb", "write", "flush", "fsync", "close"]),
        ({"dir_fsync": "1"}, "dir_fsync", ["mkdir", "open:wb", "write", "flush", "fsync", "close", "fsync_dir:.coroner_probe"]),
        ({"read": "1"}, "read", SUCCESS_FRESH[:-2] + ["read", "close"]),
    ],
)
def test_each_failing_step_gives_not_ok_with_recorded_events(tmp_path: Path, fail, step, events) -> None:
    env = make(tmp_path, **fail)
    result = env.probe.check()
    assert not result.ok
    assert result.step == step
    assert result.error and "\n" not in result.error and len(result.error) <= 200
    assert env.events == events


@pytest.mark.parametrize("mode", ["mismatch", "prefix", "suffix"])
def test_compare_requires_exact_bytes(tmp_path: Path, mode: str) -> None:
    env = make(tmp_path)
    original = env._open

    def tampering(p, m):
        h = original(p, m)
        if m == "rb":
            h.fail = mode
        return h

    env.probe._opener = tampering
    result = env.probe.check()
    assert not result.ok and result.step == "compare"


def test_failed_dir_fsync_is_retried_even_though_directories_exist(tmp_path: Path) -> None:
    env = make(tmp_path, dir_fsync="1")
    assert not env.probe.check().ok
    env.fail.clear()
    env.events.clear()
    result = env.probe.check(force=True)
    assert result.ok
    assert env.events == [
        "open:wb", "write", "flush", "fsync", "close", "fsync_dir:.coroner_probe", "fsync_dir:evidence", "open:rb", "read", "close",
    ]


def test_recovery_after_failure(tmp_path: Path) -> None:
    env = make(tmp_path, fsync="1")
    assert not env.probe.check().ok
    env.fail.clear()
    assert env.probe.check(force=True).ok


def test_win32_style_noop_dir_fsync_still_succeeds(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    synced: list[Path] = []
    probe = StorageProbe(root, fsync=lambda fd: None, fsync_dir=lambda d: synced.append(Path(d)))
    assert probe.check().ok
    assert synced == [root / PROBE_DIR_NAME, root]


def test_missing_evidence_dir_fails_without_raising(tmp_path: Path) -> None:
    probe = StorageProbe(tmp_path / "nope")
    result = probe.check()
    assert not result.ok and result.step == "mkdir"
    assert not (tmp_path / "nope").exists()


def test_symlink_probe_dir_is_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, root / PROBE_DIR_NAME, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    result = StorageProbe(root).check()
    assert not result.ok
    assert list(outside.iterdir()) == []


def test_probe_dir_that_is_a_file_fails(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    (root / PROBE_DIR_NAME).write_bytes(b"x")
    assert not StorageProbe(root).check().ok


# --- cache -------------------------------------------------------------------------------------


def test_cache_hit_within_one_second_does_no_io(tmp_path: Path) -> None:
    env = make(tmp_path)
    first = env.probe.check()
    env.events.clear()
    env.clock.now += 0.999
    assert env.probe.check() is first
    assert env.events == []


@pytest.mark.parametrize("delta", [1.0, 1.001, 5.0])
def test_cache_expires_at_one_second(tmp_path: Path, delta: float) -> None:
    env = make(tmp_path)
    first = env.probe.check()
    env.events.clear()
    env.clock.now += delta
    second = env.probe.check()
    assert second is not first and env.events == SUCCESS_EXISTING


def test_clock_going_backwards_is_not_fresh(tmp_path: Path) -> None:
    env = make(tmp_path)
    env.probe.check()
    env.events.clear()
    env.clock.now -= 1.0
    env.probe.check()
    assert env.events


def test_failed_result_is_cached_too(tmp_path: Path) -> None:
    env = make(tmp_path, fsync="1")
    first = env.probe.check()
    env.fail.clear()
    assert env.probe.check() is first and not first.ok


def test_force_reprobes_and_startup_is_forced(tmp_path: Path) -> None:
    env = make(tmp_path)
    env.probe.check()
    env.fsync_calls = 0
    assert env.probe.check(force=True).ok
    assert env.probe.run_startup().ok
    assert env.fsync_calls == 2


def test_invalidate_makes_next_call_probe(tmp_path: Path) -> None:
    env = make(tmp_path)
    env.probe.check()
    env.fsync_calls = 0
    env.probe.invalidate()
    env.probe.check()
    assert env.fsync_calls == 1


def test_invalidate_during_inflight_probe_is_not_cached(tmp_path: Path) -> None:
    env = make(tmp_path)
    inside, release = threading.Event(), threading.Event()
    base = env._fsync

    def slow_fsync(fd: int) -> None:
        base(fd)
        inside.set()
        assert release.wait(5)

    env.probe._fsync = slow_fsync
    box: list = []
    t = threading.Thread(target=lambda: box.append(env.probe.check()))
    t.start()
    assert inside.wait(5)
    env.probe.invalidate()
    release.set()
    t.join(5)
    assert box and box[0].ok
    env.probe._fsync = base
    env.fsync_calls = 0
    env.probe.check()
    assert env.fsync_calls == 1  # the pre-invalidation result was not served as fresh


# --- isolation ---------------------------------------------------------------------------------


def _digest(p: Path) -> tuple[str, int]:
    return hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns


def test_run_files_are_never_touched(tmp_path: Path) -> None:
    env = make(tmp_path)
    names = ["run-a.jsonl", "run-b.jsonl", "_rejections.jsonl", ".collector.lock"]
    for i, n in enumerate(names):
        (env.root / n).write_bytes(b'{"k":%d}\n' % i)
    before = {n: _digest(env.root / n) for n in names}
    for _ in range(3):
        assert env.probe.check(force=True).ok
    assert {n: _digest(env.root / n) for n in names} == before
    reserved = env.root / PROBE_DIR_NAME
    assert env.write_paths and all(p == reserved / PROBE_FILE_NAME for p in env.write_paths)
    assert sorted(x.name for x in env.root.iterdir()) == sorted(names + [PROBE_DIR_NAME])
    assert [x.name for x in reserved.iterdir()] == [PROBE_FILE_NAME]


# --- concurrency -------------------------------------------------------------------------------


def test_concurrent_checks_do_not_interleave(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    active = 0
    peak = 0
    guard = threading.Lock()

    def fsync(fd: int) -> None:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        os.fsync(fd)
        with guard:
            active -= 1

    probe = StorageProbe(root, fsync=fsync)
    results: list = []
    errors: list = []

    def worker() -> None:
        try:
            for i in range(10):
                results.append(probe.check(force=i % 2 == 0))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors and len(results) == 80
    assert all(r.ok for r in results)
    assert peak == 1  # real probes are serialised, so token files are never interleaved


# --- real filesystem smoke ---------------------------------------------------------------------


def test_real_filesystem_smoke(tmp_path: Path) -> None:
    probe = StorageProbe(tmp_path)
    result = probe.check()
    assert result.ok, result
    assert (tmp_path / PROBE_DIR_NAME / PROBE_FILE_NAME).is_file()
    assert probe.check(force=True).ok
