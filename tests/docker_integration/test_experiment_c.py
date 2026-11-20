"""Experiment C integration test (task 7.5; Requirements 11.1, 11.3-11.5, 11.10-11.20, 13.5).

Runs only with --run-docker or CORONER_RUN_DOCKER=1 (see tests/conftest.py). Live only: there is no mocked variant.

The test starts its own Collector, then launches the real Experiment_Runner (`python -m coroner.runner run c`, default
128m limit, 4 MiB leak chunks) as a subprocess. The Runner calls `POST /leak` about once per second until the kernel
OOM-kills the Demo_App container (or 120 s pass), then reads `docker inspect`. The test then checks the outcome against
the manifest, an independent `docker inspect` of the full container id, `docker logs` of the Demo_App and the
evidence read back from the Collector.

OOM SAFETY
- A genuine OOM kill happens only when this test is run with --run-docker, which must be separately authorised.
  Collection, py_compile and the default (deselected) run never touch Docker.
- This test never kills, stops or signals anything and never calls `/crash` or `/leak`. The Runner never calls
  `DockerPort.kill` or `/crash` in Experiment C (see coroner/runner/experiments/exp_c.py); it only leaks.
- If OOMKilled is not true the Runner reports FAILED and this test fails honestly. Do not retry with another tactic and
  do not relax checks; stop and ask (task 7.5).
- ASSUMPTION TO VALIDATE: that a cgroup memory limit of 128 MiB produces State.OOMKilled=true, ExitCode=137 on
  Windows / Docker Desktop (WSL2). Nothing here asserts that it will; a real run decides.
- The 120 s deadline path (Runner outcome failed, code `timeout`, `container_left_running` true, container still
  running) is documented but not triggered by this test. If it occurs, test_01 fails and the Demo_App is left
  running for the user to inspect. The test never stops it; remove it with `down` (no -v).
- Diagnostics (`rss_monotonic_tail`, `peak_rss_ratio_of_limit`) are recorded but never asserted to have a particular
  value, and they never feed the verdict.

Rules this module follows (same as test_experiment_a.py and test_experiment_b.py):
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

MANUAL REAL-RUN STEPS (run only after separate authorisation; the Runner leaks until the kernel OOM-kills the Demo_App)
1. Confirm the images exist: `docker image inspect coroner-collector:m0 coroner-demo-app:m0`.
2. Confirm no coroner-m07 container exists: `docker ps -a --filter label=com.docker.compose.project=coroner-m07`.
   If one does: `docker compose -p coroner-m07 -f compose.yaml down` (no -v), then re-check.
3. From the repository root, with the virtual environment active (or `.\\.venv\\Scripts\\python.exe -m pytest`):
   `$env:CORONER_KEEP_STACK = '1'` (PowerShell; optional, keeps the exited Demo_App for manual `docker inspect`), then
   `python -m pytest tests/docker_integration/test_experiment_c.py --run-docker -v -s -p no:cacheprovider`
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
RUNNER_TIMEOUT_S = 360  # Runner preamble + up to 120 s of leaking + inspect + evidence
HTTP_TIMEOUT_S = 5.0
MEMORY_BYTES = 134217728
TERMINATION_TEXT = "genuine Docker OOM kill (kernel cgroup memory limit)"
CAUSE_LABEL = "docker_oom_kill"
FORBIDDEN_CAUSE_LABEL = "sigkill_sent_by_runner"
MANDATORY_CHECKS = (
    "oom_killed_true",
    "exit_code_137",
    "collector_survival",
    "preserved_precrash_evidence",
    "measurable_memory_growth",
    "docker_inspect_recorded",
)
EXTRA_CHECKS = ("memory_limit_matches_host_config", "durability_matches_run_metadata", "termination_recorded")

_HEX64 = re.compile(r"[0-9a-f]{64}")
_RUN_LINE = re.compile(r"^Experiment C (\S+): (?:PASSED|FAILED|ABORTED)\b", re.MULTILINE | re.IGNORECASE)

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


def _capture_order(samples: list[dict]) -> list[dict]:
    """Samples in capture order: Capture_Time (`sample.ts`), then `seq`."""
    return sorted(samples, key=lambda r: (_parse_time(r["sample"]["ts"]), r["sample"].get("seq", 0)))


def _resolve(path_text: str) -> Path:
    path = Path(path_text)
    return path if path.is_absolute() else REPO_ROOT / path


# ---------------------------------------------------------------- fixture


@pytest.fixture(scope="module")
def experiment():
    """Start the Collector, run Experiment C through the real Runner, and yield what was observed.

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
            "Experiment C needs a fresh stack because the Runner recreates the Demo_App."
        )
    volumes_before = _volume_names(env)
    runs_before = {p.name for p in RUNS_DIR.iterdir()} if RUNS_DIR.is_dir() else set()

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
            [sys.executable, "-m", "coroner.runner", "run", "c", "--runs-dir", str(RUNS_DIR)],
            capture_output=True,
            text=True,
            timeout=RUNNER_TIMEOUT_S,
            cwd=str(REPO_ROOT),
            env=runner_env,
        )
        elapsed = time.monotonic() - t0
        obs["runner"] = {
            "argv": "python -m coroner.runner run c",
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
            "runs_before": runs_before,
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
    # The Runner's own saved copy (what its manifest values were computed from), read only; None if absent.
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
    """Independent `docker inspect` of the manifest's full container id (read only)."""
    manifest = experiment["manifest"]
    assert manifest is not None, f"no manifest at {experiment['manifest_path']}"
    taken = datetime.now().astimezone()
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
        "MemorySwap": info["HostConfig"].get("MemorySwap"),
        "Mounts": info["Mounts"],
        "independent_inspect_time": taken.isoformat(),
    }
    return info


