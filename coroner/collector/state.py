"""Collector run state and registry (Task 3.2; Requirements 4.16, 5.2, 5.3, 5.5; design D26 and "Per-run locking and single instance").

Standard library plus `coroner.evidence` and `coroner.common`. No HTTP, no watchdog thread, no ingest or
gap classification: those are Tasks 3.3 to 3.9. This module owns three things.

1. `RunState`: the in-memory facts about one run, each run with its own `threading.Lock`.
2. `RunRegistry`: a registry-level lock that guards only the dict of states, lazy loading of run files, and
   the recovery gate (`open_run`) that every append must pass.
3. Startup: create and verify the evidence directory, take the single-Collector lock, repair and fsync every
   existing evidence file, then fsync the directory.

Recovery sequence (design D26), for a run file that exists and for every dirty run:
    tail repair (`repair_file`) -> fsync the file and the directory (Crash_Durable only)
    -> replay hook (Task 3.5 owns the real replay of persisted Samples) -> rebuild state from the file.
`dirty` is cleared only after all of that, and after the `run_metadata` write on a first open, succeeded.
No write may reach a run whose state is dirty or not loaded.

State is rebuilt from `read_evidence` records only (valid, non-quarantined), so quarantined, isolated and
malformed lines never count. Records present in the file count as persisted even if their request was never
acknowledged (design "ack-lost"): a retry of such a Sample is later classified as a duplicate.

`last_sample_monotonic` is never reconstructed from wall-clock time. After a reload it is None, which means
"no Sample was received during this process lifetime". The silence watchdog (Task 3.9) must skip a run whose
`last_sample_monotonic` is None, which is how the design avoids re-flagging old runs after each restart. Only
`RunState.apply` with a new Sample sets it. A rebuild inside a running process keeps the value it had.
"""
from __future__ import annotations

import errno
import os
import re
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from coroner.collector.config import CollectorConfig
from coroner.common.timeutil import format_utc_ms, utc_now_ms
from coroner.evidence.gaps import IntervalSet
from coroner.evidence.reader import ReadResult, read_evidence
from coroner.evidence.repair import repair_file
from coroner.evidence.schema import SCHEMA_VERSION, validate_run_id
from coroner.evidence.writer import (
    FILE_SUFFIX,
    REJECTIONS_STEM,
    AppendResult,
    Durability,
    EvidenceWriter,
    TailRepairRequired,
    fsync_directory,
)

__all__ = [
    "COLLECTOR_VERSION",
    "LOCK_FILE_NAME",
    "CollectorAlreadyRunning",
    "CollectorStartupError",
    "NewSample",
    "Outcome",
    "RunDirtyError",
    "RunRegistry",
    "RunSnapshot",
    "RunState",
    "StartupReport",
    "VolumeLock",
    "gap_id_number",
    "scan_records",
]

# The design's run_metadata example uses "0.1.0". pyproject.toml still says 0.0.0 and no version constant exists,
# so this module is the single source for the value written into run_metadata.
COLLECTOR_VERSION = "0.1.0"
LOCK_FILE_NAME = ".collector.lock"
_GAP_ID_RE = re.compile(r"gap-([0-9]+)")  # used with fullmatch; [0-9] is ASCII digits only

ReplayHook = Callable[[str, Path, ReadResult], None]


class CollectorStartupError(RuntimeError):
    """The Collector cannot start safely (storage not usable, directory fsync failed, volume owned by another)."""


class CollectorAlreadyRunning(CollectorStartupError):
    """Another Collector process holds `.collector.lock` on this evidence volume."""


class RunDirtyError(RuntimeError):
    """The run is dirty or not loaded: recovery (`open_run`) must complete before any write or state change."""


