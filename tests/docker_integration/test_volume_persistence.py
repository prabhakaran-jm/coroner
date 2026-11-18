"""Named-volume ownership and persistence under UID/GID 10001 (task 5.6; Docker integration, opt-in).

Runs only with --run-docker or CORONER_RUN_DOCKER=1. Starts ONLY the `collector` service of compose project
`coroner` (`up -d --no-deps collector`; the image `coroner-collector:m0` must already be built), checks the
volume ownership and mode, ingests a Sample, restarts the container, and reads the evidence back through the
read-only API.

Safety: this module never removes anything. Teardown is `docker compose stop collector`; it never uses `-v`,
`down -v`, `volume rm` or any prune. A fresh volume is guaranteed by the coordinator's preflight absence check
(Execution Rule 11), not by this module. On a failure the diagnostics (inspect output, logs) are printed, nothing
is deleted. Every Sample uses a unique run id `m05-vol-<uuid8>`, so a reused volume is not disturbed.

Run live:  python -m pytest tests/docker_integration/test_volume_persistence.py --run-docker -q
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.docker_integration

REPO = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO / "compose.yaml"
DOCKERFILE = REPO / "docker" / "collector.Dockerfile"
PROJECT = "coroner"
SERVICE = "collector"
IMAGE = "coroner-collector:m0"
EVIDENCE_DIR = "/data/evidence"
UID_GID = 10001
HEALTHY_TIMEOUT_S = 90
CMD_TIMEOUT_S = 120

# Runs inside the container. Reads {"method","path","body"} from stdin, prints {"status","body"}.
_HTTP_SCRIPT = (
    "import json,sys,urllib.request as u,urllib.error as e\n"
    "a=json.load(sys.stdin)\n"
    "d=a['body'].encode() if a.get('body') is not None else None\n"
    "q=u.Request('http://127.0.0.1:8081'+a['path'],data=d,method=a['method'],headers={'Content-Type':'application/json'})\n"
    "try:\n"
    "  r=u.urlopen(q,timeout=10);s=r.status;b=r.read().decode()\n"
    "except e.HTTPError as x:\n"
    "  s=x.code;b=x.read().decode()\n"
    "print(json.dumps({'status':s,'body':b}))\n"
)

# Runs inside the container: write + fsync + unlink a temp file in the evidence directory as the Collector user.
_WRITE_SCRIPT = (
    "import os,sys\n"
    "p=sys.argv[1]\n"
    "fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)\n"
    "try:\n"
    "  os.write(fd,b'probe');os.fsync(fd)\n"
    "finally:\n"
    "  os.close(fd);os.unlink(p)\n"
    "print('ok')\n"
)


def _run(args: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        args, input=input_text, capture_output=True, text=True, encoding="utf-8", timeout=CMD_TIMEOUT_S, cwd=REPO
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"command failed ({proc.returncode}): {' '.join(args)}\nstdout: {proc.stdout}\nstderr: {proc.stderr}")
    return proc


def _compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *args], check=check)


def _container_id() -> str:
    cid = _compose("ps", "-q", SERVICE).stdout.strip()
    assert cid, "the collector container does not exist"
    return cid.splitlines()[0]


def _inspect(cid: str) -> dict[str, Any]:
    return json.loads(_run(["docker", "inspect", cid]).stdout)[0]


def _exec(cid: str, *cmd: str, input_text: str | None = None) -> str:
    args = ["docker", "exec"] + (["-i"] if input_text is not None else []) + [cid, *cmd]
    return _run(args, input_text=input_text).stdout


def _http(cid: str, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, str]:
    payload = json.dumps({"method": method, "path": path, "body": None if body is None else json.dumps(body)})
    out = json.loads(_exec(cid, "python", "-c", _HTTP_SCRIPT, input_text=payload))
    return out["status"], out["body"]


def _wait_healthy(cid: str) -> None:
    deadline = time.monotonic() + HEALTHY_TIMEOUT_S
    status = "unknown"
    while time.monotonic() < deadline:
        state = _inspect(cid)["State"]
        status = (state.get("Health") or {}).get("Status", "none")
        if state.get("Running") and status == "healthy":
            return
        time.sleep(1)
    logs = _run(["docker", "logs", "--tail", "50", cid], check=False)
    raise AssertionError(f"collector not healthy after {HEALTHY_TIMEOUT_S}s (health={status})\n{logs.stdout}\n{logs.stderr}")


def _dockerfile_mode() -> str:
    match = re.search(r"chmod\s+0?([0-7]{3,4})\s+/data/evidence\b", DOCKERFILE.read_text(encoding="utf-8"))
    assert match, "no `chmod <mode> /data/evidence` line in docker/collector.Dockerfile"
    return match.group(1).lstrip("0") or "0"


def _make_sample(run_id: str, seq: int) -> dict[str, Any]:
    return {
        "run_id": run_id, "seq": seq, "ts": "2025-01-01T12:00:06.512Z", "rss_bytes": 1000, "request_count": 1,
        "loop_delay_ms": 0.5, "pid": 1, "container_id": "3f1c0e0a9d7b", "container_id_source": "mountinfo",
        "interval_ms": 500, "dropped": 0,
    }


def _records(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _sample_records(text: str) -> list[dict[str, Any]]:
    return [r for r in _records(text) if r.get("record_type") == "sample"]


class Stack:
    """Mutable state shared by the ordered tests in this module."""

    def __init__(self, cid: str, run_id: str) -> None:
        self.cid = cid
        self.run_id = run_id
        self.sample0 = _make_sample(run_id, 0)
        self.before_restart_text = ""
        self.restart_id = ""

    @property
    def evidence_path(self) -> str:
        return f"{EVIDENCE_DIR}/{self.run_id}.jsonl"


@pytest.fixture(scope="module")
def stack():
    if _run(["docker", "image", "inspect", IMAGE], check=False).returncode != 0:
        pytest.fail(f"image {IMAGE} is missing. Build it first (docker compose -p coroner build collector); this test does not build.")
    _compose("up", "-d", "--no-deps", SERVICE)
    cid = _container_id()
    try:
        _wait_healthy(cid)
    except AssertionError:
        print(json.dumps(_inspect(cid), indent=2, default=str)[:6000])
        raise
    yield Stack(cid, f"m05-vol-{uuid.uuid4().hex[:8]}")
    # Teardown: stop only. Never `down -v`, never remove the volume, never prune.
    _compose("stop", SERVICE, check=False)


def _diag(stack: Stack) -> str:
    info = _inspect(stack.cid)
    keep = {k: info.get(k) for k in ("State", "Mounts", "Config")}
    keep["HostConfig"] = {k: info["HostConfig"].get(k) for k in ("CapDrop", "SecurityOpt", "ReadonlyRootfs", "Binds")}
    return json.dumps(keep, indent=2, default=str)[:6000]


def test_01_collector_runs_as_uid_gid_10001(stack: Stack) -> None:
    ids = _exec(stack.cid, "id").strip()
    status = _exec(stack.cid, "cat", "/proc/1/status")
    uid_line = next(line for line in status.splitlines() if line.startswith("Uid:")).split()[1:]
    gid_line = next(line for line in status.splitlines() if line.startswith("Gid:")).split()[1:]
    assert f"uid={UID_GID}" in ids and f"gid={UID_GID}" in ids, f"id output: {ids}\n{_diag(stack)}"
    assert set(uid_line) == {str(UID_GID)}, f"PID 1 uids {uid_line}"  # real, effective, saved, fs
    assert set(gid_line) == {str(UID_GID)}, f"PID 1 gids {gid_line}"


def test_02_evidence_dir_owner_and_mode_match_dockerfile(stack: Stack) -> None:
    out = _exec(stack.cid, "stat", "-c", "%u:%g %a", EVIDENCE_DIR).strip()
    owner, mode = out.split()
    assert owner == f"{UID_GID}:{UID_GID}", f"{EVIDENCE_DIR} owner is {owner}\n{_diag(stack)}"
    assert mode == _dockerfile_mode(), f"{EVIDENCE_DIR} mode is {mode}, Dockerfile says {_dockerfile_mode()}"
    mounts = [m for m in _inspect(stack.cid)["Mounts"] if m["Destination"] == EVIDENCE_DIR]
    assert mounts and mounts[0]["Type"] == "volume" and mounts[0]["RW"] is True, mounts


def test_03_healthz_reports_storage_ok(stack: Stack) -> None:
    status, body = _http(stack.cid, "GET", "/healthz")
    parsed = json.loads(body)
    assert status == 200 and parsed.get("storage_ok") is True, f"{status} {body}\n{_diag(stack)}"


def test_04_non_root_write_and_fsync_in_volume(stack: Stack) -> None:
    name = f"{EVIDENCE_DIR}/.m05-probe-{uuid.uuid4().hex[:8]}.tmp"  # not *.jsonl, removed by the script itself
    assert _exec(stack.cid, "python", "-c", _WRITE_SCRIPT, name).strip() == "ok"


def test_05_post_valid_sample_and_file_holds_it(stack: Stack) -> None:
    status, body = _http(stack.cid, "POST", "/v1/samples", stack.sample0)
    assert status == 200, f"{status} {body}\n{_diag(stack)}"
    assert json.loads(body)["status"] == "stored"
    names = _exec(stack.cid, "ls", "-1", EVIDENCE_DIR).split()
    assert f"{stack.run_id}.jsonl" in names, names
    text = _exec(stack.cid, "cat", stack.evidence_path)
    stored = _sample_records(text)
    assert [r["sample"] for r in stored] == [stack.sample0]
    stack.before_restart_text = text


def test_06_restart_keeps_volume_and_becomes_healthy(stack: Stack) -> None:
    stack.restart_id = _inspect(stack.cid)["State"]["StartedAt"]
    _compose("restart", SERVICE)
    cid = _container_id()
    assert cid == stack.cid, "restart must keep the same container"
    _wait_healthy(cid)
    assert _inspect(cid)["State"]["StartedAt"] != stack.restart_id, "container did not restart"


def test_07_evidence_unchanged_and_not_duplicated_after_restart(stack: Stack) -> None:
    status, body = _http(stack.cid, "GET", f"/v1/runs/{stack.run_id}/evidence")
    assert status == 200, f"{status} {body}"
    samples = _sample_records(body)
    assert len(samples) == 1, f"expected exactly one sample record, got {len(samples)}"
    assert samples[0]["sample"] == stack.sample0
    before = _sample_records(stack.before_restart_text)[0]
    assert samples[0] == before, "the stored Sample record changed across the restart"
    # Every record written before the restart is still present exactly once.
    after_lines = [line for line in body.splitlines() if line.strip()]
    for line in stack.before_restart_text.splitlines():
        if line.strip():
            assert after_lines.count(line) == 1, f"record not preserved exactly once: {line}"
    # The file in the volume agrees with the API.
    on_disk = _exec(stack.cid, "cat", stack.evidence_path)
    assert on_disk == body


def test_08_hardening_in_place_and_volume_still_writable(stack: Stack) -> None:
    host = _inspect(stack.cid)["HostConfig"]
    assert [c.upper() for c in host["CapDrop"] or []] == ["ALL"], host["CapDrop"]
    assert "no-new-privileges:true" in (host["SecurityOpt"] or []), host["SecurityOpt"]
    # A second Sample after the restart is accepted and appended, so the restricted process can still write.
    second = _make_sample(stack.run_id, 1)
    status, body = _http(stack.cid, "POST", "/v1/samples", second)
    assert status == 200, f"{status} {body}\n{_diag(stack)}"
    status, evidence = _http(stack.cid, "GET", f"/v1/runs/{stack.run_id}/evidence")
    assert status == 200
    seqs = [r["sample"]["seq"] for r in _sample_records(evidence)]
    assert seqs == [0, 1], seqs
    status, health = _http(stack.cid, "GET", "/healthz")
    assert status == 200 and json.loads(health).get("storage_ok") is True