# ---------------------------------------------------------------- tests


def test_01_runner_passed_and_genuine_oom(experiment) -> None:
    proc = experiment["proc"]
    assert proc.returncode == 0, (
        f"Runner exit {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}\n"
        "If OOMKilled was not true: report honestly, do not retry with another tactic, do not relax checks; stop and ask."
    )
    run_id = experiment["run_id"]
    assert run_id, f"no result line in Runner output: {proc.stdout!r}"
    assert run_id not in experiment["runs_before"], "run id already existed; pre-existing evidence must not be reused"
    manifest = experiment["manifest"]
    assert manifest is not None, f"no manifest at {experiment['manifest_path']}"

    assert manifest["experiment"] == "C"
    assert manifest["run_id"] == run_id
    assert manifest["outcome"] == "passed", manifest.get("failure")
    assert manifest["failure"] is None
    assert manifest["finished"] is True
    assert manifest["termination_description"] == TERMINATION_TEXT
    assert manifest["expected"] == {"exit_code": 137, "oom_killed": True}
    failed = [c for c in manifest["checks"] if not c["passed"]]
    assert not failed, failed
    # 120 s deadline path (failure code `timeout`) did not occur: the container stopped and was not left running.
    assert manifest["container_left_running"] is False


def test_02_independent_inspect_is_genuine_oom_and_matches_manifest(experiment, demo_info) -> None:
    manifest = experiment["manifest"]
    state = demo_info["State"]
    host = demo_info["HostConfig"]

    # (1) genuine Docker OOM, from docker inspect only
    assert state["OOMKilled"] is True
    assert state["ExitCode"] == 137
    assert state["Status"] == "exited"
    assert state["Running"] is False
    # (2) limit
    assert host["Memory"] == MEMORY_BYTES
    assert host["MemorySwap"] == host["Memory"]  # no extra swap

    # (8) independent inspect equals the manifest
    assert _HEX64.fullmatch(demo_info["Id"])
    assert demo_info["Id"] == manifest["container_id"]
    saved = manifest["container_state"]
    assert saved["container_id"] == demo_info["Id"]
    assert (saved["status"], saved["exit_code"], saved["oom_killed"]) == (state["Status"], state["ExitCode"], state["OOMKilled"])
    assert saved["host_config_memory_bytes"] == host["Memory"]
    excerpt = saved["raw_inspect_excerpt"]
    assert excerpt["Id"] == demo_info["Id"]
    assert excerpt["State"]["OOMKilled"] is True and excerpt["State"]["ExitCode"] == 137
    assert excerpt["HostConfig"]["Memory"] == host["Memory"]

    raw_path = _resolve(manifest["docker_inspect_path"])
    assert raw_path.is_file(), raw_path
    assert _resolve(saved["raw_inspect_path"]) == raw_path
    recorded = json.loads(raw_path.read_bytes())[0]  # the Runner's raw copy, kept unchanged
    assert recorded["Id"] == demo_info["Id"]
    for key in ("Status", "ExitCode", "OOMKilled", "StartedAt", "FinishedAt"):
        assert recorded["State"][key] == state[key], key
    assert recorded["HostConfig"]["Memory"] == host["Memory"]

    raw_finished = state["FinishedAt"]
    assert raw_finished and not raw_finished.startswith("0001-01-01"), raw_finished
    finished_at = _parse_time(raw_finished)
    manifest_finished = manifest["container_finished_at"]
    assert manifest_finished is not None
    assert abs(_parse_time(manifest_finished) - finished_at) <= timedelta(milliseconds=1)
    # inspect_time is the Runner's host clock, FinishedAt the Docker daemon (VM) clock: mixed clock domains, so the
    # margin is the manifest's clock offset magnitude plus 250 ms.
    inspect_time = _parse_time(saved["inspect_time"])
    margin = timedelta(milliseconds=abs(manifest["clock_offset_ms"]) + 250)
    assert inspect_time >= finished_at - margin, "Runner inspected before the container finished"
    assert not manifest["container_state_problems"]

    assert demo_info["Name"] == f"/{DEMO_NAME}"
    assert demo_info["Config"]["Image"] == DEMO_IMAGE
    assert demo_info["Config"]["Labels"].get("com.docker.compose.project") == PROJECT


