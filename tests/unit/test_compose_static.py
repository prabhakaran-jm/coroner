"""Docker-free static checks of compose.yaml and the two Dockerfiles (Task 5.4).

The check functions are pure: they take parsed YAML data or Dockerfile text. The expected values below are
constants written here, not read from the files under test. Fixture tests prove each check can fail
(negative controls); real-file tests run the same checks on the repository files, and a missing file fails them.

Validates: Requirements 8.3, 8.5, 8.6, 8.7, 8.9, 8.10, 8.11, 8.13, 13.8.

Design decisions encoded (design.md, Docker isolation):
- cap_drop [ALL], security_opt no-new-privileges:true and user "10001:10001" are required on BOTH services
  (the design sketch has them on demo_app as well as the collector).
- CMD must be exec-form JSON. Either it passes an explicit "--workers" "1" to uvicorn, or it is
  ["python", "-m", "coroner.<svc>.main"]. The module form is accepted only when the service's Python source
  calls the server with workers=1 (and no other workers value); the test reads that source text.
"""

from __future__ import annotations

import copy
import json
import re
import shlex
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

APPROVED_BASE = "python:3.12.8-slim-bookworm"
UID = GID = 10001
OWNER = "10001:10001"
EVIDENCE = "/data/evidence"
MODE = "0750"
VOLUME = "coroner_evidence"
COLLECTOR_URL = "http://collector:8081"

COPY_SETS = {
    "collector": {"coroner/__init__.py", "coroner/common", "coroner/evidence", "coroner/collector"},
    "demo": {"coroner/__init__.py", "coroner/probe", "coroner/demo_app"},
}
FORBIDDEN = {
    "collector": {"coroner/demo_app", "coroner/probe", "coroner/runner", "tests", "spike", "runs"},
    "demo": {"coroner/collector", "coroner/evidence", "coroner/common", "coroner/runner", "tests", "spike", "runs"},
}
REQUIREMENTS = {"collector": "collector.txt", "demo": "demo_app.txt"}
MAIN_SOURCES = {
    "collector": ["coroner/collector/main.py", "coroner/collector/app.py"],
    "demo": ["coroner/demo_app/main.py"],
}
SERVICE = {"collector": "collector", "demo": "demo_app"}
MODULE = {"collector": "coroner.collector.main", "demo": "coroner.demo_app.main"}
DOCKERFILES = {"collector": "docker/collector.Dockerfile", "demo": "docker/demo_app.Dockerfile"}
IMAGES = {"collector": "coroner-collector:m0", "demo": "coroner-demo-app:m0"}
PORTS = {"collector": ("CORONER_COLLECTOR_PORT", 8081, 8081), "demo": ("CORONER_DEMO_PORT", 8080, 8080)}


