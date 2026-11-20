"""Route tests for the Collector HTTP API (Task 3.12; design "Collector HTTP API", D31, D33, D34).

FastAPI `TestClient` (`with TestClient(app)` so the lifespan runs), real temporary evidence directories, injected
fsync / probe-opener / clock doubles and the writer's `open` seam for write and flush failures. No Docker, no network
port. Persistence across restarts belongs to Task 3.13.

Framework facts these tests rely on (checked while writing them):
- The TestClient transport (httpx + Starlette, no h11) does NOT enforce Content-Length against the body it sends, so
  understated, overstated and malformed Content-Length headers reach the app unchanged.
- httpx normalises `..` path segments before sending, so `/v1/runs/../evidence` never reaches the run-id route
  (404, no route); `%2e%2e` and `%2F` are decoded by Starlette inside `{run_id:path}` and reach validation (422).
"""
from __future__ import annotations

import base64
import builtins
import io
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from coroner.collector import app as app_module
from coroner.collector.app import create_app
from coroner.collector.config import CollectorConfig, load_config
from coroner.collector.storage_probe import StorageProbe
from coroner.common.timeutil import format_utc_ms, parse_utc_ms
from coroner.evidence import writer as writer_module
from coroner.evidence.reader import read_evidence

from datetime import datetime, timedelta, timezone

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
LIMIT = 2048
STORAGE_FAILURE = {"error": "storage_failure"}


def sample(seq: int = 0, run_id: str = "r1", **over: Any) -> dict[str, Any]:
    return {**SAMPLE, "run_id": run_id, "seq": seq, **over}


def raw(seq: int = 0, run_id: str = "r1", **over: Any) -> bytes:
    return json.dumps(sample(seq, run_id, **over)).encode()


def padded(size: int) -> bytes:
    data = raw()
    return data + b" " * (size - len(data))


def read_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_bytes().splitlines() if line]


class Ops:
    """An fsync double that can be made to fail."""

    def __init__(self) -> None:
        self.fail = False
        self.calls = 0

    def fsync(self, fd: int) -> None:
        if self.fail:
            raise OSError("fsync failed")
        self.calls += 1


class Tick:
    def __init__(self, value: float) -> None:
        self.t = value

    def __call__(self) -> float:
        return self.t


