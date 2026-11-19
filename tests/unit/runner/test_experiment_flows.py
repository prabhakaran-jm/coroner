"""Scripted experiment-flow tests (task 6.17).

Requirements 4.16, 7.4, 8.7, 8.14, 9.1 to 9.12, 10.1 to 10.13, 11.1 to 11.20, 12.1, 12.7; design "Experiment flows" and
"Error Handling".

Everything runs against an in-memory `World`: a fake clock (the fake `sleep` advances it, so a 60 s wait costs no real
time), a fake `DockerPort` that records every call, and one fake HTTP transport that plays both the Collector and the
Demo_App. The world generates the evidence file (run_metadata plus Samples) from its own timeline, so Capture_Time,
Receive_Time, the leak ledger and the container exit all stay consistent. No Docker, socket, subprocess or real sleep is
used (an autouse fixture makes any of them fail the test). Run directories live under `tmp_path`, never the repo `runs/`.

Negative controls (`TestNegativeControls`) run deliberately broken in-memory variants of the flows and show that the
shared assertion helpers reject them.
"""
from __future__ import annotations

import json
import socket
import subprocess
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from coroner.common.timeutil import format_utc_ms, parse_utc_ms
from coroner.runner import evaluate as ev
from coroner.runner.collector_client import STORAGE_UNHEALTHY_MESSAGE, CollectorClient, HttpResponse
from coroner.runner.docker_port import DockerCommandError, DockerOutputError, InspectResult
from coroner.runner.evaluate import EXPECTED_STATE, Check, Evaluation, parse_mem_size
from coroner.runner.experiments import base, exp_a, exp_b, exp_c
from coroner.runner.manifest import generate_run_id, read_manifest

MIB = 1024 * 1024
T0 = 1000.0
EPOCH = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
CID = "ab" * 32
COLLECTOR = "http://collector.test"
DEMO = "http://demo.test"
BASE_RSS = 20 * MIB
RECV_LAG = 0.02
MEM_LIMIT = "32m"
RUNNERS = {"A": exp_a.run, "B": exp_b.run, "C": exp_c.run}
ALL = ["A", "B", "C"]
A_DESCRIPTION = "controlled fatal process exit via os._exit(1)"


# ------------------------------------------------------------------------------------------------ the fake world


class World:
    """Fake clock, Docker, Collector and Demo_App in one place. Knobs are plain attributes set by keyword."""

    def __init__(self, runs_dir: Path, experiment: str, **knobs: Any) -> None:
        self.runs_dir = runs_dir
        self.experiment = experiment
        self.t = T0
        self.offset_ms = 0
        self.durability = "process"
        self.evidence_durability: str | None = None
        self.health_mode = "ok"  # ok storage_false storage_missing malformed down status503 no_server_time
        self.health_after_exit: str | None = None
        self.flaky_health_after_exit = 0  # this many calls fail (connection error) after the exit, then recover
        self.docker_available: Any = True  # True, False, or "raises"
        self.compose_error = False
        self.cid_mode = "ok"  # ok short upper error
        self.demo_health_mode = "ok"  # ok 503 down
        self.evidence_mode = "ok"  # ok no_samples exists_precheck fail_after_exit down_after_exit
        self.leak_mode = "ok"  # ok http500 drop slow
        self.leak_fail_from = 0
        self.sample_interval = 0.5
        self.rss_mode = "grow"  # grow flat null
        self.crash_mode = "exit"  # exit (drops the connection) ignore (HTTP 200) status (HTTP crash_status)
        self.crash_status = 200
        self.crash_body = b'{"status":"ignored"}'
        self.crash_status_exits = False  # with crash_mode "status": the container is already gone when inspected
        self.finished_mode = "ok"  # ok missing zero bad (State.FinishedAt once exited)
        self.leak_oom_drop = False  # C: the leak request that triggers the OOM kill loses its connection
        self.exit_override: tuple[int, bool] | None = None
        self.exit_delay: float | None = 0.2  # A: after /crash; B: after kill. None: the container never stops
        self.oom_after_bytes: int | None = None  # C: retained bytes at which the kernel kills the container
        self.inspect_mode = "ok"  # ok error incomplete incomplete_after_exit never_running
        self.kill_error = False
        self.termination_status = 200
        self.rand_values: list[str] = []
        self.extra_samples_rel_kill: list[tuple[float, float, int | None]] = []  # (capture, receive, rss) vs kill
        self.max_t = T0 + 600
        for key, value in knobs.items():
            if not hasattr(self, key):
                raise AttributeError(f"unknown World knob {key!r}")
            setattr(self, key, value)
        if experiment == "C" and self.oom_after_bytes is None and "oom_after_bytes" not in knobs:
            self.oom_after_bytes = parse_mem_size(MEM_LIMIT)
        self.host_memory = parse_mem_size(MEM_LIMIT)
        # state
        self.run_id: str | None = None
        self.demo_started = False
        self.demo_start = 0.0
        self.exit_at: float | None = None
        self.kill_t: float | None = None
        self.retained = 0
        self.leak_log: list[tuple[float, int]] = []
        self.leak_calls: list[tuple[float, int]] = []
        self.crash_calls: list[float] = []
        self.kill_calls: list[tuple[float, str, str]] = []
        self.compose_calls: list[dict[str, Any]] = []
        self.container_id_calls = 0
        self.inspect_calls: list[tuple[float, str]] = []
        self.inspect_raw: list[bytes] = []
        self.health_calls: list[tuple[float, str]] = []
        self.demo_health_calls = 0
        self.evidence_calls: list[tuple[float, int]] = []
        self.terminations: list[dict[str, Any]] = []
        self.sleeps: list[float] = []
        self.flaky_used = 0
        self._rand_counter = 0
        self.docker = FakeDocker(self)

    # ---- clock
    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += max(0.0, seconds)
        if self.t > self.max_t:
            raise AssertionError("runaway fake clock: the flow does not terminate")

    def host_dt(self, t: float) -> datetime:
        return EPOCH + timedelta(seconds=t - T0)

    def wall(self) -> datetime:
        return self.host_dt(self.t)

    def collector_dt(self, t: float) -> datetime:
        return self.host_dt(t) + timedelta(milliseconds=self.offset_ms)

    def rand_hex(self) -> str:
        if self.rand_values:
            return self.rand_values.pop(0)
        self._rand_counter += 1
        return f"{0xA0 + self._rand_counter:08x}"

    # ---- container
    @property
    def exited(self) -> bool:
        return self.exit_at is not None and self.t >= self.exit_at

    def schedule_exit(self, delay: float | None) -> None:
        if delay is not None and self.exit_at is None:
            self.exit_at = self.t + delay

    def actual_state(self) -> tuple[int, bool]:
        return self.exit_override if self.exit_override is not None else EXPECTED_STATE[self.experiment]

    # ---- evidence
    def _line(self, record: dict[str, Any]) -> bytes:
        return json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n"

    def retained_at(self, host_t: float) -> int:
        return sum(size for when, size in self.leak_log if when <= host_t)

    def _sample_record(self, seq: int, capture_host: float, recv_host: float, rss: int | None) -> dict[str, Any]:
        sample: dict[str, Any] = {
            "run_id": self.run_id,
            "seq": seq,
            "ts": format_utc_ms(self.collector_dt(capture_host)),
            "rss_bytes": rss,
            "request_count": 0,
            "loop_delay_ms": 1.0,
            "pid": 1,
            "container_id": CID,
            "container_id_source": "mountinfo",
            "interval_ms": int(self.sample_interval * 1000),
            "dropped": 0,
        }
        if rss is None:
            sample["null_reasons"] = {"rss_bytes": "unreadable in this scripted run"}
        return {
            "record_type": "sample",
            "schema_version": 1,
            "run_id": self.run_id,
            "recv_time": format_utc_ms(self.collector_dt(recv_host)),
            "sample": sample,
        }

    def evidence_bytes(self) -> bytes:
        meta = {
            "record_type": "run_metadata",
            "schema_version": 1,
            "run_id": self.run_id,
            "recv_time": format_utc_ms(self.collector_dt(self.demo_start)),
            "event": "created",
            "durability": self.evidence_durability or self.durability,
            "fsync_before_ack": (self.evidence_durability or self.durability) == "crash",
            "evidence_file": f"{self.run_id}.jsonl",
            "collector_version": "scripted",
        }
        out = [self._line(meta)]
        if self.evidence_mode == "no_samples":
            return b"".join(out)
        end = self.exit_at if self.exit_at is not None else float("inf")
        if self.kill_t is not None:
            end = min(end, self.kill_t + 0.1)  # the process dies at once; only Docker's state may lag behind
        records: list[tuple[float, dict[str, Any]]] = []
        i = 0
        while True:
            host = self.demo_start + 1.0 + i * self.sample_interval
            if host >= end or host + RECV_LAG > self.t:
                break
            if self.rss_mode == "grow":
                rss: int | None = BASE_RSS + self.retained_at(host)
            elif self.rss_mode == "flat":
                rss = BASE_RSS
            else:
                rss = None
            records.append((host + RECV_LAG, self._sample_record(i, host, host + RECV_LAG, rss)))
            i += 1
        if self.kill_t is not None:
            for k, (capture, receive, rss) in enumerate(self.extra_samples_rel_kill):
                if self.kill_t + receive <= self.t:
                    records.append(
                        (self.kill_t + receive, self._sample_record(10_000 + k, self.kill_t + capture, self.kill_t + receive, rss))
                    )
        for _, record in sorted(records, key=lambda pair: pair[0]):
            out.append(self._line(record))
        return b"".join(out)

    # ---- inspect
    def inspect_object(self) -> dict[str, Any]:
        if self.inspect_mode == "never_running":
            status = "created"
        else:
            status = "exited" if self.exited else "running"
        if self.exited:
            code, oom = self.actual_state()
            finished = self.host_dt(self.exit_at).strftime("%Y-%m-%dT%H:%M:%S.%fZ")  # type: ignore[arg-type]
        else:
            code, oom, finished = 0, False, "0001-01-01T00:00:00Z"
        state: dict[str, Any] = {"Status": status, "OOMKilled": oom, "ExitCode": code, "FinishedAt": finished}
        if self.exited and self.finished_mode == "missing":
            del state["FinishedAt"]
        elif self.exited and self.finished_mode == "zero":
            state["FinishedAt"] = "0001-01-01T00:00:00Z"
        elif self.exited and self.finished_mode == "bad":
            state["FinishedAt"] = "not-a-timestamp"
        if self.inspect_mode == "incomplete" or (self.inspect_mode == "incomplete_after_exit" and self.exited):
            del state["ExitCode"]
        return {"Id": CID, "State": state, "HostConfig": {"Memory": self.host_memory}}

    # ---- transport (Collector and Demo_App)
    def transport(self, method: str, url: str, body: bytes | None, timeout_s: float) -> HttpResponse:
        if url.startswith(COLLECTOR):
            return self._collector(method, url[len(COLLECTOR):], body)
        if url.startswith(DEMO):
            return self._demo(method, url[len(DEMO):])
        raise AssertionError(f"unexpected URL {url}")

    def _json(self, status: int, obj: Any) -> HttpResponse:
        return HttpResponse(status, json.dumps(obj).encode("utf-8"))

    def _collector(self, method: str, path: str, body: bytes | None) -> HttpResponse:
        if path == "/healthz":
            mode = self.health_mode
            if self.exited and self.health_after_exit is not None:
                mode = self.health_after_exit
            if self.exited and self.flaky_used < self.flaky_health_after_exit:
                self.flaky_used += 1
                mode = "down"
            self.health_calls.append((self.t, mode))
            if mode == "down":
                raise ConnectionRefusedError("collector down")
            if mode == "status503":
                return HttpResponse(503, b"{}")
            if mode == "malformed":
                return HttpResponse(200, b"{not json")
            payload: dict[str, Any] = {
                "status": "ok",
                "storage_ok": True,
                "durability": self.durability,
                "server_time": format_utc_ms(self.collector_dt(self.t)),
            }
            if mode == "storage_false":
                payload["storage_ok"] = False
            elif mode == "storage_missing":
                del payload["storage_ok"]
            elif mode == "no_server_time":
                del payload["server_time"]
            return self._json(200, payload)
        if path.startswith("/v1/runs/") and path.endswith("/evidence") and method == "GET":
            if not self.demo_started and self.evidence_mode != "exists_precheck":
                self.evidence_calls.append((self.t, 404))
                return HttpResponse(404, b"")
            if self.evidence_mode == "exists_precheck" and not self.demo_started:
                self.evidence_calls.append((self.t, 200))
                return HttpResponse(200, b"")
            if self.exited and self.evidence_mode == "fail_after_exit":
                self.evidence_calls.append((self.t, 500))
                return HttpResponse(500, b"storage failure")
            if self.exited and self.evidence_mode == "down_after_exit":
                raise ConnectionResetError("collector reset")
            self.evidence_calls.append((self.t, 200))
            return HttpResponse(200, self.evidence_bytes())
        if path.startswith("/v1/runs/") and path.endswith("/termination") and method == "POST":
            assert body is not None
            self.terminations.append(json.loads(body.decode("utf-8")))
            return self._json(self.termination_status, {"stored": self.termination_status == 200})
        raise AssertionError(f"unexpected Collector request {method} {path}")

    def _demo(self, method: str, path: str) -> HttpResponse:
        if path == "/health" and method == "GET":
            self.demo_health_calls += 1
            if not self.demo_started or self.demo_health_mode == "down":
                raise ConnectionRefusedError("demo down")
            if self.demo_health_mode == "503":
                return HttpResponse(503, b"{}")
            return self._json(200, {"status": "ok", "run_id": self.run_id})
        if path.startswith("/leak?bytes=") and method == "POST":
            size = int(path.split("=", 1)[1])
            index = len(self.leak_calls)
            self.leak_calls.append((self.t, size))
            if self.exited or not self.demo_started:
                raise ConnectionRefusedError("container stopped")
            if self.leak_mode != "ok" and index >= self.leak_fail_from:
                if self.leak_mode == "http500":
                    return self._json(500, {"error": "boom"})
                if self.leak_mode == "drop":
                    raise ConnectionResetError("leak connection dropped")
                if self.leak_mode == "slow":
                    self.t += 6.0
            self.retained += size
            self.leak_log.append((self.t, size))
            if self.oom_after_bytes is not None and self.retained >= self.oom_after_bytes:
                self.schedule_exit(0.3)
                if self.leak_oom_drop:
                    raise ConnectionResetError("container killed during the leak request")
            return self._json(200, {"retained_bytes_total": self.retained})
        if path == "/crash" and method == "POST":
            self.crash_calls.append(self.t)
            if self.crash_mode == "ignore":
                return self._json(200, {"status": "ignored"})
            if self.crash_mode == "status":
                if self.crash_status_exits:
                    self.kill_t = self.t
                    self.schedule_exit(0.0)
                return HttpResponse(self.crash_status, self.crash_body)
            self.kill_t = self.t
            self.schedule_exit(self.exit_delay)
            raise ConnectionResetError("process exited during the request")
        raise AssertionError(f"unexpected Demo_App request {method} {path}")


