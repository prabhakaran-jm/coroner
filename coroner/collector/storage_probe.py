"""Storage probe: a real write, flush, fsync, directory fsync, read-back and compare (Requirements 4.4, 4.17, 4.18).

`storage_ok` in the Collector health body comes from here and from nowhere else. A probe result is never a
constant and never "the process is up".

Where it writes
    Only `<evidence_dir>/.coroner_probe/probe.tmp`. Both paths are built from `evidence_dir` and two fixed names,
    so nothing outside the reserved directory is ever opened for writing. The directory name starts with a dot, so
    it cannot collide with a run file and the `*.jsonl` scans never see it. The probe takes no run lock, does not
    use the `EvidenceWriter`, and never reads, modifies, truncates or deletes a run file, `_rejections.jsonl` or
    `.collector.lock`. A symlink at either reserved path fails the probe instead of being followed.

Steps (each failure names its step; the first failing step ends the probe)
    1. `mkdir`   create `.coroner_probe` when missing (the evidence directory itself must already exist).
    2. `open`    open `probe.tmp` for binary writing; this truncates only the probe's own file.
    3. `write`   write one unique token line; a short or partial write fails.
    4. `flush`   flush to the operating system.
    5. `fsync`   `fsync(file)` through the injected callable (the same callable the `EvidenceWriter` uses).
    6. `close`   close the write handle.
    7. `dir_fsync`  `fsync_dir(directory)` through the injected callable, for each directory whose entry was newly
       created by the probe: `.coroner_probe` is synced when `probe.tmp` was created, and the evidence directory
       is synced when `.coroner_probe` was created. A directory whose sync failed stays pending and is synced on
       the next probe even though it already exists. On Windows the default `fsync_directory` is the writer's
       documented no-op; the probe adds no separate bypass.
    8. `read`    open `probe.tmp` afresh (not the write handle) and read the whole file.
    9. `compare` the bytes read must equal the token exactly. A prefix, a suffix or extra bytes fail.

The probe always performs steps 5 and 7, whatever Durability_Level the Collector runs at (design D21, "Steps").
Passing means the volume accepted a write, a flush, an fsync call and a read-back of the same bytes. It does not
prove the data survives every host or operating system crash; fsync behaviour depends on the storage stack.

Result cache and concurrency
    `check()` returns the cached result when it is fresh: `now - checked_at < 1.0` seconds on an injectable
    monotonic clock (so at exactly 1.0 s the result is stale; a clock that moves backwards is also stale).
    `checked_at` is read when the probe starts, so a result is never treated as younger than its evidence.
    `invalidate()` drops the cache immediately; the ingest path calls it after a failed append.
    Real probes are serialised by one probe lock (not a run lock). A caller that arrives during an in-flight
    probe waits for it, then re-checks the cache: it shares that probe's result if it was cached, and runs its
    own probe if the cache was invalidated in the meantime. A generation counter is bumped by `invalidate()`; a
    probe that started before an invalidation returns its result to its own caller but does not store it, so a
    pre-invalidation result is never served as fresh afterwards. `check()` never raises.
"""
from __future__ import annotations

import io
import itertools
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from coroner.evidence.writer import fsync_directory

__all__ = [
    "CACHE_MAX_AGE_S",
    "PROBE_DIR_NAME",
    "PROBE_FILE_NAME",
    "ProbeResult",
    "StorageProbe",
]