def _require(cond: object, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# --------------------------------------------------------------------------------------------- Dockerfile

def parse_dockerfile(text: str) -> list[tuple[str, str]]:
    """Return (INSTRUCTION, args) pairs with line continuations joined and comments dropped."""
    out: list[tuple[str, str]] = []
    buf = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not buf and (not line or line.startswith("#")):
            continue
        if buf and line.startswith("#"):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        parts = buf.split(None, 1)
        out.append((parts[0].upper(), parts[1].strip() if len(parts) > 1 else ""))
        buf = ""
    if buf.strip():
        parts = buf.split(None, 1)
        out.append((parts[0].upper(), parts[1].strip() if len(parts) > 1 else ""))
    return out


def check_image_ref(ref: str) -> None:
    """Pinned: tagged, not latest (Requirement 8.9)."""
    _require(ref and "$" not in ref, f"image reference {ref!r} must be literal")
    name = ref.rsplit("/", 1)[-1]
    _require(":" in name, f"image reference {ref!r} has no tag")
    tag = name.split(":", 1)[1]
    _require(tag and tag.lower() != "latest", f"image reference {ref!r} uses an unpinned tag")


def check_from(ins: list[tuple[str, str]]) -> None:
    stages: set[str] = set()
    seen = 0
    for name, args in ins:
        if name != "FROM":
            continue
        toks = [t for t in args.split() if not t.startswith("--platform")]
        _require(toks, "empty FROM")
        ref = toks[0]
        if ref not in stages:
            seen += 1
            check_image_ref(ref)
            _require(ref == APPROVED_BASE, f"FROM {ref!r} is not the approved base {APPROVED_BASE!r}")
        if len(toks) >= 3 and toks[1].upper() == "AS":
            stages.add(toks[2])
    _require(seen >= 1, "no FROM line")


def _norm_src(s: str) -> str:
    s = s.strip()
    while s.startswith("./"):
        s = s[2:]
    s = s.rstrip("/")
    return s if s else "."


def copy_sources(ins: list[tuple[str, str]]) -> list[str]:
    srcs: list[str] = []
    for name, args in ins:
        if name not in ("COPY", "ADD"):
            continue
        if name == "ADD":
            _require("://" not in args, f"ADD of a URL is not allowed: {args!r}")
        toks = [t for t in (json.loads(args) if args.startswith("[") else shlex.split(args))]
        flags = [t for t in toks if t.startswith("--")]
        if any(f.startswith("--from") for f in flags):
            continue
        toks = [t for t in toks if not t.startswith("--")]
        srcs.extend(_norm_src(t) for t in toks[:-1])
    return srcs


def check_copy_boundaries(ins: list[tuple[str, str]], role: str) -> None:
    srcs = copy_sources(ins)
    allowed = COPY_SETS[role] | {f"requirements/{REQUIREMENTS[role]}"}
    for s in srcs:
        _require(s not in {".", "*", ""}, f"whole build context copied: {s!r}")
        for f in FORBIDDEN[role]:
            _require(not (s == f or s.startswith(f + "/")), f"{role} image must not copy {s!r}")
        _require(s in allowed, f"{role} image copies {s!r}, outside the allowed set")
    missing = COPY_SETS[role] - set(srcs)
    _require(not missing, f"{role} image does not copy {sorted(missing)}")


def check_user(text: str, ins: list[tuple[str, str]]) -> None:
    runs = "\n".join(a for n, a in ins if n == "RUN")
    _require(re.search(rf"groupadd\b[^\n&;]*-g\s*{GID}\b", runs), f"missing groupadd -g {GID}")
    _require(
        re.search(rf"useradd\b[^\n&;]*-u\s*{UID}\b", runs) and re.search(rf"useradd\b[^\n&;]*-g\s*{GID}\b", runs),
        f"missing useradd -u {UID} -g {GID}",
    )
    users = [a for n, a in ins if n == "USER"]
    _require(users, "no USER instruction")
    _require(users[-1] == OWNER, f"final USER is {users[-1]!r}, expected {OWNER!r}")


def check_evidence_dir(ins: list[tuple[str, str]], role: str) -> None:
    runs = "\n".join(a for n, a in ins if n == "RUN")
    if role != "collector":
        _require(EVIDENCE not in "\n".join(a for _, a in ins), "demo image must not reference the evidence dir")
        return
    _require(re.search(rf"chown\s+(?:-R\s+)?{OWNER}\s+{EVIDENCE}/?(?:\s|$|&|;)", runs), f"missing chown {OWNER} {EVIDENCE}")
    _require(re.search(rf"chmod\s+(?:-R\s+)?0?{MODE[1:]}\s+{EVIDENCE}/?(?:\s|$|&|;)", runs), f"missing chmod {MODE} {EVIDENCE}")
    for m in re.finditer(r"chmod\s+(?:-R\s+)?(\S+)\s+(\S+)", runs):
        if m.group(2).startswith(EVIDENCE):
            _require(m.group(1).lstrip("0") == MODE[1:], f"evidence dir mode {m.group(1)} is not {MODE}")


def check_cmd(ins: list[tuple[str, str]], role: str, main_sources: str) -> None:
    cmds = [a for n, a in ins if n == "CMD"]
    _require(cmds, "no CMD")
    _require(cmds[-1].startswith("["), "CMD must be exec-form JSON")
    cmd = json.loads(cmds[-1])
    _require(isinstance(cmd, list) and all(isinstance(c, str) for c in cmd), "CMD must be a JSON string list")
    joined = " ".join(cmd)
    for i, c in enumerate(cmd):
        if c == "--workers":
            _require(i + 1 < len(cmd) and cmd[i + 1] == "1", "uvicorn --workers must be 1")
        if c.startswith("--workers="):
            _require(c == "--workers=1", "uvicorn --workers must be 1")
    if "--workers" in cmd or any(c == "--workers=1" for c in cmd):
        _require(cmd[0] == "uvicorn" or "uvicorn" in cmd[:3], "--workers given but CMD is not uvicorn")
        return
    _require(
        cmd == ["python", "-m", MODULE[role]],
        f"CMD must pass --workers 1 to uvicorn or be python -m {MODULE[role]}, got {joined!r}",
    )
    found = re.findall(r"workers\s*=\s*(\d+)", main_sources)
    _require(found and all(w == "1" for w in found), f"{MODULE[role]} source must call the server with workers=1")


def check_pip(ins: list[tuple[str, str]], role: str) -> None:
    runs = "\n".join(a for n, a in ins if n == "RUN")
    segs = [s.strip() for s in re.split(r"&&|;|\n", runs)]
    req = REQUIREMENTS[role]
    idx = None
    for i, s in enumerate(segs):
        if "pip install" in s:
            _require("--no-cache-dir" in s, f"pip install without --no-cache-dir: {s!r}")
            m = re.search(r"-r\s+(\S+)", s)
            if m and (m.group(1) == f"requirements/{req}" or m.group(1).endswith("/" + req) or m.group(1) == req):
                idx = i
    _require(idx is not None, f"no pip install -r requirements/{req}")
    _require(any(re.search(r"\bpip check\b", s) for s in segs[idx + 1:]), "pip check must run after pip install")


def check_dockerfile(text: str, role: str, main_sources: str = "") -> None:
    ins = parse_dockerfile(text)
    check_from(ins)
    check_user(text, ins)
    check_evidence_dir(ins, role)
    check_copy_boundaries(ins, role)
    check_cmd(ins, role, main_sources)
    check_pip(ins, role)


# ------------------------------------------------------------------------------------------------ Compose

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):?-([^}]*)\}")


