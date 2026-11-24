"""Memory measurement tool for parsing a 16 MiB evidence file through the Viewer pipeline.

NOT collected by pytest (name does not start with test_).
Run directly:
    .venv/Scripts/python.exe tests/unit/viewer/measure_parse_memory.py

Measures:
  - tracemalloc peak (Python-level allocation tracking)
  - ctypes GetProcessMemoryInfo PeakWorkingSetSize (Windows process working set)
  - Elapsed wall-clock time

Builds a synthetic 16,777,216-byte evidence file in a temp directory, then:
  1. reads it through files.read_artifact
  2. parses with read_evidence(bytes, name="mem-test-00000001.jsonl") + summarize
  3. builds chart payload and detail JSON via to_detail + dump_json

Never touches the real runs/ directory.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

# Allow running as a plain script from the repository root (tests/unit/viewer -> repo root).
_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ---------------------------------------------------------------------------
# Windows PROCESS_MEMORY_COUNTERS_EX via ctypes (stdlib only, no psutil)
# ---------------------------------------------------------------------------

class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    """PROCESS_MEMORY_COUNTERS_EX from psapi.h."""
    _fields_ = [
        ("cb",                          ctypes.wintypes.DWORD),
        ("PageFaultCount",              ctypes.wintypes.DWORD),
        ("PeakWorkingSetSize",          ctypes.c_size_t),
        ("WorkingSetSize",              ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage",     ctypes.c_size_t),
        ("QuotaPagedPoolUsage",         ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage",  ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage",      ctypes.c_size_t),
        ("PagefileUsage",               ctypes.c_size_t),
        ("PeakPagefileUsage",           ctypes.c_size_t),
        ("PrivateUsage",                ctypes.c_size_t),
    ]


def _peak_working_set_bytes() -> int | None:
    """Return PeakWorkingSetSize for the current process (bytes), or None on failure."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = []
        # K32GetProcessMemoryInfo lives in kernel32 on Windows 7+.
        fn = kernel32.K32GetProcessMemoryInfo
        fn.restype = ctypes.wintypes.BOOL
        fn.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.POINTER(_PROCESS_MEMORY_COUNTERS_EX),
            ctypes.wintypes.DWORD,
        ]
        pmc = _PROCESS_MEMORY_COUNTERS_EX()
        pmc.cb = ctypes.sizeof(pmc)
        ok = fn(kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        if not ok:
            print(f"  (GetProcessMemoryInfo failed, error {ctypes.get_last_error()})")
            return None
        return int(pmc.PeakWorkingSetSize)
    except Exception as exc:  # report, never hide
        print(f"  (peak working set unavailable: {type(exc).__name__}: {exc})")
        return None


# ---------------------------------------------------------------------------
# Build the synthetic 16 MiB evidence file
# ---------------------------------------------------------------------------

RUN_ID = "mem-test-00000001"
TARGET_BYTES = 16_777_216  # exactly 16 MiB


def _make_sample_line(seq: int, ts_ms: int, run_id: str) -> bytes:
    """One Sample line in the Collector envelope."""
    # Base epoch for timestamps: 2026-11-20T21:13:00.000Z = 1763672380000 ms
    _EPOCH_MS = 1_763_672_380_000
    ts_epoch_ms = _EPOCH_MS + ts_ms
    ts_s = ts_epoch_ms // 1000
    ts_frac = ts_epoch_ms % 1000
    # Build ISO timestamp with 3 decimal places
    import datetime as _dt
    dt = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc) + _dt.timedelta(milliseconds=ts_epoch_ms)
    ts_str = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    recv_epoch_ms = ts_epoch_ms + 20
    dt_recv = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc) + _dt.timedelta(milliseconds=recv_epoch_ms)
    recv_str = dt_recv.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt_recv.microsecond // 1000:03d}Z"

    sample = {
        "run_id": run_id,
        "seq": seq,
        "ts": ts_str,
        "rss_bytes": 71_303_168 + seq * 128,
        "request_count": 9,
        "loop_delay_ms": 0.84,
        "pid": 1,
        "container_id": "a" * 12,
        "container_id_source": "mountinfo",
        "interval_ms": 500,
        "dropped": 0,
    }
    record = {
        "record_type": "sample",
        "schema_version": 1,
        "run_id": run_id,
        "recv_time": recv_str,
        "sample": sample,
    }
    return json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii") + b"\n"