def gap_id_number(gap_id: object) -> int | None:
    """The number in a valid gap identifier (`gap-` followed by ASCII digits only), else None."""
    if not isinstance(gap_id, str):
        return None
    match = _GAP_ID_RE.fullmatch(gap_id)
    return int(match.group(1)) if match else None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class RunSnapshot:
    """Facts derived from a run file's valid records. `last_sample_monotonic` is deliberately absent."""

    highest_seq: int | None
    seen: IntervalSet
    last_sample_recv_time: str | None
    interval_ms: int | None
    next_gap_number: int
    silence_open: bool
    terminated: bool
    has_records: bool


def scan_records(records: Sequence[Mapping[str, Any]]) -> RunSnapshot:
    """Derive run state from valid records in file order (pure).

    * `sample` records only feed `highest_seq`, `seen`, `interval_ms` and `last_sample_recv_time` (the last one
      in file order). `sequence_anomaly` records never do.
    * `next_gap_number` is 1 plus the highest number in a valid gap record's `gap_id` (`gap-` plus ASCII digits).
      Other identifiers are ignored. Quarantined lines are not in `records`, so they are ignored too.
    * `terminated` is true iff a valid `termination` record exists.
    * `silence_open` is true iff a `silence_timeout` gap exists with no valid `sample` record after it.
    """
    highest: int | None = None
    seen = IntervalSet()
    recv_time: str | None = None
    interval: int | None = None
    highest_gap = 0
    silence_open = False
    terminated = False
    for record in records:
        kind = record.get("record_type")
        if kind == "sample":
            sample = record["sample"]
            seq = sample["seq"]
            seen.add(seq)
            highest = seq if highest is None else max(highest, seq)
            recv_time = record["recv_time"]
            interval = sample["interval_ms"]
            silence_open = False
        elif kind == "gap":
            number = gap_id_number(record.get("gap_id"))
            if number is not None:
                highest_gap = max(highest_gap, number)
            if record.get("gap_type") == "silence_timeout":
                silence_open = True
        elif kind == "termination":
            terminated = True
    return RunSnapshot(highest, seen, recv_time, interval, highest_gap + 1, silence_open, terminated, bool(records))


@dataclass(frozen=True)
class NewSample:
    """A Sample that was classified `new` and durably stored. `monotonic` is the Collector's monotonic receive time."""

    seq: int
    recv_time: str
    interval_ms: int
    monotonic: float


@dataclass(frozen=True)
class Outcome:
    """What a successful, durable write changed. `RunState.apply` takes this; it does no classification."""

    new_sample: NewSample | None = None
    gap_ids: tuple[str, ...] = ()  # gap ids that were written in the same durable append
    silence_opened: bool = False  # a silence_timeout gap was written (watchdog)
    terminated: bool = False  # a termination record was written