def expand(value: Any) -> str:
    """Apply every ${VAR:-default} default; any remaining '$' is an error (no required variables)."""
    s = _VAR.sub(lambda m: m.group(2), str(value))
    _require("$" not in s, f"unresolved variable in {value!r}")
    return s


def _svc(doc: dict, role: str) -> dict:
    services = doc.get("services")
    _require(isinstance(services, dict) and SERVICE[role] in services, f"service {SERVICE[role]} missing")
    return services[SERVICE[role]]


def check_meta(doc: dict) -> None:
    _require(doc.get("name") == "coroner", "compose project name must be coroner")
    for role in ("collector", "demo"):
        svc = _svc(doc, role)
        check_image_ref(str(svc.get("image", "")))
        _require(svc["image"] == IMAGES[role], f"{role} image must be {IMAGES[role]}")
        build = svc.get("build")
        _require(isinstance(build, dict), f"{role} build must be a mapping")
        _require(build.get("dockerfile") == DOCKERFILES[role], f"{role} dockerfile path")
        _require(str(build.get("context")) == ".", f"{role} build context")
        _require(str(svc.get("user")) == OWNER, f"{role} user must be {OWNER}")
        _require(svc.get("network_mode") != "host", f"{role} must not use host networking")
    _require("network_mode" not in doc, "network_mode at top level")


