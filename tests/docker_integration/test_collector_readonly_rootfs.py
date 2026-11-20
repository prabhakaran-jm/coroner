"""Collector under a read-only root filesystem (task 5.8; Docker integration, opt-in).

Runs only with --run-docker or CORONER_RUN_DOCKER=1. compose.yaml does NOT make the collector root
filesystem read-only (by design). This module checks that it could be: it starts ONE uniquely named container
from the already built image `coroner-collector:m0`, outside Compose, with `--read-only` and no tmpfs, as UID/GID
10001, all capabilities dropped, no-new-privileges, no published ports, no privileged mode, the evidence volume
mounted read-write and the image CMD unchanged. It then checks that the root filesystem rejects writes, the volume
accepts them, /healthz reports storage_ok, a Sample is stored and readable, and that every evidence file that
existed beforehand is byte-identical afterwards.

Safety: the only object this module removes is its own container `coroner-ro-test-<uuid8>` (`docker stop`,
`docker rm`). It never uses `-v`, `down`, `volume rm` or any prune, and mounts the volume read-only for its
snapshots. Unique run id `m05-ro-<uuid8>`, so an existing volume is not disturbed.

Isolation: the volume is `<project>_coroner_evidence` of the project in $env:CORONER_TEST_M05_PROJECT (the task 8.3
run uses `coroner-t83-m05`; no default, a missing or protected project fails before any Docker call, see
_isolation.py). BOTH the read-only snapshot mounts and the writable container mount use that volume only.
Module order: it needs that volume to exist (created by test_compose_isolation.py / test_volume_persistence.py in the
same project) and no container may use it. test_volume_persistence.py leaves a stopped container, so the coordinator
must first run `docker compose -p coroner-t83-m05 -f compose.yaml down` (never -v); otherwise this module fails
before starting anything.

Run live:  python -m pytest tests/docker_integration/test_collector_readonly_rootfs.py --run-docker -q
"""
from __future__ import annotations

import json
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests.docker_integration._isolation import M05_VAR, evidence_volume, gate, selected_project

pytestmark = pytest.mark.docker_integration

REPO = Path(__file__).resolve().parents[2]
IMAGE = "coroner-collector:m0"
PROJECT = selected_project(M05_VAR)  # raw value, validated by gate() before any live use
VOLUME = evidence_volume(PROJECT)
EVIDENCE_DIR = "/data/evidence"
UID_GID = "10001:10001"
HEALTHY_TIMEOUT_S = 60
CMD_TIMEOUT_S = 120
# Files the Collector legitimately touches on start (lock, storage probe directory); not evidence.
LEGITIMATE = {".collector.lock", ".coroner_probe"}

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

# Tries to create a file on the root filesystem; prints the OS error number and text.
_ROOT_WRITE_SCRIPT = (
    "import sys\n"
    "try:\n"
    "  open(sys.argv[1],'w').write('x')\n"
    "  print('WRITE-SUCCEEDED')\n"
    "except OSError as x:\n"
    "  print('errno=%d %s' % (x.errno, x.strerror))\n"
)

# Snapshot of the volume's top-level regular files: {name: [size, sha256]}.
_SNAPSHOT_SCRIPT = (
    "import hashlib,json,os\n"
    "r='/e';o={}\n"
    "for n in sorted(os.listdir(r)):\n"
    "  p=os.path.join(r,n)\n"
    "  if os.path.isfile(p):\n"
    "    b=open(p,'rb').read();o[n]=[len(b),hashlib.sha256(b).hexdigest()]\n"
    "print(json.dumps(o))\n"
)


def _run(args: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        args, input=input_text, capture_output=True, text=True, encoding="utf-8", timeout=CMD_TIMEOUT_S, cwd=REPO
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"command failed ({proc.returncode}): {' '.join(args)}\nstdout: {proc.stdout}\nstderr: {proc.stderr}")
    return proc


def _inspect(name: str) -> dict[str, Any]:
    return json.loads(_run(["docker", "inspect", name]).stdout)[0]