def _build_evidence(target_bytes: int, run_id: str) -> bytes:
    """Build a valid evidence file of exactly *target_bytes* bytes."""
    import datetime as _dt

    # run_metadata header
    start_str = "2026-11-20T21:13:00.000Z"
    metadata = {
        "record_type": "run_metadata",
        "schema_version": 1,
        "run_id": run_id,
        "recv_time": start_str,
        "event": "created",
        "durability": "process",
        "fsync_before_ack": False,
        "evidence_file": f"{run_id}.jsonl",
        "collector_version": "0.0.0-test",
    }
    header = json.dumps(metadata, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii") + b"\n"

    chunks = [header]
    total = len(header)
    seq = 0

    while True:
        line = _make_sample_line(seq, seq * 500, run_id)
        if total + len(line) > target_bytes:
            break
        chunks.append(line)
        total += len(line)
        seq += 1

    # Pad the last line to reach exactly target_bytes
    remaining = target_bytes - total
    if remaining > 0:
        # Build a comment-like record (run_metadata with padding) or just pad with spaces in a valid JSON line
        # Simplest: add a sample line and pad the last field to exact length
        # Actually, let's just pad with a JSON record that is exactly `remaining` bytes
        # We'll craft a run_metadata event with padding
        pad_data = "x" * max(0, remaining - 100)
        pad_record = {
            "record_type": "run_metadata",
            "schema_version": 1,
            "run_id": run_id,
            "recv_time": start_str,
            "event": "pad",
            "pad": pad_data,
        }
        pad_line = json.dumps(pad_record, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii") + b"\n"
        # Adjust pad to hit exactly target_bytes
        actual_pad_size = target_bytes - total - len(pad_line)
        if actual_pad_size >= 0:
            pad_data2 = "x" * (len(pad_data) + actual_pad_size)
            pad_record["pad"] = pad_data2
            pad_line = json.dumps(pad_record, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii") + b"\n"
        # Clamp to remaining
        if len(pad_line) > remaining:
            pad_line = pad_line[:remaining]
        elif len(pad_line) < remaining:
            # If we undershoot add more x's
            extra_x = remaining - len(pad_line)
            # Reconstruct with extra chars
            pad_data3 = "x" * (len(pad_data2) + extra_x)  # type: ignore[name-defined]
            pad_record["pad"] = pad_data3
            pad_line = json.dumps(pad_record, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii") + b"\n"
            pad_line = pad_line[:remaining]
        chunks.append(pad_line)
        total += len(pad_line)

    data = b"".join(chunks)
    # Trim or zero-pad to exactly target_bytes
    if len(data) > target_bytes:
        data = data[:target_bytes]
    elif len(data) < target_bytes:
        data = data + b"\n" * (target_bytes - len(data))
    assert len(data) == target_bytes, f"expected {target_bytes} bytes, got {len(data)}"
    return data


# ---------------------------------------------------------------------------
# IO seam for files.read_artifact with a temp directory
# ---------------------------------------------------------------------------

def _build_runs_root(tmp_dir: str, run_id: str, evidence_data: bytes) -> object:
    """Write the evidence file and a minimal manifest to tmp_dir/<run_id>/,
    then return a RunsRoot over tmp_dir."""
    # Deferred imports (viewer modules)
    from coroner.runner.manifest import (
        ContainerState, EvidenceRef, Expected, InspectExcerpt,
        RunManifest, serialize_manifest,
    )
    from coroner.viewer.files import open_runs_root

    run_dir = Path(tmp_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Write evidence
    (run_dir / "evidence.jsonl").write_bytes(evidence_data)

    # Minimal container state (needed for a valid manifest)
    cid = "a" * 64
    state = ContainerState(
        status="exited",
        exit_code=137,
        oom_killed=False,
        container_id=cid,
        host_config_memory_bytes=134_217_728,
        inspect_time="2026-11-20T21:13:21.760Z",
        raw_inspect_path=f"runs/{run_id}/docker_inspect.json",
        raw_inspect_excerpt=InspectExcerpt(
            id=cid, status="exited", oom_killed=False, exit_code=137, memory=134_217_728
        ),
    )
    manifest = RunManifest(
        experiment="B",
        run_id=run_id,
        outcome="passed",
        start_time="2026-11-20T21:13:00.000Z",
        end_time="2026-11-20T21:13:30.000Z",
        durability_level="process",
        termination_description="mem-test fixture",
        expected=Expected(exit_code=137, oom_killed=False),
        failure=None,
        container_state=state,
        evidence=EvidenceRef(
            container_path=f"/evidence/{run_id}.jsonl",
            host_copy_path=f"runs/{run_id}/evidence.jsonl",
            record_count=evidence_data.count(b"\n"),
        ),
        extra={
            "container_finished_at": "2026-11-20T21:13:21.760Z",
            "clock_offset_ms": 9,
            "clock_offset_uncertainty_ms": 12.5,
            "sample_timing": {
                "tolerance_ms": 250,
                "clock_offset_ms": 9,
                "pre_kill_count": 0,
                "pre_kill_late_received_count": 0,
                "within_tolerance_count": 0,
                "post_kill_beyond_tolerance": {"count": 0, "items": []},
                "after_container_stop": {"count": 0, "items": []},
                "after_container_stop_analysis": "unavailable",
            },
            "times": {"sigkill_sent_time": "2026-11-20T21:13:20.000Z", "crash_request_time": None},
        },
    )
    (run_dir / "manifest.json").write_bytes(serialize_manifest(manifest))

    # Write a minimal docker_inspect.json
    inspect_doc = json.dumps([{
        "Id": cid,
        "State": {
            "Status": "exited",
            "OOMKilled": False,
            "ExitCode": 137,
            "FinishedAt": "2026-11-20T21:13:21.760545826Z",
        },
        "HostConfig": {"Memory": 134_217_728},
    }], indent=2)
    (run_dir / "docker_inspect.json").write_bytes(inspect_doc.encode("utf-8"))

    return open_runs_root(tmp_dir)


# ---------------------------------------------------------------------------
# Main measurement function
# ---------------------------------------------------------------------------

def run_measurement(use_tracemalloc: bool = True) -> dict:
    """Run the full pipeline once and return measurements."""
    from coroner.viewer.files import ArtifactName, DEFAULT_IO, find_run, read_artifact
    from coroner.viewer.loader import load_detail
    from coroner.viewer.model import dump_json

    # Reset peak working set baseline (read once before allocating)
    _ = _peak_working_set_bytes()

    with tempfile.TemporaryDirectory() as tmp:
        # Build evidence once (outside measurement)
        evidence_data = _build_evidence(TARGET_BYTES, RUN_ID)
        assert len(evidence_data) == TARGET_BYTES

        root = _build_runs_root(tmp, RUN_ID, evidence_data)

        # --- start measurement ---
        if use_tracemalloc:
            tracemalloc.start()
        t0 = time.perf_counter()

        # Step 1: read through files.read_artifact
        entry = find_run(root, RUN_ID)
        assert entry is not None, "run not found"
        ar = read_artifact(root, entry, ArtifactName.EVIDENCE)
        assert ar.fault is None, f"artifact fault: {ar.fault}"
        assert ar.data is not None and len(ar.data) == TARGET_BYTES
        del ar  # release; load_detail below repeats read + parse exactly as a request does

        # Steps 2 and 3: load_detail = read_artifact + read_evidence(bytes, name=...) +
        # summarize + samples + chart payload, the same path a request takes.
        record = load_detail(root, RUN_ID, DEFAULT_IO, time.time_ns)
        assert record.summary is not None, "evidence was not parsed"
        summary = record.summary

        # Step 4: detail dict + JSON bytes.
        json_bytes = b""
        serialize_error = None
        try:
            from coroner.viewer.model import to_detail
            json_bytes = dump_json(to_detail(record))
        except Exception as exc:  # recorded in the report, not hidden
            serialize_error = f"{type(exc).__name__}: {exc}"

        t1 = time.perf_counter()
        peak_traced = None
        if use_tracemalloc:
            _, peak_traced = tracemalloc.get_traced_memory()
            tracemalloc.stop()

        peak_ws = _peak_working_set_bytes()

        return {
            "sample_count": summary.sample_count,
            "evidence_bytes": TARGET_BYTES,
            "json_bytes": len(json_bytes),
            "serialize_error": serialize_error,
            "elapsed_s": t1 - t0,
            "tracemalloc_peak_bytes": peak_traced,
            "peak_working_set_bytes": peak_ws,
        }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Python {sys.version}")
    print(f"Measuring peak memory for a {TARGET_BYTES:,}-byte ({TARGET_BYTES / 1024 / 1024:.1f} MiB) evidence file")
    print(f"Run ID: {RUN_ID}")
    print()

    use_trace = "--no-tracemalloc" not in sys.argv
    print(f"tracemalloc: {'on' if use_trace else 'off (working-set only)'}")
    result = run_measurement(use_trace)

    print(f"  Sample count parsed   : {result['sample_count']:,}")
    print(f"  Evidence bytes        : {result['evidence_bytes']:,}")
    print(f"  JSON response bytes   : {result['json_bytes']:,}")
    print(f"  to_detail/dump_json   : {result['serialize_error'] or 'ok'}")
    print(f"  Elapsed time          : {result['elapsed_s'] * 1000:.1f} ms")
    print()
    if result['tracemalloc_peak_bytes'] is not None:
        print(f"  tracemalloc peak      : {result['tracemalloc_peak_bytes']:,} bytes"
              f"  ({result['tracemalloc_peak_bytes'] / 1024 / 1024:.1f} MiB)")
    if result['peak_working_set_bytes'] is not None:
        print(f"  PeakWorkingSetSize    : {result['peak_working_set_bytes']:,} bytes"
              f"  ({result['peak_working_set_bytes'] / 1024 / 1024:.1f} MiB)")
        ws_mb = result['peak_working_set_bytes'] / 1024 / 1024
        if ws_mb > 150:
            print(f"  ** FLAG: PeakWorkingSetSize ({ws_mb:.0f} MiB) is materially larger than ~100 MiB **")
    else:
        print("  PeakWorkingSetSize    : unavailable (non-Windows or ctypes failure)")