def check_ports(doc: dict) -> None:
    for role in ("collector", "demo"):
        var, host_default, container = PORTS[role]
        ports = _svc(doc, role).get("ports")
        _require(isinstance(ports, list) and len(ports) == 1, f"{role} must publish exactly one port")
        p = ports[0]
        if isinstance(p, dict):
            _require(p.get("host_ip") == "127.0.0.1", f"{role} port host_ip must be 127.0.0.1")
            host, target = expand(p.get("published")), str(p.get("target"))
            raw = str(p.get("published"))
        else:
            raw = str(p)
            _require(raw.startswith("127.0.0.1:"), f"{role} port {raw!r} must bind 127.0.0.1")
            m = re.fullmatch(r"127\.0\.0\.1:(.+):(\d+)", expand(raw))
            _require(m, f"{role} port {raw!r} must be 127.0.0.1:<host>:<container>")
            host, target = m.group(1), m.group(2)
        _require("0.0.0.0" not in raw, f"{role} port is public")
        _require(host == str(host_default), f"{role} host port default {host!r} != {host_default}")
        _require(target == str(container), f"{role} container port {target!r} != {container}")
        if isinstance(p, str):
            _require(var in raw, f"{role} port should use ${{{var}:-{host_default}}}")


def check_restart(doc: dict) -> None:
    c = _svc(doc, "collector").get("restart")
    _require(isinstance(c, str) and c == "on-failure:5", f"collector restart is {c!r}, expected 'on-failure:5'")
    d = _svc(doc, "demo").get("restart")
    _require(isinstance(d, str) and d == "no", f"demo restart is {d!r}, expected the string 'no'")


def check_hardening(doc: dict) -> None:
    for role in ("collector", "demo"):
        svc = _svc(doc, role)
        _require("ALL" in (svc.get("cap_drop") or []), f"{role} cap_drop must contain ALL")
        _require("no-new-privileges:true" in (svc.get("security_opt") or []), f"{role} needs no-new-privileges:true")
        _require(svc.get("privileged", False) is False, f"{role} must not be privileged")
    demo = _svc(doc, "demo")
    _require(demo.get("privileged") is False, "demo privileged must be explicitly false")
    _require(demo.get("read_only") is True, "demo read_only must be true")
    tmpfs = demo.get("tmpfs")
    tmpfs = [tmpfs] if isinstance(tmpfs, str) else (tmpfs or [])
    _require(any(str(t).split(":")[0] == "/tmp" for t in tmpfs), "demo tmpfs must include /tmp")


def check_healthcheck(doc: dict) -> None:
    hc = _svc(doc, "collector").get("healthcheck")
    _require(isinstance(hc, dict), "collector healthcheck missing")
    test = hc.get("test")
    _require(isinstance(test, list) and test[:3] == ["CMD", "python", "-c"] and len(test) == 4,
             "collector healthcheck must be [CMD, python, -c, code]")
    code = test[3]
    _require(not re.search(r"\b(curl|wget)\b", " ".join(test)), "healthcheck must not use curl or wget")
    _require("/healthz" in code, "healthcheck must request /healthz")
    _require("200" in code, "healthcheck must check HTTP status 200")
    _require("storage_ok" in code, "healthcheck must check storage_ok")
    _require(re.search(r"storage_ok[^;]*(is True|== True)", code), "healthcheck must compare storage_ok strictly to True")


def check_depends(doc: dict) -> None:
    dep = _svc(doc, "demo").get("depends_on")
    _require(isinstance(dep, dict) and "collector" in dep, "demo depends_on must be a mapping with collector")
    cond = dep["collector"]
    _require(isinstance(cond, dict) and cond.get("condition") == "service_healthy",
             "demo depends_on collector needs condition service_healthy")


def check_memory_and_flag(doc: dict) -> None:
    demo = _svc(doc, "demo")
    mem = demo.get("mem_limit")
    _require(isinstance(mem, str) and re.fullmatch(r"\$\{CORONER_DEMO_MEM_LIMIT:-128m\}", mem),
             f"demo mem_limit must be ${{CORONER_DEMO_MEM_LIMIT:-128m}}, got {mem!r}")
    _require(demo.get("memswap_limit") == mem, "memswap_limit must equal mem_limit")
    env = demo.get("environment")
    if isinstance(env, list):
        env = dict(e.split("=", 1) for e in env)
    _require(isinstance(env, dict), "demo environment missing")
    flag = env.get("CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS")
    _require(flag is not None and expand(flag) == "0", f"destructive flag default must be 0, got {flag!r}")
    _require(expand(env.get("CORONER_COLLECTOR_URL")) == COLLECTOR_URL, f"collector URL must be {COLLECTOR_URL}")


