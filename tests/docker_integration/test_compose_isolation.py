"""Live Compose isolation and health checks (task 5.7).

Runs only with --run-docker or CORONER_RUN_DOCKER=1 (see tests/conftest.py).

Rules this module follows:
- Project `coroner`, file compose.yaml. Images coroner-collector:m0 and coroner-demo-app:m0 must already
  exist; nothing is built or pruned here.
- Teardown is `docker compose down` WITHOUT `-v`. No volume is ever removed, no prune is ever run.
- Assertions read the actual running containers through `docker inspect`, not compose.yaml.
- /crash is never called when the destructive flag could be enabled; no OOM experiment is run.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.docker_integration

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "compose.yaml"
PROJECT = "coroner"
COLLECTOR_IMAGE = "coroner-collector:m0"
DEMO_IMAGE = "coroner-demo-app:m0"
EVIDENCE_VOLUME = "coroner_coroner_evidence"
EVIDENCE_MOUNT = "/data/evidence"
DEFAULT_MEMORY = 134217728
STARTUP_TIMEOUT_S = 60

# Compose variables that would change the defaults under test.
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


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_ENV}
    env.update(extra)
    return env


def _run(args: list[str], *, timeout: int = 120, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, cwd=str(REPO_ROOT), env=env or _env()
    )


def _compose(*args: str, timeout: int = 180, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return _run(["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *args], timeout=timeout, env=env)


def _inspect(target: str) -> dict:
    proc = _run(["docker", "inspect", target])
    assert proc.returncode == 0, f"docker inspect {target} failed: {proc.stderr}"
    return json.loads(proc.stdout)[0]


def _container_id(service: str) -> str:
    proc = _compose("ps", "-q", service)
    cid = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    assert proc.returncode == 0 and cid, f"service {service} has no container: {proc.stderr}"
    return cid


def _exec(cid: str, *cmd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return _run(["docker", "exec", cid, *cmd], timeout=timeout)


def _exec_python(cid: str, code: str) -> str:
    proc = _exec(cid, "python", "-c", code)
    assert proc.returncode == 0, f"docker exec python failed: {proc.stderr}"
    return proc.stdout


def _parse_time(text: str) -> datetime:
    """Parse Docker's RFC3339 time with nanoseconds (truncate to microseconds)."""
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, rest = text.split(".", 1)
        digits = ""
        while rest and rest[0].isdigit():
            digits, rest = digits + rest[0], rest[1:]
        text = f"{head}.{digits[:6].ljust(6, '0')}{rest}"
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def _host_port(info: dict, container_port: str) -> tuple[str, int]:
    bindings = info["HostConfig"]["PortBindings"][container_port]
    assert len(bindings) == 1, bindings
    return bindings[0]["HostIp"], int(bindings[0]["HostPort"])


def _http_get(port: int, path: str, timeout: float = 5.0) -> tuple[int, bytes]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:  # 4xx and 5xx are answers, not failures
        return exc.code, exc.read()


def _health_status(cid: str) -> str:
    return _inspect(cid)["State"].get("Health", {}).get("Status", "none")


