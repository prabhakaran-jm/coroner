"""Ordinary HTTP 500 non-termination integration test (task 7.2; Requirements 13.4, 13.15, 15.1-15.5).

Runs only with --run-docker or CORONER_RUN_DOCKER=1 (see tests/conftest.py).

Claim under test: an ordinary exception turned into HTTP 500 by `GET /error` does NOT end the Demo_App process.
The process keeps its PID, the container keeps running (no restart), `/health` answers 200 right away, and the
Probe keeps sending Samples with the same `pid` and a `seq` that grows by exactly 1 per Sample.

Rules this module follows:
- Isolated Compose project from $env:CORONER_TEST_PROJECT (the task 8.3 run uses `coroner-t83`; no default, a missing
  or protected project such as `coroner`, `coroner-m07`, `coroner-spike` fails before any Docker call, see
  tests/docker_integration/_isolation.py), file compose.yaml, always passed explicitly. Evidence volume
  `<project>_coroner_evidence`.
- Images coroner-collector:m0 and coroner-demo-app:m0 must already exist; nothing is built (`--no-build`).
- Teardown is `docker compose down` WITHOUT `-v` (skipped when CORONER_KEEP_STACK=1). No volume is removed,
  no prune is run. A unique run id is used, so no existing evidence file is touched.
- /crash and /leak are never called, and nothing is killed. Only `GET /error` is sent.
- Assertions read the running containers through `docker inspect` / `docker exec`, not compose.yaml.
- Set CORONER_OBSERVATIONS_PATH to also write the recorded observations as JSON to that file.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from tests.docker_integration._isolation import M07_VAR, evidence_volume, gate, selected_project

pytestmark = pytest.mark.docker_integration

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "compose.yaml"
PROJECT = selected_project(M07_VAR)  # raw value, validated by gate() before any live use
COLLECTOR_IMAGE = "coroner-collector:m0"
DEMO_IMAGE = "coroner-demo-app:m0"
EVIDENCE_VOLUME = evidence_volume(PROJECT)
EVIDENCE_MOUNT = "/data/evidence"
DEMO_PORT = 8080
COLLECTOR_PORT = 8081
MEMORY_BYTES = 134217728
STARTUP_TIMEOUT_S = 90
HEALTH_MAX_LATENCY_S = 1.0
MIN_NEW_SAMPLES = 4
EVIDENCE_WAIT_S = 15.0
HTTP_TIMEOUT_S = 5.0

# Variables that would change the defaults under test; removed from the subprocess environment only.
_SCRUBBED_ENV = (
    "CORONER_DEMO_MEM_LIMIT",
    "CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS",
    "CORONER_COLLECTOR_PORT",
    "CORONER_DEMO_PORT",
    "CORONER_DURABILITY",
    "CORONER_RUN_ID",
    "CORONER_SAMPLE_INTERVAL_MS",
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
    """GET http://127.0.0.1:<port><path>; 4xx and 5xx are answers (read from HTTPError), not failures."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _health() -> dict:
    status, body = _http_get(DEMO_PORT, "/health")
    assert status == 200, (status, body)
    return json.loads(body)


def _evidence_samples(run_id: str) -> list[dict]:
    """Persisted Sample records of the run, in file order, read through the Collector's read API."""
    status, body = _http_get(COLLECTOR_PORT, f"/v1/runs/{run_id}/evidence")
    if status == 404:
        return []
    assert status == 200, (status, body[:200])
    records = [json.loads(line) for line in body.decode("utf-8").splitlines() if line.strip()]
    return [r for r in records if r.get("record_type") == "sample"]


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


def _state_view(info: dict) -> dict:
    state = info["State"]
    return {
        "Id": info["Id"],
        "Status": state["Status"],
        "Running": state["Running"],
        "Restarting": state["Restarting"],
        "Dead": state["Dead"],
        "OOMKilled": state["OOMKilled"],
        "ExitCode": state["ExitCode"],
        "Pid": state["Pid"],
        "StartedAt": state["StartedAt"],
        "FinishedAt": state["FinishedAt"],
        "RestartCount": info["RestartCount"],
    }