def _bind_like(source: str) -> bool:
    return source.startswith(("/", ".", "~", "\\")) or bool(re.match(r"^[A-Za-z]:", source)) or "/" in source


def check_volumes(doc: dict) -> None:
    top = doc.get("volumes")
    _require(isinstance(top, dict) and VOLUME in top, f"top-level volume {VOLUME} not declared")
    for name, spec in top.items():
        opts = (spec or {}).get("driver_opts") or {}
        _require("bind" not in str(opts.get("o", "")) and not opts.get("device"), f"volume {name} is a host bind")
    for role, svc_name in SERVICE.items():
        svc = _svc(doc, role)
        vols = svc.get("volumes") or []
        for v in vols:
            if isinstance(v, dict):
                _require(v.get("type") == "volume", f"{svc_name} has a non-volume mount {v!r}")
                src, tgt = str(v.get("source")), str(v.get("target"))
            else:
                parts = str(v).split(":")
                _require(len(parts) >= 2, f"{svc_name} anonymous volume {v!r}")
                src, tgt = parts[0], parts[1]
                _require(not re.match(r"^[A-Za-z]$", src), f"{svc_name} drive-letter bind {v!r}")
            _require(not _bind_like(src), f"{svc_name} host bind mount {v!r}")
            _require(src in top, f"{svc_name} mounts undeclared volume {src!r}")
        if role == "demo":
            _require(not vols, "demo_app must have no volumes")
        else:
            _require(len(vols) == 1, "collector must mount exactly the evidence volume")
            v = vols[0]
            src, tgt = (v["source"], v["target"]) if isinstance(v, dict) else str(v).split(":")[:2]
            _require(src == VOLUME and tgt == EVIDENCE, f"collector must mount {VOLUME}:{EVIDENCE}")


def check_compose(doc: dict) -> None:
    _require(isinstance(doc, dict), "compose document must be a mapping")
    for fn in (check_meta, check_ports, check_restart, check_hardening, check_healthcheck, check_depends,
               check_memory_and_flag, check_volumes):
        fn(doc)


# ------------------------------------------------------------------------------------------------ fixtures

GOOD_COMPOSE_YAML = """\
name: coroner
services:
  collector:
    build: { context: ., dockerfile: docker/collector.Dockerfile }
    image: coroner-collector:m0
    environment:
      CORONER_DURABILITY: ${CORONER_DURABILITY:-crash}
    ports: ["127.0.0.1:${CORONER_COLLECTOR_PORT:-8081}:8081"]
    volumes: ["coroner_evidence:/data/evidence"]
    restart: on-failure:5
    user: "10001:10001"
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    healthcheck:
      test: ["CMD", "python", "-c", "import json,sys,urllib.request as u; r=u.urlopen('http://127.0.0.1:8081/healthz',timeout=2); b=json.load(r); sys.exit(0 if r.status==200 and b.get('storage_ok') is True else 1)"]
      interval: 2s
  demo_app:
    build: { context: ., dockerfile: docker/demo_app.Dockerfile }
    image: coroner-demo-app:m0
    depends_on:
      collector: { condition: service_healthy }
    environment:
      CORONER_RUN_ID: ${CORONER_RUN_ID:-}
      CORONER_COLLECTOR_URL: http://collector:8081
      CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS: ${CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS:-0}
    ports: ["127.0.0.1:${CORONER_DEMO_PORT:-8080}:8080"]
    mem_limit: ${CORONER_DEMO_MEM_LIMIT:-128m}
    memswap_limit: ${CORONER_DEMO_MEM_LIMIT:-128m}
    restart: "no"
    privileged: false
    user: "10001:10001"
    read_only: true
    tmpfs: ["/tmp:size=8m"]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
volumes:
  coroner_evidence: {}
"""

