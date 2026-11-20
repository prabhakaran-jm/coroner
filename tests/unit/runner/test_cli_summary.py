"""CLI and summary tests (task 6.19; contract docs/m0-results/m0.6-runner-cli.md).

Requirements 8.4, 11.20, 12.5, 12.6, 16.1; design "Experiment_Runner CLI", "Summary command".

Everything is in memory. `cli.main` is called with fake `DockerPort` / `CollectorClient` / HTTP transport / clocks and
`StringIO` streams. The experiment modules are replaced by spies where only the dispatch matters, and the real
experiment code runs where the property is "no Docker call happened" (a recording `FakeDocker` proves it). Run
directories live under `tmp_path`; the working directory is moved there too, so a default `runs` directory could never
touch the repository. An autouse fixture makes any socket connect, subprocess, real sleep or real `DockerCli` fail the
test.

Expected summary and clock-check text is written out literally from the fixture values (never produced by calling the
production renderers). Negative controls at the bottom run deliberately broken in-memory variants (a summary that
invents values, a sign-flipped offset, `--mem-limit` validation after the Docker call, a summary that writes a file,
`run` returning 0 on a failed result, `summary` returning 0 on an error) through the same check functions and show that
the checks reject them.
"""
from __future__ import annotations

import ast
import dataclasses
import hashlib
import io
import json
import runpy
import socket
import subprocess
import sys
import time
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from coroner.runner import cli
from coroner.runner import collector_client as collector_client_mod
from coroner.runner import evaluate
from coroner.runner import summary as summary_mod
from coroner.runner.collector_client import ClockOffset, CollectorClient, HttpResponse
from coroner.runner.docker_port import DockerCli as RealDockerCli
from coroner.runner.experiments import base, exp_a, exp_b, exp_c
from coroner.runner.manifest import (
    ContainerState,
    EvidenceRef,
    Expected,
    Failure,
    InspectExcerpt,
    RunManifest,
    write_manifest,
)

UTC = timezone.utc
CID = "ab" * 32
RID_A = "a-20250101T120000Z-9f3a1c2e"
RID_B = "b-20250101T120000Z-5d2e7b10"
RID_C = "c-20250101T121500Z-4be07a11"
UNKNOWN_RID = "a-20250101T120000Z-00000000"
UNOBSERVED = "The interval between the last received Sample and the actual termination time is unobserved."
STATEMENTS = (
    "Sampling is periodic, so growth between two Samples is not observed.",
    "The leak rate is approximate and comes from the request schedule.",
    "The Probe cannot report a Sample after the kernel kills the process.",
)


# ------------------------------------------------------------------------------------------------ guards