def test_03_runner_never_killed_or_crashed(experiment, demo_info) -> None:
    manifest = experiment["manifest"]
    # The Runner records no crash request and no SIGKILL for Experiment C; the cause is the kernel OOM kill.
    assert manifest["times"] == {"crash_request_time": None, "sigkill_sent_time": None}
    assert manifest["termination"]["cause_label"] == CAUSE_LABEL
    assert manifest["termination"]["cause_label"] != FORBIDDEN_CAUSE_LABEL
    assert manifest["termination_description"] != "SIGKILL sent by the runner (docker kill --signal=KILL)"

    # Read-only `docker logs`. The real protection against a crash call is the verdict (ExitCode 137 and OOMKilled
    # true; a /crash call would exit 1 via os._exit). The "/crash" absence check below is only supplementary: the
    # process exits inside the handler, so a POST /crash would never be written to the access log.
    logs = subprocess.run(
        ["docker", "logs", manifest["container_id"]],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
        env=experiment["env"],
    )
    assert logs.returncode == 0, logs.stderr
    text = logs.stdout + logs.stderr
    # uvicorn.run(...) in coroner/demo_app/main.py keeps the default access log, so served requests appear as
    # `"POST /leak?bytes=N HTTP/1.1"`. Requiring one stops an empty log from passing the check vacuously.
    assert re.search(r'"POST /leak\b', text), f"no /leak access-log line in the Demo_App logs ({len(text)} chars)"
    assert "/crash" not in text
    experiment["obs"]["demo_log_lines"] = len(text.splitlines())
    experiment["obs"]["demo_log_leak_requests"] = text.count("/leak")


def test_04_pre_termination_samples_and_rss_growth(experiment, demo_info, evidence) -> None:
    manifest = experiment["manifest"]
    samples = evidence["samples"]
    assert samples, "no Samples in the evidence"
    offset = timedelta(milliseconds=manifest["clock_offset_ms"])
    finished_at = _parse_time(demo_info["State"]["FinishedAt"])

    # (4) at least one Sample received (host clock after offset conversion) before the container finished.
    pre = [r for r in samples if _parse_time(r["recv_time"]) - offset < finished_at]
    assert len(pre) >= 1, f"no pre-termination Sample; FinishedAt {finished_at}, offset {offset}"

    # (5) RSS growth recomputed from evidence: null RSS is skipped, never 0; capture order.
    rss = [r["sample"]["rss_bytes"] for r in _capture_order(samples) if r["sample"].get("rss_bytes") is not None]
    assert rss, "no readable RSS in the evidence"
    assert all(type(v) is int and v > 0 for v in rss)
    assert max(rss) > rss[0], f"no RSS growth: first {rss[0]}, peak {max(rss)}"

    memory = manifest["memory"]
    assert memory["configured_limit_bytes"] == MEMORY_BYTES
    assert memory["memory_limit_bytes"] == MEMORY_BYTES
    # Exact against the Runner's saved evidence copy; against the live read only `<=` for the maximum, because
    # samples stored after the Runner's fetch can only raise it (and could in principle precede the first one).
    if evidence["runner_samples"] is not None:
        runner_rss = [
            r["sample"]["rss_bytes"] for r in _capture_order(evidence["runner_samples"]) if r["sample"].get("rss_bytes") is not None
        ]
        assert memory["first_rss_bytes"] == runner_rss[0]
        assert memory["highest_rss_bytes"] == max(runner_rss)
        assert manifest["highest_rss_bytes"] == max(runner_rss)
    else:
        assert memory["highest_rss_bytes"] <= max(rss)
        assert manifest["highest_rss_bytes"] <= max(rss)
        assert memory["first_rss_bytes"] > 0

    # Sample container_id where reported: equal to the full id, or a prefix of it when the source is `hostname`.
    for rec in samples:
        s = rec["sample"]
        cid = s.get("container_id")
        if cid is None:
            continue
        if s.get("container_id_source") == "hostname":
            assert demo_info["Id"].startswith(cid), (cid, demo_info["Id"])
        else:
            assert cid == demo_info["Id"], (cid, demo_info["Id"])

    experiment["obs"]["samples"] = {
        "count": len(samples),
        "pre_termination": len(pre),
        "first_rss_bytes": rss[0],
        "peak_rss_bytes": max(rss),
        "peak_ratio_of_limit": round(max(rss) / MEMORY_BYTES, 4),
        "clock_offset_ms": manifest["clock_offset_ms"],
        "container_finished_at": demo_info["State"]["FinishedAt"],
    }