GOOD_COLLECTOR_DOCKERFILE = """\
FROM python:3.12.8-slim-bookworm
RUN groupadd -g 10001 coroner \\
    && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin coroner
WORKDIR /app
COPY requirements/collector.txt requirements/collector.txt
RUN python -m pip install --no-cache-dir -r requirements/collector.txt && python -m pip check
COPY coroner/__init__.py coroner/__init__.py
COPY coroner/common coroner/common
COPY coroner/evidence coroner/evidence
COPY coroner/collector coroner/collector
RUN mkdir -p /data/evidence && chown 10001:10001 /data/evidence && chmod 0750 /data/evidence
USER 10001:10001
CMD ["uvicorn", "coroner.collector.app:app", "--host", "0.0.0.0", "--port", "8081", "--workers", "1"]
"""

GOOD_DEMO_DOCKERFILE = """\
FROM python:3.12.8-slim-bookworm
RUN groupadd -g 10001 coroner && useradd -u 10001 -g 10001 -M coroner
WORKDIR /app
COPY requirements/demo_app.txt requirements/demo_app.txt
RUN pip install --no-cache-dir -r requirements/demo_app.txt && pip check
COPY coroner/__init__.py coroner/__init__.py
COPY coroner/probe coroner/probe
COPY coroner/demo_app coroner/demo_app
USER 10001:10001
CMD ["python", "-m", "coroner.demo_app.main"]
"""

GOOD_MAIN = "run(app, host=HOST, port=PORT, workers=1)"


def good_compose() -> dict:
    return yaml.safe_load(GOOD_COMPOSE_YAML)


def _mut(fn: Callable[[dict], None]) -> dict:
    doc = copy.deepcopy(good_compose())
    fn(doc)
    return doc


def _col(doc: dict) -> dict:
    return doc["services"]["collector"]


def _demo(doc: dict) -> dict:
    return doc["services"]["demo_app"]


# ------------------------------------------------------------------------------------------ fixture tests

def test_fixture_good_compose_passes() -> None:
    check_compose(good_compose())


def test_fixture_good_dockerfiles_pass() -> None:
    check_dockerfile(GOOD_COLLECTOR_DOCKERFILE, "collector", GOOD_MAIN)
    check_dockerfile(GOOD_DEMO_DOCKERFILE, "demo", GOOD_MAIN)


def test_fixture_unquoted_no_parses_as_boolean() -> None:
    doc = yaml.safe_load(GOOD_COMPOSE_YAML.replace('restart: "no"', "restart: no"))
    assert _demo(doc)["restart"] is False