def _env_map(info: dict) -> dict[str, str]:
    return dict(e.split("=", 1) for e in info["Config"]["Env"] if "=" in e)


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def stack():
    """Start collector and demo_app from prebuilt images with the destructive flag on (it only enables the route
    set; this test calls only /error). Tear down without removing volumes unless CORONER_KEEP_STACK=1."""
    gate(M07_VAR)
    run_id = f"m07-500-{uuid.uuid4().hex[:8]}"
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_ENV}
    env.update({"CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS": "1", "CORONER_RUN_ID": run_id})

    for image in (COLLECTOR_IMAGE, DEMO_IMAGE):
        proc = _run(["docker", "image", "inspect", image], env=env)
        if proc.returncode != 0:
            pytest.fail(f"image {image} is missing; build it first (this test never builds): {proc.stderr.strip()}")

    # Refuse to start over an existing project: report instead of touching it.
    existing = _run(
        ["docker", "ps", "-a", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}"], env=env
    )
    assert existing.returncode == 0, existing.stderr
    assert not existing.stdout.strip(), f"containers of project {PROJECT} already exist: {existing.stdout.split()}"

    try:
        up = _compose(env, "up", "-d", "--no-build", "collector", "demo_app")
        assert up.returncode == 0, f"docker compose up failed:\n{up.stdout}\n{up.stderr}"
        ids = {}
        for service in ("collector", "demo_app"):
            ps = _compose(env, "ps", "-q", service)
            cid = ps.stdout.strip().splitlines()[0] if ps.stdout.strip() else ""
            assert ps.returncode == 0 and cid, f"service {service} has no container: {ps.stderr}"
            ids[service] = cid

        def _collector_ready() -> bool:
            status, body = _http_get(COLLECTOR_PORT, "/healthz")
            return status == 200 and json.loads(body).get("storage_ok") is True

        def _demo_ready() -> bool:
            status, _ = _http_get(DEMO_PORT, "/health")
            return status == 200

        _wait_for(_collector_ready, "collector /healthz 200 with storage_ok true", STARTUP_TIMEOUT_S)
        _wait_for(_demo_ready, "demo_app /health 200", STARTUP_TIMEOUT_S)
        yield {"run_id": run_id, "env": env, "collector": ids["collector"], "demo": ids["demo_app"], "obs": {}}
    finally:
        if os.environ.get("CORONER_KEEP_STACK") == "1":
            print(f"\nCORONER_KEEP_STACK=1: stack of project {PROJECT} left running (run id {run_id})")
        else:
            _compose(env, "down", timeout=180)  # never -v: the evidence volume must survive


# ---------------------------------------------------------------- tests


def test_01_infrastructure_as_running(stack) -> None:
    env = stack["env"]
    collector = _inspect(env, stack["collector"])
    demo = _inspect(env, stack["demo"])

    # Images are the existing :m0 images.
    assert collector["Config"]["Image"] == COLLECTOR_IMAGE
    assert demo["Config"]["Image"] == DEMO_IMAGE

    # Both published ports are bound to 127.0.0.1 only.
    for info, port in ((collector, "8081/tcp"), (demo, "8080/tcp")):
        bindings = info["HostConfig"]["PortBindings"]
        assert set(bindings) == {port}, bindings
        assert [b["HostIp"] for b in bindings[port]] == ["127.0.0.1"], bindings
        for entries in info["NetworkSettings"]["Ports"].values():
            for b in entries or []:
                assert b["HostIp"] == "127.0.0.1", b

    # Demo App: user, memory, swap, restart policy, privileges, destructive flag, run id.
    assert demo["Config"]["User"] == "10001:10001"
    assert demo["HostConfig"]["Memory"] == MEMORY_BYTES
    assert demo["HostConfig"]["MemorySwap"] == MEMORY_BYTES
    assert demo["HostConfig"]["RestartPolicy"]["Name"] == "no"
    assert demo["HostConfig"]["Privileged"] is False
    demo_env = _env_map(demo)
    assert demo_env["CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS"] == "1"
    assert demo_env["CORONER_RUN_ID"] == stack["run_id"]

    # Collector mounts only the isolated evidence volume; the Demo App has no evidence mount.
    mounts = [(m.get("Name"), m["Destination"]) for m in collector["Mounts"]]
    assert mounts == [(EVIDENCE_VOLUME, EVIDENCE_MOUNT)], mounts
    assert collector["HostConfig"]["Privileged"] is False
    for m in demo["Mounts"]:
        assert "coroner_evidence" not in (m.get("Name") or ""), m
        assert m["Destination"] != EVIDENCE_MOUNT, m

    # Collector healthy with real storage: HTTP 200 and storage_ok exactly true.
    status, body = _http_get(COLLECTOR_PORT, "/healthz")
    assert status == 200
    assert json.loads(body)["storage_ok"] is True


