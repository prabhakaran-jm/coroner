"""Synthetic Runs_Directory builder for the Viewer tests (design: Testing Strategy, fixture builder).

Every run is written under the `root` the caller passes (always a pytest `tmp_path`). Nothing here reads
the real `runs/` directory, runs a subprocess, or opens a socket.

Manifests are written with the M0 `serialize_manifest`, so they pass `deserialize_manifest` by
construction. Evidence lines are written by hand in the Collector envelope (not with `EvidenceWriter`,
which fsyncs and manages durability).
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from coroner.runner.manifest import (
    ContainerState,
    Expected,
    EvidenceRef,
    Failure,
    InspectExcerpt,
    RunManifest,
    serialize_manifest,
)

AUTO = "auto"

DEFAULT_START_TIME = "2026-11-20T21:13:00.000Z"
DEFAULT_FINISHED_AT = "2026-11-20T21:13:21.760545826Z"  # nine fractional digits, as Docker writes it
RECV_DELAY_MS = 20
COLLECTOR_VERSION = "0.0.0-test"
MANIFEST_NAME = "manifest.json"
EVIDENCE_NAME = "evidence.jsonl"
INSPECT_NAME = "docker_inspect.json"

_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


# ---------------------------------------------------------------- specs


@dataclass(frozen=True)
class SampleSpec:
    """One Sample. `ts` and `recv_time` default to values derived from `seq` and the run start."""

    seq: int
    rss_bytes: int | None = 71_303_168
    ts: str | None = None
    recv_time: str | None = None
    request_count: int = 9
    interval_ms: int = 500
    null_reason: str = "cannot read /proc/self/statm: FileNotFoundError"


@dataclass(frozen=True)
class GapSpec:
    """A gap record: `sequence_discontinuity` (needs the missing range) or `silence_timeout`."""

    kind: str = "sequence_discontinuity"
    first_missing_seq: int = 0
    last_missing_seq: int = 0
    gap_id: str | None = None
    start_time: str | None = None
    end_time: str | None = None


@dataclass(frozen=True)
class StateSpec:
    """The manifest `container_state` block."""

    status: str = "exited"
    exit_code: int = 137
    oom_killed: bool = False
    memory: int = 134_217_728
    container_id: str | None = None


@dataclass(frozen=True)
class InspectSpec:
    """The raw `docker inspect` document (a list with one object)."""

    status: str = "exited"
    exit_code: int = 137
    oom_killed: bool = False
    memory: int = 134_217_728
    container_id: str | None = None
    finished_at: str = DEFAULT_FINISHED_AT


# ---------------------------------------------------------------- helpers


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, _TS_FORMAT)


def format_ts(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def container_id_for(run_id: str) -> str:
    """A stable 64-hex container id derived from the run id."""
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()


def serialize_line(record: Mapping[str, Any]) -> bytes:
    text = json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    return text.encode("ascii") + b"\n"


def envelope(run_id: str, record_type: str, recv_time: str, **fields: Any) -> dict[str, Any]:
    return {"record_type": record_type, "schema_version": 1, "run_id": run_id, "recv_time": recv_time, **fields}


def build_inspect_document(spec: InspectSpec, run_id: str) -> bytes:
    cid = spec.container_id or container_id_for(run_id)
    doc = [
        {
            "Id": cid,
            "State": {
                "Status": spec.status,
                "OOMKilled": spec.oom_killed,
                "ExitCode": spec.exit_code,
                "FinishedAt": spec.finished_at,
            },
            "HostConfig": {"Memory": spec.memory},
        }
    ]
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


def sample_specs(count: int, *, rss_start: int = 50_000_000, rss_step: int = 1_000_000) -> list[SampleSpec]:
    """`count` consecutive Samples (seq 0..count-1) with a rising RSS."""
    return [SampleSpec(seq=i, rss_bytes=rss_start + i * rss_step) for i in range(count)]


_EXPECTED = {"A": (1, False), "B": (137, False), "C": (137, True)}


class RunsBuilder:
    """Writes synthetic runs into `root` (a temporary directory owned by the test)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._counter = 0

    # -- runs ------------------------------------------------------------

    def add_run(
        self,
        experiment: str = "B",
        *,
        run_id: str | None = None,
        outcome: str = "passed",
        samples: Sequence[SampleSpec] = (),
        gaps: Sequence[GapSpec] = (),
        extra_records: Sequence[dict] = (),
        container_state: StateSpec | None | str = AUTO,
        inspect: InspectSpec | bytes | None | str = AUTO,
        manifest_extra: Mapping[str, Any] | None = None,
        clock_offset_ms: int | None = 9,
        uncertainty_ms: float | None = 12.5,
        sample_timing: Mapping[str, Any] | None | str = AUTO,
        evidence_bytes: bytes | None = None,
        manifest_bytes: bytes | None = None,
        start_time: str | None = None,
    ) -> str:
        """Create `<root>/<run_id>/` with manifest.json, evidence.jsonl and docker_inspect.json.

        `container_state=None` writes `container_state: null`; `inspect=None` writes no inspect file;
        `sample_timing=None` omits the key; `evidence_bytes` and `manifest_bytes` replace the generated
        files verbatim. Returns the run id.
        """
        exp = experiment.upper()
        self._counter += 1
        if run_id is None:
            run_id = f"{experiment.lower()}-20261120T211300Z-{self._counter:08x}"
        run_dir = self.root / run_id
        run_dir.mkdir(parents=False, exist_ok=False)

        start_text = start_time if start_time is not None else DEFAULT_START_TIME
        try:
            start_dt = parse_ts(start_text)
        except ValueError:
            start_dt = parse_ts(DEFAULT_START_TIME)
        end_dt = start_dt + timedelta(seconds=30)

        # evidence
        if evidence_bytes is None:
            evidence = self._evidence(run_id, start_dt, samples, gaps, extra_records)
            record_count = evidence.count(b"\n")
        else:
            evidence = evidence_bytes
            record_count = evidence.count(b"\n")
        (run_dir / EVIDENCE_NAME).write_bytes(evidence)

        # inspect and container state
        state_spec: StateSpec | None
        if container_state == AUTO:
            exit_code, oom = _EXPECTED.get(exp, (137, False))
            state_spec = StateSpec(exit_code=exit_code, oom_killed=oom)
        else:
            state_spec = container_state  # type: ignore[assignment]
        inspect_spec: InspectSpec | bytes | None
        if inspect == AUTO:
            if state_spec is None:
                inspect_spec = None
            else:
                inspect_spec = InspectSpec(
                    status=state_spec.status, exit_code=state_spec.exit_code,
                    oom_killed=state_spec.oom_killed, memory=state_spec.memory,
                    container_id=state_spec.container_id,
                )
        else:
            inspect_spec = inspect  # type: ignore[assignment]
        if isinstance(inspect_spec, InspectSpec):
            (run_dir / INSPECT_NAME).write_bytes(build_inspect_document(inspect_spec, run_id))
        elif isinstance(inspect_spec, bytes):
            (run_dir / INSPECT_NAME).write_bytes(inspect_spec)

        # manifest
        if manifest_bytes is not None:
            (run_dir / MANIFEST_NAME).write_bytes(manifest_bytes)
            return run_id

        finished_at = inspect_spec.finished_at if isinstance(inspect_spec, InspectSpec) else DEFAULT_FINISHED_AT
        container: ContainerState | None = None
        if state_spec is not None:
            cid = state_spec.container_id or container_id_for(run_id)
            container = ContainerState(
                status=state_spec.status,
                exit_code=state_spec.exit_code,
                oom_killed=state_spec.oom_killed,
                container_id=cid,
                host_config_memory_bytes=state_spec.memory,
                inspect_time=format_ts(end_dt),
                raw_inspect_path=f"runs/{run_id}/{INSPECT_NAME}",
                raw_inspect_excerpt=InspectExcerpt(
                    id=cid, status=state_spec.status, oom_killed=state_spec.oom_killed,
                    exit_code=state_spec.exit_code, memory=state_spec.memory,
                ),
            )
        exp_code, exp_oom = _EXPECTED.get(exp, (137, False))
        extra = self._manifest_extra(
            exp, start_dt, len(samples), finished_at, clock_offset_ms, uncertainty_ms, sample_timing
        )
        if manifest_extra:
            extra.update(manifest_extra)
        manifest = RunManifest(
            experiment=experiment,
            run_id=run_id,
            outcome=outcome,
            start_time=start_text,
            end_time=format_ts(end_dt),
            durability_level="process",
            termination_description=f"synthetic experiment {exp} fixture",
            expected=Expected(exit_code=exp_code, oom_killed=exp_oom),
            failure=None if outcome == "passed" else Failure("synthetic_failure", "synthetic fixture failure"),
            container_state=container,
            evidence=EvidenceRef(
                container_path=f"/evidence/{run_id}.jsonl",
                host_copy_path=f"runs/{run_id}/{EVIDENCE_NAME}",
                record_count=record_count,
            ),
            extra=extra,
        )
        (run_dir / MANIFEST_NAME).write_bytes(serialize_manifest(manifest))
        return run_id

    # -- loose entries ---------------------------------------------------

    def add_loose_file(self, name: str, data: bytes = b"") -> None:
        (self.root / name).write_bytes(data)

    def add_empty_dir(self, name: str) -> None:
        (self.root / name).mkdir(parents=False, exist_ok=False)

    def try_symlink(self, link: str, target: Path) -> bool:
        """Create `<root>/<link>` pointing at `target`. False when the platform cannot create it."""
        try:
            os.symlink(target, self.root / link, target_is_directory=Path(target).is_dir())
        except (OSError, NotImplementedError):
            return False
        return True

    def snapshot(self) -> dict[str, tuple[int, str]]:
        """Relative POSIX path -> (size, sha256) of every regular file under the root (links not followed)."""
        result: dict[str, tuple[int, str]] = {}
        for current, dirs, files in os.walk(self.root, followlinks=False):
            dirs.sort()
            for name in sorted(files):
                path = Path(current) / name
                if path.is_symlink():
                    continue
                data = path.read_bytes()
                rel = path.relative_to(self.root).as_posix()
                result[rel] = (len(data), hashlib.sha256(data).hexdigest())
        return result

    # -- internals -------------------------------------------------------

    @staticmethod
    def _evidence(
        run_id: str, start: datetime, samples: Sequence[SampleSpec], gaps: Sequence[GapSpec],
        extra_records: Sequence[dict],
    ) -> bytes:
        recv0 = format_ts(start)
        records: list[dict[str, Any]] = [
            envelope(
                run_id, "run_metadata", recv0, event="created", durability="process",
                fsync_before_ack=False, evidence_file=f"{run_id}.jsonl", collector_version=COLLECTOR_VERSION,
            )
        ]
        for spec in samples:
            ts_dt = start + timedelta(milliseconds=spec.seq * spec.interval_ms)
            ts = spec.ts if spec.ts is not None else format_ts(ts_dt)
            recv = spec.recv_time if spec.recv_time is not None else format_ts(ts_dt + timedelta(milliseconds=RECV_DELAY_MS))
            body: dict[str, Any] = {
                "run_id": run_id, "seq": spec.seq, "ts": ts, "rss_bytes": spec.rss_bytes,
                "request_count": spec.request_count, "loop_delay_ms": 0.84, "pid": 1,
                "container_id": "3f1c0e0a9d7b", "container_id_source": "mountinfo",
                "interval_ms": spec.interval_ms, "dropped": 0,
            }
            if spec.rss_bytes is None:
                body["null_reasons"] = {"rss_bytes": spec.null_reason}
            records.append(envelope(run_id, "sample", recv, sample=body))
        for index, gap in enumerate(gaps, start=1):
            gap_id = gap.gap_id or f"gap-{index:04d}"
            g_start = gap.start_time or recv0
            g_end = gap.end_time or g_start
            if gap.kind == "silence_timeout":
                records.append(envelope(
                    run_id, "gap", g_end, gap_id=gap_id, gap_type="silence_timeout", start_time=g_start,
                    end_time=g_end, last_sample_time=g_start, detected_time=g_end, threshold_ms=1500,
                    interval_ms=500, sample_count="unknown", reconciliation="not_applicable", reconciles_with=[],
                ))
            else:
                records.append(envelope(
                    run_id, "gap", g_end, gap_id=gap_id, gap_type="sequence_discontinuity", start_time=g_start,
                    end_time=g_end, first_missing_seq=gap.first_missing_seq, last_missing_seq=gap.last_missing_seq,
                    missing_count=gap.last_missing_seq - gap.first_missing_seq + 1, reconciliation="none",
                    reconciles_with=[], detected_at_seq=gap.last_missing_seq + 1,
                ))
        records.extend(extra_records)
        return b"".join(serialize_line(r) for r in records)

    @staticmethod
    def _manifest_extra(
        exp: str, start: datetime, sample_count: int, finished_at: str, clock_offset_ms: int | None,
        uncertainty_ms: float | None, sample_timing: Mapping[str, Any] | None | str,
    ) -> dict[str, Any]:
        sigkill = format_ts(start + timedelta(seconds=20))
        extra: dict[str, Any] = {}
        if exp == "B":
            extra["times"] = {"sigkill_sent_time": sigkill, "crash_request_time": None}
        else:
            extra["times"] = {"crash_request_time": sigkill, "sigkill_sent_time": None}
        if exp == "C":
            extra["sampling_limitations"] = {"statements": ["synthetic sampling limitation statement"]}
        if exp == "B":
            if clock_offset_ms is not None:
                extra["clock_offset_ms"] = clock_offset_ms
            if uncertainty_ms is not None:
                extra["clock_offset_uncertainty_ms"] = uncertainty_ms
        extra["container_finished_at"] = finished_at[:23] + "Z" if len(finished_at) >= 23 else finished_at
        if sample_timing == AUTO:
            if exp == "B":
                timing: dict[str, Any] = {
                    "tolerance_ms": 250,
                    "pre_kill_count": sample_count,
                    "pre_kill_late_received_count": 0,
                    "within_tolerance_count": sample_count,
                    # the M0 shape (coroner/runner/evaluate.py to_dict): plain lists of Capture_Time strings
                    "post_kill_beyond_tolerance": [],
                    "after_container_stop": [],
                    "after_container_stop_analysis": "unavailable",
                }
                if clock_offset_ms is not None:
                    timing["clock_offset_ms"] = clock_offset_ms
                extra["sample_timing"] = timing
        elif sample_timing is not None:
            extra["sample_timing"] = dict(sample_timing)  # type: ignore[arg-type]
        return extra
