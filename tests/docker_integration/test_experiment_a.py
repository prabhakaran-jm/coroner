"""Experiment A integration test (task 7.3; Requirements 9.1, 9.3, 9.5, 9.6, 9.8-9.12, 13.4).

Runs only with --run-docker or CORONER_RUN_DOCKER=1 (see tests/conftest.py).

The test starts its own Collector, then launches the real Experiment_Runner
(`python -m coroner.runner run a`) as a subprocess and checks the controlled fatal process exit
(`POST /crash` -> `os._exit(1)`) against an independent `docker inspect` of the Demo_App container.

Rules this module follows:
- Compose project `coroner-m07`, file compose.yaml, always passed explicitly to every compose command the test
  issues. The Runner gets the project only through `CORONER_COMPOSE_PROJECT=coroner-m07` in the SUBPROCESS env.
  The project `coroner` is never targeted.
- Images coroner-collector:m0 and coroner-demo-app:m0 must already exist; the test starts the Collector with
  `--no-build` and never builds, tags or removes an image.
- No coroner-m07 container may exist at the start (the Runner recreates the Demo_App; a previous exited container
  would be replaced, so the test refuses to start over one). Run sequentially, never in parallel.
- Teardown is `docker compose -p coroner-m07 -f compose.yaml down` WITHOUT `-v`, skipped when
  CORONER_KEEP_STACK=1 so independent `docker inspect` evidence can be taken first. No volume is removed and no
  prune is run. The Runner creates a unique run id each time, so no existing evidence file is touched.
- Only the Runner calls `/crash`. This test never sends it, and nothing is killed.
- Set CORONER_OBSERVATIONS_PATH to also write the recorded observations as JSON to that file.
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
COLLECTOR_PORT = 8081
STARTUP_TIMEOUT_S = 90
RUNNER_TIMEOUT_S = 300
HTTP_TIMEOUT_S = 5.0
TERMINATION_TEXT = "controlled fatal process exit via os._exit(1)"
MEMORY_BYTES = 134217728

_HEX64 = re.compile(r"[0-9a-f]{64}")
_RUN_LINE = re.compile(r"^Experiment A (\S+): PASSED\b", re.MULTILINE)

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
)


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


def _wait_for(predicate, what: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
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
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _parse_records(raw: bytes) -> list[dict]:
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------- fixture


@pytest.fixture(scope="module")
def experiment():
    """Start the Collector, run Experiment A through the real Runner, and yield what was observed.

    Teardown is `down` without `-v` unless CORONER_KEEP_STACK=1."""
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_ENV}
    runner_env = dict(env)
    runner_env["CORONER_COMPOSE_PROJECT"] = PROJECT  # subprocess env only; os.environ is never changed

    for image in (COLLECTOR_IMAGE, DEMO_IMAGE):
        proc = _run(["docker", "image", "inspect", image], env=env)
        if proc.returncode != 0:
            pytest.fail(f"image {image} is missing; build it first (this test never builds): {proc.stderr.strip()}")

    # The labelled evidence volume legitimately persists between runs; only containers and the network must be gone.
    existing = _project_resources(env, PROJECT)
    existing.pop("volumes")
    assert not any(existing.values()), (
        f"resources of project {PROJECT} already exist: {existing}; run "
        f"`docker compose -p {PROJECT} -f compose.yaml down` (no -v) first. This test needs a fresh stack."
    )
    volumes_before = _volume_names(env)

    obs: dict = {"project": PROJECT, "runner_env_project": runner_env["CORONER_COMPOSE_PROJECT"]}
    try:
        up = _compose(env, "up", "-d", "--no-deps", "--no-build", "collector")
        assert up.returncode == 0, f"docker compose up collector failed:\n{up.stdout}\n{up.stderr}"

        def _collector_ready() -> bool:
            status, body = _http_get(COLLECTOR_PORT, "/healthz")
            return status == 200 and json.loads(body).get("storage_ok") is True

        _wait_for(_collector_ready, "collector /healthz 200 with storage_ok true", STARTUP_TIMEOUT_S)
        ps = _compose(env, "ps", "-q", "collector")
        collector_id = ps.stdout.strip().splitlines()[0] if ps.stdout.strip() else ""
        assert ps.returncode == 0 and _HEX64.fullmatch(collector_id), (ps.stdout, ps.stderr)

        started = datetime.now().astimezone()
        proc = subprocess.run(
            [sys.executable, "-m", "coroner.runner", "run", "a", "--runs-dir", str(RUNS_DIR)],
            capture_output=True,
            text=True,
            timeout=RUNNER_TIMEOUT_S,
            cwd=str(REPO_ROOT),
            env=runner_env,
        )
        finished = datetime.now().astimezone()
        obs["runner"] = {
            "argv": "python -m coroner.runner run a",
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "started": started.isoformat(),
            "finished": finished.isoformat(),
        }
        match = _RUN_LINE.search(proc.stdout)
        run_id = match.group(1) if match else None
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


# ---------------------------------------------------------------- tests


def test_01_runner_passed_and_manifest(experiment) -> None:
    proc = experiment["proc"]
    assert proc.returncode == 0, f"Runner exit {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    run_id = experiment["run_id"]
    assert run_id, f"no PASSED line in Runner output: {proc.stdout!r}"
    manifest = experiment["manifest"]
    assert manifest is not None, f"no manifest at {experiment['manifest_path']}"
    experiment["obs"]["run_id"] = run_id

    assert manifest["experiment"] == "A"
    assert manifest["run_id"] == run_id
    assert manifest["outcome"] == "passed", manifest.get("failure")
    assert manifest["failure"] is None
    assert manifest["finished"] is True
    assert manifest["termination_description"] == TERMINATION_TEXT
    assert manifest["expected"] == {"exit_code": 1, "oom_killed": False}
    state = manifest["container_state"]
    assert state["status"] == "exited"
    assert state["exit_code"] == 1
    assert state["oom_killed"] is False
    assert state["host_config_memory_bytes"] == MEMORY_BYTES
    assert _HEX64.fullmatch(state["container_id"])
    assert manifest["container_id"] == state["container_id"]
    assert manifest["container_left_running"] is False
    assert manifest["collector_health"]["ok"] is True
    assert manifest["collector_health"]["storage_ok"] is True
    assert manifest["collector_health"]["http_status"] == 200
    assert manifest["collector_health"]["elapsed_s"] <= 5.0
    assert manifest["times"]["crash_request_time"]
    assert manifest["times"]["sigkill_sent_time"] is None
    assert manifest["termination"]["posted"] is True
    failed = [c for c in manifest["checks"] if not c["passed"]]
    assert not failed, failed
    # Requirement 9.12: the description (and the rest of the manifest) never calls the exit an exception.
    assert "exception" not in json.dumps(manifest).lower()


def test_02_independent_docker_inspect(experiment) -> None:
    manifest = experiment["manifest"]
    assert manifest is not None
    container_id = manifest["container_id"]
    info = _inspect(experiment["env"], container_id)
    state = info["State"]
    view = {
        "Id": info["Id"],
        "Name": info["Name"],
        "Status": state["Status"],
        "ExitCode": state["ExitCode"],
        "OOMKilled": state["OOMKilled"],
        "Pid": state["Pid"],
        "StartedAt": state["StartedAt"],
        "FinishedAt": state["FinishedAt"],
        "RestartCount": info["RestartCount"],
        "Memory": info["HostConfig"]["Memory"],
        "Image": info["Config"]["Image"],
        "User": info["Config"]["User"],
        "Mounts": info["Mounts"],
        "Project": info["Config"]["Labels"].get("com.docker.compose.project"),
    }
    experiment["obs"]["demo_inspect"] = view

    assert info["Id"] == container_id  # full 64-hex id, same container the Runner reported
    assert _HEX64.fullmatch(info["Id"])
    assert state["Status"] == "exited"
    assert state["ExitCode"] == 1
    assert state["OOMKilled"] is False
    assert state["Running"] is False
    assert state["Dead"] is False
    assert info["RestartCount"] == 0
    assert info["HostConfig"]["Memory"] == MEMORY_BYTES
    assert info["Config"]["Image"] == DEMO_IMAGE
    assert info["Config"]["User"] == "10001:10001"
    assert info["Name"] == f"/{PROJECT}-demo_app-1"
    assert info["Config"]["Labels"].get("com.docker.compose.project") == PROJECT
    assert info["Mounts"] == []  # the Demo_App has no evidence mount

    # The Runner's values equal the independent ones.
    saved = manifest["container_state"]
    assert (saved["status"], saved["exit_code"], saved["oom_killed"]) == (
        state["Status"],
        state["ExitCode"],
        state["OOMKilled"],
    )
    assert saved["host_config_memory_bytes"] == info["HostConfig"]["Memory"]
    # Crash request came before the container finished.
    crash_time = _parse_time(manifest["times"]["crash_request_time"])
    finished_at = _parse_time(state["FinishedAt"])  # nanosecond digits beyond 6 are truncated by fromisoformat
    assert finished_at >= crash_time - timedelta(milliseconds=1)


def test_03_collector_healthy_and_isolated(experiment) -> None:
    env = experiment["env"]
    status, body = _http_get(COLLECTOR_PORT, "/healthz")
    health = json.loads(body)
    experiment["obs"]["collector_healthz"] = {"status": status, "body": health}
    assert status == 200
    assert health["storage_ok"] is True

    collector = _inspect(env, experiment["collector_id"])
    assert collector["State"]["Status"] == "running"
    assert collector["State"]["Health"]["Status"] == "healthy"
    assert collector["RestartCount"] == 0
    assert collector["Config"]["Labels"].get("com.docker.compose.project") == PROJECT
    mounts = [(m.get("Name"), m["Destination"]) for m in collector["Mounts"]]
    assert mounts == [(EVIDENCE_VOLUME, EVIDENCE_MOUNT)], mounts
    bindings = collector["HostConfig"]["PortBindings"]
    assert [b["HostIp"] for b in bindings["8081/tcp"]] == ["127.0.0.1"], bindings

    # Nothing was created in project `coroner`, and no new volume appeared.
    # (Volumes are checked by name below: the existing, labelled `coroner_coroner_evidence` is expected to persist.)
    forbidden = _project_resources(env, FORBIDDEN_PROJECT)
    assert not forbidden["containers"] and not forbidden["networks"], forbidden
    assert forbidden["volumes"] == [f"{FORBIDDEN_PROJECT}_coroner_evidence"] or not forbidden["volumes"], forbidden
    new_volumes = _volume_names(env) - experiment["volumes_before"]
    assert not new_volumes, f"new volumes appeared: {new_volumes}"
    for protected in PROTECTED_VOLUMES:
        assert protected not in {m.get("Name") for m in collector["Mounts"]}
    names = set(_lines(_run(["docker", "ps", "-a", "--format", "{{.Names}}"], env=env)))
    # Exactly the two containers of project coroner-m07 exist; none is named like a `coroner` project container
    # (`coroner-collector-1`, `coroner-demo_app-1`).
    assert names == {f"{PROJECT}-collector-1", f"{PROJECT}-demo_app-1"}, names


def test_04_evidence_has_samples_before_crash(experiment) -> None:
    manifest = experiment["manifest"]
    run_id = experiment["run_id"]
    assert manifest is not None and run_id
    status, raw = _http_get(COLLECTOR_PORT, f"/v1/runs/{run_id}/evidence")
    assert status == 200
    records = _parse_records(raw)
    samples = [r["sample"] for r in records if r.get("record_type") == "sample"]
    metadata = [r for r in records if r.get("record_type") == "run_metadata"]
    terminations = [r for r in records if r.get("record_type") == "termination"]
    assert len(metadata) == 1
    assert len(terminations) == 1
    assert terminations[0]["container_state"] == {"status": "exited", "exit_code": 1, "oom_killed": False}
    assert terminations[0]["cause_label"] == "controlled_fatal_process_exit"
    assert samples, "no Samples in the evidence"
    assert {s["run_id"] for s in samples} == {run_id}
    assert {s["pid"] for s in samples} == {samples[0]["pid"]}
    seqs = [s["seq"] for s in samples]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), f"seq not consecutive: {seqs}"
    assert metadata[0]["durability"] == manifest["durability_level"]

    # At least one Sample captured before crash_request_time, after clock correction (ts - clock_offset_ms).
    crash_time = _parse_time(manifest["times"]["crash_request_time"])
    offset = timedelta(milliseconds=manifest["clock_offset_ms"])
    before = [s for s in samples if _parse_time(s["ts"]) - offset < crash_time]
    assert len(before) >= 1

    # Memory growth: peak RSS is above the first non-null RSS.
    rss = [s["rss_bytes"] for s in samples if s.get("rss_bytes") is not None]
    assert rss and max(rss) > rss[0]
    assert manifest["highest_rss_bytes"] == max(rss)

    experiment["obs"]["evidence"] = {
        "run_id": run_id,
        "sample_count": len(samples),
        "samples_before_crash_request": len(before),
        "first_rss_bytes": rss[0],
        "peak_rss_bytes": max(rss),
        "first_seq": seqs[0],
        "last_seq": seqs[-1],
        "durability": metadata[0]["durability"],
    }
    path = os.environ.get("CORONER_OBSERVATIONS_PATH")
    print("\nEXPERIMENT_A_OBSERVATIONS " + json.dumps(experiment["obs"], sort_keys=True))
    if path:
        Path(path).write_text(
            json.dumps(experiment["obs"], indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
