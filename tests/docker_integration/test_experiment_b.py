"""Experiment B integration test (task 7.4; Requirements 10.1-10.13, 13.4).

Runs only with --run-docker or CORONER_RUN_DOCKER=1 (see tests/conftest.py). Live only: there is no mocked variant.

The test starts its own Collector, then launches the real Experiment_Runner (`python -m coroner.runner run b`) as a
subprocess. The Runner leaks memory in the Demo_App for at least 5 s, then sends SIGKILL to the Demo_App through
DockerPort using the FULL 64-hex container id (`docker kill --signal=KILL <id>`). The test then checks the outcome
against the manifest, an independent `docker inspect` and the evidence read back from the Collector.

SIGKILL SAFETY
- SIGKILL is sent ONLY by the Runner, and only when the user has separately authorised the real run.
- This test never sends a kill or any other signal and never calls `/crash` or `/leak`.

Rules this module follows (same as test_experiment_a.py):
- Compose project `coroner-m07`, file compose.yaml, always passed explicitly to every compose command the test
  issues. The Runner gets the project only through `CORONER_COMPOSE_PROJECT=coroner-m07` in the SUBPROCESS env
  (os.environ is never changed). The project `coroner` is never targeted.
- Images coroner-collector:m0 and coroner-demo-app:m0 must already exist; nothing is built, tagged or removed.
- No coroner-m07 container or network may exist at the start; otherwise the fixture fails early and cleans up
  nothing. The labelled evidence volume `coroner-m07_coroner_evidence` legitimately persists between runs and is
  tolerated. Run sequentially, never in parallel with another coroner-m07 test.
- Teardown is `docker compose -p coroner-m07 -f compose.yaml down` WITHOUT `-v`, skipped when CORONER_KEEP_STACK=1
  so independent `docker inspect` evidence can be taken first. No volume is removed and no prune is run. The Runner
  creates a unique run id each time, so no existing evidence file is touched or overwritten.
- Set CORONER_OBSERVATIONS_PATH to also write the recorded observations as JSON to that file.

MANUAL REAL-RUN STEPS (run only after separate authorisation; the Runner sends the SIGKILL)
1. Confirm the images exist: `docker image inspect coroner-collector:m0 coroner-demo-app:m0`.
2. Confirm no coroner-m07 container exists: `docker ps -a --filter label=com.docker.compose.project=coroner-m07`.
   If one does: `docker compose -p coroner-m07 -f compose.yaml down` (no -v), then re-check.
3. From the repository root, with the virtual environment active (or `.\\.venv\\Scripts\\python.exe -m pytest`):
   `$env:CORONER_KEEP_STACK = '1'` (PowerShell; optional, keeps the exited Demo_App for manual `docker inspect`), then
   `python -m pytest tests/docker_integration/test_experiment_b.py --run-docker -v -s -p no:cacheprovider`
4. Afterwards, if the stack was kept: `docker inspect coroner-m07-demo_app-1`, then
   `docker compose -p coroner-m07 -f compose.yaml down` (no -v).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.docker_integration

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "compose.yaml"
RUNS_DIR = REPO_ROOT / "runs"
PROJECT = "coroner-m07"
FORBIDDEN_PROJECT = "coroner"
COLLECTOR_IMAGE = "coroner-collector:m0"
DEMO_IMAGE = "coroner-demo-app:m0"
EVIDENCE_VOLUME = f"{PROJECT}_coroner_evidence"
PROTECTED_VOLUMES = ("coroner_coroner_evidence", "coroner-spike_coroner_spike_evidence")
EVIDENCE_MOUNT = "/data/evidence"
COLLECTOR_NAME = f"{PROJECT}-collector-1"
DEMO_NAME = f"{PROJECT}-demo_app-1"
COLLECTOR_PORT = 8081
STARTUP_TIMEOUT_S = 90
RUNNER_TIMEOUT_S = 300
HTTP_TIMEOUT_S = 5.0
CLOCK_TOLERANCE_MS = 250
MEMORY_BYTES = 134217728
TERMINATION_TEXT = "SIGKILL sent by the runner (docker kill --signal=KILL)"
CAUSE_LABEL = "sigkill_sent_by_runner"

_HEX64 = re.compile(r"[0-9a-f]{64}")
_RUN_LINE = re.compile(r"^Experiment B (\S+): (?:PASSED|FAILED|ABORTED)\b", re.MULTILINE | re.IGNORECASE)

# Variables that would change the defaults under test; removed from the subprocess environment only.
_SCRUBBED_ENV = (
    "CORONER_DEMO_MEM_LIMIT",
    "CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS",
    "CORONER_COLLECTOR_PORT",
    "CORONER_DEMO_PORT",
    "CORONER_DURABILITY",
    "CORONER_RUN_ID",
    "CORONER_SAMPLE_INTERVAL_MS",
    "CORONER_COMPOSE_PROJECT",
    "COMPOSE_FILE",
    "COMPOSE_PROFILES",
    "COMPOSE_PROJECT_NAME",
    "COMPOSE_PATH_SEPARATOR",
)
# Not scrubbed (the Docker CLI may depend on them); only recorded in the observations.
_RECORDED_ENV = ("DOCKER_HOST", "DOCKER_CONTEXT")
EVIDENCE_COPY_NAME = "evidence.jsonl"  # the Runner's own host copy, runs/<run_id>/evidence.jsonl


# ---------------------------------------------------------------- helpers


def _run(args: list[str], *, env: dict[str, str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=str(REPO_ROOT), env=env)


def _compose(env: dict[str, str], *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return _run(["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *args], env=env, timeout=timeout)


def _inspect(env: dict[str, str], target: str) -> dict:
    proc = _run(["docker", "inspect", target], env=env)
    assert proc.returncode == 0, f"docker inspect {target} failed: {proc.stderr}"
    return json.loads(proc.stdout)[0]


def _http_get(port: int, path: str, timeout: float = HTTP_TIMEOUT_S) -> tuple[int, bytes]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _wait_for(predicate, what: str, timeout: float) -> float:
    """Bounded polling; returns the elapsed seconds."""
    start = time.monotonic()
    deadline = start + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return time.monotonic() - start
        except (OSError, urllib.error.URLError, AssertionError, json.JSONDecodeError):
            pass
        time.sleep(0.5)
    pytest.fail(f"timed out after {timeout}s waiting for {what}")


def _lines(proc: subprocess.CompletedProcess) -> list[str]:
    assert proc.returncode == 0, proc.stderr
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _volume_names(env: dict[str, str]) -> set[str]:
    return set(_lines(_run(["docker", "volume", "ls", "--format", "{{.Name}}"], env=env)))


def _project_resources(env: dict[str, str], project: str) -> dict[str, list[str]]:
    label = f"label=com.docker.compose.project={project}"
    return {
        "containers": _lines(_run(["docker", "ps", "-a", "-q", "--filter", label], env=env)),
        "networks": _lines(_run(["docker", "network", "ls", "-q", "--filter", label], env=env)),
        "volumes": _lines(_run(["docker", "volume", "ls", "-q", "--filter", label], env=env)),
    }


def _parse_time(text: str) -> datetime:
    """RFC 3339 `Z` time; fractional digits beyond 6 (Docker's nanoseconds) are truncated."""
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _parse_records(raw: bytes) -> list[dict]:
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


def _classify(samples: list[dict], kill: datetime, offset_ms: int, finished_at: datetime | None) -> dict:
    """Independent oracle: capture = ts - offset; pre < kill; within kill..kill+250 ms inclusive; beyond otherwise.
    Late-received = pre-kill by capture but received (recv_time - offset) after the kill."""
    offset = timedelta(milliseconds=offset_ms)
    limit = kill + timedelta(milliseconds=CLOCK_TOLERANCE_MS)
    out: dict = {"pre": [], "late": [], "within": [], "beyond": [], "after_stop": []}
    for rec in samples:
        capture = _parse_time(rec["sample"]["ts"]) - offset
        received = _parse_time(rec["recv_time"]) - offset
        if capture < kill:
            out["pre"].append(rec)
            if received > kill:
                out["late"].append(rec)
        elif capture <= limit:
            out["within"].append(rec)
        else:
            out["beyond"].append(rec)
        if finished_at is not None and capture > finished_at:
            out["after_stop"].append(rec)
    return out


# ---------------------------------------------------------------- fixture


@pytest.fixture(scope="module")
def experiment():
    """Start the Collector, run Experiment B through the real Runner, and yield what was observed.

    Teardown is `down` without `-v` unless CORONER_KEEP_STACK=1."""
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_ENV}
    runner_env = dict(env)
    runner_env["CORONER_COMPOSE_PROJECT"] = PROJECT  # subprocess env only; os.environ is never changed

    for image in (COLLECTOR_IMAGE, DEMO_IMAGE):
        proc = _run(["docker", "image", "inspect", image], env=env)
        if proc.returncode != 0:
            pytest.fail(f"image {image} is missing; build it first (this test never builds): {proc.stderr.strip()}")

    # Guards run before anything is started and clean up nothing (no try/finally yet). The labelled evidence
    # volume persists between runs; only containers and networks must be absent. Protected volumes are not conflicts.
    existing = _project_resources(env, PROJECT)
    existing.pop("volumes")
    names = set(_lines(_run(["docker", "ps", "-a", "--format", "{{.Names}}"], env=env)))
    clashing = sorted(names & {COLLECTOR_NAME, DEMO_NAME})
    if any(existing.values()) or clashing:
        pytest.fail(
            f"resources of project {PROJECT} already exist: {existing}, containers by name: {clashing}. "
            f"Nothing was changed. Run `docker compose -p {PROJECT} -f compose.yaml down` (no -v) first. "
            "Experiment B needs a fresh stack because the Runner kills and replaces the Demo_App."
        )
    volumes_before = _volume_names(env)

    obs: dict = {"project": PROJECT, "runner_env_project": runner_env["CORONER_COMPOSE_PROJECT"]}
    obs["docker_env_recorded"] = {k: os.environ.get(k) for k in _RECORDED_ENV}
    try:
        up = _compose(env, "up", "-d", "--no-deps", "--no-build", "collector")
        assert up.returncode == 0, f"docker compose up collector failed:\n{up.stdout}\n{up.stderr}"

        def _collector_ready() -> bool:
            status, body = _http_get(COLLECTOR_PORT, "/healthz")
            return status == 200 and json.loads(body).get("storage_ok") is True

        obs["collector_ready_s"] = round(_wait_for(_collector_ready, "collector /healthz 200 with storage_ok true", STARTUP_TIMEOUT_S), 2)
        ps = _compose(env, "ps", "-q", "collector")
        collector_id = ps.stdout.strip().splitlines()[0] if ps.stdout.strip() else ""
        assert ps.returncode == 0 and _HEX64.fullmatch(collector_id), (ps.stdout, ps.stderr)

        started = datetime.now().astimezone()
        t0 = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-m", "coroner.runner", "run", "b", "--runs-dir", str(RUNS_DIR)],
            capture_output=True,
            text=True,
            timeout=RUNNER_TIMEOUT_S,
            cwd=str(REPO_ROOT),
            env=runner_env,
        )
        elapsed = time.monotonic() - t0
        obs["runner"] = {
            "argv": "python -m coroner.runner run b",
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "started": started.isoformat(),
            "finished": datetime.now().astimezone().isoformat(),
            "elapsed_s": round(elapsed, 2),
        }
        print(f"\nRunner finished in {elapsed:.1f}s with exit code {proc.returncode}")
        match = _RUN_LINE.search(proc.stdout)
        run_id = match.group(1) if match else None
        obs["run_id"] = run_id
        manifest = None
        manifest_path = RUNS_DIR / run_id / "manifest.json" if run_id else None
        if manifest_path is not None and manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        yield {
            "env": env,
            "collector_id": collector_id,
            "proc": proc,
            "run_id": run_id,
            "manifest": manifest,
            "manifest_path": manifest_path,
            "volumes_before": volumes_before,
            "obs": obs,
        }
    finally:
        if os.environ.get("CORONER_KEEP_STACK") == "1":
            print(f"\nCORONER_KEEP_STACK=1: stack of project {PROJECT} left in place (run id {obs.get('run_id')})")
        else:
            _compose(env, "down", timeout=180)  # never -v: the evidence volume must survive


