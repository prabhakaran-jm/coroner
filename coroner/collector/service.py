"""Collector ingest service (Task 3.5; Requirements 4.2, 4.3, 4.5, 4.7, 4.8, 4.9, 4.15, 6.1, 6.3, 6.4, 7.5, 7.7; design D26, D29, D31).

No HTTP and no watchdog: `IngestService.ingest(raw_body)` and `IngestService.record_termination(run_id, raw_body)`
return a status code and a JSON-ready body, and a later task (3.11) turns them into routes. Everything about
schemas, sequence classification and gap arithmetic comes from `coroner.evidence`; this module only orders the steps.

Ordering of `ingest` (design "Append path and acknowledge-after-persist ordering")
    1. raw size against `config.max_body_bytes`            -> 413 `body_too_large`
    2. strict JSON parse (NaN, Infinity and duplicate keys refused, D33) -> 422 `invalid_json`
    3. `validate_sample`; the first issue decides           -> 422 with that issue's reason_code
    4. the per-run lock (`registry.lock`)
    5. `open_run` (recover or create, run the replay hook, load state, write run_metadata)
    6. `classify` against the run's SeqState
    7. `build_records`: the Sample or `sequence_anomaly` first, then the discontinuity gap, then the drop gap;
       reconciliation is marked here, before the write
    8. `validate_record` on every generated record (a failure is a programming error: nothing is written, 500)
    9. ONE `registry.append` (one write, flush, fsync at the configured level)
   10. `RunState.apply` (only after the append returned)
   11. acknowledge, with the durability the writer reports for that append
`OSError` or `TailRepairRequired` at step 5 or 9 marks the run dirty (the registry does it), invalidates the storage
probe and returns 503 `storage_failure`; nothing is acknowledged and the in-memory state is unchanged.

Rejections (422, 413) are persisted before the response is built: in the run file when the body names a valid
`run_id` (through `open_run`, so a dirty run is recovered first), otherwise in `_rejections.jsonl` with a null
run_id. A failed rejection write leaves the status as it was and adds `rejection_recorded: false` to the body.

recv_time is the Receive_Time: `self._now()` is read once when a request starts (design, "IngestService.ingest"), before
the size check, validation and any lock, and that value is stored and returned. It is never replaced by the time the
lock is acquired or the record is appended, so recv_time need not increase in file order. `ingest_ms` is measured
from request start with `monotonic`, so it includes any wait for the lock.

Gap times (design, "Gap_Record" examples). A live gap's `end_time` and `recv_time` are the receive time of the
Sample that detected it; `start_time` is the receive time of the previous accepted Sample, or the detecting
Sample's receive time when there is none. Replay uses the same `start_time` and `end_time`, taken from the
persisted Sample records, so a regenerated gap describes the same interval a live gap would have. A regenerated gap's `recv_time` is
the time recovery detected the missing record (the envelope field is "receipt, detection, or repair"); it is not
back-dated to the original ingest, because no Collector observed it then.

Replay (`GapReplay`, design D26 item 4). After tail repair and before the state rebuild, `open_run` calls the hook
with the valid records. The hook replays the `sample` records in file order through `classify` and
`SeqState.apply`, computes the sequence discontinuity and Probe-reported drop gaps each Sample must have caused,
and compares them with the stored gap records by semantic identity (never `gap_id`, never times). Missing gaps are
appended in one durable append with new ids from `next_gap_number`. A stored gap with the same `gap_type` and
`detected_at_seq` but other fields is a conflict: it is reported (`ReplayReport.conflicts`, a warning log line)
and nothing is added or overwritten. Silence gaps and anomalies are never regenerated or replayed. Replaying twice
adds nothing the second time.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from coroner.collector.config import CollectorConfig
from coroner.collector.state import NewSample, Outcome, RunRegistry, VolumeLock, scan_records
from coroner.collector.storage_probe import StorageProbe
from coroner.common.timeutil import format_utc_ms, utc_now_ms
from coroner.evidence.gaps import SeqEvents, SeqRange, SeqState, classify, reconcile
from coroner.evidence.reader import ReadResult
from coroner.evidence.schema import (
    BODY_PREVIEW_MAX,
    SCHEMA_VERSION,
    validate_record,
    validate_run_id,
    validate_sample,
)
from coroner.evidence.writer import (
    REJECTIONS_STEM,
    RecordError,
    TailRepairRequired,
    fsync_directory,
)

__all__ = [
    "GapReplay",
    "GeneratedRecordError",
    "IngestResult",
    "IngestService",
    "LOGGER_NAME",
    "ReplayConflict",
    "ReplayReport",
    "TerminationResult",
    "build_records",
    "create_ingest_service",
]

LOGGER_NAME = "coroner.collector.service"
logger = logging.getLogger(LOGGER_NAME)

_DISCONTINUITY = "sequence_discontinuity"
_DROP = "probe_reported_drop"
_ENVELOPE_KEYS = ("record_type", "schema_version", "run_id", "recv_time")
_REASON_MAX = 200


class GeneratedRecordError(RuntimeError):
    """A record the Collector generated failed `validate_record`. A programming error; nothing was written."""

    def __init__(self, message: str, issues: Sequence[object] = ()) -> None:
        super().__init__(message)
        self.issues = tuple(issues)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _default_now() -> str:
    return format_utc_ms(utc_now_ms())


def _gap_id(number: int) -> str:
    return f"gap-{number:04d}"


def _check_generated(records: Sequence[Mapping[str, Any]]) -> None:
    for index, record in enumerate(records):
        issues = validate_record(record)
        if issues:
            detail = "; ".join(f"{i.field}: {i.reason_code}" for i in issues[:5])
            raise GeneratedRecordError(f"generated record {index} ({record.get('record_type')!r}) is invalid ({detail})", issues)


# --- gaps ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _GapSpec:
    """What one gap must say, without ids, times or reconciliation. Its `identity` is the D29 semantic identity."""

    gap_type: str
    detected_at_seq: int
    first: int | None
    last: int | None
    dropped_count: int | None = None
    range_status: str | None = None
    range_note: str | None = None

    @property
    def range(self) -> SeqRange | None:
        return None if self.first is None or self.last is None else (self.first, self.last)

    @property
    def identity(self) -> tuple[Any, ...]:
        if self.gap_type == _DISCONTINUITY:
            return (self.gap_type, self.detected_at_seq, self.first, self.last)
        return (self.gap_type, self.detected_at_seq, self.dropped_count, self.range_status, self.first, self.last)


def _specs_for(events: SeqEvents) -> list[_GapSpec]:
    """The gaps a `new` Sample causes, in batch order: discontinuity first, then the drop."""
    specs: list[_GapSpec] = []
    if events.discontinuity is not None:
        first, last = events.discontinuity
        specs.append(_GapSpec(_DISCONTINUITY, events.seq, first, last))
    if events.drop is not None:
        drop = events.drop
        first, last = drop.range if drop.range is not None else (None, None)
        specs.append(_GapSpec(_DROP, events.seq, first, last, drop.dropped_count, drop.range_status, drop.range_note))
    return specs


def _identity_of_record(gap: Mapping[str, Any]) -> tuple[Any, ...]:
    gap_type = gap.get("gap_type")
    first, last = gap.get("first_missing_seq"), gap.get("last_missing_seq")
    if gap_type == _DISCONTINUITY:
        return (gap_type, gap.get("detected_at_seq"), first, last)
    return (gap_type, gap.get("detected_at_seq"), gap.get("dropped_count"), gap.get("range_status"), first, last)


def _stored_range(gap: Mapping[str, Any]) -> SeqRange | None:
    first, last = gap.get("first_missing_seq"), gap.get("last_missing_seq")
    return (first, last) if _is_int(first) and _is_int(last) else None


def _gap_record(
    run_id: str, spec: _GapSpec, gap_id: str, recv_time: str, start: str, end: str, reconciliation: str, reconciles_with: list[str]
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "record_type": "gap",
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "recv_time": recv_time,
        "gap_id": gap_id,
        "gap_type": spec.gap_type,
        "start_time": start,
        "end_time": end,
    }
    if spec.gap_type == _DISCONTINUITY:
        assert spec.first is not None and spec.last is not None
        record.update(first_missing_seq=spec.first, last_missing_seq=spec.last, missing_count=spec.last - spec.first + 1)
    else:
        record.update(
            dropped_count=spec.dropped_count, range_status=spec.range_status,
            first_missing_seq=spec.first, last_missing_seq=spec.last,
        )
        if spec.range_note is not None:
            record["range_note"] = spec.range_note
    record.update(reconciliation=reconciliation, reconciles_with=list(reconciles_with), detected_at_seq=spec.detected_at_seq)
    return record


def build_records(
    run_id: str,
    sample: Mapping[str, Any],
    recv_time: str,
    events: SeqEvents,
    previous_recv_time: str | None,
    next_gap_number: int,
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """The records one ingest writes, in order, and the gap ids among them. Pure; nothing is validated here.

    `new`: the `sample` record, then the discontinuity gap, then the drop gap (ids from `next_gap_number`, two
    gaps of one ingest are marked against each other here, before the write). `duplicate` and `reset`: one
    `sequence_anomaly` record with the Sample as received and no gap.
    """
    if events.kind != "new":
        anomaly = {
            "record_type": "sequence_anomaly", "schema_version": SCHEMA_VERSION, "run_id": run_id, "recv_time": recv_time,
            "anomaly": events.kind, "seq": events.seq, "previous_seq": events.previous_highest, "sample": dict(sample),
        }
        return [anomaly], ()
    records: list[dict[str, Any]] = [
        {"record_type": "sample", "schema_version": SCHEMA_VERSION, "run_id": run_id, "recv_time": recv_time, "sample": dict(sample)}
    ]
    specs = _specs_for(events)
    ids = [_gap_id(next_gap_number + i) for i in range(len(specs))]
    start = previous_recv_time if previous_recv_time is not None else recv_time
    for index, spec in enumerate(specs):
        peers = [(ids[j], specs[j].range) for j in range(len(specs)) if j != index]
        reconciliation, with_ids = reconcile(spec.range, peers)
        records.append(_gap_record(run_id, spec, ids[index], recv_time, start, recv_time, reconciliation, with_ids))
    return records, tuple(ids)


# --- replay (design D26 item 4) --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayConflict:
    """A stored gap that shares `gap_type` and `detected_at_seq` with an expected gap but differs elsewhere."""

    gap_type: str
    detected_at_seq: int
    expected: tuple[Any, ...]  # the semantic identity replay computed
    stored: tuple[tuple[str, tuple[Any, ...]], ...]  # (gap_id, identity) of each stored gap with that key


@dataclass(frozen=True)
class ReplayReport:
    run_id: str
    replayed_samples: int
    regenerated_gap_ids: tuple[str, ...]
    conflicts: tuple[ReplayConflict, ...]
    skipped_samples: int  # stored `sample` records that classify did not call `new` (never expected in a clean file)


class GapReplay:
    """The registry's replay hook: regenerate gap records that a failed or interrupted append left out.

    Wire it with `RunRegistry(config, replay_hook=replay)` and `replay.attach(registry)` (or use
    `create_ingest_service`). It runs inside `open_run` under the run lock, after tail repair.
    """

    def __init__(self, *, now: Callable[[], str] | None = None) -> None:
        self._now = now if now is not None else _default_now
        self._registry: RunRegistry | None = None
        self.reports: dict[str, ReplayReport] = {}

    def attach(self, registry: RunRegistry) -> None:
        self._registry = registry

    def __call__(self, run_id: str, path: Path, read: ReadResult) -> None:
        registry = self._registry
        if registry is None:
            raise RuntimeError("GapReplay is not attached to a registry")
        records = read.records
        by_key: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
        by_seq: dict[int, list[Mapping[str, Any]]] = {}
        for record in records:
            if record.get("record_type") == "gap" and record.get("gap_type") in (_DISCONTINUITY, _DROP):
                detected = record.get("detected_at_seq")
                if _is_int(detected):
                    by_key.setdefault((record["gap_type"], detected), []).append(record)
                    by_seq.setdefault(detected, []).append(record)
        # never reuse a number: the file's highest valid gap id, and any id this process already handed out
        next_number = max(scan_records(records).next_gap_number, registry.get(run_id).next_gap_number)
        detected_now = self._now()
        seq_state = SeqState()
        previous_recv: str | None = None
        new_records: list[dict[str, Any]] = []
        regenerated: list[str] = []
        conflicts: list[ReplayConflict] = []
        replayed = skipped = 0
        for record in records:
            if record.get("record_type") != "sample":
                continue
            sample, recv = record["sample"], record["recv_time"]
            events = classify(seq_state, sample["seq"], sample["dropped"])
            if events.kind != "new":
                skipped += 1
                continue
            replayed += 1
            missing: list[_GapSpec] = []
            for spec in _specs_for(events):
                stored = by_key.get((spec.gap_type, spec.detected_at_seq), [])
                if any(_identity_of_record(g) == spec.identity for g in stored):
                    continue
                if stored:
                    conflicts.append(
                        ReplayConflict(spec.gap_type, spec.detected_at_seq, spec.identity,
                                       tuple((str(g.get("gap_id")), _identity_of_record(g)) for g in stored))
                    )
                    continue
                missing.append(spec)
            ids = [_gap_id(next_number + i) for i in range(len(missing))]
            next_number += len(missing)
            start = previous_recv if previous_recv is not None else recv
            for index, spec in enumerate(missing):
                # D29: peers are the gaps of the same ingest (same detected_at_seq), stored ones included
                peers = [(str(g["gap_id"]), _stored_range(g)) for g in by_seq.get(spec.detected_at_seq, []) if g["gap_type"] != spec.gap_type]
                peers += [(ids[j], missing[j].range) for j in range(len(missing)) if j != index]
                reconciliation, with_ids = reconcile(spec.range, peers)
                new_records.append(_gap_record(run_id, spec, ids[index], detected_now, start, recv, reconciliation, with_ids))
                regenerated.append(ids[index])
            seq_state.apply(events)
            previous_recv = recv
        report = ReplayReport(run_id, replayed, tuple(regenerated), tuple(conflicts), skipped)
        for conflict in conflicts:
            logger.warning(
                "replay conflict run_id=%s gap_type=%s detected_at_seq=%d expected=%r stored=%r (nothing added, nothing overwritten)",
                run_id, conflict.gap_type, conflict.detected_at_seq, conflict.expected, conflict.stored,
            )
        if new_records:
            _check_generated(new_records)
            registry.writer.append(run_id, new_records)  # one durable append; an OSError leaves the run dirty (open_run)
            logger.info("replay regenerated %d gap record(s) run_id=%s ids=%s", len(new_records), run_id, ",".join(regenerated))
        self.reports[run_id] = report


# --- results ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class IngestResult:
    """What the HTTP layer needs: `http_status` and `body` (JSON-ready). The rest is for logs and tests.

    `kind`: `stored`, `duplicate`, `reset` (200), `rejected` (413, 422), `storage_failure` (503), `internal_error` (500).
    `rejection_recorded` is None unless a rejection was attempted; the body carries `rejection_recorded: false`
    only when the write failed (design "Error Handling").
    """

    http_status: int
    body: dict[str, Any]
    kind: str
    run_id: str | None = None
    seq: int | None = None
    durability: str | None = None
    recv_time: str | None = None
    gap_ids: tuple[str, ...] = ()
    rejection_recorded: bool | None = None
    ingest_ms: float = 0.0


@dataclass(frozen=True)
class TerminationResult:
    http_status: int
    body: dict[str, Any]
    recorded: bool
    run_id: str | None = None
    rejection_recorded: bool | None = None
    ingest_ms: float = 0.0


def _parse_constant(name: str) -> Any:
    raise ValueError(f"non-finite JSON constant {name}")


class _DuplicateKeyError(ValueError):
    """An object repeats a key after escape decoding (D33). A `ValueError`, so it takes the `invalid_json` path."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKeyError(f"duplicate key {key[:40]!r}")
        out[key] = value
    return out