class Wall:
    """A wall clock that advances 10 ms per reading."""

    def __init__(self) -> None:
        self.ticks = 0

    def now(self) -> str:
        self.ticks += 1
        return format_utc_ms(BASE + timedelta(milliseconds=10 * self.ticks))


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
    """Fail the next open of `<run>.jsonl` through the writer's `open` seam."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.mode: str | None = None
        self.name = ""
        monkeypatch.setattr(writer_module, "open", self._open, raising=False)

    def arm(self, mode: str, name: str) -> None:
        self.mode, self.name = mode, name

    def _open(self, path: Any, mode: str) -> Any:
        real = builtins.open(path, mode)
        if self.mode is not None and Path(path).name == self.name:
            fault, self.mode = self.mode, None  # one failure
            return FailingFile(real, fault)
        return real


class Harness:
    def __init__(self, tmp_path: Path, durability: str = "crash", max_body: int | None = None, probe_fails: bool = False) -> None:
        env = {"CORONER_DURABILITY": durability}
        if max_body is not None:
            env["CORONER_MAX_BODY_BYTES"] = str(max_body)
        self.config: CollectorConfig = load_config(env, evidence_dir=tmp_path / "evidence")
        self.directory = self.config.evidence_dir
        self.ops, self.probe_ops = Ops(), Ops()
        self.probe_ops.fail = probe_fails
        self.mono, self.pclock, self.wall = Tick(1000.0), Tick(0.0), Wall()
        self.probe_opens = 0

        def opener(path: Path, mode: str) -> Any:
            if mode == "wb":
                self.probe_opens += 1
            return io.open(path, mode)

        self.probe = StorageProbe(
            self.directory, fsync=self.probe_ops.fsync, fsync_dir=lambda p: None, clock=self.pclock, opener=opener
        )
        self.app = create_app(
            self.config, fsync=self.ops.fsync, fsync_dir=lambda p: None, now=self.wall.now, monotonic=self.mono, probe=self.probe
        )

    def path(self, run_id: str = "r1") -> Path:
        return self.directory / f"{run_id}.jsonl"

    def records(self, run_id: str = "r1") -> list[dict[str, Any]]:
        return list(read_evidence(self.path(run_id)).records)  # skips a quarantined damaged fragment, as the reader does

    def types(self, run_id: str = "r1") -> list[str]:
        return [r["record_type"] for r in self.records(run_id)]

    def rejections(self) -> list[dict[str, Any]]:
        return read_records(self.directory / "_rejections.jsonl")

    def state(self, run_id: str = "r1") -> Any:
        return self.app.state.registry.get(run_id)


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def client_for(harness: Harness) -> TestClient:
    return TestClient(harness.app)


# --- A. /healthz -----------------------------------------------------------------------------------------


def test_healthz_healthy_body(h: Harness) -> None:
    with client_for(h) as c:
        r = c.get("/healthz")
    body = r.json()
    assert r.status_code == 200 and parse_utc_ms(body.pop("server_time")) is not None
    assert body == {"status": "ok", "durability": "crash", "uptime_s": 0, "storage_ok": True}


def test_healthz_is_200_with_storage_error_when_the_probe_fails(tmp_path: Path) -> None:
    h = Harness(tmp_path, probe_fails=True)
    with client_for(h) as c:
        r = c.get("/healthz")
    body = r.json()
    assert r.status_code == 200 and body["storage_ok"] is False and body["status"] == "ok"
    assert body["storage_error"].startswith("fsync: OSError: fsync failed")
    assert set(body) == {"status", "durability", "server_time", "uptime_s", "storage_ok", "storage_error"}


def test_healthz_storage_error_appears_only_on_failure_and_recovery_restores_ok(h: Harness) -> None:
    with client_for(h) as c:
        assert "storage_error" not in c.get("/healthz").json()
        h.probe_ops.fail = True
        h.pclock.t += 1.0
        failed = c.get("/healthz").json()
        assert failed["storage_ok"] is False and "storage_error" in failed
        h.probe_ops.fail = False
        h.pclock.t += 1.0
        healed = c.get("/healthz")
    assert healed.status_code == 200 and healed.json()["storage_ok"] is True and "storage_error" not in healed.json()


@pytest.mark.parametrize("level", ["process", "crash"])
def test_healthz_and_ack_report_the_configured_durability(tmp_path: Path, level: str) -> None:
    h = Harness(tmp_path, durability=level)
    with client_for(h) as c:
        assert c.get("/healthz").json()["durability"] == level
        assert c.post("/v1/samples", content=raw()).json()["durability"] == level
    assert (h.ops.calls == 0) == (level == "process")  # crash fsyncs before acknowledging, process never does


def test_healthz_uptime_follows_the_injected_monotonic_clock(h: Harness) -> None:
    with client_for(h) as c:
        h.mono.t += 125.9
        assert c.get("/healthz").json()["uptime_s"] == 125


def test_healthz_probe_result_is_cached_for_one_second(h: Harness) -> None:
    with client_for(h) as c:
        base = h.probe_opens  # the startup probe is already counted
        assert base == 1
        c.get("/healthz"), c.get("/healthz")
        h.pclock.t += 0.5
        c.get("/healthz")
        assert h.probe_opens == base
        h.pclock.t += 0.5  # age exactly 1.0 s is stale
        c.get("/healthz"), c.get("/healthz")
        assert h.probe_opens == base + 1


def test_a_failed_append_invalidates_the_probe_cache(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    faults = Faults(monkeypatch)
    with client_for(h) as c:
        assert c.post("/v1/samples", content=raw(0)).status_code == 200
        c.get("/healthz")
        before = h.probe_opens
        c.get("/healthz")
        assert h.probe_opens == before  # cached
        faults.arm("write", "r1.jsonl")
        assert c.post("/v1/samples", content=raw(1)).status_code == 503
        assert c.get("/healthz").json()["storage_ok"] is True
        assert h.probe_opens == before + 1  # re-probed within the same second


# --- B. POST /v1/samples ---------------------------------------------------------------------------------


def test_valid_sample_is_stored_with_the_exact_ack(h: Harness) -> None:
    with client_for(h) as c:
        r = c.post("/v1/samples", content=raw())
    stored = [x for x in h.records() if x["record_type"] == "sample"]
    assert len(stored) == 1
    assert r.status_code == 200
    assert r.json() == {"status": "stored", "run_id": "r1", "seq": 0, "durability": "crash", "recv_time": stored[0]["recv_time"]}
    assert h.types() == ["run_metadata", "sample"]


def test_duplicate_and_reset_are_200_with_their_status(h: Harness) -> None:
    with client_for(h) as c:
        for seq in (0, 3):
            c.post("/v1/samples", content=raw(seq))
        duplicate = c.post("/v1/samples", content=raw(3))
        reset = c.post("/v1/samples", content=raw(1))
    assert (duplicate.status_code, duplicate.json()["status"]) == (200, "duplicate")
    assert (reset.status_code, reset.json()["status"]) == (200, "reset")
    assert [x["anomaly"] for x in h.records() if x["record_type"] == "sequence_anomaly"] == ["duplicate", "reset"]


def test_first_sample_with_a_seq_gap_is_stored_and_records_a_discontinuity(h: Harness) -> None:
    with client_for(h) as c:
        r = c.post("/v1/samples", content=raw(5))
    assert r.status_code == 200 and r.json()["status"] == "stored"
    (gap,) = [x for x in h.records() if x["record_type"] == "gap"]
    assert (gap["gap_type"], gap["first_missing_seq"], gap["last_missing_seq"], gap["missing_count"]) == ("sequence_discontinuity", 0, 4, 5)


def test_validation_failures_are_422_with_the_documented_body(h: Harness) -> None:
    missing = {k: v for k, v in SAMPLE.items() if k != "rss_bytes"}
    with client_for(h) as c:
        r_missing = c.post("/v1/samples", content=json.dumps(missing).encode())
        r_type = c.post("/v1/samples", content=raw(rss_bytes="big"))
    assert r_missing.status_code == 422 and r_missing.json() == {
        "error": "validation_failed", "reason_code": "missing_field", "field": "rss_bytes", "reason": r_missing.json()["reason"],
    }
    assert r_type.status_code == 422 and (r_type.json()["reason_code"], r_type.json()["field"]) == ("wrong_type", "rss_bytes")
    assert h.types() == ["run_metadata", "rejection", "rejection"]  # attributable: stored in the run file


def test_invalid_run_id_is_422_and_recorded_unattributed(h: Harness) -> None:
    with client_for(h) as c:
        r = c.post("/v1/samples", content=raw(run_id="bad id"))
    assert r.status_code == 422 and r.json()["reason_code"] == "invalid_run_id" and r.json()["field"] == "run_id"
    (rejection,) = h.rejections()
    assert rejection["run_id"] is None and rejection["http_status"] == 422 and rejection["reason_code"] == "invalid_run_id"
    assert not h.path("bad id").exists()


@pytest.mark.parametrize("payload", [b"{", b"", b'{"run_id":"r1","run_id":"r2"}', b'{"a":{"b":1,"b":2}}', b"\xff\xfe"])
def test_invalid_json_and_duplicate_keys_are_422_invalid_json(h: Harness, payload: bytes) -> None:
    with client_for(h) as c:
        r = c.post("/v1/samples", content=payload)
    assert r.status_code == 422 and r.json()["reason_code"] == "invalid_json" and r.json()["error"] == "validation_failed"
    (rejection,) = h.rejections()
    assert rejection["run_id"] is None and rejection["reason_code"] == "invalid_json"
    assert base64.b64decode(rejection["body_preview_b64"]) == payload and rejection["body_len"] == len(payload)


def test_body_at_the_limit_is_processed_and_one_over_is_413(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_body=LIMIT)
    with client_for(h) as c:
        at = c.post("/v1/samples", content=padded(LIMIT))
        over = c.post("/v1/samples", content=padded(LIMIT + 1))
    assert at.status_code == 200 and at.json()["status"] == "stored"
    assert over.status_code == 413 and over.json()["error"] == "body_too_large" and over.json()["reason_code"] == "body_too_large"
    assert h.types() == ["run_metadata", "sample"]  # the oversized body never touched r1


def test_oversized_content_length_is_413_without_reading_the_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(tmp_path, max_body=LIMIT)
    streams: list[int] = []
    original = Request.stream

    async def spy(self: Request) -> Any:
        streams.append(1)
        async for chunk in original(self):
            yield chunk

    monkeypatch.setattr(Request, "stream", spy)
    with client_for(h) as c:
        r = c.post("/v1/samples", content=b"x" * 50, headers={"content-length": "5000"})
    assert r.status_code == 413 and streams == []  # request.stream() was never called
    (rejection,) = h.rejections()
    assert (rejection["body_len"], rejection["body_preview_b64"]) == (5000, "")
    assert rejection["reason"] == f"declared Content-Length is 5000 bytes (client-declared, body not read or verified), the limit is {LIMIT}"


def test_chunked_body_without_content_length_is_processed_at_the_limit_and_413_above(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_body=LIMIT)

    def chunks(data: bytes) -> Any:
        for i in range(0, len(data), 100):
            yield data[i : i + 100]

    with client_for(h) as c:
        at = c.post("/v1/samples", content=chunks(padded(LIMIT)))
        over = c.post("/v1/samples", content=chunks(padded(LIMIT + 1)))
    assert at.status_code == 200 and over.status_code == 413
    (rejection,) = h.rejections()
    assert rejection["body_len"] == LIMIT + 1  # observed byte count at the stop, a lower bound
    assert rejection["reason"] == f"body exceeds the limit of {LIMIT} bytes; reading stopped at {LIMIT + 1} bytes (lower bound, true length not measured)"
    assert len(base64.b64decode(rejection["body_preview_b64"])) == 1024  # the preview cap


def test_understated_content_length_cannot_bypass_the_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(tmp_path, max_body=LIMIT)
    bounded: list[int] = []
    original = app_module._read_bounded

    async def spy(request: Any, limit: int) -> Any:
        result = await original(request, limit)
        bounded.append(len(result[0]))
        return result

    monkeypatch.setattr(app_module, "_read_bounded", spy)
    with client_for(h) as c:
        r = c.post("/v1/samples", content=padded(LIMIT) + b" " * 30000, headers={"content-length": "10"})
    assert r.status_code == 413 and bounded == [LIMIT + 1]  # reading stopped at limit + 1
    (rejection,) = h.rejections()
    assert rejection["body_len"] == LIMIT + 1 and "lower bound" in rejection["reason"]


@pytest.mark.parametrize("value", ["abc", "-1", "1.5", "1e3", " 5", ""])
def test_malformed_content_length_is_ignored_and_the_real_limit_still_applies(tmp_path: Path, value: str) -> None:
    h = Harness(tmp_path, max_body=LIMIT)
    with client_for(h) as c:
        ok = c.post("/v1/samples", content=raw(), headers={"content-length": value})
        big = c.post("/v1/samples", content=padded(LIMIT + 500), headers={"content-length": value})
    assert ok.status_code == 200 and big.status_code == 413  # never 500, never a bypass
    assert "lower bound" in h.rejections()[-1]["reason"]


def test_conflicting_content_length_headers_cannot_bypass_the_limit(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_body=LIMIT)
    with client_for(h) as c:
        r = c.post("/v1/samples", content=padded(LIMIT + 500), headers=[("content-length", "5"), ("content-length", "9")])
    assert r.status_code == 413


def test_a_body_read_in_full_records_its_exact_observed_length(tmp_path: Path) -> None:
    """Over-limit bodies are never read in full by the HTTP layer; a direct service call can have one (D34 B)."""
    h = Harness(tmp_path, max_body=LIMIT)
    data = padded(LIMIT + 7)
    with client_for(h) as c:
        result = c.app.state.service.ingest(data)  # type: ignore[attr-defined]
    assert result.http_status == 413
    (rejection,) = h.rejections()
    assert rejection["body_len"] == LIMIT + 7 and rejection["reason"] == f"body is {LIMIT + 7} bytes (observed in full), the limit is {LIMIT}"


@pytest.mark.parametrize("mode", ["write", "flush", "partial"])
def test_write_and_flush_failures_are_503_unacknowledged_and_the_process_keeps_serving(
    h: Harness, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    faults = Faults(monkeypatch)
    with client_for(h) as c:
        assert c.post("/v1/samples", content=raw(0)).status_code == 200
        before = h.path().read_bytes()
        faults.arm(mode, "r1.jsonl")
        failed = c.post("/v1/samples", content=raw(1))
        assert (failed.status_code, failed.json()) == (503, STORAGE_FAILURE)
        assert h.state().highest_seq == 0  # not applied, not acknowledged
        assert h.path().read_bytes().startswith(before)  # nothing before the failure changed
        assert c.get("/healthz").status_code == 200
        retry = c.post("/v1/samples", content=raw(1))
    assert retry.status_code == 200
    assert retry.json()["status"] == ("duplicate" if mode == "flush" else "stored")  # a flushed-but-unacked line is on disk
    assert h.types().count("run_metadata") == 1  # recovery did not re-announce the run
    if mode == "partial":
        assert "repair" in h.types()


def test_a_file_fsync_failure_under_crash_is_503_and_recovers(h: Harness) -> None:
    with client_for(h) as c:
        c.post("/v1/samples", content=raw(0))
        before = h.path().read_bytes()
        h.ops.fail = True
        failed = c.post("/v1/samples", content=raw(1))
        assert (failed.status_code, failed.json()) == (503, STORAGE_FAILURE) and h.state().highest_seq == 0
        assert h.path().read_bytes().startswith(before)
        still = c.post("/v1/samples", content=raw(1))  # still failing: recovery fsync fails too
        assert still.status_code == 503
        h.ops.fail = False
        retry = c.post("/v1/samples", content=raw(1))
        later = c.post("/v1/samples", content=raw(2))
    assert retry.status_code == 200 and later.json()["status"] == "stored" and h.state().highest_seq == 2


def test_process_durability_does_not_fsync_so_an_fsync_failure_is_not_a_503(tmp_path: Path) -> None:
    h = Harness(tmp_path, durability="process")
    h.ops.fail = True
    with client_for(h) as c:
        assert c.post("/v1/samples", content=raw()).status_code == 200


# --- C. POST /v1/runs/{run_id}/termination ---------------------------------------------------------------


def post_termination(c: TestClient, run_id: str = "r1", content: bytes | None = None) -> Any:
    return c.post(f"/v1/runs/{run_id}/termination", content=json.dumps(TERMINATION).encode() if content is None else content)


def test_termination_is_durable_before_the_response_and_marks_the_run_terminated(h: Harness) -> None:
    with client_for(h) as c:
        c.post("/v1/samples", content=raw())
        r = post_termination(c)
        assert (r.status_code, r.json()) == (200, {"status": "recorded"})
        assert h.types()[-1] == "termination" and h.state().terminated is True  # already on disk when the response arrived
    (record,) = [x for x in h.records() if x["record_type"] == "termination"]
    assert record["source"] == "experiment_runner" and record["container_state"]["exit_code"] == 137


def test_termination_rejections_are_routed_by_what_is_known(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_body=1000)
    with client_for(h) as c:
        c.post("/v1/samples", content=raw())
        bad_id = post_termination(c, "bad id")
        bad_body = post_termination(c, content=json.dumps({"source": "x"}).encode())
        bad_json = post_termination(c, content=b"{")
        dup_keys = post_termination(c, content=b'{"source":"a","source":"b"}')
        big = post_termination(c, content=b"x" * 3000)
    assert [r.status_code for r in (bad_id, bad_body, bad_json, dup_keys, big)] == [422, 422, 422, 422, 413]
    assert dup_keys.json()["reason_code"] == "invalid_json" and bad_json.json()["reason_code"] == "invalid_json"
    (unattributed,) = h.rejections()
    assert unattributed["run_id"] is None and unattributed["reason_code"] == "invalid_run_id"  # URL id invalid: _rejections
    in_run = [x for x in h.records() if x["record_type"] == "rejection"]
    assert [(x["http_status"], x["run_id"]) for x in in_run] == [(422, "r1"), (422, "r1"), (422, "r1"), (413, "r1")]
    assert in_run[-1]["body_len"] == 3000 + 0 and in_run[-1]["body_preview_b64"] == ""  # declared, not read
    assert "declared Content-Length is 3000" in in_run[-1]["reason"]
    assert h.state().terminated is False and "termination" not in h.types()


def test_termination_storage_failure_is_503_and_not_recorded(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    faults = Faults(monkeypatch)
    with client_for(h) as c:
        c.post("/v1/samples", content=raw())
        before = h.path().read_bytes()
        faults.arm("write", "r1.jsonl")
        failed = post_termination(c)
        assert (failed.status_code, failed.json()) == (503, STORAGE_FAILURE)
        assert h.state().terminated is False and h.path().read_bytes().startswith(before)
        assert post_termination(c).json() == {"status": "recorded"}  # keeps serving
    assert h.types().count("termination") == 1


def test_samples_after_termination_are_accepted_but_silence_monitoring_stays_off(h: Harness) -> None:
    with client_for(h) as c:
        c.post("/v1/samples", content=raw(0, "r1"))
        c.post("/v1/samples", content=raw(0, "r2"))
        post_termination(c, "r1")
        h.mono.t += 10.0  # far past 3 x interval
        watchdog = h.app.state.watchdog
        watchdog.check_once()
        assert c.post("/v1/samples", content=raw(1, "r1")).json()["status"] == "stored"
        h.mono.t += 10.0
        watchdog.check_once()
        assert h.state().terminated is True
    gaps = lambda run: [x for x in h.records(run) if x["record_type"] == "gap"]  # noqa: E731
    assert gaps("r1") == []  # terminated: never reactivated
    assert [x["gap_type"] for x in gaps("r2")] == ["silence_timeout"]  # control: an unterminated run does go silent


# --- D. GET /v1/runs/{run_id}/evidence --------------------------------------------------------------------


def test_evidence_returns_the_exact_file_bytes(h: Harness) -> None:
    with client_for(h) as c:
        c.post("/v1/samples", content=raw())
        r = c.get("/v1/runs/r1/evidence")
    assert r.status_code == 200 and r.content == h.path().read_bytes() and r.content.endswith(b"\n")
    assert r.headers["content-type"].split(";")[0] == "application/x-ndjson"


def test_evidence_for_an_unknown_valid_run_is_404_and_creates_nothing(h: Harness) -> None:
    with client_for(h) as c:
        before = sorted(p.name for p in h.directory.iterdir())
        r = c.get("/v1/runs/nope/evidence")
        assert (r.status_code, r.json()) == (404, {"error": "not_found"})
        assert sorted(p.name for p in h.directory.iterdir()) == before
        assert h.app.state.registry.states() == []


@pytest.mark.parametrize(
    "segment", ["a%2Fb", "..%2Fx", "%2e%2e", "%2E%2E%2Fx", "a/b", "x/y/z", "_rejections", ".hidden", "a%20b", "x" * 65, "a%5Cb"]
)
def test_evidence_invalid_run_ids_are_422_and_never_serve_a_file(h: Harness, segment: str) -> None:
    with client_for(h) as c:
        (h.directory / "_rejections.jsonl").write_bytes(b"secret\n")
        r = c.get(f"/v1/runs/{segment}/evidence")
    assert r.status_code == 422 and r.json()["error"] == "validation_failed" and r.json()["field"] == "run_id"
    assert b"secret" not in r.content


@pytest.mark.parametrize("path", ["/v1/runs/../evidence", "/v1/runs/../x/evidence", "/v1/nothing", "/v1/runs/r1", "/"])
def test_unknown_endpoints_and_normalised_traversal_are_404(h: Harness, path: str) -> None:
    with client_for(h) as c:
        r = c.get(path)
    assert r.status_code in (404, 405) and r.status_code != 200


def test_unknown_endpoint_is_404_and_wrong_method_is_not_a_success(h: Harness) -> None:
    with client_for(h) as c:
        assert c.get("/v1/nothing").status_code == 404
        assert c.post("/healthz").status_code == 405
        assert c.get("/v1/samples").status_code == 405


def test_evidence_read_changes_nothing_and_does_not_repair_a_damaged_tail(h: Harness) -> None:
    with client_for(h) as c:
        c.post("/v1/samples", content=raw())
        damaged = h.directory / "r2.jsonl"
        damaged.write_bytes(b'{"record_type":"sample","sch')  # unterminated last line, created after startup recovery
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in h.directory.iterdir() if p.is_file()}
        r = c.get("/v1/runs/r2/evidence")
        assert (r.status_code, r.content) == (200, b'{"record_type":"sample","sch')
        c.get("/v1/runs/r1/evidence")
        c.get("/v1/runs/missing/evidence")
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in h.directory.iterdir() if p.is_file()}
    assert after == before
    states = {s.run_id: s for s in h.app.state.registry.states()}
    assert "missing" not in states and states["r2"].loaded is False and states["r2"].highest_seq is None  # only an unloaded lock holder


def test_a_read_during_an_append_returns_a_complete_snapshot(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    in_write, release = threading.Event(), threading.Event()
    gate = {"armed": False}

    class Gated:
        def __init__(self, real: Any) -> None:
            self._real = real

        def __enter__(self) -> "Gated":
            return self

        def __exit__(self, *exc: object) -> None:
            self._real.close()

        def write(self, data: bytes) -> int:
            if gate["armed"]:
                gate["armed"] = False
                self._real.write(data[: len(data) // 2])  # a torn line is on disk while the lock is held
                self._real.flush()
                in_write.set()
                assert release.wait(10)
                return self._real.write(data[len(data) // 2 :]) + len(data) // 2
            return self._real.write(data)

        def flush(self) -> None:
            self._real.flush()

        def fileno(self) -> int:
            return self._real.fileno()

    monkeypatch.setattr(writer_module, "open", lambda path, mode: Gated(builtins.open(path, mode)), raising=False)
    result: dict[str, Any] = {}
    with client_for(h) as c:
        c.post("/v1/samples", content=raw(0))
        before = h.path().read_bytes()
        gate["armed"] = True
        service = h.app.state.service
        writer = threading.Thread(target=lambda: result.update(ingest=service.ingest(raw(1))))
        writer.start()
        assert in_write.wait(10)
        assert not h.path().read_bytes().endswith(b"\n")  # the file really is torn right now
        reader = threading.Thread(target=lambda: result.update(get=c.get("/v1/runs/r1/evidence")))
        reader.start()
        reader.join(0.3)
        assert reader.is_alive()  # the read waits for the run lock instead of returning the torn line
        release.set()
        writer.join(10), reader.join(10)
        final = h.path().read_bytes()
    assert result["ingest"].http_status == 200
    assert result["get"].status_code == 200 and result["get"].content == final and final.endswith(b"\n") and final.startswith(before)


# --- startup, shutdown and isolation ----------------------------------------------------------------------


def test_create_app_has_no_filesystem_side_effects(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    assert list(tmp_path.iterdir()) == []
    with client_for(h):
        assert h.directory.is_dir()


def test_startup_order_and_readiness(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[tuple[str, bool]] = []
    app = h.app

    def spy(owner: Any, name: str, label: str) -> None:
        original = getattr(owner, name)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            order.append((label, app.state.ready))
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, wrapper)

    spy(app.state.registry, "start", "registry.start")
    spy(app.state.probe, "run_startup", "probe.run_startup")
    spy(app.state.watchdog, "start", "watchdog.start")
    with client_for(h) as c:
        assert app.state.ready is True
        assert c.post("/v1/samples", content=raw()).status_code == 200
    assert order == [("registry.start", False), ("probe.run_startup", False), ("watchdog.start", False)]
    assert app.state.ready is False


def test_the_startup_probe_is_forced_even_when_a_fresh_failed_result_is_cached(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    assert h.probe.check().ok is False  # the evidence directory does not exist yet: a fresh, failed cached result
    with client_for(h) as c:
        assert c.get("/healthz").json()["storage_ok"] is True  # no clock advance: only a forced probe can explain this


def test_recovery_runs_before_any_request_and_blocks_requests_until_then(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.directory.mkdir()
    h.path().write_bytes(b'{"record_type":"sample","sch')
    client = TestClient(h.app)  # lifespan not started: not ready
    assert client.post("/v1/samples", content=raw()).status_code == 503
    assert client.post("/v1/samples", content=raw()).json() == STORAGE_FAILURE
    assert client.get("/v1/runs/r1/evidence").status_code == 503
    assert h.path().read_bytes() == b'{"record_type":"sample","sch'  # untouched: nothing was processed
    with client:
        assert b'"record_type":"repair"' in h.path().read_bytes().replace(b" ", b"")  # repaired before the first request
        assert client.post("/v1/samples", content=raw()).status_code == 200


class LockSpy:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.held = False

    def acquire(self) -> None:
        self.events.append("acquire")
        self.held = True

    def release(self) -> None:
        self.events.append("release")
        self.held = False


def test_shutdown_stops_the_watchdog_then_releases_the_lock_and_keeps_evidence(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    lock = LockSpy()
    app = create_app(h.config, now=h.wall.now, monotonic=h.mono, probe=h.probe, volume_lock=lock)  # type: ignore[arg-type]
    with TestClient(app) as c:
        c.post("/v1/samples", content=raw())
        assert lock.held and app.state.watchdog.running
    assert lock.events == ["acquire", "release"] and not app.state.watchdog.running and not app.state.registry.started
    assert h.path().is_file()


def test_http_cannot_bypass_service_recovery_of_a_dirty_run(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    faults = Faults(monkeypatch)
    with client_for(h) as c:
        c.post("/v1/samples", content=raw(0))
        faults.arm("partial", "r1.jsonl")
        assert c.post("/v1/samples", content=raw(1)).status_code == 503
        assert h.state().dirty is True
        torn = h.path().read_bytes()
        assert not torn.endswith(b"\n")
        assert c.post("/v1/samples", content=raw(1)).status_code == 200  # the service repaired the tail first
    lines = h.path().read_bytes().split(b"\n")
    assert h.types().count("repair") == 1 and all(x for x in lines[:-1] if x) and h.path().read_bytes().endswith(b"\n")


def test_every_route_calls_a_service_method_and_never_the_writer(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    service = h.app.state.service
    for name in ("ingest", "record_termination"):
        original = getattr(service, name)
        monkeypatch.setattr(service, name, lambda *a, _o=original, _n=name, **k: (called.append(_n), _o(*a, **k))[1])
    with client_for(h) as c:
        c.post("/v1/samples", content=raw())
        post_termination(c)
    assert called == ["ingest", "record_termination"]