@pytest.fixture(scope="module")
def evidence(experiment):
    """Evidence read back from the Collector, independent of the Runner's host copy."""
    run_id = experiment["run_id"]
    assert run_id and experiment["manifest"] is not None, "no run id or manifest"
    status, raw = _http_get(COLLECTOR_PORT, f"/v1/runs/{run_id}/evidence")
    assert status == 200, f"evidence read returned HTTP {status}"
    records = _parse_records(raw)
    # The Runner's own saved copy (what its manifest counts were computed from), read only; None if absent.
    copy_path = RUNS_DIR / run_id / EVIDENCE_COPY_NAME
    runner_samples = None
    if copy_path.is_file():
        runner_samples = [r for r in _parse_records(copy_path.read_bytes()) if r.get("record_type") == "sample"]
    return {
        "runner_samples": runner_samples,
        "records": records,
        "samples": [r for r in records if r.get("record_type") == "sample"],
        "metadata": [r for r in records if r.get("record_type") == "run_metadata"],
        "terminations": [r for r in records if r.get("record_type") == "termination"],
    }


@pytest.fixture(scope="module")
def demo_info(experiment):
    manifest = experiment["manifest"]
    assert manifest is not None, f"no manifest at {experiment['manifest_path']}"
    info = _inspect(experiment["env"], manifest["container_id"])
    state = info["State"]
    experiment["obs"]["demo_inspect"] = {
        "Id": info["Id"],
        "Name": info["Name"],
        "Status": state["Status"],
        "ExitCode": state["ExitCode"],
        "OOMKilled": state["OOMKilled"],
        "StartedAt": state["StartedAt"],
        "FinishedAt": state["FinishedAt"],
        "Memory": info["HostConfig"]["Memory"],
        "Mounts": info["Mounts"],
    }
    return info