@pytest.fixture(autouse=True)
def _isolation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No network, no subprocess, no real sleep, no real DockerCli; cwd is tmp_path (never the repo)."""

    def forbidden(name: str) -> Callable[..., Any]:
        def fail(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(f"forbidden call in a unit test: {name}")

        return fail

    monkeypatch.setattr(socket.socket, "connect", forbidden("socket.connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden("socket.connect_ex"))
    monkeypatch.setattr(socket, "create_connection", forbidden("socket.create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", forbidden("socket.getaddrinfo"))
    monkeypatch.setattr(subprocess, "Popen", forbidden("subprocess.Popen"))
    monkeypatch.setattr(time, "sleep", forbidden("time.sleep"))
    if hasattr(cli, "DockerCli"):
        monkeypatch.setattr(cli, "DockerCli", forbidden("DockerCli()"))
    monkeypatch.chdir(tmp_path)


# ------------------------------------------------------------------------------------------------ fakes


class FakeDocker:
    """Records every call, including `available()`. Everything except `available` fails the test."""

    def __init__(self, available: Any = True) -> None:
        self.availability = available
        self.calls: list[str] = []

    def available(self) -> bool:
        self.calls.append("available")
        if self.availability == "raises":
            raise OSError("docker probe blew up")
        return bool(self.availability)

    def _bad(self, name: str) -> Any:
        self.calls.append(name)
        raise AssertionError(f"unexpected Docker call {name}")

    def compose_up(self, service: str, *, env: Any, no_deps: bool, recreate: bool) -> None:
        self._bad("compose_up")

    def container_id(self, service: str) -> str:
        return self._bad("container_id")

    def inspect(self, container_id: str) -> Any:
        return self._bad("inspect")

    def kill(self, container_id: str, signal: str) -> None:
        self._bad("kill")


class FakeCollector:
    """Any use is recorded and then fails the test."""

    base_url = "http://fake-collector.test"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _bad(self, name: str) -> Any:
        self.calls.append(name)
        raise AssertionError(f"unexpected Collector call {name}")

    def health(self) -> Any:
        return self._bad("health")

    def get_evidence(self, run_id: str) -> Any:
        return self._bad("get_evidence")

    def post_termination(self, run_id: str, payload: dict[str, Any]) -> Any:
        return self._bad("post_termination")


@dataclass
class Outcome:
    rc: int
    out: str
    err: str
    docker: FakeDocker
    collector: FakeCollector
    sleeps: list[float] = field(default_factory=list)

    @property
    def out_lines(self) -> list[str]:
        return self.out.splitlines()


def run_main(argv: list[str], *, main_fn: Callable[..., int] | None = None, docker: FakeDocker | None = None,
             env: dict[str, str] | None = None, **kwargs: Any) -> Outcome:
    out, err = io.StringIO(), io.StringIO()
    docker = docker if docker is not None else FakeDocker()
    sleeps: list[float] = []
    kwargs.setdefault("sleep", sleeps.append)
    collector = kwargs.pop("collector", None)
    fake_collector = FakeCollector()
    if collector is None and "http" not in kwargs:
        collector = fake_collector
    fn = main_fn if main_fn is not None else cli.main
    rc = fn(argv, docker=docker, collector=collector, env=env if env is not None else {}, out=out, err=err, **kwargs)
    return Outcome(rc, out.getvalue(), err.getvalue(), docker, fake_collector, sleeps)


def snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    """Every entry under `root` with size, content hash and mtime (directories by name only)."""
    snap: dict[str, tuple[Any, ...]] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            snap[rel] = ("dir",)
        else:
            stat = path.stat()
            snap[rel] = ("file", stat.st_size, hashlib.sha256(path.read_bytes()).hexdigest(), stat.st_mtime_ns)
    return snap


# ------------------------------------------------------------------------------------------------ experiment spies


def exp_result(letter: str, outcome: str = "passed", exit_code: int = 0, *, run_id: str | None = None,
               manifest: Path | None = None, message: str = "all good",
               failure: Failure | None = None) -> base.ExperimentResult:
    return base.ExperimentResult(exit_code=exit_code, outcome=outcome, experiment=letter, run_id=run_id,
                                 manifest_path=manifest, message=message, failure=failure)


class ExpSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, base.ExperimentContext]] = []
        self.result: base.ExperimentResult | None = None
        self.raises: BaseException | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "ExpSpy":
        for letter, module in (("A", exp_a), ("B", exp_b), ("C", exp_c)):
            monkeypatch.setattr(module, "run", self._make(letter))
        return self

    def _make(self, letter: str) -> Callable[[base.ExperimentContext], base.ExperimentResult]:
        def run(ctx: base.ExperimentContext) -> base.ExperimentResult:
            self.calls.append((letter, ctx))
            if self.raises is not None:
                raise self.raises
            return self.result if self.result is not None else exp_result(letter)

        return run


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> ExpSpy:
    return ExpSpy().install(monkeypatch)


# ------------------------------------------------------------------------------------------------ evidence and manifest fixtures


def jl(record: dict[str, Any]) -> bytes:
    return json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n"


def _env(record_type: str, run_id: str, recv: str, **fields: Any) -> dict[str, Any]:
    return {"record_type": record_type, "schema_version": 1, "run_id": run_id, "recv_time": recv, **fields}


def meta_rec(run_id: str, durability: str = "process") -> dict[str, Any]:
    return _env("run_metadata", run_id, "2025-01-01T12:00:00.100Z", event="created", durability=durability,
                fsync_before_ack=durability == "crash", evidence_file=f"{run_id}.jsonl", collector_version="scripted")


def sample_rec(run_id: str, seq: int, dropped: int = 0) -> dict[str, Any]:
    sample = {"run_id": run_id, "seq": seq, "ts": f"2025-01-01T12:00:{seq + 1:02d}.000Z", "rss_bytes": 1000 + seq,
              "request_count": 0, "loop_delay_ms": 1.0, "pid": 1, "container_id": CID,
              "container_id_source": "mountinfo", "interval_ms": 500, "dropped": dropped}
    return _env("sample", run_id, f"2025-01-01T12:00:{seq + 1:02d}.020Z", sample=sample)


def term_rec(run_id: str, cause: str, exit_code: int, oom: bool) -> dict[str, Any]:
    return _env("termination", run_id, "2025-01-01T12:00:09.600Z", source="experiment_runner",
                observed_time="2025-01-01T12:00:09.500Z",
                container_state={"status": "exited", "exit_code": exit_code, "oom_killed": oom},
                cause_label=cause, note="scripted")


def discontinuity_rec(run_id: str) -> dict[str, Any]:
    """Sequence numbers 2, 3 and 4 never arrived (3 missing Samples)."""
    return _env("gap", run_id, "2025-01-01T12:00:06.020Z", gap_id="g1", gap_type="sequence_discontinuity",
                start_time="2025-01-01T12:00:02.020Z", end_time="2025-01-01T12:00:05.020Z", first_missing_seq=2,
                last_missing_seq=4, missing_count=3, detected_at_seq=5, reconciliation="none", reconciles_with=[])


def drop_rec(run_id: str) -> dict[str, Any]:
    """The Probe reported 4 dropped Samples with no known range: an unreconciled, uncertain drop."""
    return _env("gap", run_id, "2025-01-01T12:00:07.020Z", gap_id="g2", gap_type="probe_reported_drop",
                start_time="2025-01-01T12:00:05.020Z", end_time="2025-01-01T12:00:06.020Z", dropped_count=4,
                range_status="unknown", first_missing_seq=None, last_missing_seq=None, detected_at_seq=6,
                reconciliation="none", reconciles_with=[])


def make_run(runs: Path, experiment: str, run_id: str, *, outcome: str = "passed", failure: Failure | None = None,
             durability: str = "process", records: list[dict[str, Any]] | None = None, raw_tail: bytes = b"",
             record_count: int | None = None, with_evidence: bool = True,
             state: tuple[str, int, bool, str] | None = None, extra: dict[str, Any] | None = None) -> Path:
    """Write `<runs>/<run_id>/manifest.json` (via write_manifest) and `evidence.jsonl`; return the run directory."""
    run_dir = runs / run_id
    records = records if records is not None else []
    container = None
    if state is not None:
        status, code, oom, inspected = state
        container = ContainerState(
            status=status, exit_code=code, oom_killed=oom, container_id=CID, host_config_memory_bytes=33554432,
            inspect_time=inspected, raw_inspect_path=f"runs/{run_id}/inspect.json",
            raw_inspect_excerpt=InspectExcerpt(id=CID, status=status, oom_killed=oom, exit_code=code, memory=33554432))
    evidence = None
    if with_evidence:
        evidence = EvidenceRef(container_path=f"/data/evidence/{run_id}.jsonl",
                               host_copy_path=f"runs/{run_id}/evidence.jsonl",
                               record_count=len(records) if record_count is None else record_count)
    blocks: dict[str, Any] = {"container_left_running": False, "sample_timing": None, "diagnostics": None,
                              "sampling_limitations": None, "evidence_summary": None, "limitations": None,
                              "clock_offset_ms": 0, "checks": []}
    blocks.update(extra or {})
    manifest = RunManifest(
        experiment=experiment, run_id=run_id, outcome=outcome, start_time="2025-01-01T12:00:00.000Z",
        end_time="2025-01-01T12:00:10.000Z", durability_level=durability, termination_description="scripted",
        expected=Expected(exit_code={"A": 1, "B": 137, "C": 137}[experiment], oom_killed=experiment == "C"),
        failure=failure, container_state=container, evidence=evidence, extra=blocks)
    write_manifest(runs, manifest)
    if with_evidence:
        (run_dir / "evidence.jsonl").write_bytes(b"".join(jl(r) for r in records) + raw_tail)
    return run_dir


def fixture_a(runs: Path, **overrides: Any) -> str:
    kwargs: dict[str, Any] = dict(
        records=[meta_rec(RID_A, "process"), *[sample_rec(RID_A, i) for i in range(3)],
                 term_rec(RID_A, "controlled_fatal_process_exit", 1, False)],
        state=("exited", 1, False, "2025-01-01T12:00:09.500Z"), durability="process")
    kwargs.update(overrides)
    make_run(runs, "A", RID_A, **kwargs)
    return RID_A


TIMING_B: dict[str, Any] = {
    "tolerance_ms": 250, "clock_offset_ms": 12, "container_finished_at": "2025-01-01T12:00:09.000Z",
    "pre_kill_count": 9, "pre_kill_late_received_count": 2, "within_tolerance_count": 1,
    "post_kill_beyond_tolerance": ["2025-01-01T12:00:09.700Z"],
    "after_container_stop": ["2025-01-01T12:00:10.100Z", "2025-01-01T12:00:10.600Z"],
    "after_container_stop_analysis": "available",
}


def fixture_b(runs: Path, **overrides: Any) -> str:
    kwargs: dict[str, Any] = dict(
        records=[meta_rec(RID_B, "crash"), *[sample_rec(RID_B, i) for i in range(4)],
                 term_rec(RID_B, "sigkill_sent_by_runner", 137, False)],
        state=("exited", 137, False, "2025-01-01T12:00:10.250Z"), durability="crash",
        extra={"sample_timing": dict(TIMING_B), "clock_offset_ms": 12})
    kwargs.update(overrides)
    make_run(runs, "B", RID_B, **kwargs)
    return RID_B


DIAG_C: dict[str, Any] = {"note": "informational", "rss_monotonic_tail": True, "peak_rss_ratio_of_limit": 0.9349,
                          "sample_count": 5}
LIMITS_C: dict[str, Any] = {"sample_interval_ms": 500, "approx_leak_rate_bytes_per_s": 4194304,
                            "statements": list(STATEMENTS)}


def fixture_c(runs: Path, **overrides: Any) -> str:
    kwargs: dict[str, Any] = dict(
        records=[meta_rec(RID_C, "crash"), *[sample_rec(RID_C, i) for i in range(5)],
                 term_rec(RID_C, "docker_oom_kill", 137, True)],
        state=("exited", 137, True, "2025-01-01T12:17:03.221Z"), durability="crash",
        extra={"diagnostics": dict(DIAG_C), "sampling_limitations": dict(LIMITS_C)})
    kwargs.update(overrides)
    make_run(runs, "C", RID_C, **kwargs)
    return RID_C


def summary_of(runs: Path, run_id: str, **kwargs: Any) -> Outcome:
    return run_main(["summary", run_id, "--runs-dir", str(runs)], **kwargs)


def label(name: str, value: str) -> str:
    """One summary line: label padded to 16 columns (contract section 4)."""
    return f"{name:<16}{value}"


EXPECTED_A = [
    "Run:            a-20250101T120000Z-9f3a1c2e  (Experiment A)",
    "Outcome:        passed",
    "Container:      status=exited exit_code=1 oom_killed=false (inspected 2025-01-01T12:00:09.500Z)",
    "Samples:        3",
    "Gaps:           0 total (missing samples, from sequence discontinuities only: 0)",
    "Unreconciled:   0 probe-reported drops (uncertain: no)",
    "Malformed:      0 lines quarantined (never counted as samples)",
    "Last sample:    2025-01-01T12:00:03.020Z",
    f"Termination:    controlled_fatal_process_exit; {UNOBSERVED}",
    "Durability:     process (no fsync before acknowledgement; evidence may be lost on a host crash)",
]
EXPECTED_B = [
    "Run:            b-20250101T120000Z-5d2e7b10  (Experiment B)",
    "Outcome:        passed",
    "Container:      status=exited exit_code=137 oom_killed=false (inspected 2025-01-01T12:00:10.250Z)",
    "Samples:        4",
    "Gaps:           0 total (missing samples, from sequence discontinuities only: 0)",
    "Unreconciled:   0 probe-reported drops (uncertain: no)",
    "Malformed:      0 lines quarantined (never counted as samples)",
    "Last sample:    2025-01-01T12:00:04.020Z",
    f"Termination:    sigkill_sent_by_runner; {UNOBSERVED}",
    "Durability:     crash (fsync before acknowledgement; storage caveats apply)",
    "Sample timing:  pre_kill=9 (late_received=2) within_tolerance=1 beyond_tolerance=1 after_container_stop=2 "
    "tolerance_ms=250 clock_offset_ms=12",
    "                anomaly beyond_tolerance: 2025-01-01T12:00:09.700Z",
    "                anomaly after_container_stop: 2025-01-01T12:00:10.100Z",
    "                anomaly after_container_stop: 2025-01-01T12:00:10.600Z",
]
EXPECTED_C = [
    "Run:            c-20250101T121500Z-4be07a11  (Experiment C)",
    "Outcome:        passed",
    "Container:      status=exited exit_code=137 oom_killed=true (inspected 2025-01-01T12:17:03.221Z)",
    "Samples:        5",
    "Gaps:           0 total (missing samples, from sequence discontinuities only: 0)",
    "Unreconciled:   0 probe-reported drops (uncertain: no)",
    "Malformed:      0 lines quarantined (never counted as samples)",
    "Last sample:    2025-01-01T12:00:05.020Z",
    f"Termination:    docker_oom_kill; {UNOBSERVED}",
    "Durability:     crash (fsync before acknowledgement; storage caveats apply)",
    "Diagnostics:    rss_monotonic_tail=true peak_rss_ratio_of_limit=0.93 sample_count=5 (informational, not pass/fail)",
    f"Limitations:    interval 500 ms; approx leak rate 4194304 bytes/s; {STATEMENTS[0]}; {STATEMENTS[1]}; {STATEMENTS[2]}",
]


# ================================================================================================ run: dispatch and exit codes


class TestRunDispatch:
    @pytest.mark.parametrize("letter", ["a", "b", "c"])
    def test_dispatches_to_the_matching_experiment_only(self, spy: ExpSpy, letter: str) -> None:
        docker, collector = FakeDocker(), FakeCollector()
        run_main(["run", letter], docker=docker, collector=collector)
        assert [c[0] for c in spy.calls] == [letter.upper()]
        ctx = spy.calls[0][1]
        assert ctx.experiment == letter.upper()
        assert ctx.docker is docker and ctx.collector is collector

    def test_defaults_reach_the_context(self, spy: ExpSpy) -> None:
        run_main(["run", "a"])
        ctx = spy.calls[0][1]
        assert ctx.runs_dir == Path("runs")
        assert ctx.mem_limit == "128m"
        assert ctx.leak_chunk_bytes == 4194304
        assert ctx.demo_url == "http://127.0.0.1:8080"
        assert ctx.sample_interval_ms is None and ctx.rand_hex is None

    def test_options_reach_the_context_verbatim(self, spy: ExpSpy, tmp_path: Path) -> None:
        run_main(["run", "c", "--runs-dir", str(tmp_path / "r"), "--mem-limit", "64M", "--leak-chunk-bytes", "1048576"])
        ctx = spy.calls[0][1]
        assert ctx.runs_dir == tmp_path / "r"
        assert ctx.mem_limit == "64M"
        assert ctx.leak_chunk_bytes == 1048576

    @pytest.mark.parametrize("chunk", [1, 104857600])
    def test_leak_chunk_boundaries_are_accepted(self, spy: ExpSpy, chunk: int) -> None:
        out = run_main(["run", "b", "--leak-chunk-bytes", str(chunk)])
        assert out.rc == 0
        assert spy.calls[0][1].leak_chunk_bytes == chunk

    def test_injected_clocks_and_http_are_forwarded(self, spy: ExpSpy) -> None:
        def transport(method: str, url: str, body: Any, timeout_s: float) -> HttpResponse:
            raise AssertionError("no request expected")

        def sleep(seconds: float) -> None:
            raise AssertionError("no sleep expected")

        def mono() -> float:
            return 1.0

        def wall() -> datetime:
            return datetime(2025, 1, 1, tzinfo=UTC)

        out, err = io.StringIO(), io.StringIO()
        cli.main(["run", "a"], docker=FakeDocker(), collector=FakeCollector(), http=transport, wall_clock=wall,
                 monotonic=mono, sleep=sleep, env={}, out=out, err=err)
        ctx = spy.calls[0][1]
        assert ctx.http is transport and ctx.sleep is sleep and ctx.monotonic is mono and ctx.wall_clock is wall

    def test_default_urls_and_port_overrides_come_from_env_only(self, spy: ExpSpy) -> None:
        def transport(method: str, url: str, body: Any, timeout_s: float) -> HttpResponse:
            raise AssertionError("no request expected")

        run_main(["run", "a"], http=transport)
        ctx = spy.calls[0][1]
        assert ctx.collector.base_url == "http://127.0.0.1:8081" and ctx.demo_url == "http://127.0.0.1:8080"
        run_main(["run", "a"], http=transport, env={"CORONER_COLLECTOR_PORT": "9091", "CORONER_DEMO_PORT": "9090"})
        ctx = spy.calls[1][1]
        assert ctx.collector.base_url == "http://127.0.0.1:9091" and ctx.demo_url == "http://127.0.0.1:9090"

    def test_windows_style_runs_dir_with_spaces(self, spy: ExpSpy, tmp_path: Path) -> None:
        raw = str(tmp_path / "my runs" / "sub dir").replace("/", "\\")
        run_main(["run", "a", "--runs-dir", raw])
        assert spy.calls[0][1].runs_dir == Path(raw)


class TestRunExitCodesAndOutput:
    @pytest.mark.parametrize(
        "outcome, code, failure",
        [
            ("passed", 0, None),
            ("failed", 1, Failure("evidence_unavailable", "Evidence could not be retrieved")),
            ("aborted", 1, Failure("collector_unhealthy", "Collector is not healthy")),
            ("failed", 2, Failure("docker_compose_failed", "docker compose up failed")),
        ],
    )
    def test_exit_code_is_the_result_exit_code(self, spy: ExpSpy, tmp_path: Path, outcome: str, code: int,
                                               failure: Failure | None) -> None:
        manifest = tmp_path / "runs" / RID_A / "manifest.json"
        spy.result = exp_result("A", outcome, code, run_id=RID_A, manifest=manifest, message="msg", failure=failure)
        out = run_main(["run", "a"])
        assert out.rc == code
        expected = [f"Experiment A {RID_A}: {outcome.upper()} - msg", "Manifest: " + manifest.as_posix()]
        if failure is not None:
            expected.append(f"Failure: {failure.code}: {failure.message}")
        assert out.out_lines == expected
        assert out.err == ""

    def test_manifest_line_uses_forward_slashes(self, spy: ExpSpy, tmp_path: Path) -> None:
        manifest = Path(str(tmp_path / "my runs").replace("/", "\\")) / RID_A / "manifest.json"
        spy.result = exp_result("A", run_id=RID_A, manifest=manifest)
        out = run_main(["run", "a"])
        line = out.out_lines[1]
        assert line.startswith("Manifest: ") and "\\" not in line and line.endswith(f"my runs/{RID_A}/manifest.json")

    def test_not_started_goes_to_stderr_and_exits_2(self, spy: ExpSpy) -> None:
        spy.result = exp_result("B", "not_started", 2, message="Docker is unavailable")
        out = run_main(["run", "b"])
        assert out.rc == 2
        assert out.out == ""
        assert out.err == "Experiment B -: NOT_STARTED - Docker is unavailable\n"

    def test_keyboard_interrupt_exits_1(self, spy: ExpSpy) -> None:
        spy.raises = KeyboardInterrupt()
        out = run_main(["run", "c"])
        assert out.rc == 1
        assert out.err == "Interrupted. The run manifest was marked aborted.\n"
        assert out.out == ""

    def test_lines_are_lf_ascii(self, spy: ExpSpy, tmp_path: Path) -> None:
        spy.result = exp_result("A", "failed", 1, run_id=RID_A, manifest=tmp_path / "m.json",
                                failure=Failure("x", "y"))
        out = run_main(["run", "a"])
        assert "\r" not in out.out and out.out.isascii() and out.out.endswith("\n")


class TestRunEnvironmentErrors:
    @pytest.mark.parametrize("letter", ["a", "b", "c"])
    def test_missing_docker_exits_2_without_run_id_manifest_or_run_dir(self, tmp_path: Path, letter: str) -> None:
        runs = tmp_path / "runs"
        out = run_main(["run", letter, "--runs-dir", str(runs)], docker=FakeDocker(available=False))
        assert out.rc == 2
        assert out.out == ""
        assert out.err == f"Experiment {letter.upper()} -: NOT_STARTED - Docker is unavailable\n"
        assert out.docker.calls == ["available"]
        assert not runs.exists()
        assert snapshot(tmp_path) == {}

    def test_docker_probe_that_raises_is_exit_2(self, tmp_path: Path) -> None:
        out = run_main(["run", "a", "--runs-dir", str(tmp_path / "runs")], docker=FakeDocker(available="raises"))
        assert out.rc == 2 and out.out == "" and out.err.startswith("Experiment A -: NOT_STARTED - ")
        assert snapshot(tmp_path) == {}


# ================================================================================================ run: argument errors


ARGPARSE_ERRORS = [
    [], ["bogus"], ["run"], ["run", "d"], ["run", "A"], ["run", ""], ["run", "a", "b"], ["run", "a", "--bogus"],
    ["run", "a", "--leak-chunk-bytes", "x"], ["run", "a", "--leak-chunk-bytes"], ["run", "a", "--repeat", "3"],
    ["run", "a", "--mem-limit"], ["summary"], ["summary", "--fetch", "x"], ["summary", RID_A, "--bogus"],
    ["clock-check", "--count", "0"], ["clock-check", "--count", "1001"], ["clock-check", "--count", "abc"],
    ["clock-check", "--bogus"],
]


class TestArgumentErrors:
    @pytest.mark.parametrize("argv", ARGPARSE_ERRORS, ids=[" ".join(a) or "(none)" for a in ARGPARSE_ERRORS])
    def test_argparse_errors_exit_2_and_touch_nothing(self, spy: ExpSpy, tmp_path: Path, argv: list[str]) -> None:
        out = run_main(argv)
        assert out.rc == 2
        assert out.out == "" and out.err != ""
        assert out.docker.calls == [] and out.collector.calls == []
        assert spy.calls == []
        assert snapshot(tmp_path) == {}

    @pytest.mark.parametrize("argv", [["--help"], ["run", "--help"], ["summary", "--help"], ["clock-check", "--help"]])
    def test_help_exits_0(self, argv: list[str], tmp_path: Path) -> None:
        out = run_main(argv)
        assert out.rc == 0
        assert out.docker.calls == [] and out.collector.calls == []
        assert out.out != ""
        assert snapshot(tmp_path) == {}

    def test_top_level_help_names_the_subcommands(self) -> None:
        text = run_main(["--help"]).out
        assert all(word in text for word in ("run", "summary", "clock-check"))

    @pytest.mark.parametrize("letter", ["a", "b", "c"])
    @pytest.mark.parametrize("value", ["abc", "0", "-1m", "128x", "", "0k", "1.5m", " 64m"])
    def test_invalid_mem_limit_exits_2_before_any_docker_call(self, tmp_path: Path, letter: str, value: str) -> None:
        """Real experiment code runs here: a missed pre-validation would reach `docker.available()`."""
        runs = tmp_path / "runs"
        out = run_main(["run", letter, "--runs-dir", str(runs), "--mem-limit", value])
        assert out.rc == 2
        assert out.out == ""
        assert "--mem-limit" in out.err and out.err.startswith("error: ")
        assert out.docker.calls == [] and out.collector.calls == []
        assert not runs.exists() and snapshot(tmp_path) == {}

    def test_zero_mem_limit_message_says_greater_than_zero(self) -> None:
        out = run_main(["run", "c", "--mem-limit", "0"])
        assert out.err == "error: invalid --mem-limit '0' (must be greater than zero)\n"

    @pytest.mark.parametrize("value", ["0", "-1", "104857601", "99999999999999999999"])
    def test_invalid_leak_chunk_bytes_exits_2_before_any_docker_call(self, tmp_path: Path, value: str) -> None:
        runs = tmp_path / "runs"
        out = run_main(["run", "c", "--runs-dir", str(runs), "--leak-chunk-bytes", value])
        assert out.rc == 2
        assert out.out == ""
        assert "--leak-chunk-bytes" in out.err
        assert out.docker.calls == [] and out.collector.calls == []
        assert snapshot(tmp_path) == {}

    @pytest.mark.parametrize("var", ["CORONER_COLLECTOR_PORT", "CORONER_DEMO_PORT"])
    @pytest.mark.parametrize("value", ["abc", "0", "65536", "-1", "", "80 ", "\uff18\uff10"])
    def test_invalid_port_env_exits_2_before_any_docker_call(self, tmp_path: Path, var: str, value: str) -> None:
        out = run_main(["run", "a", "--runs-dir", str(tmp_path / "runs")], env={var: value})
        assert out.rc == 2 and out.out == ""
        assert var in out.err
        assert out.docker.calls == []
        assert snapshot(tmp_path) == {}

    def test_mem_limit_is_checked_even_when_docker_is_unavailable(self) -> None:
        out = run_main(["run", "a", "--mem-limit", "nope"], docker=FakeDocker(available=False))
        assert out.rc == 2 and "--mem-limit" in out.err and out.docker.calls == []


class TestModuleEntryPoint:
    def test_dunder_main_is_a_thin_wrapper(self) -> None:
        source = (Path(cli.__file__).parent / "__main__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        assert "sys.exit(main())" in source
        assert not any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "main" for n in tree.body)

    def test_cli_main_never_calls_sys_exit(self) -> None:
        tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "exit" and getattr(n.func.value, "id", "") == "sys"]
        assert calls == []

    def test_python_dash_m_help_and_usage_error(self, monkeypatch: pytest.MonkeyPatch,
                                                capsys: pytest.CaptureFixture[str]) -> None:
        for argv, code in ((["--help"], 0), ([], 2), (["run", "z"], 2)):
            monkeypatch.setattr(sys, "argv", ["python -m coroner.runner", *argv])
            with pytest.raises(SystemExit) as info:
                runpy.run_module("coroner.runner", run_name="__main__")
            assert info.value.code == code
        assert "clock-check" in capsys.readouterr().out

    def test_python_dash_m_summary_missing_run_exits_2(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setattr(sys, "argv", ["python -m coroner.runner", "summary", UNKNOWN_RID])
        with pytest.raises(SystemExit) as info:
            runpy.run_module("coroner.runner", run_name="__main__")
        assert info.value.code == 2
        assert capsys.readouterr().out == ""
        assert snapshot(tmp_path) == {}


# ================================================================================================ summary: content


class TestSummaryContent:
    def test_experiment_a_full_output(self, tmp_path: Path) -> None:
        rid = fixture_a(tmp_path / "runs")
        out = summary_of(tmp_path / "runs", rid)
        assert out.rc == 0 and out.err == ""
        assert out.out_lines == EXPECTED_A
        assert out.out.endswith("\n") and "\r" not in out.out and out.out.isascii()

    def test_experiment_b_full_output_with_timing_classification(self, tmp_path: Path) -> None:
        rid = fixture_b(tmp_path / "runs")
        out = summary_of(tmp_path / "runs", rid)
        assert out.rc == 0 and out.err == ""
        assert out.out_lines == EXPECTED_B

    def test_experiment_c_full_output_with_diagnostics_and_limitations(self, tmp_path: Path) -> None:
        rid = fixture_c(tmp_path / "runs")
        out = summary_of(tmp_path / "runs", rid)
        assert out.rc == 0 and out.err == ""
        assert out.out_lines == EXPECTED_C
        # all three sampling-limitation statements are present
        assert all(s in out.out for s in STATEMENTS)

    def test_clean_runs_have_no_warning_failure_or_left_running_lines(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        for build in (fixture_a, fixture_b, fixture_c):
            out = summary_of(runs, build(runs))
            assert not [ln for ln in out.out_lines if ln.startswith(("Warning:", "Failure:", "Container left running"))]

    def test_experiment_gating(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        # A carrying B and C blocks prints neither; B carrying C blocks prints no Diagnostics; C carrying B prints no timing.
        extras = {"sample_timing": dict(TIMING_B), "diagnostics": dict(DIAG_C), "sampling_limitations": dict(LIMITS_C)}
        a = summary_of(runs, fixture_a(runs, extra=extras)).out
        assert not any(w in a for w in ("Sample timing", "Diagnostics", "Limitations", "anomaly"))
        assert "exception" not in a.lower()
        b = summary_of(runs, fixture_b(runs, extra=extras)).out
        assert "Sample timing:" in b and "Diagnostics" not in b and "Limitations" not in b
        c = summary_of(runs, fixture_c(runs, extra=extras)).out
        assert "Diagnostics:" in c and "Limitations:" in c and "Sample timing" not in c

    def test_failed_manifest_prints_failure_and_exits_0(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        failure = Failure("evidence_unavailable", "Evidence could not be retrieved")
        rid = fixture_a(runs, outcome="failed", failure=failure)
        out = summary_of(runs, rid)
        assert out.rc == 0
        assert out.out_lines[1:3] == ["Outcome:        failed",
                                      "Failure:        evidence_unavailable: Evidence could not be retrieved"]

    def test_aborted_manifest_without_container_state(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        rid = fixture_b(runs, outcome="aborted", failure=Failure("interrupted", "Run interrupted"), state=None)
        out = summary_of(runs, rid)
        assert out.rc == 0
        assert "Outcome:        aborted" in out.out_lines
        assert "Failure:        interrupted: Run interrupted" in out.out_lines
        assert "Container:      not recorded" in out.out_lines

    def test_container_left_running_line(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        rid = fixture_b(runs, extra={"sample_timing": dict(TIMING_B), "container_left_running": True})
        assert "Container left running: yes" in summary_of(runs, rid).out_lines

    def test_container_state_flags_are_lowercase_booleans(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        out = summary_of(runs, fixture_a(runs, state=("running", 0, False, "2025-01-01T12:00:05.000Z")))
        assert "Container:      status=running exit_code=0 oom_killed=false (inspected 2025-01-01T12:00:05.000Z)" in out.out_lines

    def test_b_timing_with_negative_offset_and_empty_lists(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        timing = dict(TIMING_B, clock_offset_ms=-7, pre_kill_count=4, pre_kill_late_received_count=0,
                      within_tolerance_count=0, post_kill_beyond_tolerance=[], after_container_stop=[])
        out = summary_of(runs, fixture_b(runs, extra={"sample_timing": timing}))
        assert ("Sample timing:  pre_kill=4 (late_received=0) within_tolerance=0 beyond_tolerance=0 "
                "after_container_stop=0 tolerance_ms=250 clock_offset_ms=-7") in out.out_lines
        assert not [ln for ln in out.out_lines if "anomaly" in ln]

    def test_b_timing_recorded_zeros_render_as_0(self, tmp_path: Path) -> None:
        """A stored 0 is a value, not a missing value: every `_value` field prints `0`, never `unknown`."""
        runs = tmp_path / "runs"
        timing = dict(TIMING_B, tolerance_ms=0, clock_offset_ms=0, pre_kill_count=0, pre_kill_late_received_count=0,
                      within_tolerance_count=0, post_kill_beyond_tolerance=[], after_container_stop=[])
        out = summary_of(runs, fixture_b(runs, extra={"sample_timing": timing}))
        assert out.rc == 0
        assert ("Sample timing:  pre_kill=0 (late_received=0) within_tolerance=0 beyond_tolerance=0 "
                "after_container_stop=0 tolerance_ms=0 clock_offset_ms=0") in out.out_lines
        assert "unknown" not in next(ln for ln in out.out_lines if ln.startswith("Sample timing:"))

    def test_b_timing_null_or_missing_values_render_unknown_never_0(self, tmp_path: Path) -> None:
        """None and absent keys print `unknown` (contract: `None` as `unknown`); lists that are not lists count unknown."""
        runs = tmp_path / "runs"
        timing = {"tolerance_ms": None, "clock_offset_ms": None, "container_finished_at": None,
                  "pre_kill_late_received_count": None, "within_tolerance_count": None,
                  "post_kill_beyond_tolerance": None, "after_container_stop": None,
                  "after_container_stop_analysis": "unavailable"}  # pre_kill_count key is absent
        out = summary_of(runs, fixture_b(runs, extra={"sample_timing": timing}))
        assert out.rc == 0
        expected = ("Sample timing:  pre_kill=unknown (late_received=unknown) within_tolerance=unknown "
                    "beyond_tolerance=unknown after_container_stop=unknown tolerance_ms=unknown "
                    "clock_offset_ms=unknown")
        assert expected in out.out_lines
        line = next(ln for ln in out.out_lines if ln.startswith("Sample timing:"))
        assert "=0" not in line and "None" not in line
        assert not [ln for ln in out.out_lines if "anomaly" in ln]

    def test_b_timing_zero_and_unknown_differ_in_the_same_output(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        timing = {"tolerance_ms": 250, "clock_offset_ms": None, "container_finished_at": None,
                  "pre_kill_count": 7, "pre_kill_late_received_count": 0, "within_tolerance_count": 0,
                  "post_kill_beyond_tolerance": [], "after_container_stop": None,
                  "after_container_stop_analysis": "unavailable"}
        out = summary_of(runs, fixture_b(runs, extra={"sample_timing": timing}))
        assert out.rc == 0
        assert ("Sample timing:  pre_kill=7 (late_received=0) within_tolerance=0 beyond_tolerance=0 "
                "after_container_stop=unknown tolerance_ms=250 clock_offset_ms=unknown") in out.out_lines
        # and the reverse pairing: a recorded zero offset next to a missing count
        timing2 = {"tolerance_ms": 250, "clock_offset_ms": 0, "pre_kill_count": None,
                   "pre_kill_late_received_count": 3, "within_tolerance_count": None,
                   "post_kill_beyond_tolerance": [], "after_container_stop": []}
        runs2 = tmp_path / "runs2"
        out2 = summary_of(runs2, fixture_b(runs2, extra={"sample_timing": timing2}))
        assert ("Sample timing:  pre_kill=unknown (late_received=3) within_tolerance=unknown beyond_tolerance=0 "
                "after_container_stop=0 tolerance_ms=250 clock_offset_ms=0") in out2.out_lines

    def test_b_timing_and_c_blocks_absent_are_not_recorded(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        b = summary_of(runs, fixture_b(runs, extra={"sample_timing": None}))
        assert "Sample timing:  not recorded" in b.out_lines
        c = summary_of(runs, fixture_c(runs, extra={"diagnostics": None, "sampling_limitations": None}))
        assert "Diagnostics:    not recorded" in c.out_lines and "Limitations:    not recorded" in c.out_lines

    def test_c_null_values_are_unknown_not_invented(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        diag = {"note": "n", "rss_monotonic_tail": None, "peak_rss_ratio_of_limit": None, "sample_count": 5}
        limits = {"sample_interval_ms": None, "approx_leak_rate_bytes_per_s": None, "statements": list(STATEMENTS)}
        out = summary_of(runs, fixture_c(runs, extra={"diagnostics": diag, "sampling_limitations": limits}))
        lines = out.out_lines
        assert ("Diagnostics:    rss_monotonic_tail=unknown peak_rss_ratio_of_limit=unknown sample_count=5 "
                "(informational, not pass/fail)") in lines
        limit_line = next(ln for ln in lines if ln.startswith("Limitations:"))
        assert "unknown" in limit_line and "None" not in limit_line
        assert all(s in limit_line for s in STATEMENTS)
        import re
        assert not re.search(r"interval \d|rate \d", limit_line)

    @pytest.mark.parametrize("ratio, text", [(1.0, "1.00"), (0.5, "0.50"), (0.9349, "0.93"), (0.0, "0.00")])
    def test_peak_ratio_has_two_decimals(self, tmp_path: Path, ratio: float, text: str) -> None:
        runs = tmp_path / "runs"
        out = summary_of(runs, fixture_c(runs, extra={"diagnostics": dict(DIAG_C, peak_rss_ratio_of_limit=ratio),
                                                      "sampling_limitations": dict(LIMITS_C)}))
        assert f"peak_rss_ratio_of_limit={text} " in out.out

    def test_rss_monotonic_false_is_false_not_unknown(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        out = summary_of(runs, fixture_c(runs, extra={"diagnostics": dict(DIAG_C, rss_monotonic_tail=False),
                                                      "sampling_limitations": dict(LIMITS_C)}))
        assert "rss_monotonic_tail=false " in out.out

    @pytest.mark.parametrize("level, text", [
        ("crash", "crash (fsync before acknowledgement; storage caveats apply)"),
        ("process", "process (no fsync before acknowledgement; evidence may be lost on a host crash)"),
    ])
    def test_durability_wording(self, tmp_path: Path, level: str, text: str) -> None:
        runs = tmp_path / "runs"
        records = [meta_rec(RID_A, level), sample_rec(RID_A, 0)]
        out = summary_of(runs, fixture_a(runs, durability=level, records=records))
        assert f"Durability:     {text}" in out.out_lines

    def test_missing_samples_are_reported_from_the_discontinuity(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        records = [meta_rec(RID_A), sample_rec(RID_A, 0), sample_rec(RID_A, 1), discontinuity_rec(RID_A),
                   sample_rec(RID_A, 5), sample_rec(RID_A, 6)]
        out = summary_of(runs, fixture_a(runs, records=records))
        assert out.rc == 0
        assert "Samples:        4" in out.out_lines
        assert "Gaps:           1 total (missing samples, from sequence discontinuities only: 3)" in out.out_lines
        assert "Unreconciled:   0 probe-reported drops (uncertain: no)" in out.out_lines
        assert "Last sample:    2025-01-01T12:00:07.020Z" in out.out_lines
        assert "Termination:    unknown" in out.out_lines  # no termination record: cause unknown, no unobserved note
        assert not [ln for ln in out.out_lines if ln.startswith("Warning:")]

    def test_unreconciled_probe_drops_are_reported_as_uncertain(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        records = [meta_rec(RID_A), sample_rec(RID_A, 0), drop_rec(RID_A), sample_rec(RID_A, 1)]
        out = summary_of(runs, fixture_a(runs, records=records))
        assert "Gaps:           1 total (missing samples, from sequence discontinuities only: 0)" in out.out_lines
        assert "Unreconciled:   4 probe-reported drops (uncertain: yes)" in out.out_lines
        assert "Samples:        2" in out.out_lines

    def test_missing_and_unreconciled_together_with_malformed_lines(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        records = [meta_rec(RID_A), sample_rec(RID_A, 0), discontinuity_rec(RID_A), drop_rec(RID_A),
                   sample_rec(RID_A, 5), term_rec(RID_A, "controlled_fatal_process_exit", 1, False)]
        out = summary_of(runs, fixture_a(runs, records=records, raw_tail=b"{not json\n[1,2]\n"))
        assert out.rc == 0
        lines = out.out_lines
        assert "Samples:        2" in lines
        assert "Gaps:           2 total (missing samples, from sequence discontinuities only: 3)" in lines
        assert "Unreconciled:   4 probe-reported drops (uncertain: yes)" in lines
        assert "Malformed:      2 lines quarantined (never counted as samples)" in lines
        at = lines.index("Malformed:      2 lines quarantined (never counted as samples)")
        assert lines[at + 1:at + 3] == ["                line 7: invalid_json, 9 bytes",
                                        "                line 8: not_an_object, 5 bytes"]
        assert "Last sample:    2025-01-01T12:00:06.020Z" in lines

    def test_at_most_five_malformed_detail_lines(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        out = summary_of(runs, fixture_a(runs, raw_tail=b"{x\n" * 8))
        lines = out.out_lines
        assert "Malformed:      8 lines quarantined (never counted as samples)" in lines
        detail = [ln for ln in lines if ln.startswith("                line ")]
        assert detail == [f"                line {n}: invalid_json, 2 bytes" for n in range(6, 11)]

    def test_zero_byte_evidence_is_not_an_error(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        out = summary_of(runs, fixture_a(runs, records=[], state=None))
        assert out.rc == 0
        assert "Samples:        0" in out.out_lines and "Last sample:    none received" in out.out_lines
        assert "Termination:    unknown" in out.out_lines

    def test_manifest_without_samples_shows_none_received_not_a_time(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        out = summary_of(runs, fixture_b(runs, records=[meta_rec(RID_B, "crash")]))
        assert "Last sample:    none received" in out.out_lines
        assert "Samples:        0" in out.out_lines

    def test_warnings_for_record_count_and_durability_disagreements(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        out = summary_of(runs, fixture_a(runs, record_count=99))
        assert out.rc == 0
        assert [ln for ln in out.out_lines if ln.startswith("Warning:")]
        out = summary_of(runs, fixture_a(runs, durability="crash"))  # evidence metadata says "process"
        assert out.rc == 0
        assert [ln for ln in out.out_lines if ln.startswith("Warning:")]

    def test_evidence_is_located_from_runs_dir_not_the_stored_relative_path(self, tmp_path: Path) -> None:
        runs = tmp_path / "somewhere else" / "runs dir"
        rid = fixture_a(runs)  # stored host_copy_path is "runs/<id>/evidence.jsonl", which does not exist from cwd
        assert not Path("runs").exists()
        assert summary_of(runs, rid).out_lines == EXPECTED_A

    def test_windows_backslash_runs_dir_and_default_runs_dir(self, tmp_path: Path) -> None:
        runs = tmp_path / "my runs"
        rid = fixture_a(runs)
        backslashes = str(runs).replace("/", "\\")
        assert run_main(["summary", rid, "--runs-dir", backslashes]).out_lines == EXPECTED_A
        assert run_main(["summary", rid, "--runs-dir", str(runs) + "\\"]).out_lines == EXPECTED_A
        default = tmp_path / "runs"
        fixture_a(default)
        assert run_main(["summary", rid]).out_lines == EXPECTED_A  # default `runs` relative to cwd (= tmp_path)

    def test_output_does_not_depend_on_cwd_or_clock(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        runs = tmp_path / "runs"
        rid = fixture_c(runs)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        first = summary_of(runs, rid).out
        monkeypatch.chdir(elsewhere)

        def no_clock() -> datetime:
            raise AssertionError("summary must not read the clock")

        second = summary_of(runs, rid, wall_clock=no_clock, monotonic=no_clock).out
        assert first == second

    def test_summary_never_touches_docker_collector_or_sleep(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        out = summary_of(runs, fixture_c(runs))
        assert out.docker.calls == [] and out.collector.calls == [] and out.sleeps == []

    def test_build_summary_and_render_api(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        rid = fixture_c(runs)
        built = summary_mod.build_summary(runs, rid)
        assert (built.run_id, built.experiment, built.outcome, built.sample_count) == (rid, "C", "passed", 5)
        assert (built.missing_sample_count, built.unreconciled_drop_count, built.malformed_line_count) == (0, 0, 0)
        assert built.last_sample_time == "2025-01-01T12:00:05.020Z"
        assert summary_mod.summary_exit_code(built) == 0
        assert summary_mod.render_summary(built).splitlines() == EXPECTED_C


# ================================================================================================ summary: error paths


def _write_raw(runs: Path, run_id: str, data: bytes) -> None:
    (runs / run_id).mkdir(parents=True, exist_ok=True)
    (runs / run_id / "manifest.json").write_bytes(data)


def _valid_manifest_dict(tmp_path: Path) -> dict[str, Any]:
    scratch = tmp_path / "scratch"
    fixture_a(scratch)
    data = json.loads((scratch / RID_A / "manifest.json").read_text(encoding="utf-8"))
    for item in sorted(scratch.rglob("*"), reverse=True):
        item.unlink() if item.is_file() else item.rmdir()
    scratch.rmdir()
    return data


def case_unknown_run_in_populated_runs_dir(tmp_path: Path) -> tuple[Path, str]:
    runs = tmp_path / "runs"
    fixture_a(runs)
    return runs, UNKNOWN_RID


def case_runs_dir_missing(tmp_path: Path) -> tuple[Path, str]:
    return tmp_path / "never created", UNKNOWN_RID


def case_run_dir_without_manifest(tmp_path: Path) -> tuple[Path, str]:
    (tmp_path / "runs" / UNKNOWN_RID).mkdir(parents=True)
    return tmp_path / "runs", UNKNOWN_RID


def case_manifest_invalid_json(tmp_path: Path) -> tuple[Path, str]:
    _write_raw(tmp_path / "runs", RID_A, b"{this is not json")
    return tmp_path / "runs", RID_A


def case_manifest_empty(tmp_path: Path) -> tuple[Path, str]:
    _write_raw(tmp_path / "runs", RID_A, b"")
    return tmp_path / "runs", RID_A


def case_manifest_invalid_utf8(tmp_path: Path) -> tuple[Path, str]:
    _write_raw(tmp_path / "runs", RID_A, b"\xff\xfe{\"a\": 1}")
    return tmp_path / "runs", RID_A


def case_manifest_json_array(tmp_path: Path) -> tuple[Path, str]:
    _write_raw(tmp_path / "runs", RID_A, b"[1, 2, 3]\n")
    return tmp_path / "runs", RID_A


def case_manifest_wrong_schema_version(tmp_path: Path) -> tuple[Path, str]:
    data = _valid_manifest_dict(tmp_path)
    data["schema_version"] = 2
    _write_raw(tmp_path / "runs", RID_A, json.dumps(data).encode("utf-8"))
    return tmp_path / "runs", RID_A


def case_manifest_missing_fields(tmp_path: Path) -> tuple[Path, str]:
    data = _valid_manifest_dict(tmp_path)
    del data["outcome"], data["expected"]
    _write_raw(tmp_path / "runs", RID_A, json.dumps(data).encode("utf-8"))
    return tmp_path / "runs", RID_A


def case_manifest_bad_outcome(tmp_path: Path) -> tuple[Path, str]:
    data = _valid_manifest_dict(tmp_path)
    data["outcome"] = "exploded"
    _write_raw(tmp_path / "runs", RID_A, json.dumps(data).encode("utf-8"))
    return tmp_path / "runs", RID_A


def case_manifest_belongs_to_another_run(tmp_path: Path) -> tuple[Path, str]:
    data = _valid_manifest_dict(tmp_path)
    other = "a-20250101T120000Z-11111111"
    _write_raw(tmp_path / "runs", other, json.dumps(data).encode("utf-8"))
    (tmp_path / "runs" / other / "evidence.jsonl").write_bytes(b"")
    return tmp_path / "runs", other


def case_manifest_is_a_directory(tmp_path: Path) -> tuple[Path, str]:
    (tmp_path / "runs" / RID_A / "manifest.json").mkdir(parents=True)
    return tmp_path / "runs", RID_A


def case_no_evidence_block_after_collector_failure(tmp_path: Path) -> tuple[Path, str]:
    runs = tmp_path / "runs"
    make_run(runs, "A", RID_A, outcome="aborted", failure=Failure("collector_unhealthy", "Collector is not healthy"),
             with_evidence=False)
    return runs, RID_A


def case_evidence_file_missing(tmp_path: Path) -> tuple[Path, str]:
    runs = tmp_path / "runs"
    fixture_a(runs)
    (runs / RID_A / "evidence.jsonl").unlink()
    return runs, RID_A


def case_evidence_all_garbage(tmp_path: Path) -> tuple[Path, str]:
    runs = tmp_path / "runs"
    fixture_a(runs, records=[], raw_tail=b"not json at all\n{broken\n\xff\xfe\n")
    return runs, RID_A


ERROR_CASES = [
    case_unknown_run_in_populated_runs_dir, case_runs_dir_missing, case_run_dir_without_manifest,
    case_manifest_invalid_json, case_manifest_empty, case_manifest_invalid_utf8, case_manifest_json_array,
    case_manifest_wrong_schema_version, case_manifest_missing_fields, case_manifest_bad_outcome,
    case_manifest_belongs_to_another_run, case_manifest_is_a_directory,
    case_no_evidence_block_after_collector_failure, case_evidence_file_missing, case_evidence_all_garbage,
]


class TestSummaryErrorPaths:
    @pytest.mark.parametrize("case", ERROR_CASES, ids=[c.__name__[5:] for c in ERROR_CASES])
    def test_error_exits_2_with_message_and_changes_nothing(self, tmp_path: Path, case: Callable[..., Any]) -> None:
        runs, run_id = case(tmp_path)
        before = snapshot(tmp_path)
        out = summary_of(runs, run_id)
        assert out.rc == 2
        assert out.out == ""
        assert out.err.startswith("error: ") and out.err.endswith("\n") and out.err.count("\n") == 1
        assert out.docker.calls == [] and out.collector.calls == []
        assert snapshot(tmp_path) == before

    def test_unknown_run_names_the_run_and_does_not_create_the_runs_dir(self, tmp_path: Path) -> None:
        runs = tmp_path / "nothing here"
        out = summary_of(runs, UNKNOWN_RID)
        assert out.rc == 2 and UNKNOWN_RID in out.err and "not found" in out.err
        assert not runs.exists()

    def test_missing_evidence_message_carries_outcome_and_failure_code(self, tmp_path: Path) -> None:
        runs, rid = case_no_evidence_block_after_collector_failure(tmp_path)
        out = summary_of(runs, rid)
        assert rid in out.err and "aborted" in out.err and "collector_unhealthy" in out.err

    @pytest.mark.parametrize("bad", ["", "../x", "..\\..\\evil", "a/b", "a\\b", ".", "..", "-x", "x y", "a" * 65,
                                     "C:\\evil", "a-20250101T120000Z-9f3a1c2e/../../x"])
    def test_invalid_run_id_exits_2_without_building_a_path(self, tmp_path: Path, bad: str) -> None:
        fixture_a(tmp_path / "runs")
        before = snapshot(tmp_path)
        out = summary_of(tmp_path / "runs", bad)
        assert out.rc == 2 and out.out == "" and out.err.startswith("error: ")
        assert snapshot(tmp_path) == before

    def test_unexpected_exception_is_reported_not_swallowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fixture_a(tmp_path / "runs")

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("boom")

        patch_everywhere(monkeypatch, "build_summary", boom)
        out = summary_of(tmp_path / "runs", RID_A)
        assert out.rc == 2 and out.out == "" and out.err == "error: RuntimeError: boom\n"

    @pytest.mark.parametrize("build", [fixture_a, fixture_b, fixture_c])
    def test_successful_summary_is_read_only(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                             build: Callable[..., str]) -> None:
        runs = tmp_path / "runs"
        rid = build(runs)
        before = snapshot(tmp_path)
        real_open = open

        def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if any(flag in mode for flag in "wax+"):
                raise AssertionError(f"summary opened {file!r} for writing")
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr("builtins.open", guarded_open)
        monkeypatch.setattr("io.open", guarded_open)
        out = summary_of(runs, rid)
        monkeypatch.undo()
        assert out.rc == 0
        assert snapshot(tmp_path) == before


# ================================================================================================ clock-check


class ClockSim:
    """Synthetic host clock plus Collector. Time advances only inside the transport, so extra clock reads are harmless.

    A script step is `(offset_ms, rtt_ms)` (Collector clock minus host midpoint, round trip), or "down", "503",
    "badtime". The Collector's `server_time` is formatted here, independently of production code.
    """

    def __init__(self, script: list[Any]) -> None:
        self.now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        self.script = list(script)
        self.requests: list[tuple[str, str]] = []

    def wall(self) -> datetime:
        return self.now

    @staticmethod
    def stamp(moment: datetime) -> str:
        return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"

    def transport(self, method: str, url: str, body: Any, timeout_s: float) -> HttpResponse:
        self.requests.append((method, url))
        step = self.script.pop(0)
        if step == "down":
            raise ConnectionRefusedError("collector down")
        if step == "503":
            return HttpResponse(503, b"{}")
        if step == "badtime":
            return HttpResponse(200, json.dumps({"status": "ok", "storage_ok": True, "server_time": "garbage"}).encode())
        offset_ms, rtt_ms = step
        half = timedelta(milliseconds=rtt_ms / 2)
        self.now += half
        server = self.now + timedelta(milliseconds=offset_ms)
        self.now += half
        payload = {"status": "ok", "storage_ok": True, "durability": "process", "server_time": self.stamp(server)}
        return HttpResponse(200, json.dumps(payload).encode())


def clock_check(script: list[Any], count: int, *extra: str, main_fn: Callable[..., int] | None = None,
                env: dict[str, str] | None = None) -> tuple[Outcome, ClockSim]:
    sim = ClockSim(script)
    out = run_main(["clock-check", "--count", str(count), *extra], main_fn=main_fn, http=sim.transport,
                   wall_clock=sim.wall, env=env)
    return out, sim


class TestClockCheck:
    def test_full_output_for_three_measurements(self) -> None:
        out, sim = clock_check([(12, 4), (10, 2), (14, 8)], 3)
        assert out.rc == 0 and out.err == ""
        assert out.out_lines == [
            "clock-check:    collector=http://127.0.0.1:8081",
            "Measured at:    2025-01-01T12:00:00.000Z",
            "Measurements:   3 of 3 usable",
            "Offset:         +10 ms (positive = Collector clock ahead of host; host time = collector time - offset)",
            "Uncertainty:    1.0 ms (half the round trip of the best measurement)",
            "Offset median:  +12 ms",
            "Offset range:   +10 ms to +14 ms (spread 4 ms)",
            "Round trip:     min 2.0 ms, median 4.0 ms, max 8.0 ms",
            "Tolerance:      250 ms (documented Experiment B clock tolerance; never changed automatically)",
        ]
        assert sim.requests == [("GET", "http://127.0.0.1:8081/healthz")] * 3
        assert out.sleeps == [0.1, 0.1]
        assert out.docker.calls == []

    def test_collector_ahead_is_a_positive_offset_with_half_rtt_uncertainty(self) -> None:
        """Literal values: host midpoint 12:00:00.005, server 12:00:00.055 -> +50 ms; rtt 10 ms -> 5.0 ms."""
        clock = {"now": datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)}

        def transport(method: str, url: str, body: Any, timeout_s: float) -> HttpResponse:
            clock["now"] += timedelta(milliseconds=10)
            payload = {"status": "ok", "storage_ok": True, "server_time": "2025-01-01T12:00:00.055Z"}
            return HttpResponse(200, json.dumps(payload).encode())

        out = run_main(["clock-check", "--count", "1"], http=transport, wall_clock=lambda: clock["now"])
        assert out.rc == 0
        assert out.out_lines[3].startswith("Offset:         +50 ms (positive = Collector clock ahead of host")
        assert out.out_lines[4].startswith("Uncertainty:    5.0 ms")
        assert out.sleeps == []

    def test_collector_behind_is_a_negative_offset(self) -> None:
        clock = {"now": datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)}

        def transport(method: str, url: str, body: Any, timeout_s: float) -> HttpResponse:
            clock["now"] += timedelta(milliseconds=4)
            payload = {"status": "ok", "storage_ok": True, "server_time": "2025-01-01T11:59:59.980Z"}
            return HttpResponse(200, json.dumps(payload).encode())

        out = run_main(["clock-check", "--count", "1"], http=transport, wall_clock=lambda: clock["now"])
        assert out.out_lines[3].startswith("Offset:         -22 ms (positive = Collector clock ahead")
        assert out.out_lines[4].startswith("Uncertainty:    2.0 ms")

    def test_zero_offset_is_plus_zero(self) -> None:
        out, _ = clock_check([(0, 2)], 1)
        assert out.out_lines[3].startswith("Offset:         +0 ms ")
        assert "Offset median:  +0 ms" in out.out_lines
        assert "Offset range:   +0 ms to +0 ms (spread 0 ms)" in out.out_lines

    def test_best_measurement_is_the_smallest_round_trip_ties_earliest(self) -> None:
        out, _ = clock_check([(30, 6), (21, 2), (-5, 2), (40, 10), (11, 8)], 5)
        assert out.out_lines[3].startswith("Offset:         +21 ms ")  # rtt 2 appears twice: earliest wins
        assert out.out_lines[4].startswith("Uncertainty:    1.0 ms")
        assert "Offset median:  +21 ms" in out.out_lines  # sorted -5, 11, 21, 30, 40
        assert "Offset range:   -5 ms to +40 ms (spread 45 ms)" in out.out_lines
        assert "Round trip:     min 2.0 ms, median 6.0 ms, max 10.0 ms" in out.out_lines

    def test_even_count_offset_median_is_the_lower_middle_and_round_trip_median_is_the_mean(self) -> None:
        """Contract section 5: offset median is `median_low` (20, not 25). The contract does not define the round-trip
        median for even counts; the conventional mean of the two middle values is expected (4 and 6 -> 5.0)."""
        out, _ = clock_check([(40, 10), (10, 2), (30, 6), (20, 4)], 4)
        assert "Measurements:   4 of 4 usable" in out.out_lines
        assert "Offset median:  +20 ms" in out.out_lines  # sorted 10, 20, 30, 40
        assert "Offset range:   +10 ms to +40 ms (spread 30 ms)" in out.out_lines
        assert "Round trip:     min 2.0 ms, median 5.0 ms, max 10.0 ms" in out.out_lines  # sorted 2, 4, 6, 10

    def test_repeated_measurements_use_the_count_and_sleep_between_only(self) -> None:
        out, sim = clock_check([(1, 2)] * 7, 7)
        assert "Measurements:   7 of 7 usable" in out.out_lines
        assert len(sim.requests) == 7 and out.sleeps == [0.1] * 6

    def test_default_count_is_twenty(self) -> None:
        sim = ClockSim([(3, 2)] * 20)
        out = run_main(["clock-check"], http=sim.transport, wall_clock=sim.wall)
        assert out.rc == 0 and "Measurements:   20 of 20 usable" in out.out_lines
        assert len(sim.requests) == 20 and len(out.sleeps) == 19

    def test_count_bounds(self) -> None:
        out, sim = clock_check([(1, 2)], 1)
        assert out.rc == 0 and len(sim.requests) == 1
        sim = ClockSim([(1, 2)] * 1000)
        big = run_main(["clock-check", "--count", "1000"], http=sim.transport, wall_clock=sim.wall)
        assert big.rc == 0 and len(sim.requests) == 1000

    def test_failed_measurements_are_counted_and_do_not_abort(self) -> None:
        out, sim = clock_check([(8, 4), "down", "503", "badtime", (9, 6)], 5)
        assert out.rc == 0
        assert "Measurements:   2 of 5 usable" in out.out_lines
        failures = [ln for ln in out.out_lines if ln.startswith("Failures:")]
        assert len(failures) == 1 and failures[0].startswith("Failures:       3 (last: ")
        assert out.out_lines[3].startswith("Offset:         +8 ms ")
        assert len(sim.requests) == 5 and len(out.sleeps) == 4

    @pytest.mark.parametrize("script", [["down", "down"], ["503", "503"], ["badtime", "badtime"], ["down", "503"]])
    def test_no_usable_measurement_exits_2_with_one_line_on_stdout(self, script: list[Any]) -> None:
        out, _ = clock_check(script, 2)
        assert out.rc == 2
        assert len(out.out_lines) == 1
        assert out.out_lines[0].startswith("error: no usable clock measurement from http://127.0.0.1:8081 (2 attempts): ")
        assert out.err == ""

    def test_collector_url_option_and_env_port(self) -> None:
        out, sim = clock_check([(1, 2)], 1, "--collector-url", "http://collector.test:9000")
        assert out.out_lines[0] == "clock-check:    collector=http://collector.test:9000"
        assert sim.requests == [("GET", "http://collector.test:9000/healthz")]
        out, sim = clock_check([(1, 2)], 1, env={"CORONER_COLLECTOR_PORT": "9191"})
        assert out.out_lines[0] == "clock-check:    collector=http://127.0.0.1:9191"

    def test_offset_plus_uncertainty_over_tolerance_warns_but_exits_0(self) -> None:
        warning = "Warning:        offset plus uncertainty exceeds the 250 ms tolerance; ask before changing the tolerance"
        over, _ = clock_check([(250, 2)], 1)  # 250 + 1.0 > 250
        assert over.rc == 0 and warning in over.out_lines
        under, _ = clock_check([(249, 2)], 1)  # 249 + 1.0 = 250, not above
        assert under.rc == 0 and warning not in under.out_lines
        negative, _ = clock_check([(-300, 2)], 1)
        assert warning in negative.out_lines

    def test_tolerance_is_shown_but_never_changed_or_written(self, tmp_path: Path) -> None:
        source = Path(evaluate.__file__)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        out, _ = clock_check([(900, 4), (-900, 4)], 2)
        assert "Tolerance:      250 ms (documented Experiment B clock tolerance; never changed automatically)" in out.out_lines
        assert evaluate.CLOCK_TOLERANCE_MS == 250
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
        assert snapshot(tmp_path) == {}

    def test_run_clock_check_direct_call(self) -> None:
        sim = ClockSim([(5, 2), (7, 4)])
        client = CollectorClient("http://c.test", transport=sim.transport, wall_clock=sim.wall)
        out, sleeps = io.StringIO(), []
        rc = cli.run_clock_check(client, count=2, wall_clock=sim.wall, sleep=sleeps.append, out=out)
        assert rc == 0 and sleeps == [0.1]
        assert "Offset:         +5 ms (positive" in out.getvalue()


# ================================================================================================ negative controls


def patch_everywhere(monkeypatch: pytest.MonkeyPatch, name: str, fn: Callable[..., Any]) -> None:
    for module in (summary_mod, cli):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, fn)


def exit_mapping_problems(main_fn: Callable[..., int], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    spy = ExpSpy().install(monkeypatch)
    problems = []
    rows = [("passed", 0, None), ("failed", 1, Failure("c", "m")), ("aborted", 1, Failure("c", "m")),
            ("not_started", 2, None)]
    for outcome, code, failure in rows:
        spy.result = exp_result("A", outcome, code, run_id=None if outcome == "not_started" else RID_A,
                                manifest=None if outcome == "not_started" else tmp_path / "m.json", failure=failure)
        rc = run_main(["run", "a"], main_fn=main_fn).rc
        if rc != code:
            problems.append(f"{outcome}: returned {rc}, expected {code}")
    return problems


def mem_prevalidation_problems(main_fn: Callable[..., int], tmp_path: Path) -> list[str]:
    problems = []
    for value in ("abc", "0", "-1m", "128x", ""):
        out = run_main(["run", "c", "--runs-dir", str(tmp_path / "runs"), "--mem-limit", value], main_fn=main_fn)
        if out.rc != 2:
            problems.append(f"{value!r}: rc {out.rc}")
        if out.docker.calls:
            problems.append(f"{value!r}: Docker called {out.docker.calls}")
    if (tmp_path / "runs").exists():
        problems.append("runs dir created")
    return problems


def summary_readonly_problems(main_fn: Callable[..., int], tmp_path: Path) -> list[str]:
    rid = fixture_a(tmp_path / "runs")
    before = snapshot(tmp_path)
    out = run_main(["summary", rid, "--runs-dir", str(tmp_path / "runs")], main_fn=main_fn)
    problems = [] if out.rc == 0 else [f"rc {out.rc}"]
    if snapshot(tmp_path) != before:
        problems.append("runs tree changed")
    return problems


def summary_error_problems(main_fn: Callable[..., int], tmp_path: Path) -> list[str]:
    out = run_main(["summary", UNKNOWN_RID, "--runs-dir", str(tmp_path / "runs")], main_fn=main_fn)
    problems = []
    if out.rc != 2:
        problems.append(f"unknown run returned {out.rc}")
    if out.out != "":
        problems.append("stdout not empty")
    return problems


def summary_no_invention_problems(main_fn: Callable[..., int], tmp_path: Path) -> list[str]:
    """C run with no samples and no stored blocks: everything unavailable must read as unavailable."""
    runs = tmp_path / "runs"
    rid = fixture_c(runs, records=[meta_rec(RID_C, "crash")], state=None,
                    extra={"diagnostics": None, "sampling_limitations": None})
    out = run_main(["summary", rid, "--runs-dir", str(runs)], main_fn=main_fn)
    wanted = ["Last sample:    none received", "Diagnostics:    not recorded", "Limitations:    not recorded",
              "Container:      not recorded", "Samples:        0"]
    return [f"missing line {w!r}" for w in wanted if w not in out.out_lines]


def clock_sign_problems(main_fn: Callable[..., int]) -> list[str]:
    clock = {"now": datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)}

    def transport(method: str, url: str, body: Any, timeout_s: float) -> HttpResponse:
        clock["now"] += timedelta(milliseconds=10)
        payload = {"status": "ok", "storage_ok": True, "server_time": "2025-01-01T12:00:00.055Z"}
        return HttpResponse(200, json.dumps(payload).encode())

    out = run_main(["clock-check", "--count", "1"], main_fn=main_fn, http=transport, wall_clock=lambda: clock["now"])
    return [] if any(ln.startswith("Offset:         +50 ms ") for ln in out.out_lines) else ["Collector ahead is not +50 ms"]


class TestNegativeControls:
    """The checks above are shown to pass on the real code and to fail on deliberately broken variants."""

    def test_checks_pass_on_the_real_code(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        assert exit_mapping_problems(cli.main, monkeypatch, tmp_path) == []
        assert mem_prevalidation_problems(cli.main, tmp_path / "m") == []
        assert summary_readonly_problems(cli.main, tmp_path / "r") == []
        assert summary_error_problems(cli.main, tmp_path / "e") == []
        assert summary_no_invention_problems(cli.main, tmp_path / "n") == []
        assert clock_sign_problems(cli.main) == []

    def test_run_returning_zero_on_a_failed_result_is_caught(self, monkeypatch: pytest.MonkeyPatch,
                                                              tmp_path: Path) -> None:
        def mutant(argv: Any, **kwargs: Any) -> int:
            rc = cli.main(argv, **kwargs)
            return 0 if rc == 1 else rc

        assert exit_mapping_problems(mutant, monkeypatch, tmp_path)

    def test_mem_validation_after_the_docker_call_is_caught(self, tmp_path: Path) -> None:
        def mutant(argv: Any, **kwargs: Any) -> int:
            kwargs["docker"].available()
            return cli.main(argv, **kwargs)

        assert mem_prevalidation_problems(mutant, tmp_path)

    def test_summary_that_writes_a_file_is_caught(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        real = summary_mod.build_summary

        def mutant(runs_dir: Any, run_id: str) -> Any:
            (Path(runs_dir) / run_id / "touched.txt").write_text("x", encoding="utf-8")
            return real(runs_dir, run_id)

        patch_everywhere(monkeypatch, "build_summary", mutant)
        assert summary_readonly_problems(cli.main, tmp_path) == ["runs tree changed"]

    def test_summary_that_invents_values_is_caught(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        real = summary_mod.build_summary

        def mutant(runs_dir: Any, run_id: str) -> Any:
            built = real(runs_dir, run_id)
            return dataclasses.replace(
                built, last_sample_time="2025-01-01T00:00:00.000Z", container_status="exited", container_exit_code=137,
                container_oom_killed=True, container_inspect_time="2025-01-01T00:00:00.000Z",
                diagnostics={"rss_monotonic_tail": True, "peak_rss_ratio_of_limit": 0.9, "sample_count": 3},
                sampling_limitations={"sample_interval_ms": 500, "approx_leak_rate_bytes_per_s": 1, "statements": []})

        patch_everywhere(monkeypatch, "build_summary", mutant)
        assert len(summary_no_invention_problems(cli.main, tmp_path)) >= 3

    def test_sign_flipped_offset_is_caught(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = collector_client_mod.estimate_clock_offset_ms

        def flipped(before: datetime, after: datetime, server_time: Any) -> ClockOffset:
            found = real(before, after, server_time)
            return dataclasses.replace(found, offset_ms=-found.offset_ms)

        monkeypatch.setattr(collector_client_mod, "estimate_clock_offset_ms", flipped)
        assert clock_sign_problems(cli.main)

    def test_summary_returning_zero_on_error_is_caught(self, tmp_path: Path) -> None:
        def mutant(argv: Any, **kwargs: Any) -> int:
            rc = cli.main(argv, **kwargs)
            return 0 if rc == 2 else rc

        assert summary_error_problems(mutant, tmp_path)


# ================================================================================================ M0.7 compose project


class _ProjectFakeRun:
    """A subprocess.run replacement that records argv lists and never starts a process."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []

    def __call__(self, argv: Any, **kwargs: Any) -> Any:
        self.argvs.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout=(CID + "\n").encode(), stderr=b"")