COMPOSE_NEGATIVES: list[tuple[str, Callable[[dict], None], Callable[[dict], None]]] = [
    ("image-latest", lambda d: _col(d).__setitem__("image", "coroner-collector:latest"), check_meta),
    ("image-untagged", lambda d: _demo(d).__setitem__("image", "coroner-demo-app"), check_meta),
    ("user-root", lambda d: _demo(d).__setitem__("user", "0:0"), check_meta),
    ("port-public-0000", lambda d: _col(d).__setitem__("ports", ["0.0.0.0:8081:8081"]), check_ports),
    ("port-bare", lambda d: _col(d).__setitem__("ports", ["8081:8081"]), check_ports),
    ("port-demo-bare", lambda d: _demo(d).__setitem__("ports", ["8080"]), check_ports),
    ("restart-always", lambda d: _col(d).__setitem__("restart", "always"), check_restart),
    ("restart-collector-missing", lambda d: _col(d).pop("restart"), check_restart),
    ("restart-demo-false", lambda d: _demo(d).__setitem__("restart", False), check_restart),
    ("cap-drop-missing", lambda d: _col(d).pop("cap_drop"), check_hardening),
    ("demo-cap-drop-missing", lambda d: _demo(d).pop("cap_drop"), check_hardening),
    ("no-new-privileges-missing", lambda d: _col(d).pop("security_opt"), check_hardening),
    ("privileged-true", lambda d: _demo(d).__setitem__("privileged", True), check_hardening),
    ("privileged-absent", lambda d: _demo(d).pop("privileged"), check_hardening),
    ("read-only-false", lambda d: _demo(d).__setitem__("read_only", False), check_hardening),
    ("tmpfs-missing-tmp", lambda d: _demo(d).__setitem__("tmpfs", ["/run:size=8m"]), check_hardening),
    ("network-host", lambda d: _demo(d).__setitem__("network_mode", "host"), check_meta),
    ("healthcheck-curl",
     lambda d: _col(d)["healthcheck"].__setitem__("test", ["CMD", "curl", "-f", "http://127.0.0.1:8081/healthz"]),
     check_healthcheck),
    ("healthcheck-status-only",
     lambda d: _col(d)["healthcheck"].__setitem__(
         "test", ["CMD", "python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8081/healthz').status==200"]),
     check_healthcheck),
    ("healthcheck-loose-truthy",
     lambda d: _col(d)["healthcheck"].__setitem__(
         "test", ["CMD", "python", "-c", "import json,urllib.request as u; r=u.urlopen('http://x/healthz'); r.status==200 and json.load(r).get('storage_ok')"]),
     check_healthcheck),
    ("depends-no-condition", lambda d: _demo(d).__setitem__("depends_on", ["collector"]), check_depends),
    ("depends-started", lambda d: _demo(d).__setitem__("depends_on", {"collector": {"condition": "service_started"}}),
     check_depends),
    ("mem-256m", lambda d: _demo(d).__setitem__("mem_limit", "${CORONER_DEMO_MEM_LIMIT:-256m}"),
     check_memory_and_flag),
    ("memswap-different", lambda d: _demo(d).__setitem__("memswap_limit", "${CORONER_DEMO_MEM_LIMIT:-256m}"),
     check_memory_and_flag),
    ("flag-default-1",
     lambda d: _demo(d)["environment"].__setitem__(
         "CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS", "${CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS:-1}"),
     check_memory_and_flag),
    ("collector-url-wrong",
     lambda d: _demo(d)["environment"].__setitem__("CORONER_COLLECTOR_URL", "http://localhost:8081"),
     check_memory_and_flag),
    ("bind-mount", lambda d: _col(d).__setitem__("volumes", ["./data:/data/evidence"]), check_volumes),
    ("bind-mount-windows", lambda d: _col(d).__setitem__("volumes", ["C:/data:/data/evidence"]), check_volumes),
    ("bind-mount-long",
     lambda d: _col(d).__setitem__("volumes", [{"type": "bind", "source": "/srv/e", "target": "/data/evidence"}]),
     check_volumes),
    ("evidence-volume-in-demo", lambda d: _demo(d).__setitem__("volumes", ["coroner_evidence:/data/evidence"]),
     check_volumes),
    ("volume-undeclared", lambda d: d.__setitem__("volumes", {}), check_volumes),
]


@pytest.mark.parametrize("name,mutate,check", COMPOSE_NEGATIVES, ids=[n for n, _, _ in COMPOSE_NEGATIVES])
def test_negative_compose(name: str, mutate: Callable[[dict], None], check: Callable[[dict], None]) -> None:
    doc = _mut(mutate)
    with pytest.raises(AssertionError):
        check(doc)
    with pytest.raises(AssertionError):
        check_compose(doc)


def test_negative_compose_unquoted_no_boolean() -> None:
    doc = yaml.safe_load(GOOD_COMPOSE_YAML.replace('restart: "no"', "restart: no"))
    with pytest.raises(AssertionError):
        check_restart(doc)


def _replace(text: str, old: str, new: str) -> str:
    assert old in text
    return text.replace(old, new)