# ---------------------------------------------------------------- tests


def test_01_runner_passed_and_sigkill_by_runner(experiment) -> None:
    proc = experiment["proc"]
    assert proc.returncode == 0, f"Runner exit {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    run_id = experiment["run_id"]
    assert run_id, f"no result line in Runner output: {proc.stdout!r}"
    manifest = experiment["manifest"]
    assert manifest is not None, f"no manifest at {experiment['manifest_path']}"

    assert manifest["experiment"] == "B"
    assert manifest["run_id"] == run_id
    assert manifest["outcome"] == "passed", manifest.get("failure")
    assert manifest["failure"] is None
    assert manifest["finished"] is True
    assert manifest["termination_description"] == TERMINATION_TEXT
    assert manifest["expected"] == {"exit_code": 137, "oom_killed": False}
    failed = [c for c in manifest["checks"] if not c["passed"]]
    assert not failed, failed
    assert manifest["container_left_running"] is False

    # SIGKILL went to the full 64-hex id, recorded by the Runner; this test never sends a signal.
    assert _HEX64.fullmatch(manifest["container_id"]), manifest["container_id"]
    assert manifest["container_state"]["container_id"] == manifest["container_id"]
    assert manifest["times"]["sigkill_sent_time"]
    assert manifest["times"]["crash_request_time"] is None  # B never calls /crash