@dataclass(eq=False)
class RunState:
    """In-memory facts about one run. Callers hold `lock` while reading or changing anything else.

    `highest_seq` is None until a Sample is accepted. `loaded` is false until `RunRegistry.open_run` has rebuilt the
    state from the file. `dirty` means the file may end in a partial line or the last write may not be durable.
    """

    run_id: str
    highest_seq: int | None = None
    seen: IntervalSet = field(default_factory=IntervalSet)
    last_sample_recv_time: str | None = None
    last_sample_monotonic: float | None = None
    interval_ms: int | None = None
    next_gap_number: int = 1
    silence_open: bool = False
    terminated: bool = False
    dirty: bool = False
    loaded: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def mark_dirty(self) -> None:
        """Force the recovery path before the next append or state change. The caller holds `lock`."""
        self.dirty = True

    def apply(self, outcome: Outcome) -> None:
        """Advance state after a durable write. Call it only after the append returned normally.

        All inputs are checked before anything changes, so a bad `outcome` leaves the state untouched.
        Raises RunDirtyError if the run is dirty or not loaded, RuntimeError if `lock` is not held.
        """
        if not self.lock.locked():
            raise RuntimeError("apply requires the run lock")
        if self.dirty or not self.loaded:
            raise RunDirtyError(f"run {self.run_id!r} is dirty or not loaded; call open_run first")
        sample = outcome.new_sample
        if sample is not None:
            if not (_is_int(sample.seq) and sample.seq >= 0 and isinstance(sample.recv_time, str)
                    and _is_int(sample.interval_ms) and isinstance(sample.monotonic, (int, float))
                    and not isinstance(sample.monotonic, bool)):
                raise ValueError(f"invalid new sample outcome: {sample!r}")
            if outcome.silence_opened:
                raise ValueError("a silence gap cannot be opened by the same write that stored a new Sample")
        numbers = []
        for gap_id in outcome.gap_ids:
            number = gap_id_number(gap_id)
            if number is None:
                raise ValueError(f"invalid gap id {gap_id!r} (expected 'gap-' followed by ASCII digits)")
            numbers.append(number)
        if sample is not None:
            self.highest_seq = sample.seq if self.highest_seq is None else max(self.highest_seq, sample.seq)
            self.seen.add(sample.seq)
            self.last_sample_recv_time = sample.recv_time
            self.last_sample_monotonic = float(sample.monotonic)
            self.interval_ms = sample.interval_ms
            self.silence_open = False
        if numbers:
            self.next_gap_number = max(self.next_gap_number, max(numbers) + 1)
        if outcome.silence_opened:
            self.silence_open = True
        if outcome.terminated:
            self.terminated = True

    def _install(self, snapshot: RunSnapshot) -> None:
        self.highest_seq = snapshot.highest_seq
        self.seen = snapshot.seen
        self.last_sample_recv_time = snapshot.last_sample_recv_time
        self.interval_ms = snapshot.interval_ms
        # never reuse an identifier this process already handed out
        self.next_gap_number = max(self.next_gap_number, snapshot.next_gap_number) if self.loaded else snapshot.next_gap_number
        self.silence_open = snapshot.silence_open
        self.terminated = snapshot.terminated
        # last_sample_monotonic is kept as is: None after a first load, unchanged after an in-process rebuild


def _default_flock(fd: int) -> None:
    import fcntl  # POSIX only; imported here so this module imports on Windows

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


class VolumeLock:
    """Exclusive, non-blocking `flock` on `<evidence_dir>/.collector.lock` (design: one Collector per volume).

    POSIX only. On win32 `acquire` does nothing and `held` stays False: the design makes the lock a documented
    no-op there (the Collector always runs in a Linux container). The lock file is created if missing, is never
    deleted (removing it would let a second Collector lock a different inode), and is not an evidence file.
    `release` closes the descriptor, which drops the lock; it is idempotent. `flock` is injectable for tests.
    """

    def __init__(self, directory: Path, *, flock: Callable[[int], None] | None = None) -> None:
        self._path = Path(directory) / LOCK_FILE_NAME
        self._flock = flock if flock is not None else _default_flock
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if sys.platform == "win32" or self._fd is not None:
            return
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self._flock(fd)
        except OSError as exc:
            os.close(fd)
            if isinstance(exc, BlockingIOError) or exc.errno in (errno.EAGAIN, errno.EACCES, getattr(errno, "EWOULDBLOCK", errno.EAGAIN)):
                raise CollectorAlreadyRunning(
                    f"another Collector already holds {self._path}; only one Collector may use an evidence volume"
                ) from exc
            raise CollectorStartupError(f"cannot lock {self._path}: {exc}") from exc
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)


@dataclass(frozen=True)
class StartupReport:
    """What `RunRegistry.start` did. `failed` files are gated: their run (or `_rejections`) stays dirty."""

    created_directory: bool
    evidence_files: tuple[str, ...]  # run files and _rejections.jsonl that were examined, sorted
    repaired: tuple[str, ...]  # files for which a repair record was appended
    synced_files: tuple[str, ...]  # files explicitly fsynced (Crash_Durable only)
    synced_directory: bool
    skipped: tuple[tuple[str, str], ...]  # (name, reason) for *.jsonl names that are not evidence files
    failed: tuple[tuple[str, str], ...]  # (name, error) where repair or fsync failed
    lock_held: bool