def _wait(predicate, what: str, timeout: float = STARTUP_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(1)
    pytest.fail(f"timed out after {timeout}s waiting for {what}")


def _evidence_snapshot(cid: str) -> dict[str, int]:
    """Map evidence-directory file name to size, read inside the collector container."""
    code = (
        "import json,os;d='/data/evidence';"
        "print(json.dumps({n:os.path.getsize(os.path.join(d,n)) for n in sorted(os.listdir(d))}))"
    )
    return json.loads(_exec_python(cid, code))


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def stack():
    """Bring both services up from prebuilt images; tear down without removing volumes."""
    for image in (COLLECTOR_IMAGE, DEMO_IMAGE):
        proc = _run(["docker", "image", "inspect", image])
        if proc.returncode != 0:
            pytest.fail(f"image {image} is missing; build it first (this test never builds): {proc.stderr.strip()}")

    up = _compose("up", "-d", "--no-build", timeout=300)
    try:
        assert up.returncode == 0, f"docker compose up failed:\n{up.stdout}\n{up.stderr}"
        collector = _container_id("collector")
        demo = _container_id("demo_app")
        _wait(lambda: _health_status(collector) == "healthy", "collector healthy")
        _wait(lambda: _inspect(demo)["State"]["Running"], "demo_app running")
        yield {"collector": collector, "demo": demo}
    finally:
        # Never `-v`: the evidence volume must survive.
        _compose("down", timeout=180)


@pytest.fixture(scope="module")
def collector_info(stack) -> dict:
    return _inspect(stack["collector"])


@pytest.fixture(scope="module")
def demo_info(stack) -> dict:
    return _inspect(stack["demo"])


# ---------------------------------------------------------------- tests


def test_01_both_services_running(stack) -> None:
    for name in ("collector", "demo"):
        state = _inspect(stack[name])["State"]
        assert state["Running"] is True, state
        assert state["Restarting"] is False
        assert state["Dead"] is False


def test_02_collector_is_healthy(stack) -> None:
    assert _health_status(stack["collector"]) == "healthy"


def test_03_collector_health_requires_200_and_storage_ok(collector_info) -> None:
    test = collector_info["Config"]["Healthcheck"]["Test"]
    assert test[0] == "CMD"
    joined = " ".join(test)
    assert "/healthz" in joined
    assert "r.status==200" in joined
    assert "storage_ok" in joined and "is True" in joined
    # Observed behavior: the live body reports storage_ok true.
    _, port = _host_port(collector_info, "8081/tcp")
    status, body = _http_get(port, "/healthz")
    assert status == 200
    assert json.loads(body)["storage_ok"] is True


def test_04_demo_waits_for_healthy_collector(stack, collector_info, demo_info) -> None:
    cfg = _compose("config", "--format", "json")
    assert cfg.returncode == 0, cfg.stderr
    depends = json.loads(cfg.stdout)["services"]["demo_app"]["depends_on"]
    assert depends["collector"]["condition"] == "service_healthy"
    # A healthcheck cannot pass before its first interval (2s) elapses, so the demo, which Compose
    # starts only after the collector is healthy, must start at least that long after the collector.
    collector_started = _parse_time(collector_info["State"]["StartedAt"])
    demo_started = _parse_time(demo_info["State"]["StartedAt"])
    interval = timedelta(seconds=collector_info["Config"]["Healthcheck"]["Interval"] / 1e9)
    assert demo_started >= collector_started + interval
    # Docker keeps only the last 5 probe results, so the first pass may be gone. When the log is not full,
    # it still holds the first passing probe, and the demo must not have started before it ended.
    log = collector_info["State"]["Health"]["Log"]
    passes = [_parse_time(e["End"]) for e in log if e["ExitCode"] == 0]
    if passes and len(log) < 5:
        assert demo_started >= min(passes)


def test_05_ports_bound_to_loopback_only(collector_info, demo_info) -> None:
    for info, port in ((collector_info, "8081/tcp"), (demo_info, "8080/tcp")):
        bindings = info["HostConfig"]["PortBindings"]
        assert set(bindings) == {port}
        for b in bindings[port]:
            assert b["HostIp"] == "127.0.0.1", b
        published = info["NetworkSettings"]["Ports"]
        for entries in published.values():
            for b in entries or []:
                assert b["HostIp"] not in ("0.0.0.0", "::", ""), b
                assert b["HostIp"] == "127.0.0.1"


def test_06_demo_memory_limit_default(demo_info) -> None:
    assert demo_info["HostConfig"]["Memory"] == DEFAULT_MEMORY


def test_07_demo_swap_equals_memory(demo_info) -> None:
    hc = demo_info["HostConfig"]
    assert hc["MemorySwap"] == hc["Memory"]


def test_08_demo_not_privileged_and_hardened(demo_info) -> None:
    hc = demo_info["HostConfig"]
    assert hc["Privileged"] is False
    assert [c.upper() for c in hc["CapDrop"]] == ["ALL"]
    assert not hc.get("CapAdd")
    assert "no-new-privileges:true" in hc["SecurityOpt"] or "no-new-privileges" in hc["SecurityOpt"]
    assert demo_info["Config"]["User"] == "10001:10001"


def test_09_demo_has_no_evidence_mount(demo_info) -> None:
    for m in demo_info["Mounts"]:
        assert m.get("Name") != EVIDENCE_VOLUME, m
        assert m["Destination"] != EVIDENCE_MOUNT, m
        assert "coroner_evidence" not in (m.get("Name") or ""), m
    assert not any(b.endswith(EVIDENCE_MOUNT) for b in demo_info["HostConfig"].get("Binds") or [])


def test_10_demo_root_filesystem_read_only(demo_info) -> None:
    assert demo_info["HostConfig"]["ReadonlyRootfs"] is True


def test_11_tmp_is_tmpfs(stack, demo_info) -> None:
    tmpfs = demo_info["HostConfig"].get("Tmpfs") or {}
    assert "/tmp" in tmpfs
    code = (
        "import json;rows=[l.split() for l in open('/proc/mounts')];"
        "print(json.dumps([r[2] for r in rows if r[1]=='/tmp']))"
    )
    types = json.loads(_exec_python(stack["demo"], code))
    assert types and types[-1] == "tmpfs", types


def test_12_restart_policies_match_design(collector_info, demo_info) -> None:
    c = collector_info["HostConfig"]["RestartPolicy"]
    assert (c["Name"], c["MaximumRetryCount"]) == ("on-failure", 5)
    assert demo_info["HostConfig"]["RestartPolicy"]["Name"] in ("no", "")


def test_13_destructive_endpoints_disabled_by_default(demo_info) -> None:
    env = dict(e.split("=", 1) for e in demo_info["Config"]["Env"] if "=" in e)
    flag = env.get("CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS")
    # Guard: only send these requests when the flag is exactly "0". With "0" the routes do not exist,
    # so even GET /crash cannot reach the crash handler.
    assert flag == "0", f"destructive flag is {flag!r}; refusing to probe /crash and /leak"
    _, port = _host_port(demo_info, "8080/tcp")
    for path in ("/crash", "/leak", "/error"):
        status, _ = _http_get(port, path)
        assert status == 404, (path, status)
    status, _ = _http_get(port, "/health")
    assert status == 200


_PROC_SCAN = (
    "import json,os\n"
    "me=os.getpid();out={}\n"
    "for p in os.listdir('/proc'):\n"
    "    if p.isdigit() and int(p)!=me:\n"
    "        try: out[p]=open('/proc/%s/cmdline'%p,'rb').read().decode().split(chr(0))[:-1]\n"
    "        except OSError: pass\n"
    "print(json.dumps(out))\n"
)


@pytest.mark.parametrize(
    "key,module",
    [("collector", "coroner.collector.main"), ("demo", "coroner.demo_app.main")],
)
def test_14_pid1_is_module_entrypoint_single_worker(stack, key: str, module: str) -> None:
    cid = stack[key]
    cmdline = _exec(cid, "python", "-c", "print(open('/proc/1/cmdline','rb').read().decode().replace(chr(0),' ').strip())")
    assert cmdline.returncode == 0, cmdline.stderr
    assert cmdline.stdout.strip().endswith(f"python -m {module}") or cmdline.stdout.split()[1:] == ["-m", module], cmdline.stdout

    procs = json.loads(_exec_python(cid, _PROC_SCAN))
    assert set(procs) == {"1"}, f"unexpected extra processes (workers?): {procs}"

    top = _run(["docker", "top", cid, "-eo", "pid,args"])
    assert top.returncode == 0, top.stderr
    rows = [ln for ln in top.stdout.splitlines()[1:] if ln.strip()]
    assert len(rows) == 1, top.stdout
    assert f"-m {module}" in rows[0]
    assert "uvicorn" not in rows[0] or "--workers" not in rows[0]


def test_15_invalid_memory_limit_rejected_without_touching_evidence(stack) -> None:
    collector = stack["collector"]
    marker = f".isolation-marker-{uuid.uuid4().hex}"
    marker_text = "isolation-check"
    write = _exec_python(
        collector,
        f"open('{EVIDENCE_MOUNT}/{marker}','w').write('{marker_text}');print('ok')",
    )
    assert write.strip() == "ok"
    started_at = _inspect(collector)["State"]["StartedAt"]
    container_id = _inspect(collector)["Id"]
    before = _evidence_snapshot(collector)
    assert marker in before
    try:
        bad_env = _env(CORONER_DEMO_MEM_LIMIT="bogus")
        cfg = _compose("config", env=bad_env)
        assert cfg.returncode != 0, "compose config accepted CORONER_DEMO_MEM_LIMIT=bogus"
        assert "bogus" in (cfg.stderr + cfg.stdout)
        # `up` must reject it too (compose fails at parse time, before touching any container).
        up = _compose("up", "-d", "--no-deps", "--no-build", "demo_app", env=bad_env)
        assert up.returncode != 0, "invalid CORONER_DEMO_MEM_LIMIT was accepted by up"
        assert "bogus" in (up.stderr + up.stdout)

        # Collector untouched: same container, same start time, still running and healthy.
        info = _inspect(collector)
        assert info["Id"] == container_id
        assert info["State"]["Running"] is True
        assert info["State"]["StartedAt"] == started_at
        assert info["RestartCount"] == 0

        # Volume still exists and the evidence content is intact (append-only growth is allowed).
        vol = _run(["docker", "volume", "inspect", EVIDENCE_VOLUME])
        assert vol.returncode == 0, vol.stderr
        after = _evidence_snapshot(collector)
        assert after[marker] == before[marker]
        for name, size in before.items():
            assert name in after, f"evidence file vanished: {name}"
            assert after[name] >= size, f"evidence file shrank: {name}"
        content = _exec_python(collector, f"print(open('{EVIDENCE_MOUNT}/{marker}').read(),end='')")
        assert content == marker_text
    finally:
        _exec(collector, "python", "-c", f"import os;os.remove('{EVIDENCE_MOUNT}/{marker}')")


def _image_probe(image: str, code: str) -> str:
    proc = _run(["docker", "run", "--rm", "--entrypoint", "python", image, "-c", code], timeout=120)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _pip_names(image: str) -> set[str]:
    out = _image_probe(
        image,
        "import json,importlib.metadata as m;print(json.dumps(sorted(d.metadata['Name'].lower() for d in m.distributions())))",
    )
    return set(json.loads(out))


@pytest.mark.parametrize(
    "image,expected_dirs,forbidden",
    [
        (COLLECTOR_IMAGE, {"common", "evidence", "collector"}, {"demo_app", "probe", "runner", "tests", "docs", "spike"}),
        (DEMO_IMAGE, {"probe", "demo_app"}, {"collector", "evidence", "common", "runner", "tests", "docs", "spike"}),
    ],
)
def test_16_image_source_and_dependency_boundaries(image: str, expected_dirs: set[str], forbidden: set[str]) -> None:
    listing = json.loads(
        _image_probe(
            image,
            "import json,os;print(json.dumps(sorted(n for n in os.listdir('/app/coroner') if n!='__pycache__')))",
        )
    )
    names = set(listing)
    assert names == expected_dirs | {"__init__.py"}, listing
    assert not (names & forbidden)

    top = json.loads(_image_probe(image, "import json,os;print(json.dumps(sorted(os.listdir('/app'))))"))
    assert not ({"tests", "docs", "spike", "runner", "compose.yaml"} & set(top)), top

    pip_names = _pip_names(image)
    # Runner and test tooling never ship in service images.
    assert not (pip_names & {"pytest", "hypothesis", "docker", "typer", "click"} - _allowed_extras(image)), pip_names


def _allowed_extras(image: str) -> set[str]:
    # click comes in transitively with uvicorn.
    return {"click"}


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "--run-docker", "-q"]))