class FakeDocker:
    """Records every DockerPort call; behaviour comes from the world."""

    def __init__(self, world: World) -> None:
        self.w = world

    def available(self) -> bool:
        if self.w.docker_available == "raises":
            raise OSError("docker probe blew up")
        return bool(self.w.docker_available)

    def compose_up(self, service: str, *, env: Any, no_deps: bool, recreate: bool) -> None:
        self.w.compose_calls.append(
            {"t": self.w.t, "service": service, "env": dict(env), "no_deps": no_deps, "recreate": recreate}
        )
        if self.w.compose_error:
            raise DockerCommandError(["docker", "compose", "up"], 1, "service demo_app failed")
        self.w.run_id = env["CORONER_RUN_ID"]
        self.w.demo_started = True
        self.w.demo_start = self.w.t

    def container_id(self, service: str) -> str:
        self.w.container_id_calls += 1
        if self.w.cid_mode == "short":
            return "abc123"
        if self.w.cid_mode == "upper":
            return CID.upper()
        if self.w.cid_mode == "error":
            raise DockerOutputError("docker compose ps returned nothing")
        return CID

    def inspect(self, container_id: str) -> InspectResult:
        self.w.inspect_calls.append((self.w.t, container_id))
        if self.w.inspect_mode == "error":
            raise DockerCommandError(["docker", "inspect", container_id], 1, "No such object")
        raw = json.dumps([self.w.inspect_object()], indent=1).encode("utf-8")
        self.w.inspect_raw.append(raw)
        return InspectResult(raw=raw, parsed=json.loads(raw), inspect_time=self.w.wall())

    def kill(self, container_id: str, signal: str) -> None:
        self.w.kill_calls.append((self.w.t, container_id, signal))
        if self.w.kill_error:
            raise DockerCommandError(["docker", "kill", container_id], 1, "cannot kill")
        self.w.kill_t = self.w.t
        self.w.schedule_exit(self.w.exit_delay)


# ------------------------------------------------------------------------------------------------ helpers


@dataclass
class Run:
    result: base.ExperimentResult
    ctx: base.ExperimentContext
    world: World
    started: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def manifest_path(self) -> Path:
        assert self.ctx.run_id is not None
        return self.world.runs_dir / self.ctx.run_id / "manifest.json"

    def manifest(self):
        return read_manifest(self.manifest_path)

    def checks(self) -> dict[str, dict[str, Any]]:
        return {c["name"]: c for c in self.manifest().extra["checks"]}


def make_ctx(world: World, experiment: str, **overrides: Any) -> base.ExperimentContext:
    collector = CollectorClient(COLLECTOR, transport=world.transport, wall_clock=world.wall)
    kwargs: dict[str, Any] = dict(
        experiment=experiment,
        docker=world.docker,
        collector=collector,
        runs_dir=world.runs_dir,
        mem_limit=MEM_LIMIT,
        leak_chunk_bytes=4 * MIB,
        demo_url=DEMO,
        http=world.transport,
        wall_clock=world.wall,
        monotonic=world.monotonic,
        sleep=world.sleep,
        rand_hex=world.rand_hex,
    )
    kwargs.update(overrides)
    return base.ExperimentContext(**kwargs)


def run_exp(tmp_path: Path, experiment: str, ctx_kw: dict[str, Any] | None = None, **knobs: Any) -> Run:
    world = World(tmp_path / "runs", experiment, **knobs)
    ctx = make_ctx(world, experiment, **(ctx_kw or {}))
    started = world.t
    result = RUNNERS[experiment](ctx)
    return Run(result, ctx, world, started)


def assert_no_crash_no_kill(world: World) -> None:
    assert world.crash_calls == [], f"/crash was POSTed: {world.crash_calls}"
    assert world.kill_calls == [], f"docker kill was called: {world.kill_calls}"


def assert_failure(
    run: Run, code: str, *, exit_code: int | None = None, outcome: str | None = None, message_has: str | None = None
) -> Any:
    """Exit code, failure code and message per FAILURE_TABLE, and a readable manifest that agrees with the result."""
    table_outcome, table_exit = base.FAILURE_TABLE[code] if code in base.FAILURE_TABLE else ("failed", 1)
    result = run.result
    assert result.exit_code == (table_exit if exit_code is None else exit_code), result.message
    assert result.failure is not None and result.failure.code == code, (result.failure, result.message)
    assert result.failure.message
    assert result.outcome == (table_outcome if outcome is None else outcome)
    if message_has is not None:
        assert message_has in result.failure.message
    assert run.ctx.run_id is not None, "a run id should exist"
    assert result.run_id == run.ctx.run_id
    assert result.manifest_path == run.manifest_path
    manifest = run.manifest()
    assert manifest.outcome == result.outcome
    assert manifest.failure is not None
    assert (manifest.failure.code, manifest.failure.message) == (result.failure.code, result.failure.message)
    assert manifest.extra["finished"] is True
    assert set(manifest.extra) == set(base.MANIFEST_EXTRA_KEYS)
    return manifest


def assert_pre_run_failure(run: Run, code: str, *, exit_code: int = 2) -> None:
    result = run.result
    assert result.exit_code == exit_code
    assert result.outcome == "not_started"
    assert result.failure is not None and result.failure.code == code
    assert result.run_id is None and result.manifest_path is None
    assert run.ctx.run_id is None


def assert_passed(run: Run) -> Any:
    result = run.result
    assert result.exit_code == 0, result.message
    assert result.outcome == "passed" and result.failure is None
    manifest = run.manifest()
    assert manifest.outcome == "passed" and manifest.failure is None
    assert manifest.extra["finished"] is True
    assert set(manifest.extra) == set(base.MANIFEST_EXTRA_KEYS)
    assert all(c["passed"] for c in manifest.extra["checks"]), manifest.extra["checks"]
    return manifest


def elapsed(run: Run) -> float:
    return run.world.t - run.started


def snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture(autouse=True)
def _no_real_world(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a real sleep, socket or subprocess was used")

    monkeypatch.setattr(time, "sleep", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


# ------------------------------------------------------------------------------------------------ success flows


class TestSuccessFlows:
    def test_experiment_a_passes_with_exit_code_1(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "A")
        manifest = assert_passed(run)
        world = run.world
        assert run.result.line.startswith(f"Experiment A {run.ctx.run_id}: PASSED")
        state = manifest.container_state
        assert state is not None
        assert (state.exit_code, state.oom_killed, state.status, state.container_id) == (1, False, "exited", CID)
        assert state.host_config_memory_bytes == 32 * MIB
        assert (manifest.expected.exit_code, manifest.expected.oom_killed) == (1, False)
        # one /crash POST, no kill, and Docker was only inspected after the crash request
        assert len(world.crash_calls) == 1 and world.kill_calls == []
        assert all(t >= world.crash_calls[0] for t, _ in world.inspect_calls)
        # the compose call used the run id and the overrides
        up = world.compose_calls[0]
        assert up["service"] == "demo_app" and up["no_deps"] is True and up["recreate"] is True
        assert up["env"]["CORONER_RUN_ID"] == run.ctx.run_id
        assert up["env"]["CORONER_DEMO_MEM_LIMIT"] == MEM_LIMIT
        assert up["env"]["CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS"] == "1"
        # leak of at least 5 s, recorded, and the crash time recorded between the leak and the request
        leak = manifest.extra["leak"]
        assert leak["reason"] == "complete" and leak["duration_s"] >= 5.0
        assert leak["cumulative_bytes"] == leak["successful_requests"] * 4 * MIB
        crash_time = manifest.extra["times"]["crash_request_time"]
        assert crash_time is not None and manifest.extra["times"]["sigkill_sent_time"] is None
        assert crash_time >= leak["last_response_time"]
        # the raw inspect output is saved unchanged, and so is the evidence copy
        assert (run.world.runs_dir / run.ctx.run_id / "docker_inspect.json").read_bytes() == world.inspect_raw[-1]
        assert manifest.evidence is not None and manifest.evidence.record_count >= 2
        copy = Path(manifest.evidence.host_copy_path).read_bytes()
        assert copy.startswith(b'{"record_type":"run_metadata"')
        assert manifest.extra["sample_timing"] is None and manifest.extra["memory"] is None
        assert manifest.extra["diagnostics"] is None and manifest.extra["sampling_limitations"] is None
        assert manifest.extra["container_left_running"] is False
        names = set(run.checks())
        assert {
            "exit_code",
            "oom_killed",
            "status",
            "collector_survival",
            "at_least_one_sample_before_crash_request",
            "termination_recorded",
            "durability_matches_run_metadata",
            "memory_growth_matches_leak",
        } <= names

    def test_experiment_a_records_highest_rss(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "A")
        manifest = assert_passed(run)
        assert manifest.extra["highest_rss_bytes"] >= BASE_RSS + manifest.extra["leak"]["cumulative_bytes"]

    def test_experiment_a_wording_never_says_exception(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "A")
        manifest = assert_passed(run)
        assert manifest.termination_description == A_DESCRIPTION
        assert base.TERMINATION_DESCRIPTIONS["A"] == A_DESCRIPTION
        posted = run.world.terminations[0]
        texts = [manifest.termination_description, json.dumps(manifest.extra["termination"]), json.dumps(posted)]
        assert all("exception" not in text.lower() for text in texts)
        assert posted["cause_label"] == "controlled_fatal_process_exit"
        assert posted["container_state"] == {"status": "exited", "exit_code": 1, "oom_killed": False}
        assert posted["source"] == "experiment_runner"

    def test_experiment_a_failure_manifest_keeps_the_wording(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "A", leak_mode="http500")
        manifest = assert_failure(run, "leak_failed")
        assert manifest.termination_description == A_DESCRIPTION

    def test_experiment_b_passes_with_kill_then_137(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "B", exit_delay=0.1)
        manifest = assert_passed(run)
        world = run.world
        assert world.kill_calls == [(world.kill_calls[0][0], CID, "KILL")]
        assert len(world.kill_calls[0][1]) == 64
        assert world.crash_calls == []
        state = manifest.container_state
        assert state is not None and (state.exit_code, state.oom_killed, state.status) == (137, False, "exited")
        assert (manifest.expected.exit_code, manifest.expected.oom_killed) == (137, False)
        sent = manifest.extra["times"]["sigkill_sent_time"]
        assert sent is not None and manifest.extra["times"]["crash_request_time"] is None
        assert sent >= manifest.extra["leak"]["last_response_time"]
        timing = manifest.extra["sample_timing"]
        assert timing is not None and timing["tolerance_ms"] == 250 and timing["pre_kill_count"] >= 1
        assert timing["post_kill_beyond_tolerance"] == [] and timing["clock_offset_ms"] == 0
        assert manifest.extra["container_finished_at"] is not None
        assert timing["container_finished_at"] == manifest.extra["container_finished_at"]
        assert manifest.extra["memory"] is None and manifest.extra["diagnostics"] is None
        assert manifest.extra["sampling_limitations"] is None
        leak_bytes = manifest.extra["leak"]["cumulative_bytes"]
        assert manifest.extra["highest_rss_bytes"] >= BASE_RSS + leak_bytes
        names = set(run.checks())
        assert {
            "exit_code_137",
            "oom_killed_false",
            "stopped_within_10s",
            "collector_survival",
            "at_least_one_sample_captured_before_kill",
            "no_sample_captured_after_kill_beyond_tolerance",
            "no_shutdown_handler_records",
            "termination_recorded",
            "durability_matches_run_metadata",
            "memory_growth_matches_leak",
        } <= names
        assert "container_finished_at_recorded" not in names
        assert timing["after_container_stop_analysis"] == "available"
        assert world.terminations[0]["cause_label"] == "sigkill_sent_by_runner"
        assert manifest.termination_description == base.TERMINATION_DESCRIPTIONS["B"]

    def test_experiment_c_passes_with_a_genuine_oom_and_no_kill_or_crash(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "C")
        manifest = assert_passed(run)
        world = run.world
        assert_no_crash_no_kill(world)
        state = manifest.container_state
        assert state is not None and (state.exit_code, state.oom_killed, state.status) == (137, True, "exited")
        assert state.container_id == CID and state.host_config_memory_bytes == 32 * MIB
        assert state.raw_inspect_excerpt.memory == 32 * MIB
        assert (manifest.expected.exit_code, manifest.expected.oom_killed) == (137, True)
        assert (run.world.runs_dir / run.ctx.run_id / "docker_inspect.json").read_bytes() == world.inspect_raw[-1]
        memory = manifest.extra["memory"]
        assert memory["configured_limit_bytes"] == memory["memory_limit_bytes"] == 32 * MIB
        assert memory["highest_rss_bytes"] == manifest.extra["highest_rss_bytes"] > memory["first_rss_bytes"]
        diag = manifest.extra["diagnostics"]
        assert diag is not None and diag["sample_count"] >= 1 and "rss_monotonic_tail" in diag
        limits = manifest.extra["sampling_limitations"]
        assert limits is not None and len(limits["statements"]) == 3 and limits["sample_interval_ms"] == 500
        assert manifest.extra["container_left_running"] is False
        assert manifest.extra["sample_timing"] is None
        assert {
            "oom_killed_true",
            "exit_code_137",
            "collector_survival",
            "preserved_precrash_evidence",
            "measurable_memory_growth",
            "docker_inspect_recorded",
            "memory_limit_matches_host_config",
            "durability_matches_run_metadata",
            "termination_recorded",
        } <= set(run.checks())
        assert world.terminations[0]["cause_label"] == "docker_oom_kill"
        # leak chunks were 4 MiB and at most 1 s apart
        times = [t for t, _ in world.leak_calls]
        assert all(size == 4 * MIB for _, size in world.leak_calls)
        assert all(b - a <= 1.0 + 1e-9 for a, b in zip(times, times[1:]))
        assert manifest.extra["leak"]["reason"] == "stopped"

    @pytest.mark.parametrize("experiment", ALL)
    def test_manifests_are_structurally_compatible(self, tmp_path: Path, experiment: str) -> None:
        manifests = {}
        for exp in ALL:
            run = run_exp(tmp_path / exp, exp)
            manifests[exp] = assert_passed(run)
        reference = manifests["A"]
        mine = manifests[experiment]
        assert set(mine.to_dict()) == set(reference.to_dict())
        assert set(mine.extra) == set(base.MANIFEST_EXTRA_KEYS)
        assert set(mine.extra["times"]) == set(reference.extra["times"]) == {"crash_request_time", "sigkill_sent_time"}
        assert set(mine.extra["leak"]) == set(reference.extra["leak"])
        assert set(mine.extra["collector_health"]) == set(reference.extra["collector_health"])
        assert set(mine.extra["termination"]) == set(reference.extra["termination"])
        assert set(mine.extra["evidence_summary"]) == set(reference.extra["evidence_summary"])
        assert set(mine.extra["collector"]) == set(reference.extra["collector"])
        assert mine.container_state.to_dict().keys() == reference.container_state.to_dict().keys()
        assert mine.evidence.to_dict().keys() == reference.evidence.to_dict().keys()
        assert mine.schema_version == reference.schema_version == 1
        assert mine.extra["collector_health"]["ok"] is True and mine.extra["collector_health"]["storage_ok"] is True
        assert mine.extra["collector_health"]["http_status"] == 200
        assert mine.extra["clock_offset_ms"] == 0 and mine.extra["phase"] == "finished"
        assert mine.extra["container_id"] == CID
        for item in mine.extra["checks"]:
            assert set(item) <= {"name", "passed", "detail"} and item["passed"] is True
        # only the experiment-specific blocks differ
        assert (mine.extra["sample_timing"] is not None) == (experiment == "B")
        for key in ("memory", "diagnostics", "sampling_limitations"):
            assert (mine.extra[key] is not None) == (experiment == "C")


# ------------------------------------------------------------------------------------------------ preamble failures


class TestPreambleFailures:
    @pytest.mark.parametrize("experiment", ALL)
    @pytest.mark.parametrize("available", [False, "raises"])
    def test_1_docker_unavailable(self, tmp_path: Path, experiment: str, available: Any) -> None:
        run = run_exp(tmp_path, experiment, docker_available=available)
        assert_pre_run_failure(run, "docker_unavailable")
        assert not run.world.runs_dir.exists(), "no run directory may be created"
        assert run.world.compose_calls == [] and run.world.health_calls == []
        assert run.result.failure.message == "Docker is unavailable"
        assert_no_crash_no_kill(run.world)

    @pytest.mark.parametrize("experiment", ALL)
    def test_2_collector_not_healthy_within_60_s(self, tmp_path: Path, experiment: str) -> None:
        run = run_exp(tmp_path, experiment, health_mode="down")
        manifest = assert_failure(run, "collector_unhealthy", outcome="aborted")
        assert 60.0 <= elapsed(run) <= 61.5
        assert len(run.world.health_calls) >= 60
        assert "60 s" in run.result.failure.message
        assert run.world.compose_calls == [], "Demo_App must not start"
        assert manifest.extra["collector"]["readiness"]["status"] == "collector_unhealthy"
        assert manifest.container_state is None and manifest.evidence is None

    def test_2_collector_503_is_retried_until_the_window_ends(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "A", health_mode="status503")
        assert_failure(run, "collector_unhealthy")
        assert 60.0 <= elapsed(run) <= 61.5

    @pytest.mark.parametrize("experiment", ALL)
    @pytest.mark.parametrize("mode", ["storage_false", "storage_missing"])
    def test_3_storage_unhealthy_aborts_at_once(self, tmp_path: Path, experiment: str, mode: str) -> None:
        run = run_exp(tmp_path, experiment, health_mode=mode)
        manifest = assert_failure(run, "collector_storage_unhealthy", outcome="aborted")
        assert run.result.failure.message == STORAGE_UNHEALTHY_MESSAGE == "Collector storage is not healthy"
        assert run.world.compose_calls == [], "compose_up must never be called"
        assert len(run.world.health_calls) == 1 and run.world.sleeps == []
        assert manifest.extra["container_id"] is None
        assert run.world.demo_health_calls == 0 and run.world.leak_calls == []

    @pytest.mark.parametrize("experiment", ALL)
    def test_4_malformed_health_body_is_an_immediate_storage_unhealthy_abort(self, tmp_path: Path, experiment: str) -> None:
        run = run_exp(tmp_path, experiment, health_mode="malformed")
        assert_failure(run, "collector_storage_unhealthy", outcome="aborted")
        assert run.result.failure.message == STORAGE_UNHEALTHY_MESSAGE
        assert len(run.world.health_calls) == 1 and run.world.sleeps == [] and elapsed(run) == 0.0
        assert run.world.compose_calls == []

    @pytest.mark.parametrize("experiment", ALL)
    def test_5_demo_app_start_failure(self, tmp_path: Path, experiment: str) -> None:
        run = run_exp(tmp_path, experiment, compose_error=True)
        manifest = assert_failure(run, "docker_compose_failed", exit_code=2, outcome="aborted")
        assert "docker compose up failed" in run.result.failure.message
        assert len(run.world.compose_calls) == 1 and run.world.container_id_calls == 0
        assert manifest.extra["phase"] == "failed:docker_compose_failed"
        assert run.world.leak_calls == [] and run.world.inspect_calls == []
        assert_no_crash_no_kill(run.world)

    @pytest.mark.parametrize("experiment", ALL)
    @pytest.mark.parametrize("mode", ["short", "upper", "error"])
    def test_6_invalid_container_id(self, tmp_path: Path, experiment: str, mode: str) -> None:
        run = run_exp(tmp_path, experiment, cid_mode=mode)
        manifest = assert_failure(run, "invalid_container_id", outcome="aborted")
        assert manifest.extra["container_id"] is None
        assert run.world.demo_health_calls == 0 and run.world.leak_calls == []
        assert_no_crash_no_kill(run.world)

    @pytest.mark.parametrize("experiment", ALL)
    @pytest.mark.parametrize("mode", ["503", "down"])
    def test_7_demo_app_health_timeout_30_s(self, tmp_path: Path, experiment: str, mode: str) -> None:
        run = run_exp(tmp_path, experiment, demo_health_mode=mode)
        manifest = assert_failure(run, "demo_app_failed_to_start", outcome="aborted")
        compose_t = run.world.compose_calls[0]["t"]
        assert 30.0 <= run.world.t - compose_t <= 31.5
        assert "30 s" in run.result.failure.message
        assert manifest.extra["container_id"] == CID
        assert run.world.leak_calls == []
        assert_no_crash_no_kill(run.world)

    @pytest.mark.parametrize("experiment", ALL)
    def test_8_no_initial_sample(self, tmp_path: Path, experiment: str) -> None:
        run = run_exp(tmp_path, experiment, evidence_mode="no_samples")
        manifest = assert_failure(run, "no_baseline_sample", outcome="aborted")
        compose_t = run.world.compose_calls[0]["t"]
        assert 30.0 <= run.world.t - compose_t <= 31.5
        assert run.world.leak_calls == [] and manifest.evidence is None
        assert_no_crash_no_kill(run.world)

    def test_collector_already_holds_evidence_for_the_run_id(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "A", evidence_mode="exists_precheck")
        assert_failure(run, "run_id_collision", outcome="aborted")
        assert run.world.compose_calls == []

    def test_collector_without_server_time_cannot_give_a_clock_offset(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "B", health_mode="no_server_time")
        assert_failure(run, "clock_offset_unavailable", outcome="aborted")
        assert run.world.compose_calls == []

    def test_invalid_arguments_make_no_run(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "A", ctx_kw={"mem_limit": "lots"})
        assert_pre_run_failure(run, "invalid_arguments")
        assert not run.world.runs_dir.exists()


# ------------------------------------------------------------------------------------------------ leak failures


class TestLeakFailures:
    @pytest.mark.parametrize("experiment", ["A", "B"])
    @pytest.mark.parametrize("mode", ["http500", "drop"])
    @pytest.mark.parametrize("fail_from", [0, 2])
    def test_9_leak_failure_aborts_without_crash_or_kill(
        self, tmp_path: Path, experiment: str, mode: str, fail_from: int
    ) -> None:
        run = run_exp(tmp_path, experiment, leak_mode=mode, leak_fail_from=fail_from)
        manifest = assert_failure(run, "leak_failed", outcome="aborted")
        assert_no_crash_no_kill(run.world)
        assert run.world.inspect_calls == [], "nothing may be inspected after an aborted leak"
        assert manifest.extra["leak"]["reason"] == "request_failed" and manifest.extra["leak"]["error"]
        assert manifest.extra["times"] == {"crash_request_time": None, "sigkill_sent_time": None}
        assert manifest.extra["phase"] == "failed:leak_failed"
        assert "aborted before any crash or kill" in run.result.failure.message
        assert manifest.container_state is None and manifest.evidence is None
        assert len(run.world.leak_calls) == fail_from + 1

    @pytest.mark.parametrize("experiment", ["A", "B"])
    def test_10_leak_request_over_5_s_is_a_leak_failure(self, tmp_path: Path, experiment: str) -> None:
        run = run_exp(tmp_path, experiment, leak_mode="slow")
        manifest = assert_failure(run, "leak_failed", outcome="aborted")
        assert_no_crash_no_kill(run.world)
        assert "limit 5 s" in manifest.extra["leak"]["error"]
        assert len(run.world.leak_calls) == 1

    @pytest.mark.parametrize("mode", ["http500", "drop", "slow"])
    def test_c_failed_leak_with_container_still_running_is_leak_failed(self, tmp_path: Path, mode: str) -> None:
        run = run_exp(tmp_path, "C", leak_mode=mode, oom_after_bytes=None)
        manifest = assert_failure(run, "leak_failed", outcome="aborted")
        assert_no_crash_no_kill(run.world)
        assert manifest.extra["container_left_running"] is True
        assert manifest.container_state is not None and manifest.container_state.status == "running"
        assert (run.world.runs_dir / run.ctx.run_id / "docker_inspect.json").read_bytes() == run.world.inspect_raw[-1]
        assert manifest.extra["diagnostics"] is not None and manifest.extra["sampling_limitations"] is not None
        assert run.world.terminations == []
        assert manifest.extra["leak"]["reason"] == "request_failed"
        assert len(run.world.leak_calls) == 1, "leak calls stop after the failure"


# ------------------------------------------------------------------------------------------------ stop / state failures


class TestStopAndStateFailures:
    def test_11_a_fails_to_exit_within_30_s(self, tmp_path: Path) -> None:
        # the connection drops (no HTTP response) but the container never stops
        run = run_exp(tmp_path, "A", crash_mode="exit", exit_delay=None)
        manifest = assert_failure(run, "timeout")
        crash_t = run.world.crash_calls[0]
        assert 30.0 <= run.world.t - crash_t <= 31.5
        assert "30 s" in run.result.failure.message
        assert len(run.world.crash_calls) == 1 and run.world.kill_calls == []
        assert manifest.container_state is not None and manifest.container_state.status == "running"
        assert manifest.extra["times"]["crash_request_time"] is not None

    def test_12_b_fails_to_exit_within_10_s(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "B", exit_delay=12.0)
        manifest = assert_failure(run, "timeout")
        kill_t = run.world.kill_calls[0][0]
        assert 10.0 <= run.world.t - kill_t <= 10.5
        assert "10 s" in run.result.failure.message
        assert manifest.container_state is not None and manifest.container_state.status == "running"
        assert manifest.extra["sample_timing"] is not None, "timing evidence exists, so it must be persisted on failure"
        assert manifest.extra["times"]["sigkill_sent_time"] is not None
        assert len(run.world.kill_calls) == 1

    def test_12_b_never_stops(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "B", exit_delay=None)
        assert_failure(run, "timeout")

    def test_12_b_stopping_just_inside_10_s_passes(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "B", exit_delay=9.5)
        manifest = assert_passed(run)
        assert run.checks()["stopped_within_10s"]["passed"] is True
        assert manifest.extra["container_finished_at"] is not None

    @pytest.mark.parametrize(
        ("experiment", "override"),
        [("A", (2, False)), ("A", (137, False)), ("B", (1, False)), ("B", (0, False)), ("C", (1, True)), ("C", (143, True))],
    )
    def test_13_wrong_exit_code(self, tmp_path: Path, experiment: str, override: tuple[int, bool]) -> None:
        run = run_exp(tmp_path, experiment, exit_override=override)
        manifest = assert_failure(run, "unexpected_exit_code")
        state = manifest.container_state
        assert state is not None and (state.exit_code, state.oom_killed) == override  # what Docker said, nothing predicted
        assert run.checks()[f"exit_code{'_137' if experiment != 'A' else ''}"]["passed"] is False
        assert run.world.terminations[0]["cause_label"] == "unknown"
        assert str(override[0]) in run.result.failure.message
        assert_no_crash_no_kill(run.world) if experiment == "C" else None

    @pytest.mark.parametrize(
        ("experiment", "override"), [("A", (1, True)), ("B", (137, True)), ("C", (137, False))]
    )
    def test_14_oom_killed_mismatch(self, tmp_path: Path, experiment: str, override: tuple[int, bool]) -> None:
        run = run_exp(tmp_path, experiment, exit_override=override)
        manifest = assert_failure(run, "oomkilled_mismatch")
        state = manifest.container_state
        assert state is not None and (state.exit_code, state.oom_killed) == override
        assert run.world.terminations[0]["cause_label"] == "unknown"
        assert "oom" in run.result.failure.message.lower()
        if experiment == "C":
            assert_no_crash_no_kill(run.world)
            assert manifest.extra["diagnostics"] is not None and manifest.extra["sampling_limitations"] is not None

    @pytest.mark.parametrize("experiment", ALL)
    @pytest.mark.parametrize("mode", ["error", "incomplete"])
    def test_15_inspect_failure(self, tmp_path: Path, experiment: str, mode: str) -> None:
        run = run_exp(tmp_path, experiment, inspect_mode=mode)
        manifest = assert_failure(run, "docker_inspect_failed")
        assert manifest.container_state is None
        assert run.world.terminations == [], "nothing is posted when the state could not be read"
        if experiment == "C":
            assert_no_crash_no_kill(run.world)
            assert manifest.extra["diagnostics"] is not None and manifest.extra["sampling_limitations"] is not None
        if mode == "incomplete" and experiment != "C":
            assert manifest.extra["container_state_problems"], "the missing field is named"
            assert (run.world.runs_dir / run.ctx.run_id / "docker_inspect.json").read_bytes() == run.world.inspect_raw[-1]

    def test_15_c_keeps_the_raw_inspect_output_when_the_start_poll_is_incomplete(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "C", inspect_mode="incomplete")
        manifest = assert_failure(run, "docker_inspect_failed")
        assert manifest.extra["container_state_problems"]
        assert (run.world.runs_dir / run.ctx.run_id / "docker_inspect.json").read_bytes() == run.world.inspect_raw[-1]

    @pytest.mark.parametrize("experiment", ["A", "B"])
    def test_15_inspect_incomplete_only_after_the_exit(self, tmp_path: Path, experiment: str) -> None:
        run = run_exp(tmp_path, experiment, inspect_mode="incomplete_after_exit")
        assert_failure(run, "docker_inspect_failed")

    def test_c_container_that_never_runs_fails_to_start(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "C", inspect_mode="never_running")
        assert_failure(run, "demo_app_failed_to_start")
        assert run.world.leak_calls == []
        assert_no_crash_no_kill(run.world)

    def test_b_docker_kill_error_is_recorded(self, tmp_path: Path) -> None:
        assert base.FAILURE_TABLE["docker_kill_failed"] == ("failed", 1)
        run = run_exp(tmp_path, "B", kill_error=True)
        assert_failure(run, "docker_kill_failed", exit_code=1, outcome="failed")
        assert len(run.world.kill_calls) == 1 and run.world.crash_calls == []


# ------------------------------------------------------------------------------------------------ Collector failures


class TestCollectorSideFailures:
    @pytest.mark.parametrize("experiment", ALL)
    @pytest.mark.parametrize("status", [500, "down"])
    def test_16_evidence_retrieval_failure(self, tmp_path: Path, experiment: str, status: Any) -> None:
        mode = "fail_after_exit" if status == 500 else "down_after_exit"
        run = run_exp(tmp_path, experiment, evidence_mode=mode)
        manifest = assert_failure(run, "evidence_unavailable")
        assert manifest.evidence is None
        assert manifest.container_state is not None, "the container state already observed is kept"
        assert manifest.extra["collector_health"]["ok"] is True
        if status == 500:
            assert "HTTP 500" in run.result.failure.message
        if experiment == "C":
            assert_no_crash_no_kill(run.world)
            assert manifest.extra["diagnostics"] is not None and manifest.extra["sampling_limitations"] is not None
        if experiment == "B":
            assert manifest.extra["sample_timing"] is None  # no evidence, so no timing and none invented

    @pytest.mark.parametrize("experiment", ALL)
    @pytest.mark.parametrize("mode", ["down", "storage_false", "storage_missing", "status503", "malformed"])
    def test_17_collector_survival_failure(self, tmp_path: Path, experiment: str, mode: str) -> None:
        run = run_exp(tmp_path, experiment, health_after_exit=mode)
        manifest = assert_failure(run, "collector_survival_failed")
        health = manifest.extra["collector_health"]
        assert health["ok"] is False and health["within_s"] == 5.0
        assert run.checks()["collector_survival"]["passed"] is False
        if mode in ("down", "status503"):
            assert health["elapsed_s"] >= 5.0 and health["storage_ok"] is None
        else:
            assert health["http_status"] == 200 and health["storage_ok"] is not True
        if experiment == "B":
            assert manifest.extra["sample_timing"] is not None
        if experiment == "C":
            assert_no_crash_no_kill(run.world)
            assert manifest.extra["diagnostics"] is not None and manifest.extra["sampling_limitations"] is not None

    @pytest.mark.parametrize("experiment", ALL)
    def test_17_collector_recovering_inside_5_s_survives(self, tmp_path: Path, experiment: str) -> None:
        run = run_exp(tmp_path, experiment, flaky_health_after_exit=3)
        manifest = assert_passed(run)
        assert manifest.extra["collector_health"]["ok"] is True
        assert 1.0 <= manifest.extra["collector_health"]["elapsed_s"] <= 5.0

    @pytest.mark.parametrize("experiment", ALL)
    def test_18_durability_mismatch(self, tmp_path: Path, experiment: str) -> None:
        run = run_exp(tmp_path, experiment, durability="process", evidence_durability="crash")
        manifest = assert_failure(run, "durability_mismatch")
        assert manifest.durability_level == "process"
        assert run.checks()["durability_matches_run_metadata"]["passed"] is False
        assert manifest.container_state is not None and manifest.evidence is not None
        if experiment == "C":
            assert_no_crash_no_kill(run.world)
            assert manifest.extra["diagnostics"] is not None

    @pytest.mark.parametrize("durability", ["process", "crash"])
    def test_matching_durability_levels_pass(self, tmp_path: Path, durability: str) -> None:
        run = run_exp(tmp_path, "A", durability=durability)
        manifest = assert_passed(run)
        assert manifest.durability_level == durability

    def test_termination_post_failure_fails_the_run(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "A", termination_status=500)
        assert_failure(run, "termination_post_failed")

    def test_no_memory_growth_in_the_leak_fails_a_and_b(self, tmp_path: Path) -> None:
        for experiment in ("A", "B"):
            run = run_exp(tmp_path / experiment, experiment, rss_mode="flat")
            assert_failure(run, "no_memory_growth_observed")


# ------------------------------------------------------------------------------------------------ Experiment C only


class TestExperimentCSpecifics:
    def test_19_c_reaches_the_120_s_timeout(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "C", oom_after_bytes=None)
        manifest = assert_failure(run, "timeout")
        assert_no_crash_no_kill(run.world)
        assert manifest.extra["container_left_running"] is True
        assert "left running" in run.result.failure.message and "120 s" in run.result.failure.message
        first_leak = run.world.leak_calls[0][0]
        assert 119.9 <= run.world.t - first_leak <= 122.0
        assert max(t for t, _ in run.world.leak_calls) - first_leak <= 120.0
        state = manifest.container_state
        assert state is not None and state.status == "running" and state.oom_killed is False
        assert manifest.extra["diagnostics"] is not None
        limits = manifest.extra["sampling_limitations"]
        assert limits is not None and len(limits["statements"]) == 3
        assert manifest.extra["memory"] is not None
        assert run.world.terminations == [], "no termination is reported for a container that never stopped"
        assert manifest.extra["leak"]["reason"] == "timeout"

    @pytest.mark.parametrize("rss_mode", ["null", "flat"])
    def test_20_c_without_readable_memory_growth(self, tmp_path: Path, rss_mode: str) -> None:
        run = run_exp(tmp_path, "C", rss_mode=rss_mode)
        manifest = assert_failure(run, "no_memory_growth_observed")
        assert_no_crash_no_kill(run.world)
        assert run.checks()["measurable_memory_growth"]["passed"] is False
        # the other mandatory checks still ran on real observations
        assert run.checks()["oom_killed_true"]["passed"] is True
        assert manifest.container_state is not None and manifest.container_state.oom_killed is True
        assert manifest.extra["diagnostics"] is not None and manifest.extra["sampling_limitations"] is not None
        if rss_mode == "null":
            assert manifest.extra["highest_rss_bytes"] is None
            assert manifest.extra["memory"]["first_rss_bytes"] is None
            assert manifest.extra["diagnostics"]["peak_rss_ratio_of_limit"] is None

    @pytest.mark.parametrize("knobs", [{}, {"exit_override": (137, False)}, {"evidence_mode": "fail_after_exit"}])
    def test_c_never_kills_or_crashes_in_any_scenario(self, tmp_path: Path, knobs: dict[str, Any]) -> None:
        run = run_exp(tmp_path, "C", **knobs)
        assert_no_crash_no_kill(run.world)
        assert run.world.docker.__class__ is FakeDocker

    def test_c_observed_137_with_oom_false_fails_and_records_what_was_seen(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "C", exit_override=(137, False))
        manifest = assert_failure(run, "oomkilled_mismatch")
        state = manifest.container_state
        assert (state.exit_code, state.oom_killed) == (137, False)
        assert state.raw_inspect_excerpt.oom_killed is False
        raw = json.loads((run.world.runs_dir / run.ctx.run_id / "docker_inspect.json").read_bytes())
        assert raw[0]["State"]["OOMKilled"] is False
        assert run.checks()["oom_killed_true"]["passed"] is False
        assert run.checks()["exit_code_137"]["passed"] is True
        assert run.world.terminations[0]["cause_label"] == "unknown"

    def test_c_diagnostics_do_not_decide_the_verdict(self, tmp_path: Path) -> None:
        # non-monotonic tail and a tiny peak ratio must not fail the run: only the six mandatory checks decide
        run = run_exp(tmp_path, "C", ctx_kw={"sample_interval_ms": 500})
        manifest = assert_passed(run)
        assert manifest.extra["sampling_limitations"]["sample_interval_ms"] == 500


# ------------------------------------------------------------------------------------------------ Experiment B timing


class TestExperimentBTiming:
    def test_late_received_pre_kill_sample_is_valid_and_counted(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "B", exit_delay=0.1, extra_samples_rel_kill=[(-0.05, 0.08, BASE_RSS + 24 * MIB)])
        manifest = assert_passed(run)
        timing = manifest.extra["sample_timing"]
        assert timing["pre_kill_late_received_count"] == 1
        assert run.checks()["at_least_one_sample_captured_before_kill"]["passed"] is True

    def test_after_container_stop_anomaly_alone_does_not_fail(self, tmp_path: Path) -> None:
        # captured 150 ms after the kill: inside the 250 ms tolerance, but after FinishedAt (kill + 100 ms)
        run = run_exp(tmp_path, "B", exit_delay=0.1, extra_samples_rel_kill=[(0.15, 0.17, BASE_RSS + 24 * MIB)])
        manifest = assert_passed(run)
        timing = manifest.extra["sample_timing"]
        assert len(timing["after_container_stop"]) == 1
        assert timing["post_kill_beyond_tolerance"] == [] and timing["within_tolerance_count"] >= 1

    def test_sample_captured_beyond_tolerance_fails_and_persists_timing(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "B", exit_delay=0.6, extra_samples_rel_kill=[(0.4, 0.45, BASE_RSS + 24 * MIB)])
        manifest = assert_failure(run, "sample_captured_after_kill")
        timing = manifest.extra["sample_timing"]
        assert len(timing["post_kill_beyond_tolerance"]) >= 1
        assert run.checks()["no_sample_captured_after_kill_beyond_tolerance"]["passed"] is False
        assert manifest.container_state is not None and manifest.container_state.exit_code == 137

    def test_sample_timing_is_persisted_on_failure_when_timing_evidence_exists(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "B", health_after_exit="storage_false")
        manifest = assert_failure(run, "collector_survival_failed")
        timing = manifest.extra["sample_timing"]
        assert timing["pre_kill_count"] >= 1 and timing["tolerance_ms"] == 250

    def test_stopped_within_10s_false_fails_the_verdict(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        real = exp_b.poll_inspect

        def slow_poll(*args: Any, **kwargs: Any) -> Any:
            return replace(real(*args, **kwargs), elapsed_s=10.4)  # matched, but not within 10 s

        monkeypatch.setattr(exp_b, "poll_inspect", slow_poll)
        run = run_exp(tmp_path, "B")
        manifest = assert_failure(run, "timeout")
        assert run.checks()["stopped_within_10s"]["passed"] is False
        assert run.checks()["exit_code_137"]["passed"] is True
        assert manifest.container_state is not None

    def test_no_shutdown_handler_records_false_fails_the_verdict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(exp_b, "check_no_shutdown_records", lambda bundle: Check("no_shutdown_handler_records", False, "forced"))
        run = run_exp(tmp_path, "B")
        assert_failure(run, "shutdown_handler_records_found")
        assert run.checks()["no_shutdown_handler_records"]["passed"] is False

    def test_the_evaluator_receives_the_computed_values(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}
        real = exp_b.evaluate_experiment_b

        def spy(*args: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            seen["args"] = args
            return real(*args, **kwargs)

        monkeypatch.setattr(exp_b, "evaluate_experiment_b", spy)
        run = run_exp(tmp_path, "B", offset_ms=1700)
        assert_passed(run)
        assert seen["stopped_within_10s"] is True and seen["no_shutdown_handler_records"] is True
        assert seen["args"][5] == 1700  # the recorded clock offset reaches the evaluator
        state = seen["args"][0]
        assert (state.exit_code, state.oom_killed, state.status) == (137, False, "exited")  # from inspect


# ------------------------------------------------------------------------------------------------ clock offset


class TestClockOffsetCorrection:
    @pytest.mark.parametrize("experiment", ALL)
    @pytest.mark.parametrize("offset_ms", [3000, -3000, 1234])
    def test_flows_use_the_recorded_offset(self, tmp_path: Path, experiment: str, offset_ms: int) -> None:
        run = run_exp(tmp_path, experiment, offset_ms=offset_ms)
        manifest = assert_passed(run)
        assert manifest.extra["clock_offset_ms"] == offset_ms
        assert manifest.extra["clock_offset_uncertainty_ms"] == 0
        if experiment == "B":
            assert manifest.extra["sample_timing"]["clock_offset_ms"] == offset_ms
            assert manifest.extra["sample_timing"]["post_kill_beyond_tolerance"] == []

    def test_b_would_fail_without_the_offset(self, tmp_path: Path) -> None:
        # same scenario, but the evaluator is fed offset 0: the +3 s container clock puts Samples after the kill
        run = run_exp(tmp_path, "B", offset_ms=3000)
        manifest = assert_passed(run)
        bundle = base.read_evidence_bundle(run.ctx)
        sent = manifest.extra["times"]["sigkill_sent_time"]
        wrong = ev.classify_b_samples(bundle.samples, parse_utc_ms(sent), None, 250, 0)
        right = ev.classify_b_samples(bundle.samples, parse_utc_ms(sent), None, 250, 3000)
        assert len(wrong.post_kill_beyond_tolerance) >= 1 and right.post_kill_beyond_tolerance == ()

    def test_a_precrash_and_growth_checks_use_the_offset(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "A", offset_ms=-3000)
        manifest = assert_passed(run)
        bundle = base.read_evidence_bundle(run.ctx)
        leak = manifest.extra["leak"]
        fake_leak = base.LeakResult(
            chunk_bytes=4 * MIB,
            requests=(),
            cumulative_bytes=leak["cumulative_bytes"],
            reason="complete",
            error=None,
            first_call_time=parse_utc_ms(leak["first_call_time"]),
            last_response_time=parse_utc_ms(leak["last_response_time"]),
            last_response_mono=None,
            duration_s=leak["duration_s"],
        )
        assert base.growth_check(bundle.samples, fake_leak, -3000).passed is True
        assert base.growth_check(bundle.samples, fake_leak, 0).passed is False  # the offset really matters here


# ------------------------------------------------------------------------------------------------ directories


class TestExistingDirectoriesAreNeverTouched:
    def _seed(self, runs: Path) -> dict[str, bytes]:
        other = runs / "a-20240101T000000Z-11111111"
        other.mkdir(parents=True)
        (other / "manifest.json").write_bytes(b'{"keep": "me"}\n')
        (other / "evidence.jsonl").write_bytes(b"old evidence\n")
        (other / "docker_inspect.json").write_bytes(b"[]")
        stray = runs / "notes.txt"
        stray.write_bytes(b"not a run")
        return snapshot(runs)

    @pytest.mark.parametrize("experiment", ALL)
    @pytest.mark.parametrize("scenario", [{}, {"leak_mode": "http500"}, {"evidence_mode": "fail_after_exit"}])
    def test_unrelated_run_directories_survive_every_outcome(
        self, tmp_path: Path, experiment: str, scenario: dict[str, Any]
    ) -> None:
        runs = tmp_path / "runs"
        before = self._seed(runs)
        if experiment == "C" and "leak_mode" in scenario:
            scenario = {}
        run = run_exp(tmp_path, experiment, **scenario)
        after = snapshot(runs)
        for name, data in before.items():
            assert after[name] == data, f"{name} was changed"
        assert run.ctx.run_id is not None
        assert any(k.startswith(run.ctx.run_id) for k in after)

    @pytest.mark.parametrize("experiment", ALL)
    def test_a_colliding_run_directory_is_skipped_not_reused(self, tmp_path: Path, experiment: str) -> None:
        runs = tmp_path / "runs"
        probe = World(runs, experiment)
        taken = generate_run_id(experiment.lower(), (), clock=probe.wall, rand_hex=lambda: "c0ffee00")
        (runs / taken).mkdir(parents=True)
        (runs / taken / "manifest.json").write_bytes(b"UNRELATED")
        (runs / taken / "keep.txt").write_bytes(b"keep")
        before = snapshot(runs / taken)
        run = run_exp(tmp_path, experiment, rand_values=["c0ffee00", "c0ffee01"])
        assert run.ctx.run_id == taken[:-8] + "c0ffee01"
        assert run.result.run_id != taken
        assert snapshot(runs / taken) == before
        assert_passed(run)

    def test_every_candidate_taken_makes_no_run_and_no_change(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        probe = World(runs, "A")
        taken = generate_run_id("a", (), clock=probe.wall, rand_hex=lambda: "c0ffee00")
        (runs / taken).mkdir(parents=True)
        (runs / taken / "manifest.json").write_bytes(b"UNRELATED")
        before = snapshot(runs)
        world = World(runs, "A")
        ctx = make_ctx(world, "A", rand_hex=lambda: "c0ffee00")
        result = exp_a.run(ctx)
        assert result.exit_code == 2 and result.outcome == "not_started" and result.run_id is None
        assert result.manifest_path is None and result.failure.code == "environment_error"
        assert snapshot(runs) == before
        assert world.compose_calls == []

    def test_a_failed_run_never_deletes_its_own_evidence(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "A", health_after_exit="down")
        manifest = assert_failure(run, "collector_survival_failed")
        assert manifest.evidence is not None and Path(manifest.evidence.host_copy_path).exists()
        assert (run.world.runs_dir / run.ctx.run_id / "docker_inspect.json").exists()
        run2 = run_exp(tmp_path / "second", "B", durability="process", evidence_durability="crash")
        manifest2 = assert_failure(run2, "durability_mismatch")
        assert Path(manifest2.evidence.host_copy_path).exists()


# ------------------------------------------------------------------------------------------------ negative controls


def _vacuous_evaluation() -> Evaluation:
    return Evaluation(True, "vacuous", (Check("vacuous", True),))


def mutant_a_crashes_after_failed_leak(ctx: base.ExperimentContext, pre: base.PreambleResult) -> base.ExperimentResult:
    base.drive_leak(ctx, abort_on_failure=False)  # BUG: ignores the failed leak
    try:
        base.demo_request(ctx, "POST", "/crash", timeout_s=5.0)
    except base.DemoRequestError:
        pass
    return base.finalize_run(ctx, evaluation=_vacuous_evaluation())


def mutant_b_kills_after_failed_leak(ctx: base.ExperimentContext, pre: base.PreambleResult) -> base.ExperimentResult:
    base.drive_leak(ctx, abort_on_failure=False)  # BUG: ignores the failed leak
    ctx.docker.kill(pre.container_id, "KILL")
    return base.finalize_run(ctx, evaluation=_vacuous_evaluation())


def mutant_c_kills_the_container(ctx: base.ExperimentContext, pre: base.PreambleResult) -> base.ExperimentResult:
    ctx.docker.kill(pre.container_id, "KILL")  # BUG: C must never signal the container
    return base.finalize_run(ctx, evaluation=_vacuous_evaluation())


def mutant_c_posts_crash(ctx: base.ExperimentContext, pre: base.PreambleResult) -> base.ExperimentResult:
    try:
        base.demo_request(ctx, "POST", "/crash", timeout_s=5.0)  # BUG: C must never call /crash
    except base.DemoRequestError:
        pass
    return base.finalize_run(ctx, evaluation=_vacuous_evaluation())


class TestNegativeControls:
    """Each mutant is a deliberately wrong flow. The shared assertions must reject it (AssertionError)."""

    def _run_body(self, tmp_path: Path, experiment: str, body: Any, **knobs: Any) -> Run:
        world = World(tmp_path / "runs", experiment, **knobs)
        ctx = make_ctx(world, experiment)
        result = base.run_experiment(ctx, body)
        return Run(result, ctx, world, world.t)

    def test_control_a_crash_after_failed_leak_is_caught(self, tmp_path: Path) -> None:
        run = self._run_body(tmp_path, "A", mutant_a_crashes_after_failed_leak, leak_mode="http500")
        assert run.world.crash_calls, "the mutant really POSTed /crash"
        with pytest.raises(AssertionError):
            assert_no_crash_no_kill(run.world)

    def test_control_b_kill_after_failed_leak_is_caught(self, tmp_path: Path) -> None:
        run = self._run_body(tmp_path, "B", mutant_b_kills_after_failed_leak, leak_mode="http500")
        assert run.world.kill_calls
        with pytest.raises(AssertionError):
            assert_no_crash_no_kill(run.world)
        with pytest.raises(AssertionError):
            assert_failure(run, "leak_failed", outcome="aborted")

    @pytest.mark.parametrize("body", [mutant_c_kills_the_container, mutant_c_posts_crash])
    def test_control_c_kill_or_crash_is_caught(self, tmp_path: Path, body: Any) -> None:
        run = self._run_body(tmp_path, "C", body)
        assert run.world.kill_calls or run.world.crash_calls
        with pytest.raises(AssertionError):
            assert_no_crash_no_kill(run.world)

    def test_control_hardcoded_true_b_inputs_are_caught(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        real = exp_b.evaluate_experiment_b

        def hardcoded(*args: Any, **kwargs: Any) -> Any:
            kwargs["stopped_within_10s"] = True
            kwargs["no_shutdown_handler_records"] = True
            return real(*args, **kwargs)

        monkeypatch.setattr(exp_b, "evaluate_experiment_b", hardcoded)
        slow = replace  # keep the name used below explicit
        real_poll = exp_b.poll_inspect
        monkeypatch.setattr(exp_b, "poll_inspect", lambda *a, **k: slow(real_poll(*a, **k), elapsed_s=10.4))
        monkeypatch.setattr(exp_b, "check_no_shutdown_records", lambda bundle: Check("no_shutdown_handler_records", False, "x"))
        run = run_exp(tmp_path, "B")
        assert run.result.exit_code == 0, "the mutant is fooled into passing"
        with pytest.raises(AssertionError):
            assert_failure(run, "timeout")
        assert run.checks()["stopped_within_10s"]["passed"] is True  # the hardcoded value, which the real flow refuses

    def test_control_manifest_not_written_on_failure_is_caught(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(base, "fail_run", lambda ctx, error: base.pre_run_result(ctx, error))
        run = run_exp(tmp_path, "A", leak_mode="http500")
        assert run.ctx.run_id is not None
        with pytest.raises(AssertionError):
            assert_failure(run, "leak_failed", outcome="aborted")
        assert run.manifest().failure.code == "run_incomplete"  # only the placeholder is on disk

    def test_control_pass_on_wrong_exit_code_is_caught(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # a flow that predicts the exit code instead of reading Docker would pass here; the real one must not
        run = run_exp(tmp_path, "A", exit_override=(2, False))
        assert run.result.exit_code == 1
        with pytest.raises(AssertionError):
            assert_passed(run)

    def test_control_run_that_deletes_an_existing_run_directory_is_caught(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        other = runs / "a-20240101T000000Z-11111111"
        other.mkdir(parents=True)
        (other / "manifest.json").write_bytes(b'{"keep": "me"}\n')
        before = snapshot(runs)

        def destructive(ctx: base.ExperimentContext, pre: base.PreambleResult) -> base.ExperimentResult:
            (other / "manifest.json").write_bytes(b"overwritten")  # BUG: clobbers an unrelated run
            return base.finalize_run(ctx, evaluation=_vacuous_evaluation())

        world = World(runs, "A")
        base.run_experiment(make_ctx(world, "A"), destructive)
        after = snapshot(runs)
        assert any(after[name] != data for name, data in before.items()), "the mutant changed the file"
        with pytest.raises(AssertionError):
            for name, data in before.items():
                assert after[name] == data, f"{name} was changed"


# ------------------------------------------------------------------------------------------------ C clock conversion


class CSamplesWorld(World):
    """Experiment C world whose evidence holds only scripted Samples, placed relative to the container's exit.

    Each entry is (capture, receive, rss) in host seconds relative to the exit. The Collector stamps them with its
    own clock, which runs `offset_ms` ahead of the host.
    """

    scripted: list[tuple[float, float, int | None]] = []

    def evidence_bytes(self) -> bytes:
        if not self.exited:
            return super().evidence_bytes()  # the preamble baseline, before the container stops
        assert self.exit_at is not None
        meta = super().evidence_bytes().split(b"\n", 1)[0] + b"\n"
        out = [meta]
        for k, (capture, receive, rss) in enumerate(self.scripted):
            out.append(
                self._line(self._sample_record(k, self.exit_at + capture, self.exit_at + receive, rss))
            )
        return b"".join(out)


def run_c_scripted(tmp_path: Path, offset_ms: int, scripted: list[tuple[float, float, int | None]]) -> Run:
    world = CSamplesWorld(tmp_path / "runs", "C", offset_ms=offset_ms)
    world.scripted = scripted
    ctx = make_ctx(world, "C")
    result = exp_c.run(ctx)
    return Run(result, ctx, world, T0)


PRE_STOP = [(-0.6, -0.58, BASE_RSS), (-0.1, -0.08, BASE_RSS + 8 * MIB)]  # received before the container stopped
POST_STOP = [(0.5, 0.52, BASE_RSS), (0.8, 0.82, BASE_RSS + 8 * MIB)]  # received after it stopped


class TestExperimentCClockConversion:
    @pytest.mark.parametrize("offset_ms", [3000, -3000])
    def test_precrash_samples_are_counted_on_the_host_clock(self, tmp_path: Path, offset_ms: int) -> None:
        run = run_c_scripted(tmp_path, offset_ms, PRE_STOP)
        manifest = assert_passed(run)
        assert manifest.extra["clock_offset_ms"] == offset_ms
        assert run.checks()["preserved_precrash_evidence"]["passed"] is True
        assert "2 Samples received before stop" in run.checks()["preserved_precrash_evidence"]["detail"]

    @pytest.mark.parametrize("offset_ms", [3000, -3000])
    def test_post_stop_samples_are_not_counted_whatever_the_offset(self, tmp_path: Path, offset_ms: int) -> None:
        run = run_c_scripted(tmp_path, offset_ms, POST_STOP)
        assert run.result.exit_code != 0 and run.result.failure is not None
        assert run.checks()["preserved_precrash_evidence"]["passed"] is False
        assert "0 Samples received before stop" in run.checks()["preserved_precrash_evidence"]["detail"]

    def test_the_pre_crash_count_depends_on_the_offset(self, tmp_path: Path) -> None:
        # +3000: uncorrected recv_time lands after the stop (0 counted); corrected it is before it (2 counted)
        run = run_c_scripted(tmp_path, 3000, PRE_STOP)
        manifest = assert_passed(run)
        bundle = base.read_evidence_bundle(run.ctx)
        stop = parse_utc_ms(manifest.extra["container_finished_at"]) if manifest.extra.get("container_finished_at") else None
        stop = stop or run.world.host_dt(run.world.exit_at)
        wrong = sum(1 for s in bundle.samples if s.recv_time < stop)
        right = sum(1 for s in exp_c._host_clock(bundle.samples, 3000) if s.recv_time < stop)
        assert (wrong, right) == (0, 2)

    # ---- negative controls: an offset-ignoring flow flips the verdict, and the shared assertions notice
    def test_control_ignoring_a_positive_offset_fails_a_run_that_should_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(exp_c, "_host_clock", lambda samples, offset_ms: tuple(samples))
        run = run_c_scripted(tmp_path, 3000, PRE_STOP)
        assert run.result.exit_code != 0
        with pytest.raises(AssertionError):
            assert_passed(run)
        assert run.checks()["preserved_precrash_evidence"]["passed"] is False

    def test_control_ignoring_a_negative_offset_passes_a_run_that_should_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = run_c_scripted(tmp_path / "real", -3000, POST_STOP)
        assert real.checks()["preserved_precrash_evidence"]["passed"] is False  # the correct verdict
        monkeypatch.setattr(exp_c, "_host_clock", lambda samples, offset_ms: tuple(samples))
        run = run_c_scripted(tmp_path / "mutant", -3000, POST_STOP)
        assert run.checks()["preserved_precrash_evidence"]["passed"] is True  # wrongly counted as pre-crash

    def test_control_wrong_sign_conversion_is_caught(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def wrong_sign(samples: Any, offset_ms: int) -> tuple[Any, ...]:
            delta = timedelta(milliseconds=offset_ms)
            return tuple(replace(s, recv_time=s.recv_time + delta) for s in samples)

        monkeypatch.setattr(exp_c, "_host_clock", wrong_sign)
        run = run_c_scripted(tmp_path, 3000, PRE_STOP)
        with pytest.raises(AssertionError):
            assert_passed(run)


# ------------------------------------------------------------------------------------------------ A highest RSS


class TestHighestRssRecording:
    @pytest.mark.parametrize(
        "knobs, code",
        [
            ({"health_after_exit": "storage_false"}, "collector_survival_failed"),
            ({"exit_override": (2, False)}, None),
        ],
    )
    def test_a_failure_after_the_evidence_fetch_still_records_the_highest_rss(
        self, tmp_path: Path, knobs: dict[str, Any], code: str | None
    ) -> None:
        run = run_exp(tmp_path, "A", **knobs)
        assert run.result.exit_code != 0 and run.result.failure is not None
        if code is not None:
            assert run.result.failure.code == code
        manifest = run.manifest()
        assert run.world.evidence_calls and run.world.evidence_calls[-1][1] == 200, "evidence was fetched"
        retained = run.world.retained
        assert manifest.extra["highest_rss_bytes"] is not None
        assert manifest.extra["highest_rss_bytes"] >= BASE_RSS
        assert manifest.extra["highest_rss_bytes"] <= BASE_RSS + retained

    def test_a_failure_before_the_evidence_fetch_leaves_it_unrecorded(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "A", evidence_mode="fail_after_exit")
        assert run.result.failure is not None and run.result.failure.code == "evidence_unavailable"
        assert run.manifest().extra["highest_rss_bytes"] is None

    @pytest.mark.parametrize("experiment", ["A", "B"])
    def test_null_rss_never_becomes_zero(self, tmp_path: Path, experiment: str) -> None:
        run = run_exp(tmp_path, experiment, rss_mode="null")
        manifest = run.manifest()
        assert manifest.extra["highest_rss_bytes"] is None
        assert manifest.extra["highest_rss_bytes"] != 0
        bundle = base.read_evidence_bundle(run.ctx)
        assert bundle.samples and all(s.rss_bytes is None for s in bundle.samples)

    def test_c_null_rss_is_none_in_both_places(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "C", rss_mode="null")
        extra = run.manifest().extra
        assert extra["highest_rss_bytes"] is None and extra["memory"]["highest_rss_bytes"] is None
        assert extra["memory"]["first_rss_bytes"] is None

    def test_a_ignores_null_samples_when_others_are_readable(self, tmp_path: Path) -> None:
        # scripted: a null Sample sits between readable ones; the highest is the largest readable value
        samples = [
            ev.SampleView(ts=EPOCH, recv_time=EPOCH, rss_bytes=None, seq=0),
            ev.SampleView(ts=EPOCH + timedelta(seconds=1), recv_time=EPOCH, rss_bytes=7 * MIB, seq=1),
        ]
        assert base.highest_rss_bytes(samples) == 7 * MIB
        assert base.highest_rss_bytes(samples[:1]) is None


# ------------------------------------------------------------------------------------------------ B: optional FinishedAt


def assert_b_finished_at_absent_passes(run: Run) -> Any:
    """The shared assertion: B passes without a usable FinishedAt, invents no timestamp and says so."""
    manifest = assert_passed(run)
    timing = manifest.extra["sample_timing"]
    assert timing["container_finished_at"] is None
    assert manifest.extra["container_finished_at"] is None
    assert timing["after_container_stop_analysis"] == "unavailable"
    assert timing["after_container_stop"] == []
    assert any("FinishedAt" in note for note in manifest.extra["limitations"])
    assert "container_finished_at_recorded" not in run.checks()
    return manifest


class TestExperimentBOptionalFinishedAt:
    @pytest.mark.parametrize("finished_mode", ["missing", "zero", "bad"])
    def test_absent_zero_or_unparseable_finished_at_still_passes(self, tmp_path: Path, finished_mode: str) -> None:
        run = run_exp(tmp_path, "B", exit_delay=0.1, finished_mode=finished_mode)
        manifest = assert_b_finished_at_absent_passes(run)
        checks = run.checks()
        for name in (
            "exit_code_137",
            "oom_killed_false",
            "stopped_within_10s",
            "collector_survival",
            "at_least_one_sample_captured_before_kill",
            "no_sample_captured_after_kill_beyond_tolerance",
            "no_shutdown_handler_records",
            "termination_recorded",
            "durability_matches_run_metadata",
            "memory_growth_matches_leak",
        ):
            assert checks[name]["passed"] is True, name
        state = manifest.container_state
        assert state is not None and (state.exit_code, state.oom_killed, state.status) == (137, False, "exited")

    def test_present_finished_at_reports_the_analysis_as_available(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "B", exit_delay=0.1)
        manifest = assert_passed(run)
        assert manifest.extra["sample_timing"]["after_container_stop_analysis"] == "available"
        assert manifest.extra["sample_timing"]["container_finished_at"] is not None
        assert not any("FinishedAt" in note for note in manifest.extra["limitations"])

    def test_other_timing_checks_stay_intact_without_finished_at(self, tmp_path: Path) -> None:
        run = run_exp(
            tmp_path,
            "B",
            exit_delay=0.6,
            finished_mode="missing",
            extra_samples_rel_kill=[(0.4, 0.45, BASE_RSS + 24 * MIB)],
        )
        assert_failure(run, "sample_captured_after_kill")
        assert run.checks()["no_sample_captured_after_kill_beyond_tolerance"]["passed"] is False

    def test_wrong_exit_state_still_fails_without_finished_at(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "B", finished_mode="missing", exit_override=(1, False))
        assert_failure(run, "unexpected_exit_code")

    def test_unreadable_inspect_still_fails_without_finished_at(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "B", finished_mode="missing", inspect_mode="incomplete_after_exit")
        assert_failure(run, "docker_inspect_failed")

    def test_container_that_never_stops_still_times_out_without_finished_at(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "B", finished_mode="missing", exit_delay=None)
        assert_failure(run, "timeout")


# ------------------------------------------------------------------------------------------------ C: leak failures and Docker


def assert_c_passes_despite_leak_connection_loss(run: Run) -> Any:
    manifest = assert_passed(run)
    assert_no_crash_no_kill(run.world)
    state = manifest.container_state
    assert state is not None and (state.exit_code, state.oom_killed) == (137, True)
    assert manifest.extra["leak"]["reason"] == "request_failed"
    assert manifest.extra["container_left_running"] is False
    return manifest


class TestExperimentCLeakFailureJudgedByDocker:
    def test_oom_kill_during_the_leak_request_passes(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "C", leak_oom_drop=True)
        manifest = assert_c_passes_despite_leak_connection_loss(run)
        assert run.world.terminations[0]["cause_label"] == "docker_oom_kill"
        assert manifest.extra["diagnostics"] is not None and manifest.extra["sampling_limitations"] is not None
        assert run.world.leak_calls[-1][0] < run.world.exit_at, "the failed request was the one that triggered the kill"

    @pytest.mark.parametrize(
        ("override", "code"), [((137, False), "oomkilled_mismatch"), ((1, True), "unexpected_exit_code")]
    )
    def test_mismatch_after_a_lost_leak_connection_is_judged_from_docker(
        self, tmp_path: Path, override: tuple[int, bool], code: str
    ) -> None:
        run = run_exp(tmp_path, "C", leak_oom_drop=True, exit_override=override)
        manifest = assert_failure(run, code)
        assert run.result.failure.code != "leak_failed"
        assert_no_crash_no_kill(run.world)
        state = manifest.container_state
        assert state is not None and (state.exit_code, state.oom_killed) == override  # nothing synthesized
        assert manifest.extra["leak"]["reason"] == "request_failed"
        assert manifest.extra["container_left_running"] is False
        assert manifest.extra["diagnostics"] is not None

    def test_still_running_after_the_settle_window_is_leak_failed_without_killing(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "C", leak_mode="http500", oom_after_bytes=None)
        manifest = assert_failure(run, "leak_failed", outcome="aborted")
        assert_no_crash_no_kill(run.world)
        assert manifest.extra["container_left_running"] is True
        assert 3.0 <= run.world.t - run.world.leak_calls[0][0] <= 4.5, "a 3 s settle poll, not the 120 s deadline"
        assert "left running" in run.result.failure.message

    def test_deadline_with_the_container_running_is_timeout_not_leak_failed(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "C", oom_after_bytes=None)
        manifest = assert_failure(run, "timeout")
        assert manifest.extra["container_left_running"] is True
        assert_no_crash_no_kill(run.world)
        assert manifest.extra["leak"]["reason"] == "timeout"


# ------------------------------------------------------------------------------------------------ A: completed /crash response


A_CRASH_STATUSES = [(200, b'{"status":"ignored"}'), (403, b"destructive endpoints disabled"), (404, b"not found"), (500, b"boom\n\x00trace")]


def assert_a_crash_request_failed_promptly(run: Run, status: int, body: bytes) -> Any:
    manifest = assert_failure(run, "crash_request_failed")
    world = run.world
    crash_t = world.crash_calls[0]
    assert world.t - crash_t < 5.0, "no 30 s wait after a completed HTTP response"
    assert len(world.crash_calls) == 1 and world.kill_calls == []
    assert f"status {status}" in run.result.failure.message
    assert body.decode("utf-8", errors="replace").split("\n")[0].strip()[:8] in run.result.failure.message
    notes = [n for n in manifest.extra["limitations"] if "crash_request_failed" in n]
    assert len(notes) == 1 and f"HTTP {status}" in notes[0] and "not an observed controlled" in notes[0]
    assert manifest.extra["times"]["crash_request_time"] is not None
    assert manifest.outcome != "passed" and manifest.extra["finished"] is True
    return manifest


class TestExperimentACompletedCrashResponse:
    @pytest.mark.parametrize(("status", "body"), A_CRASH_STATUSES)
    def test_completed_response_fails_promptly(self, tmp_path: Path, status: int, body: bytes) -> None:
        run = run_exp(tmp_path, "A", crash_mode="status", crash_status=status, crash_body=body)
        manifest = assert_a_crash_request_failed_promptly(run, status, body)
        world = run.world
        assert len(world.inspect_calls) == 1 and world.inspect_calls[0][0] >= world.crash_calls[0]
        assert manifest.container_state is not None and manifest.container_state.status == "running"
        assert (world.runs_dir / run.ctx.run_id / "docker_inspect.json").read_bytes() == world.inspect_raw[-1]
        assert world.terminations == []

    @pytest.mark.parametrize(("status", "body"), A_CRASH_STATUSES)
    def test_never_a_pass_even_when_the_immediate_inspect_shows_exit_1(
        self, tmp_path: Path, status: int, body: bytes
    ) -> None:
        run = run_exp(
            tmp_path, "A", crash_mode="status", crash_status=status, crash_body=body, crash_status_exits=True
        )
        manifest = assert_a_crash_request_failed_promptly(run, status, body)
        state = manifest.container_state
        assert state is not None and (state.exit_code, state.oom_killed, state.status) == (1, False, "exited")
        assert run.result.exit_code == 1 and run.result.outcome == "failed"
        assert "exit_code=1" in run.result.failure.message and "oom_killed=False" in run.result.failure.message

    def test_failed_immediate_inspect_does_not_hide_the_failure(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "A", crash_mode="status", crash_status=500, inspect_mode="error")
        manifest = assert_failure(run, "crash_request_failed")
        assert len(run.world.inspect_calls) == 1
        assert manifest.container_state is None
        assert "no inspect reading available" in run.result.failure.message

    def test_long_body_is_cut_to_an_excerpt(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "A", crash_mode="status", crash_status=500, crash_body=b"x" * 5000)
        assert_failure(run, "crash_request_failed")
        assert len(run.result.failure.message) < 1000

    def test_dropped_connection_still_polls_and_passes(self, tmp_path: Path) -> None:
        run = run_exp(tmp_path, "A", crash_mode="exit")
        assert_passed(run)
        assert len(run.world.inspect_calls) >= 1

    @pytest.mark.parametrize(
        ("override", "code"), [((2, False), "unexpected_exit_code"), ((1, True), "oomkilled_mismatch")]
    )
    def test_dropped_connection_fails_per_docker_state(
        self, tmp_path: Path, override: tuple[int, bool], code: str
    ) -> None:
        run = run_exp(tmp_path, "A", crash_mode="exit", exit_override=override)
        assert_failure(run, code)
        assert run.result.failure.code != "crash_request_failed"


# ------------------------------------------------------------------------------------------------ initial manifest placeholder


class _Crash(BaseException):
    """Simulates the process dying (not an Exception, not KeyboardInterrupt) before the final write."""


def _snapshots(world: World, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    real = base.write_manifest

    def spy(runs_dir: Path, manifest: Any) -> Path:
        path = real(runs_dir, manifest)
        seen.append(json.loads(path.read_text(encoding="utf-8")))
        return path

    monkeypatch.setattr(base, "write_manifest", spy)
    return seen


def _drive(tmp_path: Path, experiment: str, **knobs: Any) -> Run:
    return run_exp(tmp_path, experiment, **knobs)


def _boom(*_a: Any, **_k: Any) -> Any:
    raise RuntimeError("body exploded")


class TestInitialManifestPlaceholder:
    def _assert_placeholder_then_final(self, seen: list[dict[str, Any]], run: Run) -> None:
        first, last = seen[0], seen[-1]
        assert first["outcome"] == "aborted" and first["failure"]["code"] == "run_incomplete"
        assert first["finished"] is False
        final = run.manifest()
        assert final.extra["finished"] is True
        assert final.failure is None or final.failure.code != "run_incomplete"
        assert last["finished"] is True

    @pytest.mark.parametrize("experiment", ALL)
    def test_success_replaces_the_placeholder(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, experiment: str) -> None:
        world = World(tmp_path / "runs", experiment)
        seen = _snapshots(world, monkeypatch)
        ctx = make_ctx(world, experiment)
        run = Run(RUNNERS[experiment](ctx), ctx, world)
        self._assert_placeholder_then_final(seen, run)
        assert run.manifest().outcome == "passed" and run.manifest().failure is None

    @pytest.mark.parametrize(
        ("experiment", "knobs", "code"),
        [
            ("A", {"exit_override": (2, False)}, "unexpected_exit_code"),
            ("B", {"exit_override": (1, False)}, "unexpected_exit_code"),
            ("C", {"exit_override": (137, False)}, "oomkilled_mismatch"),
            ("A", {"crash_mode": "status", "crash_status": 500}, "crash_request_failed"),
            ("A", {"leak_mode": "http500"}, "leak_failed"),
            ("B", {"leak_mode": "http500"}, "leak_failed"),
            ("C", {"leak_mode": "http500", "oom_after_bytes": None}, "leak_failed"),
            ("C", {"oom_after_bytes": None}, "timeout"),
            ("A", {"health_mode": "down"}, "collector_unhealthy"),
            ("B", {"cid_mode": "short"}, "invalid_container_id"),
            ("C", {"compose_error": True}, "docker_compose_failed"),
        ],
    )
    def test_every_failure_path_replaces_the_placeholder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, experiment: str, knobs: dict[str, Any], code: str
    ) -> None:
        world = World(tmp_path / "runs", experiment, **knobs)
        seen = _snapshots(world, monkeypatch)
        ctx = make_ctx(world, experiment)
        run = Run(RUNNERS[experiment](ctx), ctx, world)
        assert run.result.failure is not None and run.result.failure.code == code
        self._assert_placeholder_then_final(seen, run)
        assert run.manifest().failure.code == code != "run_incomplete"

    @pytest.mark.parametrize("experiment", ALL)
    def test_failure_during_the_body_replaces_the_placeholder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, experiment: str
    ) -> None:
        module = {"A": exp_a, "B": exp_b, "C": exp_c}[experiment]
        monkeypatch.setattr(module, "drive_leak", _boom)
        world = World(tmp_path / "runs", experiment)
        seen = _snapshots(world, monkeypatch)
        ctx = make_ctx(world, experiment)
        run = Run(RUNNERS[experiment](ctx), ctx, world)
        assert run.result.failure is not None and run.result.failure.code == "internal_error"
        self._assert_placeholder_then_final(seen, run)
        manifest = run.manifest()
        assert manifest.outcome == "aborted" and "body exploded" in manifest.failure.message

    @pytest.mark.parametrize("experiment", ALL)
    def test_crash_before_the_final_write_leaves_the_readable_placeholder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, experiment: str
    ) -> None:
        module = {"A": exp_a, "B": exp_b, "C": exp_c}[experiment]

        def die(*_a: Any, **_k: Any) -> Any:
            raise _Crash()

        monkeypatch.setattr(module, "finalize_run", die)
        world = World(tmp_path / "runs", experiment)
        ctx = make_ctx(world, experiment)
        with pytest.raises(_Crash):
            RUNNERS[experiment](ctx)
        assert ctx.run_id is not None
        manifest = read_manifest(world.runs_dir / ctx.run_id / "manifest.json")
        assert manifest.outcome == "aborted"
        assert manifest.failure is not None and manifest.failure.code == "run_incomplete"
        assert manifest.extra["finished"] is False
        assert manifest.extra["phase"] != "finished"


# ------------------------------------------------------------------------------------------------ negative controls (corrections)


class TestCorrectionNegativeControls:
    def test_control_c_reporting_leak_failed_for_an_exited_container_is_caught(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = exp_c.drive_leak

        def mutant(ctx: Any, **kwargs: Any) -> Any:
            leak = real(ctx, **kwargs)
            if leak.reason == "request_failed":  # BUG: trusts the request error, never asks Docker
                raise base.ExperimentError("leak_failed", f"{leak.error}; container still running")
            return leak

        monkeypatch.setattr(exp_c, "drive_leak", mutant)
        run = run_exp(tmp_path, "C", leak_oom_drop=True)
        assert run.result.failure is not None and run.result.failure.code == "leak_failed"
        with pytest.raises(AssertionError):
            assert_c_passes_despite_leak_connection_loss(run)
        # the same mutant is harmless where the container really is still running
        still = run_exp(tmp_path / "still", "C", leak_mode="http500", oom_after_bytes=None)
        assert still.result.failure.code == "leak_failed"

    def test_control_c_synthesizing_a_pass_from_the_request_error_is_caught(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = exp_c.evaluate_experiment_c

        def mutant(*args: Any, **kwargs: Any) -> Any:
            return Evaluation(True, "synthesized", (Check("synthesized", True),))  # BUG: ignores Docker's state

        monkeypatch.setattr(exp_c, "evaluate_experiment_c", mutant)
        run = run_exp(tmp_path, "C", leak_oom_drop=True, exit_override=(137, False))
        assert run.result.exit_code == 0, "the mutant is fooled into passing"
        with pytest.raises(AssertionError):
            assert_failure(run, "oomkilled_mismatch")
        assert real is not mutant

    def test_control_a_waiting_the_full_30_s_after_a_completed_response_is_caught(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = exp_a.demo_request

        def mutant(ctx: Any, method: str, path: str, **kwargs: Any) -> Any:
            response = real(ctx, method, path, **kwargs)
            if path == "/crash":  # BUG: treats the completed response like a dropped connection
                raise base.DemoRequestError("treated as dropped")
            return response

        monkeypatch.setattr(exp_a, "demo_request", mutant)
        run = run_exp(tmp_path, "A", crash_mode="status", crash_status=500, crash_body=b"boom")
        assert run.world.t - run.world.crash_calls[0] >= 30.0, "the mutant really waited for the full poll"
        with pytest.raises(AssertionError):
            assert_a_crash_request_failed_promptly(run, 500, b"boom")

    def test_control_a_passing_after_a_completed_response_is_caught(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = exp_a.demo_request

        def mutant(ctx: Any, method: str, path: str, **kwargs: Any) -> Any:
            real(ctx, method, path, **kwargs)
            raise base.DemoRequestError("treated as dropped")  # BUG: any response counts as the expected drop

        monkeypatch.setattr(exp_a, "demo_request", mutant)
        run = run_exp(tmp_path, "A", crash_mode="status", crash_status=200, crash_status_exits=True)
        assert run.result.exit_code == 0, "the mutant passes on a completed response"
        with pytest.raises(AssertionError):
            assert_a_crash_request_failed_promptly(run, 200, b'{"status":"ignored"}')

    def test_control_b_failing_on_missing_finished_at_is_caught(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = exp_b.evaluate_experiment_b

        def mutant(*args: Any, **kwargs: Any) -> Any:
            result = real(*args, **kwargs)
            if args[4] is None:  # BUG: FinishedAt became a verdict condition again
                checks = (*result.checks, Check("container_finished_at_recorded", False, "missing"))
                return Evaluation(False, "container_finished_at_recorded failed", checks)
            return result

        monkeypatch.setattr(exp_b, "evaluate_experiment_b", mutant)
        run = run_exp(tmp_path, "B", exit_delay=0.1, finished_mode="missing")
        assert run.result.exit_code == 1
        with pytest.raises(AssertionError):
            assert_b_finished_at_absent_passes(run)