class RunRegistry:
    """Per-run states, startup recovery and the recovery gate. See the module docstring.

    Usage (the caller of `open_run` holds the run lock, as in the design's ingest pseudocode)::

        registry = RunRegistry(config); registry.start()
        with registry.lock(run_id) as st:
            st = registry.open_run(run_id)     # same object as `st`; ensures the file, repairs, loads, writes run_metadata
            result = registry.append(st, records)
            st.apply(Outcome(...))

    Constructing a registry does no I/O. `fsync`, `fsync_dir`, `now` and the lock are injectable. The registry
    guard is held only while the dict is read or changed, never during file I/O.
    """

    def __init__(
        self,
        config: CollectorConfig,
        *,
        fsync: Callable[[int], None] = os.fsync,
        fsync_dir: Callable[[Path], None] = fsync_directory,
        now: Callable[[], str] | None = None,
        replay_hook: ReplayHook | None = None,
        volume_lock: VolumeLock | None = None,
    ) -> None:
        self._config = config
        self._fsync = fsync
        self._fsync_dir = fsync_dir
        self._now = now if now is not None else (lambda: format_utc_ms(utc_now_ms()))
        self._replay_hook: ReplayHook = replay_hook if replay_hook is not None else _no_replay
        self._writer = EvidenceWriter(config.evidence_dir, config.durability, fsync=fsync, fsync_dir=fsync_dir)
        self._volume_lock = volume_lock if volume_lock is not None else VolumeLock(config.evidence_dir)
        self._guard = threading.Lock()
        self._states: dict[str, RunState] = {}
        self._startup_dirty: set[str] = set()
        self._verified: set[Path] = set()  # files repaired and fsynced in this process (Crash_Durable)
        self._rejections_dirty = False
        self._started = False

    @property
    def writer(self) -> EvidenceWriter:
        return self._writer

    @property
    def config(self) -> CollectorConfig:
        return self._config

    @property
    def replay_hook(self) -> ReplayHook:
        """The recovery hook `open_run` calls (Task 3.5 installs `service.GapReplay` here)."""
        return self._replay_hook

    @property
    def started(self) -> bool:
        return self._started

    @property
    def rejections_dirty(self) -> bool:
        return self._rejections_dirty

    def __enter__(self) -> "RunRegistry":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- startup -----------------------------------------------------------------------------

    def start(self) -> StartupReport:
        """Verify storage, take the volume lock, repair and fsync every evidence file, fsync the directory.

        Order: create the directory if missing -> verify it is a writable directory -> take `.collector.lock`
        (POSIX) -> for each `*.jsonl` evidence file: `repair_file`, then under Crash_Durable an explicit file
        fsync (a clean last line proves nothing about an earlier fsync) -> under Crash_Durable fsync the
        directory (after every restart; a no-op on win32). Under Process_Durable no fsync is issued: the level
        promises only that data reached the operating system. Raises CollectorStartupError (and releases the
        lock) when the directory is unusable or its fsync fails. A file whose repair or fsync fails does not stop
        startup: its run stays dirty (or `rejections_dirty` is set) and is retried by `open_run`.
        Run once, before serving.
        """
        if self._started:
            raise RuntimeError("registry already started")
        directory = self._config.evidence_dir
        existed = directory.exists()
        if existed and not directory.is_dir():
            raise CollectorStartupError(f"evidence path {directory} is not a directory")
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CollectorStartupError(f"cannot create evidence directory {directory}: {exc}") from exc
        if not directory.is_dir():
            raise CollectorStartupError(f"evidence path {directory} is not a directory")
        self._check_writable(directory)
        self._volume_lock.acquire()
        try:
            report = self._recover_directory(directory, created=not existed)
        except BaseException:
            self._volume_lock.release()
            raise
        self._started = True
        return report

    def close(self) -> None:
        """Release the volume lock (never deletes the lock file or evidence) and close the writer. Idempotent."""
        self._volume_lock.release()
        self._writer.close()
        self._started = False

    @staticmethod
    def _check_writable(directory: Path) -> None:
        try:
            with tempfile.TemporaryFile(dir=directory) as probe:  # unnamed or auto-deleted: leaves nothing behind
                probe.write(b"x")
                probe.flush()
        except OSError as exc:
            raise CollectorStartupError(f"evidence directory {directory} is not writable: {exc}") from exc

    def _recover_directory(self, directory: Path, *, created: bool) -> StartupReport:
        crash = self._config.durability is Durability.CRASH
        examined: list[str] = []
        repaired: list[str] = []
        synced: list[str] = []
        skipped: list[tuple[str, str]] = []
        failed: list[tuple[str, str]] = []
        for entry in sorted(os.scandir(directory), key=lambda e: e.name):
            name = entry.name
            if not name.endswith(FILE_SUFFIX):
                continue  # .collector.lock, .coroner_probe/, anything else that is not *.jsonl
            if not entry.is_file(follow_symlinks=False):
                skipped.append((name, "not a regular file"))
                continue
            stem = name[: -len(FILE_SUFFIX)]
            if stem != REJECTIONS_STEM and validate_run_id(stem) is not None:
                skipped.append((name, "not an evidence file name"))
                continue
            examined.append(name)
            path = directory / name
            try:
                if repair_file(path, self._writer, self._now) is not None:
                    repaired.append(name)
                if crash:
                    self._fsync_file(path)
                    synced.append(name)
                self._verified.add(path)
            except OSError as exc:
                failed.append((name, f"{type(exc).__name__}: {exc}"))
                if stem == REJECTIONS_STEM:
                    self._rejections_dirty = True
                else:
                    self._startup_dirty.add(stem)
        synced_dir = False
        if crash:
            try:
                self._fsync_dir(directory)
            except OSError as exc:
                raise CollectorStartupError(f"cannot fsync evidence directory {directory}: {exc}") from exc
            synced_dir = True
        return StartupReport(
            created, tuple(examined), tuple(repaired), tuple(synced), synced_dir, tuple(skipped), tuple(failed), self._volume_lock.held
        )

    def _fsync_file(self, path: Path) -> None:
        with open(path, "ab") as handle:  # append mode opens for sync without changing any byte
            handle.flush()
            self._fsync(handle.fileno())

    # --- per-run access ----------------------------------------------------------------------

    def get(self, run_id: str) -> RunState:
        """The `RunState` for `run_id`, created unloaded on first use. Does not touch the file. ValueError for a bad id."""
        issue = validate_run_id(run_id)
        if issue is not None:
            raise ValueError(f"invalid run_id: {issue.reason}")
        with self._guard:
            state = self._states.get(run_id)
            if state is None:
                state = RunState(run_id, dirty=run_id in self._startup_dirty)
                self._states[run_id] = state
            return state

    @contextmanager
    def lock(self, run_id: str) -> Iterator[RunState]:
        """Hold the run's lock. Only this run is blocked; the registry guard is not held."""
        state = self.get(run_id)
        with state.lock:
            yield state

    def open_run(self, run_id: str) -> RunState:
        """Return a loaded, clean state, creating or recovering the run file first. The caller holds the run lock.

        New run (the file did not exist when `open_run` looked): create the file by writing `run_metadata`
        (`created`). Existing file, even an empty or malformed-only one: tail repair, fsync file and
        directory (Crash_Durable, unless already verified in this process and not dirty), replay hook, rebuild
        state, then `run_metadata` (`resumed`) on the first open in this process. If the first metadata write on a
        new file fails, the file may exist afterwards, so the retry writes `resumed`. A dirty run repeats the
        recovery (without another metadata record). Any failure leaves the run dirty and unloaded or unchanged
        and re-raises; dirty is cleared only at the very end.
        """
        self._require_started()
        state = self.get(run_id)
        if not state.lock.locked():
            raise RuntimeError("open_run requires the run lock; use `with registry.lock(run_id)`")
        if state.loaded and not state.dirty:
            return state
        path = self._writer.path_for(run_id)
        try:
            read: ReadResult | None = None
            # Decided before anything opens, creates or repairs the file. A file that exists is "resumed" even when
            # it is empty or holds no valid record. On a retry after a failed first metadata write the file exists,
            # so the retry also records "resumed".
            existed = path.exists()
            if existed:
                crash = self._config.durability is Durability.CRASH
                repair_file(path, self._writer, self._now)
                if crash and (state.dirty or path not in self._verified):
                    self._fsync_file(path)
                    self._fsync_dir(path.parent)
                size = path.stat().st_size
                read = read_evidence(path)
                self._replay_hook(run_id, path, read)  # Task 3.5 replaces the default no-op
                if path.stat().st_size != size:
                    read = read_evidence(path)
            snapshot = scan_records(read.records if read is not None else [])
            if not state.loaded:
                self._write_run_metadata(run_id, "resumed" if existed else "created")
            state._install(snapshot)
        except Exception:
            state.dirty = True
            raise
        self._verified.add(path)
        state.loaded = True
        state.dirty = False
        return state

    def append(self, state: RunState, records: Sequence[Mapping[str, Any]]) -> AppendResult:
        """Append through the writer for a clean, loaded run. The caller holds the run lock.

        An OSError or TailRepairRequired marks the run dirty and propagates; nothing is acknowledged and no state
        changes. Invalid records (RecordError, ValueError) wrote nothing and leave the run clean. The caller calls
        `state.apply` only after this returns.
        """
        if not state.lock.locked():
            raise RuntimeError("append requires the run lock")
        if state.dirty or not state.loaded:
            raise RunDirtyError(f"run {state.run_id!r} is dirty or not loaded; call open_run first")
        try:
            return self._writer.append(state.run_id, records)
        except (OSError, TailRepairRequired):
            state.mark_dirty()
            raise

    def ensure_rejections_ready(self) -> None:
        """Retry repair and fsync of `_rejections.jsonl` if startup or a later append left it dirty. Raises on failure."""
        self._require_started()
        if not self._rejections_dirty:
            return
        path = self._writer.path_for(REJECTIONS_STEM)
        if path.exists():
            repair_file(path, self._writer, self._now)
            if self._config.durability is Durability.CRASH:
                self._fsync_file(path)
                self._fsync_dir(path.parent)
        self._rejections_dirty = False

    def mark_rejections_dirty(self) -> None:
        self._rejections_dirty = True

    def _write_run_metadata(self, run_id: str, event: str) -> None:
        durability = self._writer.durability
        record = {
            "record_type": "run_metadata",
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "recv_time": self._now(),
            "event": event,
            "durability": durability.value,
            "fsync_before_ack": durability is Durability.CRASH,
            "evidence_file": f"{run_id}{FILE_SUFFIX}",
            "collector_version": COLLECTOR_VERSION,
        }
        self._writer.append(run_id, [record])

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("registry is not started; call start() before serving")


def _no_replay(run_id: str, path: Path, read: ReadResult) -> None:
    """Default replay hook: does nothing.

    TASK 3.5 OWNS THIS STEP. Design D26(4): after tail repair and before the state rebuild, 3.5 replaces this with
    a function that finds complete `sample` records whose gap records are missing and appends them (once each,
    never duplicating an existing gap). It runs under the run lock and may append through the registry's writer.
    Nothing is regenerated here.
    """