def _exec(name: str, *cmd: str, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    args = ["docker", "exec"] + (["-i"] if input_text is not None else []) + [name, *cmd]
    return _run(args, input_text=input_text, check=check)


def _http(name: str, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, str]:
    payload = json.dumps({"method": method, "path": path, "body": None if body is None else json.dumps(body)})
    out = json.loads(_exec(name, "python", "-c", _HTTP_SCRIPT, input_text=payload).stdout)
    return out["status"], out["body"]


def _snapshot() -> dict[str, list[Any]]:
    """Hashes of the volume's files, taken by a separate throw-away container with the volume mounted read-only."""
    out = _run([
        "docker", "run", "--rm", "--network", "none", "-v", f"{VOLUME}:/e:ro",
        "--entrypoint", "python", IMAGE, "-c", _SNAPSHOT_SCRIPT,
    ]).stdout
    return json.loads(out)


def _volume_users() -> list[str]:
    return _run(["docker", "ps", "-a", "-q", "--filter", f"volume={VOLUME}"]).stdout.split()


def _sample(run_id: str, seq: int) -> dict[str, Any]:
    return {
        "run_id": run_id, "seq": seq, "ts": "2025-01-01T12:00:06.512Z", "rss_bytes": 1000, "request_count": 1,
        "loop_delay_ms": 0.5, "pid": 1, "container_id": "3f1c0e0a9d7b", "container_id_source": "mountinfo",
        "interval_ms": 500, "dropped": 0,
    }


def _sample_records(text: str) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    return [r for r in rows if r.get("record_type") == "sample"]


class RoBox:
    """State shared by the ordered tests: the unique container, the run id, and the before snapshot."""

    def __init__(self, name: str, run_id: str, before: dict[str, list[Any]]) -> None:
        self.name = name
        self.run_id = run_id
        self.before = before
        self.after: dict[str, list[Any]] = {}
        self.removed = False


def _cleanup(name: str) -> None:
    """Stop and remove ONLY the container with this unique name; the volume is never touched."""
    _run(["docker", "stop", name], check=False)
    _run(["docker", "rm", name], check=False)


@pytest.fixture(scope="module")
def box():
    gate(M05_VAR)
    if _run(["docker", "image", "inspect", IMAGE], check=False).returncode != 0:
        pytest.fail(f"image {IMAGE} is missing. Build it first; this test does not build.")
    if _run(["docker", "volume", "inspect", VOLUME], check=False).returncode != 0:
        pytest.fail(
            f"volume {VOLUME} does not exist; this test does not create or recreate it. Run test_compose_isolation.py "
            f"and test_volume_persistence.py with the same CORONER_TEST_M05_PROJECT={PROJECT} first (module order)."
        )
    users = _volume_users()
    assert not users, (
        f"other containers use {VOLUME}: {users}. test_volume_persistence.py leaves a stopped container; run "
        f"`docker compose -p {PROJECT} -f compose.yaml down` (no -v) first."
    )
    before = _snapshot()
    name = f"coroner-ro-test-{uuid.uuid4().hex[:8]}"
    run_id = f"m05-ro-{uuid.uuid4().hex[:8]}"
    created = False
    try:
        # No `-p`, no `--privileged`, no tmpfs, image CMD unchanged. Same env as the compose collector service.
        _run([
            "docker", "run", "-d", "--name", name, "--read-only", "--user", UID_GID,
            "-v", f"{VOLUME}:{EVIDENCE_DIR}", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "-e", "CORONER_DURABILITY=crash", IMAGE,
        ])
        created = True
        deadline = time.monotonic() + HEALTHY_TIMEOUT_S
        last = ""
        while time.monotonic() < deadline:
            state = _inspect(name)["State"]
            if not state.get("Running"):
                logs = _run(["docker", "logs", name], check=False)
                print(json.dumps(_inspect(name), indent=2, default=str)[:6000])
                raise AssertionError(f"collector exited under --read-only: {state}\n{logs.stdout}\n{logs.stderr}")
            probe = _exec(name, "python", "-c", _HTTP_SCRIPT, input_text=json.dumps({"method": "GET", "path": "/healthz", "body": None}), check=False)
            last = probe.stdout + probe.stderr
            if probe.returncode == 0 and json.loads(probe.stdout)["status"] == 200:
                break
            time.sleep(1)
        else:
            logs = _run(["docker", "logs", name], check=False)
            print(json.dumps(_inspect(name), indent=2, default=str)[:6000])
            raise AssertionError(f"collector not ready after {HEALTHY_TIMEOUT_S}s: {last}\n{logs.stdout}\n{logs.stderr}")
        yield RoBox(name, run_id, before)
    finally:
        if created:
            _cleanup(name)


def test_01_root_filesystem_is_read_only_and_hardened(box: RoBox) -> None:
    info = _inspect(box.name)
    host = info["HostConfig"]
    assert host["ReadonlyRootfs"] is True, host["ReadonlyRootfs"]
    assert not host.get("Tmpfs"), f"unexpected tmpfs: {host.get('Tmpfs')}"
    assert host["Privileged"] is False
    assert [c.upper() for c in host["CapDrop"] or []] == ["ALL"] and not host.get("CapAdd")
    assert "no-new-privileges:true" in (host["SecurityOpt"] or [])
    assert info["Config"]["User"] == UID_GID
    assert not host.get("PortBindings"), host.get("PortBindings")  # nothing published
    assert not (info["NetworkSettings"].get("Ports") or {}).get("8081/tcp"), "port 8081 is published"
    assert info["Config"]["Cmd"] == ["python", "-m", "coroner.collector.main"]  # image CMD, not overridden
    mounts = [m for m in info["Mounts"] if m["Destination"] == EVIDENCE_DIR]
    assert len(info["Mounts"]) == 1 and mounts[0]["Type"] == "volume" and mounts[0]["Name"] == VOLUME and mounts[0]["RW"] is True
    assert _exec(box.name, "cat", "/proc/1/cmdline").stdout.replace("\x00", " ").strip() == "python -m coroner.collector.main"


def test_02_root_filesystem_rejects_writes(box: RoBox) -> None:
    for target in ("/app/ro-probe.txt", "/ro-probe.txt", "/tmp/ro-probe.txt"):
        out = _exec(box.name, "python", "-c", _ROOT_WRITE_SCRIPT, target).stdout.strip()
        assert out.startswith("errno=30") and "Read-only file system" in out, f"{target}: {out}"


def test_03_evidence_volume_is_writable_as_non_root(box: RoBox) -> None:
    ids = _exec(box.name, "id").stdout
    assert "uid=10001" in ids and "gid=10001" in ids, ids
    name = f"{EVIDENCE_DIR}/.m05-ro-probe-{uuid.uuid4().hex[:8]}.tmp"
    assert _exec(box.name, "python", "-c", _WRITE_SCRIPT, name).stdout.strip() == "ok"


def test_04_healthz_reports_storage_ok(box: RoBox) -> None:
    status, body = _http(box.name, "GET", "/healthz")
    assert status == 200 and json.loads(body).get("storage_ok") is True, f"{status} {body}"


def test_05_sample_accepted_persisted_and_readable(box: RoBox) -> None:
    sample = _sample(box.run_id, 0)
    status, body = _http(box.name, "POST", "/v1/samples", sample)
    assert status == 200 and json.loads(body)["status"] == "stored", f"{status} {body}"
    on_disk = _exec(box.name, "cat", f"{EVIDENCE_DIR}/{box.run_id}.jsonl").stdout
    assert [r["sample"] for r in _sample_records(on_disk)] == [sample]
    status, api = _http(box.name, "GET", f"/v1/runs/{box.run_id}/evidence")
    assert status == 200, f"{status} {api}"
    assert [r["sample"] for r in _sample_records(api)] == [sample]
    assert api == on_disk, "the read API and the file in the volume differ"


def test_06_pre_existing_evidence_files_unchanged(box: RoBox) -> None:
    box.after = _snapshot()
    expected = {n: v for n, v in box.before.items() if n not in LEGITIMATE}
    actual = {n: v for n, v in box.after.items() if n not in LEGITIMATE and n != f"{box.run_id}.jsonl"}
    assert actual == expected, f"pre-existing files changed.\nbefore: {expected}\nafter:  {actual}"
    assert f"{box.run_id}.jsonl" in box.after, "the new run file is missing"
    assert not (set(box.after) - set(box.before) - {f"{box.run_id}.jsonl"}), "unexpected new files in the volume"


def test_07_container_stops_and_is_removed_volume_remains(box: RoBox) -> None:
    _run(["docker", "stop", box.name])
    assert _inspect(box.name)["State"]["Running"] is False
    _run(["docker", "rm", box.name])
    box.removed = True
    assert _run(["docker", "inspect", box.name], check=False).returncode != 0, "container still exists"
    assert _run(["docker", "volume", "inspect", VOLUME], check=False).returncode == 0, "volume is gone"
    assert _volume_users() == []
    # The new run file survived the container.
    assert f"{box.run_id}.jsonl" in _snapshot()