def test_05_verdict_and_diagnostics(experiment, evidence) -> None:
    manifest = experiment["manifest"]
    names = [c["name"] for c in manifest["checks"]]
    # (7) the verdict is the six mandatory checks, in order; the Runner's extra checks follow; diagnostics never appear.
    assert tuple(names[: len(MANDATORY_CHECKS)]) == MANDATORY_CHECKS, names
    assert set(names[len(MANDATORY_CHECKS):]) == set(EXTRA_CHECKS), names
    assert all(c["passed"] is True for c in manifest["checks"]), manifest["checks"]
    assert not [n for n in names if re.search(r"diagnostic|monotonic|ratio|sampling", n)]

    # diagnostics: present, informational only; values other than consistency are not asserted.
    diag = manifest["diagnostics"]
    assert isinstance(diag, dict)
    for key in ("rss_monotonic_tail", "peak_rss_ratio_of_limit", "sample_count"):
        assert key in diag, f"diagnostics lacks {key}: {sorted(diag)}"
    assert diag["rss_monotonic_tail"] is None or isinstance(diag["rss_monotonic_tail"], bool)
    assert type(diag["sample_count"]) is int and 1 <= diag["sample_count"] <= len(evidence["samples"])
    ratio = diag["peak_rss_ratio_of_limit"]
    assert isinstance(ratio, float)
    rss = [s["sample"]["rss_bytes"] for s in evidence["samples"] if s["sample"].get("rss_bytes") is not None]
    assert ratio == pytest.approx(max(rss) / MEMORY_BYTES, rel=0.05)  # consistency only, not a threshold

    limits = manifest["sampling_limitations"]
    assert isinstance(limits, dict)
    statements = limits["statements"]
    assert isinstance(statements, list) and len(statements) == 3
    assert all(isinstance(s, str) and s.strip() for s in statements), statements
    assert "sample_interval_ms" in limits and "approx_leak_rate_bytes_per_s" in limits

    experiment["obs"]["diagnostics"] = diag
    experiment["obs"]["sampling_limitations"] = limits


def test_06_termination_record_and_durability(experiment, evidence) -> None:
    manifest = experiment["manifest"]
    assert manifest["termination"]["posted"] is True
    assert len(evidence["terminations"]) == 1
    term = evidence["terminations"][0]
    assert term["cause_label"] == CAUSE_LABEL
    assert term["cause_label"] != FORBIDDEN_CAUSE_LABEL
    assert term["container_state"] == {"status": "exited", "exit_code": 137, "oom_killed": True}

    # (11) durability matches run_metadata.
    assert len(evidence["metadata"]) == 1
    assert evidence["metadata"][0]["durability"] == manifest["durability_level"]


def test_07_collector_survives_and_isolated(experiment) -> None:
    env = experiment["env"]
    manifest = experiment["manifest"]
    assert manifest["collector_health"]["ok"] is True
    assert manifest["collector_health"]["storage_ok"] is True
    assert manifest["collector_health"]["http_status"] == 200

    status, body = _http_get(COLLECTOR_PORT, "/healthz")
    health = json.loads(body)
    experiment["obs"]["collector_healthz"] = {"status": status, "body": health}
    assert status == 200
    assert health["storage_ok"] is True  # exactly true

    collector = _inspect(env, experiment["collector_id"])
    assert collector["Name"] == f"/{COLLECTOR_NAME}"
    assert collector["State"]["Status"] == "running"
    assert collector["State"]["OOMKilled"] is False
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
    assert {COLLECTOR_NAME, DEMO_NAME} <= names  # exact names
    assert not names & {"coroner-collector-1", "coroner-demo_app-1"}, names

    print("\nEXPERIMENT_C_OBSERVATIONS " + json.dumps(experiment["obs"], sort_keys=True, default=str))
    path = os.environ.get("CORONER_OBSERVATIONS_PATH")
    if path:
        Path(path).write_text(
            json.dumps(experiment["obs"], indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8", newline="\n"
        )