def test_02_ordinary_500_does_not_terminate_demo_app(stack) -> None:
    env, run_id, demo_cid = stack["env"], stack["run_id"], stack["demo"]
    obs: dict = {"run_id": run_id, "project": PROJECT, "demo_container": demo_cid, "collector_container": stack["collector"]}
    stack["obs"] = obs

    # ---- BEFORE
    before_health = _health()
    assert before_health["run_id"] == run_id
    pid_before = before_health["pid"]
    next_seq_before = before_health["probe"]["next_seq"]
    _wait_for(lambda: len(_evidence_samples(run_id)) >= 2, "persisted Samples for the run", EVIDENCE_WAIT_S)
    samples_before = _evidence_samples(run_id)
    state_before = _state_view(_inspect(env, demo_cid))
    assert state_before["Id"] == demo_cid
    assert state_before["Status"] == "running"
    assert state_before["RestartCount"] == 0
    assert {s["sample"]["pid"] for s in samples_before} == {pid_before}
    obs["before"] = {
        "health": before_health,
        "pid": pid_before,
        "next_seq": next_seq_before,
        "persisted_samples": len(samples_before),
        "last_persisted_seq": samples_before[-1]["sample"]["seq"],
        "container": state_before,
    }

    # ---- ACT: one ordinary exception; the framework answers 500.
    t_request = time.monotonic()
    error_status, error_body = _http_get(DEMO_PORT, "/error")
    t_response = time.monotonic()
    obs["error"] = {"status": error_status, "body": error_body.decode("utf-8", "replace")[:200], "request_s": round(t_response - t_request, 4)}
    assert error_status == 500, (error_status, error_body)

    # ---- AFTER: /health must answer 200 within 1 s of the 500 response, with the same pid.
    after_status, after_body = _http_get(DEMO_PORT, "/health")
    t_health = time.monotonic()
    health_latency = t_health - t_response
    obs["health_after"] = {"status": after_status, "latency_s_from_500_response": round(health_latency, 4)}
    assert after_status == 200, (after_status, after_body)
    after_health = json.loads(after_body)
    obs["health_after"]["body"] = after_health
    assert health_latency <= HEALTH_MAX_LATENCY_S, f"/health took {health_latency:.3f}s after the 500"
    assert after_health["pid"] == pid_before, (after_health["pid"], pid_before)
    assert after_health["run_id"] == run_id

    # Samples keep arriving: at least MIN_NEW_SAMPLES beyond those persisted before the request.
    n_before = len(samples_before)
    t_wait = time.monotonic()
    _wait_for(lambda: len(_evidence_samples(run_id)) >= n_before + MIN_NEW_SAMPLES, f"{MIN_NEW_SAMPLES} new Samples", EVIDENCE_WAIT_S)
    samples_after = _evidence_samples(run_id)
    obs["samples_wait_s"] = round(time.monotonic() - t_wait, 3)

    seqs = [s["sample"]["seq"] for s in samples_after]
    pids = {s["sample"]["pid"] for s in samples_after}
    new = samples_after[n_before:]
    assert len(new) >= MIN_NEW_SAMPLES
    assert pids == {pid_before}, f"pid changed across the 500: {pids} (before: {pid_before})"
    assert all(s["sample"]["run_id"] == run_id for s in samples_after)
    # seq grows by exactly 1 per Sample across the whole run, including the 500 boundary (no gap, reset or repeat).
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), f"seq not consecutive: {seqs}"
    # Samples created after the request exist: the highest persisted seq passed the Probe's next_seq recorded before it.
    assert seqs[-1] >= next_seq_before, (seqs[-1], next_seq_before)
    final_health = _health()
    assert final_health["pid"] == pid_before
    assert final_health["probe"]["next_seq"] > next_seq_before
    obs["after"] = {
        "pid": final_health["pid"],
        "next_seq": final_health["probe"]["next_seq"],
        "dropped": final_health["probe"]["dropped"],
        "persisted_samples": len(samples_after),
        "new_samples": len(new),
        "first_seq": seqs[0],
        "last_seq": seqs[-1],
        "seq_consecutive": True,
        "pids_in_evidence": sorted(pids),
    }

    # ---- Container state: same container, still running, never restarted.
    state_after = _state_view(_inspect(env, demo_cid))
    obs["after"]["container"] = state_after
    assert state_after["Id"] == state_before["Id"]
    assert state_after["Status"] == "running"
    assert state_after["Running"] is True
    assert state_after["Restarting"] is False
    assert state_after["Dead"] is False
    assert state_after["OOMKilled"] is False
    assert state_after["ExitCode"] == 0
    assert state_after["RestartCount"] == 0
    assert state_after["StartedAt"] == state_before["StartedAt"]
    assert state_after["Pid"] == state_before["Pid"]

    # ---- Collector still healthy with real storage.
    h_status, h_body = _http_get(COLLECTOR_PORT, "/healthz")
    h = json.loads(h_body)
    obs["collector_healthz"] = {"status": h_status, "body": h}
    assert h_status == 200
    assert h["storage_ok"] is True
    collector_state = _state_view(_inspect(env, stack["collector"]))
    obs["collector_container"] = collector_state
    assert collector_state["Status"] == "running"
    assert collector_state["RestartCount"] == 0

    print("\nORDINARY_500_OBSERVATIONS " + json.dumps(obs, sort_keys=True))
    path = os.environ.get("CORONER_OBSERVATIONS_PATH")
    if path:
        Path(path).write_text(json.dumps(obs, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