def test_02_independent_inspect_matches_manifest(experiment, demo_info) -> None:
    manifest = experiment["manifest"]
    state = demo_info["State"]
    assert demo_info["Id"] == manifest["container_id"]  # full id equal to the one the Runner killed
    assert _HEX64.fullmatch(demo_info["Id"])
    assert state["ExitCode"] == 137
    assert state["OOMKilled"] is False
    assert state["Status"] == "exited"
    assert state["Running"] is False
    assert demo_info["HostConfig"]["Memory"] == MEMORY_BYTES
    assert demo_info["Name"] == f"/{DEMO_NAME}"
    assert demo_info["Config"]["Image"] == DEMO_IMAGE
    assert demo_info["Config"]["Labels"].get("com.docker.compose.project") == PROJECT

    saved = manifest["container_state"]
    assert (saved["status"], saved["exit_code"], saved["oom_killed"]) == (
        state["Status"],
        state["ExitCode"],
        state["OOMKilled"],
    )
    assert saved["host_config_memory_bytes"] == demo_info["HostConfig"]["Memory"]

    # Actual State.FinishedAt: present in inspect, and the manifest agrees (within 1 ms of rounding).
    raw_finished = state["FinishedAt"]
    assert raw_finished and not raw_finished.startswith("0001-01-01"), raw_finished
    finished_at = _parse_time(raw_finished)
    kill = _parse_time(manifest["times"]["sigkill_sent_time"])
    # Clock-domain caveat: `kill` is the Runner's host clock, FinishedAt is the Docker daemon (VM) clock, so the
    # bounds carry the 250 ms tolerance. The upper bound ties the exit to the Runner's own kill: stopped within 10 s.
    assert finished_at >= kill - timedelta(milliseconds=CLOCK_TOLERANCE_MS)
    assert finished_at <= kill + timedelta(seconds=10), f"FinishedAt {finished_at} is more than 10 s after kill {kill}"
    manifest_finished = manifest["container_finished_at"]
    assert manifest_finished is not None, "manifest has no container_finished_at although inspect has FinishedAt"
    assert abs(_parse_time(manifest_finished) - finished_at) <= timedelta(milliseconds=1)
    assert manifest["sample_timing"]["container_finished_at"] == manifest_finished
    assert manifest["sample_timing"].get("after_container_stop_analysis") == "available"