def _install_real_docker_cli(monkeypatch: pytest.MonkeyPatch, *, drop_project: bool = False) -> _ProjectFakeRun:
    """Build the real DockerCli (fake runner) wherever the CLI would build one; `drop_project` is a mutant."""
    fake = _ProjectFakeRun()

    def factory(**kwargs: Any) -> Any:
        if drop_project:
            kwargs.pop("project", None)
        return RealDockerCli(run=fake, base_env={}, docker_exe="docker", **kwargs)

    monkeypatch.setattr(cli, "DockerCli", factory)
    return fake


def _project_run(env: dict[str, str], monkeypatch: pytest.MonkeyPatch, *, drop_project: bool = False
                 ) -> tuple[int, str, str, _ProjectFakeRun, ExpSpy]:
    fake = _install_real_docker_cli(monkeypatch, drop_project=drop_project)
    spy = ExpSpy().install(monkeypatch)
    out, err = io.StringIO(), io.StringIO()
    rc = cli.main(["run", "a"], collector=FakeCollector(), env=env, out=out, err=err)
    if spy.calls:
        ctx = spy.calls[0][1]
        ctx.docker.compose_up("demo_app", env={}, no_deps=False, recreate=False)
        ctx.docker.container_id("demo_app")
    return rc, out.getvalue(), err.getvalue(), fake, spy