DOCKERFILE_NEGATIVES: list[tuple[str, str, Callable[[str], str]]] = [
    ("from-latest", "collector", lambda t: _replace(t, "python:3.12.8-slim-bookworm", "python:latest")),
    ("from-untagged", "demo", lambda t: _replace(t, "python:3.12.8-slim-bookworm", "python")),
    ("from-other-tag", "collector", lambda t: _replace(t, "3.12.8-slim-bookworm", "3.12.7-slim-bookworm")),
    ("user-root", "collector", lambda t: _replace(t, "USER 10001:10001", "USER root")),
    ("user-missing", "demo", lambda t: _replace(t, "USER 10001:10001\n", "")),
    ("user-wrong-uid", "collector", lambda t: t.replace("10001", "1000")),
    ("evidence-mode-0777", "collector", lambda t: _replace(t, "chmod 0750", "chmod 0777")),
    ("evidence-chown-missing", "collector", lambda t: _replace(t, "chown 10001:10001 /data/evidence", "true")),
    ("copy-whole-context", "collector", lambda t: t + "COPY . /app\n"),
    ("copy-whole-context-demo", "demo", lambda t: t + "COPY ./ /app\n"),
    ("collector-copies-demo-app", "collector", lambda t: t + "COPY coroner/demo_app coroner/demo_app\n"),
    ("collector-copies-tests", "collector", lambda t: t + "COPY tests tests\n"),
    ("demo-copies-collector", "demo", lambda t: t + "COPY coroner/collector coroner/collector\n"),
    ("add-url", "demo", lambda t: t + "ADD https://example.com/x.tar.gz /x\n"),
    ("workers-2", "collector", lambda t: _replace(t, '"--workers", "1"', '"--workers", "2"')),
    ("cmd-shell-form", "collector", lambda t: _replace(
        t, t[t.index("CMD "):].strip(), "CMD uvicorn coroner.collector.app:app")),
    ("pip-no-cache-flag-missing", "demo", lambda t: _replace(t, "--no-cache-dir ", "")),
    ("pip-check-missing", "demo", lambda t: _replace(t, " && pip check", "")),
    ("pip-wrong-requirements", "collector", lambda t: _replace(
        t, "-r requirements/collector.txt", "-r requirements/demo_app.txt")),
]


@pytest.mark.parametrize("name,role,mutate", DOCKERFILE_NEGATIVES, ids=[n for n, _, _ in DOCKERFILE_NEGATIVES])
def test_negative_dockerfile(name: str, role: str, mutate: Callable[[str], str]) -> None:
    good = GOOD_COLLECTOR_DOCKERFILE if role == "collector" else GOOD_DEMO_DOCKERFILE
    with pytest.raises(AssertionError):
        check_dockerfile(mutate(good), role, GOOD_MAIN)


def test_negative_module_cmd_requires_single_worker_source() -> None:
    with pytest.raises(AssertionError):
        check_dockerfile(GOOD_DEMO_DOCKERFILE, "demo", "run(app, workers=2)")
    with pytest.raises(AssertionError):
        check_dockerfile(GOOD_DEMO_DOCKERFILE, "demo", "run(app)")


def test_negative_evidence_dir_in_demo_image() -> None:
    with pytest.raises(AssertionError):
        check_dockerfile(GOOD_DEMO_DOCKERFILE + "RUN mkdir -p /data/evidence\n", "demo", GOOD_MAIN)


# ------------------------------------------------------------------------------------- real-file tests

def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _main_text(role: str) -> str:
    return "\n".join(_read(p) for p in MAIN_SOURCES[role])


def test_real_compose_passes_all_checks() -> None:
    check_compose(yaml.safe_load(_read("compose.yaml")))


def test_real_compose_build_files_exist() -> None:
    doc = yaml.safe_load(_read("compose.yaml"))
    for role in ("collector", "demo"):
        assert (ROOT / _svc(doc, role)["build"]["dockerfile"]).is_file()


def test_real_collector_dockerfile_passes_all_checks() -> None:
    check_dockerfile(_read(DOCKERFILES["collector"]), "collector", _main_text("collector"))


def test_real_demo_dockerfile_passes_all_checks() -> None:
    check_dockerfile(_read(DOCKERFILES["demo"]), "demo", _main_text("demo"))


def test_real_no_latest_or_untagged_images_anywhere() -> None:
    doc = yaml.safe_load(_read("compose.yaml"))
    for role in ("collector", "demo"):
        check_image_ref(_svc(doc, role)["image"])
    for role, rel in DOCKERFILES.items():
        for name, args in parse_dockerfile(_read(rel)):
            if name == "FROM":
                check_image_ref(args.split()[0])