PROBE_DIR_NAME = ".coroner_probe"
PROBE_FILE_NAME = "probe.tmp"
CACHE_MAX_AGE_S = 1.0
_ERROR_LIMIT = 200


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one probe. `ok` is true only when every step succeeded.

    `step` is the failing step (None when ok) and `error` a one-line reason of at most 200 characters
    (None when ok); the health body's `storage_error` is `error`. `checked_at` is the monotonic time at which
    the probe started.
    """

    ok: bool
    step: str | None
    error: str | None
    checked_at: float


class _StepError(Exception):
    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"{step}: {detail}")
        self.step = step
        self.detail = detail


def _short(text: str) -> str:
    one_line = " ".join(text.split())
    one_line = "".join(ch if ch.isprintable() else "?" for ch in one_line)  # control characters are not readable
    return one_line if len(one_line) <= _ERROR_LIMIT else one_line[: _ERROR_LIMIT - 3] + "..."


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _default_open(path: Path, mode: str) -> Any:
    return io.open(path, mode)


class StorageProbe:
    """See the module docstring. Thread-safe. Constructing it does no I/O."""

    def __init__(
        self,
        evidence_dir: Path,
        *,
        fsync: Callable[[int], None] = os.fsync,
        fsync_dir: Callable[[Path], None] = fsync_directory,
        clock: Callable[[], float] = time.monotonic,
        opener: Callable[[Path, str], Any] = _default_open,
        mkdir: Callable[[Path], None] = os.mkdir,
    ) -> None:
        self._evidence_dir = Path(evidence_dir)
        self._probe_dir = self._evidence_dir / PROBE_DIR_NAME
        self._probe_file = self._probe_dir / PROBE_FILE_NAME
        self._fsync = fsync
        self._fsync_dir = fsync_dir
        self._clock = clock
        self._opener = opener
        self._mkdir = mkdir
        self._probe_lock = threading.Lock()  # serialises real probes; never a run lock
        self._state_lock = threading.Lock()  # guards the cache and the generation only, never held during I/O
        self._cache: ProbeResult | None = None
        self._generation = 0
        self._counter = itertools.count(1)
        self._pending_dirs: list[Path] = []  # directories created by a probe whose fsync has not yet succeeded

    @property
    def probe_dir(self) -> Path:
        return self._probe_dir

    @property
    def probe_file(self) -> Path:
        return self._probe_file

    def check(self, *, force: bool = False) -> ProbeResult:
        """Return a fresh-enough result, probing when needed. `force=True` always runs a new probe."""
        if not force:
            cached = self._fresh_cache()
            if cached is not None:
                return cached
        with self._probe_lock:
            if not force:
                cached = self._fresh_cache()  # a probe that finished while this call waited
                if cached is not None:
                    return cached
            with self._state_lock:
                generation = self._generation
            result = self._probe()
            with self._state_lock:
                if generation == self._generation:
                    self._cache = result
            return result

    def run_startup(self) -> ProbeResult:
        """The forced probe the Collector runs once before it reports ready."""
        return self.check(force=True)

    def invalidate(self) -> None:
        """Drop the cached result now (call after a failed append). A probe already in flight is not cached."""
        with self._state_lock:
            self._generation += 1
            self._cache = None

    # --- internals ---------------------------------------------------------------------------

    def _fresh_cache(self) -> ProbeResult | None:
        with self._state_lock:
            cached = self._cache
        if cached is None:
            return None
        age = self._clock() - cached.checked_at
        return cached if 0.0 <= age < CACHE_MAX_AGE_S else None

    def _probe(self) -> ProbeResult:
        started = self._safe_clock()
        try:
            self._run_steps()
        except _StepError as exc:
            return ProbeResult(False, exc.step, _short(str(exc)), started)
        except Exception as exc:  # defensive: the probe never raises to its caller
            return ProbeResult(False, "unexpected", _short(f"unexpected: {_describe(exc)}"), started)
        return ProbeResult(True, None, None, started)

    def _safe_clock(self) -> float:
        try:
            return float(self._clock())
        except Exception:
            return float("-inf")  # never fresh

    def _run_steps(self) -> None:
        token = f"coroner-storage-probe {uuid.uuid4().hex} {next(self._counter)}\n".encode("ascii")
        self._ensure_probe_dir()
        file_created = not os.path.lexists(self._probe_file)
        if not file_created and os.path.islink(self._probe_file):
            raise _StepError("open", f"{PROBE_FILE_NAME} is a symlink; refusing to follow it")
        if file_created:
            self._add_pending(self._probe_dir)
        self._write_token(token)
        self._sync_pending_dirs()
        self._read_back(token)

    def _add_pending(self, directory: Path) -> None:
        if directory not in self._pending_dirs:
            self._pending_dirs.append(directory)

    def _ensure_probe_dir(self) -> None:
        directory = self._probe_dir
        try:
            if os.path.islink(directory):
                raise _StepError("mkdir", f"{PROBE_DIR_NAME} is a symlink; refusing to follow it")
            if not os.path.lexists(directory):
                self._mkdir(directory)
                self._add_pending(self._evidence_dir)  # the evidence directory gained an entry
            elif not os.path.isdir(directory):
                raise _StepError("mkdir", f"{PROBE_DIR_NAME} exists and is not a directory")
        except _StepError:
            raise
        except Exception as exc:
            raise _StepError("mkdir", _describe(exc)) from exc

    def _write_token(self, token: bytes) -> None:
        try:
            handle: BinaryIO = self._opener(self._probe_file, "wb")
        except Exception as exc:
            raise _StepError("open", _describe(exc)) from exc
        step_failed = False
        try:
            try:
                written = handle.write(token)
            except Exception as exc:
                raise _StepError("write", _describe(exc)) from exc
            if written != len(token):
                raise _StepError("write", f"short write: {written} of {len(token)} bytes")
            try:
                handle.flush()
            except Exception as exc:
                raise _StepError("flush", _describe(exc)) from exc
            try:
                self._fsync(handle.fileno())
            except Exception as exc:
                raise _StepError("fsync", _describe(exc)) from exc
        except BaseException:
            step_failed = True
            raise
        finally:
            try:
                handle.close()
            except Exception as exc:
                # A close error on an otherwise healthy path is a failure; do not mask an earlier step error.
                if not step_failed:
                    raise _StepError("close", _describe(exc)) from exc

    def _sync_pending_dirs(self) -> None:
        # Deepest first: the probe directory's entry, then the evidence directory's entry for the probe directory.
        for directory in sorted(self._pending_dirs, key=lambda p: len(p.parts), reverse=True):
            try:
                self._fsync_dir(directory)
            except Exception as exc:
                raise _StepError("dir_fsync", _describe(exc)) from exc
            self._pending_dirs.remove(directory)

    def _read_back(self, token: bytes) -> None:
        try:
            handle = self._opener(self._probe_file, "rb")
            try:
                data = handle.read()
            finally:
                handle.close()
        except Exception as exc:
            raise _StepError("read", _describe(exc)) from exc
        if not isinstance(data, (bytes, bytearray)) or bytes(data) != token:
            got = len(data) if isinstance(data, (bytes, bytearray)) else type(data).__name__
            raise _StepError("compare", f"read-back differs from the token (expected {len(token)} bytes, got {got})")