def project_cli_problems(monkeypatch: pytest.MonkeyPatch, *, drop_project: bool = False) -> list[str]:
    problems: list[str] = []
    for env, expected in (({}, "coroner"), ({"CORONER_COMPOSE_PROJECT": "coroner-m07"}, "coroner-m07")):
        rc, _, _, fake, spy = _project_run(env, monkeypatch, drop_project=drop_project)
        if rc != 0 or len(spy.calls) != 1:
            problems.append(f"{env}: rc={rc}")
            continue
        for argv in fake.argvs:
            if argv[1:4] != ["compose", "-p", expected]:
                problems.append(f"{env}: argv {argv}")
    return problems


class TestComposeProject:
    def test_unset_uses_the_default_project(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rc, _, err, fake, spy = _project_run({}, monkeypatch)
        assert rc == 0 and err == ""
        assert spy.calls[0][1].docker.project == "coroner"
        assert fake.argvs == [
            ["docker", "compose", "-p", "coroner", "up", "-d", "demo_app"],
            ["docker", "compose", "-p", "coroner", "ps", "-a", "-q", "demo_app"],
        ]

    def test_m07_project_reaches_the_experiments_docker_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rc, _, err, fake, spy = _project_run({"CORONER_COMPOSE_PROJECT": "coroner-m07"}, monkeypatch)
        assert rc == 0 and err == ""
        assert spy.calls[0][1].docker.project == "coroner-m07"
        assert fake.argvs == [
            ["docker", "compose", "-p", "coroner-m07", "up", "-d", "demo_app"],
            ["docker", "compose", "-p", "coroner-m07", "ps", "-a", "-q", "demo_app"],
        ]

    def test_project_variable_does_not_touch_ports_or_urls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, _, _, _, spy = _project_run({"CORONER_COMPOSE_PROJECT": "coroner-m07"}, monkeypatch)
        assert spy.calls[0][1].demo_url == "http://127.0.0.1:8080"

    @pytest.mark.parametrize("value", ["", "Coroner", "-x", "_x", "a b", "a.b", "a/b", "\u00e9", "x" * 64, " coroner"])
    @pytest.mark.parametrize("letter", ["a", "c"])
    def test_invalid_or_empty_project_exits_2_before_any_docker_call(self, tmp_path: Path, value: str,
                                                                     letter: str) -> None:
        runs = tmp_path / "runs"
        out = run_main(["run", letter, "--runs-dir", str(runs)], env={"CORONER_COMPOSE_PROJECT": value})
        assert out.rc == 2 and out.out == ""
        assert out.err.startswith("error: ") and "CORONER_COMPOSE_PROJECT" in out.err and "lowercase" in out.err
        assert out.docker.calls == [] and out.collector.calls == []
        assert not runs.exists() and snapshot(tmp_path) == {}

    def test_invalid_project_never_builds_a_docker_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _install_real_docker_cli(monkeypatch)
        built: list[Any] = []
        inner = cli.DockerCli
        monkeypatch.setattr(cli, "DockerCli", lambda **kw: built.append(kw) or inner(**kw))
        out, err = io.StringIO(), io.StringIO()
        rc = cli.main(["run", "b"], collector=FakeCollector(), env={"CORONER_COMPOSE_PROJECT": "BAD"}, out=out, err=err)
        assert rc == 2 and built == [] and fake.argvs == []

    def test_error_precedence_is_mem_limit_then_chunk_then_ports_then_project(self) -> None:
        env = {"CORONER_COMPOSE_PROJECT": "BAD", "CORONER_COLLECTOR_PORT": "x", "CORONER_DEMO_PORT": "y"}
        assert "--mem-limit" in run_main(["run", "a", "--mem-limit", "z"], env=env).err
        assert "--leak-chunk-bytes" in run_main(["run", "a", "--leak-chunk-bytes", "0"], env=env).err
        assert "CORONER_COLLECTOR_PORT" in run_main(["run", "a"], env=env).err
        env.pop("CORONER_COLLECTOR_PORT")
        assert "CORONER_DEMO_PORT" in run_main(["run", "a"], env=env).err
        env.pop("CORONER_DEMO_PORT")
        assert "CORONER_COMPOSE_PROJECT" in run_main(["run", "a"], env=env).err

    def test_injected_docker_still_validates_the_project(self) -> None:
        out = run_main(["run", "a"], env={"CORONER_COMPOSE_PROJECT": "BAD"}, docker=FakeDocker())
        assert out.rc == 2 and "CORONER_COMPOSE_PROJECT" in out.err and out.docker.calls == []

    def test_valid_project_with_injected_docker_changes_nothing(self, spy: ExpSpy) -> None:
        docker = FakeDocker()
        out = run_main(["run", "a"], env={"CORONER_COMPOSE_PROJECT": "coroner-m07"}, docker=docker)
        assert out.rc == 0 and spy.calls[0][1].docker is docker

    def test_summary_and_clock_check_never_read_the_variable(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        rid = fixture_a(runs)
        bad = {"CORONER_COMPOSE_PROJECT": "BAD"}
        assert summary_of(runs, rid, env=bad).out == summary_of(runs, rid).out
        assert summary_of(runs, rid, env=bad).rc == 0

        class StrictEnv(dict):  # type: ignore[type-arg]
            def __contains__(self, key: object) -> bool:
                assert key != "CORONER_COMPOSE_PROJECT"
                return super().__contains__(key)

            def __getitem__(self, key: str) -> str:
                assert key != "CORONER_COMPOSE_PROJECT"
                return super().__getitem__(key)

        assert run_main(["summary", rid, "--runs-dir", str(runs)], env=StrictEnv(bad)).rc == 0
        out, _ = clock_check([(10, 2)], 1, env=StrictEnv(bad))
        assert out.rc == 0 and out.out_lines[3].startswith("Offset:         +10 ms")

    def test_project_negative_controls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert project_cli_problems(monkeypatch) == []
        assert project_cli_problems(monkeypatch, drop_project=True)  # CLI loses the project: caught