def test_03_samples_before_kill_and_none_beyond_tolerance(experiment, demo_info, evidence) -> None:
    manifest = experiment["manifest"]
    samples = evidence["samples"]
    assert samples, "no Samples in the evidence"
    offset_ms = manifest["clock_offset_ms"]
    kill = _parse_time(manifest["times"]["sigkill_sent_time"])
    finished_at = _parse_time(demo_info["State"]["FinishedAt"])
    result = _classify(samples, kill, offset_ms, finished_at)

    # (6) at least one Sample captured before the kill, after clock correction.
    assert len(result["pre"]) >= 1, f"no pre-kill Sample; kill {kill}, offset {offset_ms} ms"
    # (7) no Sample beyond kill + 250 ms (kill + 250 ms itself is allowed).
    assert not result["beyond"], [r["sample"]["ts"] for r in result["beyond"]]

    timing = manifest["sample_timing"]
    assert timing["tolerance_ms"] == CLOCK_TOLERANCE_MS
    assert timing["clock_offset_ms"] == offset_ms
    assert timing["post_kill_beyond_tolerance"] == []

    # (8) late-received pre-kill Samples are valid and counted. The manifest counts were computed from the Runner's
    # own evidence fetch, so they must EQUAL the independent classification of the Runner's saved copy. Without
    # that file, compare to the live read with <=: samples that arrived after the Runner's fetch only add to it.
    runner_samples = evidence["runner_samples"]
    if runner_samples is not None:
        ref = _classify(runner_samples, kill, offset_ms, finished_at)
        assert timing["pre_kill_count"] == len(ref["pre"])
        assert timing["pre_kill_late_received_count"] == len(ref["late"])
        assert timing["within_tolerance_count"] == len(ref["within"])
        assert len(timing["after_container_stop"]) == len(ref["after_stop"])
        assert not ref["beyond"], [r["sample"]["ts"] for r in ref["beyond"]]
        assert len(runner_samples) <= len(samples)  # the live read is a superset
    else:
        assert timing["pre_kill_count"] <= len(result["pre"])
        assert timing["pre_kill_late_received_count"] <= len(result["late"])
        assert timing["within_tolerance_count"] <= len(result["within"])
        assert len(timing["after_container_stop"]) <= len(result["after_stop"])
    late_ts = {r["sample"]["ts"] for r in result["late"]}
    assert not late_ts & set(timing["post_kill_beyond_tolerance"])

    experiment["obs"]["timing"] = {
        "sample_count": len(samples),
        "pre_kill": len(result["pre"]),
        "pre_kill_late_received": len(result["late"]),
        "within_tolerance": len(result["within"]),
        "beyond_tolerance": len(result["beyond"]),
        "after_container_stop": len(result["after_stop"]),
        "clock_offset_ms": offset_ms,
        "sigkill_sent_time": manifest["times"]["sigkill_sent_time"],
    }