def _parse_body(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8"), parse_constant=_parse_constant, object_pairs_hook=_reject_duplicate_keys)


def _clip(text: str) -> str:
    return text if len(text) <= _REASON_MAX else text[: _REASON_MAX - 3] + "..."


# --- the service -----------------------------------------------------------------------------------------


class IngestService:
    """See the module docstring. Thread-safe through the registry's per-run locks.

    `registry` must have been built with a `GapReplay` as its `replay_hook` (use `create_ingest_service`), because
    recovery of missing gap records belongs to this task. `monotonic` is injectable and is used for `ingest_ms`
    and for `NewSample.monotonic` (the clock the silence watchdog must share). `storage_probe` is optional; when
    given, every failed persistence step invalidates its cache.
    """

    def __init__(
        self,
        registry: RunRegistry,
        *,
        storage_probe: StorageProbe | None = None,
        now: Callable[[], str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(registry.replay_hook, GapReplay):
            raise ValueError("the registry's replay_hook must be a GapReplay (see create_ingest_service)")
        self._registry = registry
        self._config: CollectorConfig = registry.config
        self._probe = storage_probe
        self._now = now if now is not None else _default_now
        self._monotonic = monotonic
        self._rejections_lock = threading.Lock()  # the writer is not thread-safe and `_rejections` has no run lock

    @property
    def registry(self) -> RunRegistry:
        return self._registry

    @property
    def replay_reports(self) -> Mapping[str, ReplayReport]:
        hook = self._registry.replay_hook
        assert isinstance(hook, GapReplay)
        return hook.reports

    # --- ingest ------------------------------------------------------------------------------------------

    def ingest(self, raw_body: bytes | bytearray | memoryview) -> IngestResult:
        """Handle one `POST /v1/samples` body. Never raises for bad input or storage failure."""
        if not isinstance(raw_body, (bytes, bytearray, memoryview)):
            raise TypeError(f"raw_body must be bytes, got {type(raw_body).__name__}")
        raw = bytes(raw_body)
        recv = self._now()  # Receive_Time: the first thing read for this request (design, "IngestService.ingest")
        started = self._monotonic()
        result = self._ingest(raw, started, recv)
        elapsed_ms = (self._monotonic() - started) * 1000.0
        logger.info(
            "ingest run_id=%s seq=%s kind=%s http=%d ingest_ms=%.3f",
            result.run_id or "-", "-" if result.seq is None else result.seq, result.kind, result.http_status, elapsed_ms,
            extra={"ingest_ms": elapsed_ms, "http_status": result.http_status, "ingest_kind": result.kind},
        )
        return replace(result, ingest_ms=elapsed_ms)

    def _ingest(self, raw: bytes, started: float, recv: str) -> IngestResult:
        limit = self._config.max_body_bytes
        if len(raw) > limit:
            return self._rejected(raw, recv, 413, None, "body_too_large", "", f"body is {len(raw)} bytes, the limit is {limit}")
        try:
            obj = _parse_body(raw)
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            return self._rejected(raw, recv, 422, None, "invalid_json", "", _clip(f"body is not valid JSON ({type(exc).__name__}: {exc})"))
        issues = validate_sample(obj)
        if issues:
            first = issues[0]
            run_id = obj["run_id"] if isinstance(obj, dict) and validate_run_id(obj.get("run_id")) is None else None
            return self._rejected(raw, recv, 422, run_id, first.reason_code, first.field, first.reason)
        run_id = obj["run_id"]
        seq = obj["seq"]
        registry = self._registry
        with registry.lock(run_id) as state:
            try:
                registry.open_run(run_id)  # may replay missing gap records and write run_metadata
            except (OSError, TailRepairRequired) as exc:
                return self._storage_failure(run_id, seq, "open_run", exc)
            except (RecordError, GeneratedRecordError) as exc:
                return self._internal_error(run_id, seq, "open_run", exc)
            events = classify(SeqState(state.highest_seq, state.seen), seq, obj["dropped"])
            records, gap_ids = build_records(run_id, obj, recv, events, state.last_sample_recv_time, state.next_gap_number)
            try:
                _check_generated(records)
            except GeneratedRecordError as exc:
                return self._internal_error(run_id, seq, "build_records", exc)
            try:
                appended = registry.append(state, records)  # one write; marks the run dirty on failure
            except (OSError, TailRepairRequired) as exc:
                return self._storage_failure(run_id, seq, "append", exc)
            except RecordError as exc:  # the writer's own check disagrees with ours: nothing was written
                return self._internal_error(run_id, seq, "append", exc)
            if events.kind == "new":
                state.apply(Outcome(new_sample=NewSample(seq, recv, obj["interval_ms"], started), gap_ids=gap_ids))
        durability = appended.durability.value
        status = "stored" if events.kind == "new" else events.kind
        body = {"status": status, "run_id": run_id, "seq": seq, "durability": durability, "recv_time": recv}
        return IngestResult(200, body, status, run_id, seq, durability, recv, gap_ids)

    # --- termination -------------------------------------------------------------------------------------

    def record_termination(self, run_id: object, raw_body: bytes | bytearray | memoryview) -> TerminationResult:
        """Record the Runner's observed termination (design "Termination record"). Never raises for bad input.

        `raw_body` is a JSON object with exactly `source`, `observed_time`, `container_state` (`status`,
        `exit_code`, `oom_killed`), `cause_label` and `note`. The Collector adds the envelope and infers nothing.
        The run is marked terminated only after the record is durable. Invalid input: 422 and a rejection record
        (413 when the body exceeds the size limit). Storage failure: 503, nothing recorded, probe invalidated.
        """
        if not isinstance(raw_body, (bytes, bytearray, memoryview)):
            raise TypeError(f"raw_body must be bytes, got {type(raw_body).__name__}")
        raw = bytes(raw_body)
        recv = self._now()  # Receive_Time, read when the request starts, before validation and the run lock
        started = self._monotonic()
        result = self._terminate(run_id, raw, recv)
        elapsed_ms = (self._monotonic() - started) * 1000.0
        logger.info(
            "termination run_id=%s recorded=%s http=%d ingest_ms=%.3f",
            result.run_id or "-", result.recorded, result.http_status, elapsed_ms,
            extra={"ingest_ms": elapsed_ms, "http_status": result.http_status},
        )
        return replace(result, ingest_ms=elapsed_ms)

    def _terminate(self, run_id: object, raw: bytes, recv: str) -> TerminationResult:
        problem = validate_run_id(run_id)
        if problem is not None:
            return self._termination_rejected(raw, recv, 422, None, problem.reason_code, problem.field, problem.reason)
        assert isinstance(run_id, str)
        limit = self._config.max_body_bytes
        if len(raw) > limit:
            return self._termination_rejected(raw, recv, 413, run_id, "body_too_large", "", f"body is {len(raw)} bytes, the limit is {limit}")
        try:
            body = _parse_body(raw)
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            return self._termination_rejected(raw, recv, 422, run_id, "invalid_json", "", _clip(f"body is not valid JSON ({type(exc).__name__}: {exc})"))
        if not isinstance(body, dict):
            return self._termination_rejected(raw, recv, 422, run_id, "wrong_type", "$", "termination body must be a JSON object")
        for key in _ENVELOPE_KEYS:
            if key in body:
                return self._termination_rejected(raw, recv, 422, run_id, "unknown_field", key, f"unknown field {key!r}: the Collector sets the envelope")
        issues = validate_record({"record_type": "termination", "schema_version": SCHEMA_VERSION, "run_id": run_id, "recv_time": recv, **body})
        if issues:
            return self._termination_rejected(raw, recv, 422, run_id, issues[0].reason_code, issues[0].field, issues[0].reason)
        registry = self._registry
        with registry.lock(run_id) as state:
            try:
                registry.open_run(run_id)
                record = {"record_type": "termination", "schema_version": SCHEMA_VERSION, "run_id": run_id, "recv_time": recv, **body}
                registry.append(state, [record])
            except (OSError, TailRepairRequired) as exc:
                self._invalidate()
                logger.warning("storage failure stage=termination run_id=%s error=%s: %s", run_id, type(exc).__name__, exc)
                return TerminationResult(503, {"error": "storage_failure"}, False, run_id)
            except (RecordError, GeneratedRecordError) as exc:
                logger.error("internal error stage=termination run_id=%s: %s", run_id, exc)
                return TerminationResult(500, {"error": "internal_error"}, False, run_id)
            state.apply(Outcome(terminated=True))  # only now: the record is durable
        return TerminationResult(200, {"status": "recorded"}, True, run_id)

    # --- rejections and failures -------------------------------------------------------------------------

    def _rejection_record(self, raw: bytes, recv: str, status: int, run_id: str | None, code: str, field: str, reason: str) -> dict[str, Any]:
        return {
            "record_type": "rejection", "schema_version": SCHEMA_VERSION, "run_id": run_id, "recv_time": recv,
            "http_status": status, "reason_code": code, "field": field, "reason": reason,
            "body_len": len(raw), "body_preview_b64": base64.b64encode(raw[:BODY_PREVIEW_MAX]).decode("ascii"),
        }

    def _rejected(self, raw: bytes, recv: str, status: int, run_id: str | None, code: str, field: str, reason: str) -> IngestResult:
        recorded = self._persist_rejection(run_id, code, self._rejection_record(raw, recv, status, run_id, code, field, reason))
        error = "body_too_large" if code == "body_too_large" else "validation_failed"
        body: dict[str, Any] = {"error": error, "reason_code": code, "field": field, "reason": reason}
        if not recorded:
            body["rejection_recorded"] = False
        return IngestResult(status, body, "rejected", run_id, None, None, recv, (), recorded)

    def _termination_rejected(self, raw: bytes, recv: str, status: int, run_id: str | None, code: str, field: str, reason: str) -> TerminationResult:
        recorded = self._persist_rejection(run_id, code, self._rejection_record(raw, recv, status, run_id, code, field, reason))
        error = "body_too_large" if code == "body_too_large" else "validation_failed"
        body: dict[str, Any] = {"error": error, "reason_code": code, "field": field, "reason": reason}
        if not recorded:
            body["rejection_recorded"] = False
        return TerminationResult(status, body, False, run_id, recorded)

    def _persist_rejection(self, run_id: str | None, code: str, record: dict[str, Any]) -> bool:
        """Write one rejection record, which already carries the request's Receive_Time; return whether it is durable.

        The record's recv_time was read when the request started, so it can be older than records already in the file.
        False means not durable (logged, probe invalidated).
        """
        registry = self._registry
        try:
            if run_id is None:
                with self._rejections_lock:
                    _check_generated([record])
                    try:
                        registry.ensure_rejections_ready()
                        registry.writer.append(REJECTIONS_STEM, [record])
                    except (OSError, TailRepairRequired):
                        registry.mark_rejections_dirty()
                        raise
            else:
                with registry.lock(run_id) as state:
                    registry.open_run(run_id)
                    _check_generated([record])
                    registry.append(state, [record])
            return True
        except (OSError, TailRepairRequired) as exc:
            self._invalidate()
            logger.warning(
                "rejection not recorded run_id=%s reason_code=%s error=%s: %s",
                run_id or "-", code, type(exc).__name__, exc,
            )
        except (RecordError, GeneratedRecordError) as exc:
            logger.error("rejection record is invalid run_id=%s reason_code=%s: %s", run_id or "-", code, exc)
        return False

    def _invalidate(self) -> None:
        if self._probe is not None:
            self._probe.invalidate()

    def _storage_failure(self, run_id: str, seq: int, stage: str, exc: BaseException) -> IngestResult:
        self._invalidate()
        logger.warning("storage failure stage=%s run_id=%s seq=%d error=%s: %s", stage, run_id, seq, type(exc).__name__, exc)
        return IngestResult(503, {"error": "storage_failure"}, "storage_failure", run_id, seq)

    def _internal_error(self, run_id: str, seq: int, stage: str, exc: BaseException) -> IngestResult:
        logger.error("internal error stage=%s run_id=%s seq=%d: %s", stage, run_id, seq, exc)
        return IngestResult(500, {"error": "internal_error"}, "internal_error", run_id, seq)


def create_ingest_service(
    config: CollectorConfig,
    *,
    fsync: Callable[[int], None] = os.fsync,
    fsync_dir: Callable[[Path], None] = fsync_directory,
    now: Callable[[], str] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    storage_probe: StorageProbe | None = None,
    volume_lock: VolumeLock | None = None,
) -> IngestService:
    """Build a `RunRegistry` with the `GapReplay` hook and an `IngestService` over it. The caller calls `registry.start()`."""
    replay = GapReplay(now=now)
    registry = RunRegistry(config, fsync=fsync, fsync_dir=fsync_dir, now=now, replay_hook=replay, volume_lock=volume_lock)
    replay.attach(registry)
    return IngestService(registry, storage_probe=storage_probe, now=now, monotonic=monotonic)
