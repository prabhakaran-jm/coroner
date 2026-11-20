"""Silence-timeout watchdog (Task 3.9; Requirements 6.2, 6.13; design "Silence-timeout watchdog", D29 to D33).

`SilenceWatchdog` is a daemon thread (`start()`/`stop()`) around a synchronous `check_once()` that tests call
directly. Nothing here sleeps except the thread loop, which waits on an Event with the tick as timeout.

Which runs. Only runs whose `RunState.last_sample_monotonic` is not None: `RunState.apply` sets it only for a Sample
accepted in this process, so runs that exist only on disk (after a restart) are never flagged. Elapsed time is
`monotonic() - last_sample_monotonic` using the same monotonic clock the service stamped Samples with. Wall-clock
times are used only for the record's fields and are never turned back into a monotonic value.

Detection. Silence is `elapsed_ms > 3 * interval_ms` (strict; `interval_ms` is the last accepted Sample's). A cheap
unlocked pre-check selects candidates. The run's lock is then taken and every condition is recomputed from the
current state (a Sample that arrived while waiting for the lock resets the clock): last Sample seen, not terminated,
no silence gap open, threshold exceeded.

Recovery decision. A dirty or not-loaded run (a failed earlier append, possibly leaving a partial line) is
recovered from the watchdog thread through the registry's own `open_run` (tail repair, fsync, replay, state rebuild),
under the run lock, before the retry; this is the "approved recovery path" and the design says a failed write is
retried on the next tick. Conditions are checked again after recovery: if the rebuilt state shows the gap record
was in fact durable (`silence_open`), nothing more is written, so a logical silence record is never duplicated.

Gap record (design "Gap_Record" example): `recv_time`, `end_time` and `detected_time` are the wall-clock time of
detection; `start_time` and `last_sample_time` are the last accepted Sample's `recv_time`; `threshold_ms` is
`3 * interval_ms`; `interval_ms` is the last Sample's; `sample_count` is exactly "unknown" and no sequence range is
written; `reconciliation` is `not_applicable` with no `reconciles_with`. `gap_id` comes from `next_gap_number`.

Failure. OSError or TailRepairRequired (append, or recovery): the registry has marked the run dirty, `silence_open`
stays false, nothing is acknowledged, `on_storage_failure` is called (the service's probe invalidation) and the
loop carries on with the other runs and the next tick. Any other exception for one run is logged and does not
stop the thread.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from coroner.collector.state import Outcome, RunRegistry, RunState
from coroner.common.timeutil import format_utc_ms, utc_now_ms
from coroner.evidence.schema import SCHEMA_VERSION, validate_record
from coroner.evidence.writer import TailRepairRequired

if TYPE_CHECKING:
    from coroner.collector.service import IngestService

__all__ = ["LOGGER_NAME", "SILENCE_FACTOR", "SilenceWatchdog", "create_watchdog"]

LOGGER_NAME = "coroner.collector.watchdog"
logger = logging.getLogger(LOGGER_NAME)

SILENCE_FACTOR = 3
_STOP_JOIN_SECONDS = 5.0


def _default_now() -> str:
    return format_utc_ms(utc_now_ms())


class SilenceWatchdog:
    def __init__(
        self,
        registry: RunRegistry,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], str] | None = None,
        tick_ms: float | None = None,
        on_storage_failure: Callable[[], None] | None = None,
    ) -> None:
        self._registry = registry
        self._monotonic = monotonic
        self._now = now if now is not None else _default_now
        tick = registry.config.watchdog_tick_ms if tick_ms is None else tick_ms
        if not tick > 0:
            raise ValueError("tick_ms must be positive")
        self._tick_s = tick / 1000.0
        self._on_failure = on_storage_failure
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._control = threading.Lock()

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    # --- thread ------------------------------------------------------------------------------------------

    def start(self) -> None:
        """Start the daemon thread. A second call while it runs does nothing."""
        with self._control:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop = threading.Event()
            thread = threading.Thread(target=self._loop, args=(self._stop,), name="coroner-silence-watchdog", daemon=True)
            self._thread = thread
            thread.start()

    def stop(self, timeout: float = _STOP_JOIN_SECONDS) -> None:
        """Ask the thread to stop and join it for at most `timeout` seconds. Idempotent."""
        with self._control:
            thread = self._thread
            self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.check_once()
            except Exception:  # never let one bad pass end the thread
                logger.exception("silence watchdog pass failed")
            if stop.wait(self._tick_s):
                break

    # --- one pass ----------------------------------------------------------------------------------------

    def check_once(self) -> list[str]:
        """One synchronous pass over every known run. Returns the gaps written, each as `run_id:gap_id`."""
        written: list[str] = []
        for state in self._registry.states():  # a copy: the registry guard is not held below
            if not self._candidate(state):
                continue
            try:
                gap_id = self._check_run(state)
            except (OSError, TailRepairRequired) as exc:
                self._storage_failed(state.run_id, exc)
                continue
            except Exception:
                logger.exception("silence watchdog failed for run_id=%s", state.run_id)
                continue
            if gap_id is not None:
                written.append(f"{state.run_id}:{gap_id}")
        return written

    def _silent(self, state: RunState) -> bool:
        """The shared condition. Reads state fields; the caller holds the run lock for the authoritative call."""
        last, interval = state.last_sample_monotonic, state.interval_ms
        if last is None or interval is None or state.terminated or state.silence_open:
            return False
        return (self._monotonic() - last) * 1000.0 > SILENCE_FACTOR * interval

    def _candidate(self, state: RunState) -> bool:
        return self._silent(state)  # unlocked pre-check only; `_check_run` decides

    def _check_run(self, state: RunState) -> str | None:
        registry = self._registry
        with state.lock:
            if not self._silent(state):
                return None  # a Sample, a termination or another detection got here first
            if state.dirty or not state.loaded:
                registry.open_run(state.run_id)  # recovery; may rebuild state from the file
                if not self._silent(state):
                    return None
            interval = state.interval_ms
            last_time = state.last_sample_recv_time
            if interval is None or last_time is None:
                return None
            detected = self._now()
            gap_id = f"gap-{state.next_gap_number:04d}"
            record: dict[str, Any] = {
                "record_type": "gap",
                "schema_version": SCHEMA_VERSION,
                "run_id": state.run_id,
                "recv_time": detected,
                "gap_id": gap_id,
                "gap_type": "silence_timeout",
                "start_time": last_time,
                "end_time": detected,
                "last_sample_time": last_time,
                "detected_time": detected,
                "threshold_ms": SILENCE_FACTOR * interval,
                "interval_ms": interval,
                "sample_count": "unknown",
                "reconciliation": "not_applicable",
                "reconciles_with": [],
            }
            issues = validate_record(record)
            if issues:
                detail = "; ".join(f"{i.field}: {i.reason_code}" for i in issues[:5])
                raise ValueError(f"generated silence_timeout record is invalid ({detail})")
            registry.append(state, [record])  # marks the run dirty on OSError/TailRepairRequired
            state.apply(Outcome(silence_opened=True, gap_ids=(gap_id,)))  # only after the durable append
            logger.info("silence_timeout run_id=%s gap_id=%s threshold_ms=%d", state.run_id, gap_id, SILENCE_FACTOR * interval)
            return gap_id

    def _storage_failed(self, run_id: str, exc: BaseException) -> None:
        logger.warning("silence gap not recorded run_id=%s error=%s: %s (retry on a later tick)", run_id, type(exc).__name__, exc)
        if self._on_failure is not None:
            try:
                self._on_failure()
            except Exception:
                logger.exception("storage failure callback failed")


def create_watchdog(service: "IngestService", *, tick_ms: float | None = None) -> SilenceWatchdog:
    """A watchdog that shares the service's registry, monotonic clock, wall clock and probe invalidation."""
    return SilenceWatchdog(
        service.registry, monotonic=service.monotonic, now=service.now, tick_ms=tick_ms, on_storage_failure=service.invalidate_storage
    )