def test_04_memory_growth_and_no_shutdown_records(experiment, evidence) -> None:
    manifest = experiment["manifest"]
    checks = {c["name"]: c for c in manifest["checks"]}
    for name in ("memory_growth_matches_leak", "no_shutdown_handler_records", "durability_matches_run_metadata"):
        assert name in checks, f"manifest has no check {name!r}: {sorted(checks)}"
        assert checks[name]["passed"] is True, checks[name]

    samples = evidence["samples"]
    rss = [s["sample"]["rss_bytes"] for s in samples if s["sample"].get("rss_bytes") is not None]
    assert rss and max(rss) > rss[0], "no RSS growth in the evidence"
    # Exact against the Runner's saved copy; `<=` against the live read (later samples may only raise the maximum).
    if evidence["runner_samples"] is not None:
        runner_rss = [s["sample"]["rss_bytes"] for s in evidence["runner_samples"] if s["sample"].get("rss_bytes") is not None]
        assert manifest["highest_rss_bytes"] == max(runner_rss)
    else:
        assert manifest["highest_rss_bytes"] <= max(rss)

    assert len(evidence["metadata"]) == 1
    assert evidence["metadata"][0]["durability"] == manifest["durability_level"]
    # No record type other than those the Collector and Probe produce (no shutdown-handler records).
    known = {"run_metadata", "sample", "gap", "sequence_anomaly", "rejection", "repair", "termination"}
    assert {r.get("record_type") for r in evidence["records"]} <= known


def test_05_termination_record(experiment, evidence) -> None:
    manifest = experiment["manifest"]
    assert manifest["termination"]["posted"] is True
    assert manifest["termination"]["cause_label"] == CAUSE_LABEL
    assert len(evidence["terminations"]) == 1
    term = evidence["terminations"][0]
    assert term["cause_label"] == CAUSE_LABEL
    assert term["container_state"] == {"status": "exited", "exit_code": 137, "oom_killed": False}


def test_06_collector_healthy_and_isolated(experiment) -> None:
    env = experiment["env"]
    manifest = experiment["manifest"]
    assert manifest["collector_health"]["ok"] is True
    assert manifest["collector_health"]["storage_ok"] is True
    assert manifest["collector_health"]["http_status"] == 200

    status, body = _http_get(COLLECTOR_PORT, "/healthz")
    health = json.loads(body)
    experiment["obs"]["collector_healthz"] = {"status": status, "body": health}
    assert status == 200
    assert health["storage_ok"] is True

    collector = _inspect(env, experiment["collector_id"])
    assert collector["Name"] == f"/{COLLECTOR_NAME}"
    assert collector["State"]["Status"] == "running"
    assert collector["RestartCount"] == 0
    assert collector["Config"]["Labels"].get("com.docker.compose.project") == PROJECT
    mounts = [(m.get("Name"), m["Destination"]) for m in collector["Mounts"]]
    assert mounts == [(EVIDENCE_VOLUME, EVIDENCE_MOUNT)], mounts
    for protected in PROTECTED_VOLUMES:
        assert protected not in {m.get("Name") for m in collector["Mounts"]}

    demo = _inspect(env, manifest["container_id"])
    assert demo["Mounts"] == []  # the Demo_App has no evidence mount
    assert not {m.get("Name") for m in demo["Mounts"]} & set(PROTECTED_VOLUMES)

    # Nothing was created in project `coroner`, and no new volume appeared (the labelled evidence volume persists).
    forbidden = _project_resources(env, FORBIDDEN_PROJECT)
    assert not forbidden["containers"] and not forbidden["networks"], forbidden
    new_volumes = _volume_names(env) - experiment["volumes_before"]
    assert not new_volumes, f"new volumes appeared: {new_volumes}"
    names = set(_lines(_run(["docker", "ps", "-a", "--format", "{{.Names}}"], env=env)))
    assert {COLLECTOR_NAME, DEMO_NAME} <= names
    assert not names & {"coroner-collector-1", "coroner-demo_app-1"}, names

    print("\nEXPERIMENT_B_OBSERVATIONS " + json.dumps(experiment["obs"], sort_keys=True, default=str))
    path = os.environ.get("CORONER_OBSERVATIONS_PATH")
    if path:
        Path(path).write_text(
            json.dumps(experiment["obs"], indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8", newline="\n"
        )
