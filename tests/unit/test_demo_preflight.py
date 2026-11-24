"""Tests for scripts/demo-preflight.ps1 in -Offline mode on synthetic evidence.

Every run directory and baseline lives in pytest's temporary directory. The real
`runs/` is never read, no server is started and Docker is never called.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.viewer.builder import RunsBuilder

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "demo-preflight.ps1"

POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")

needs_powershell = pytest.mark.skipif(
    POWERSHELL is None,
    reason="neither 'pwsh' nor 'powershell' is on PATH; the preflight script cannot be run",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _make_runs(root: Path) -> list[str]:
    builder = RunsBuilder(root)
    return [
        builder.add_run("A", run_id="a-20261120T000001Z-00000001"),
        builder.add_run("B", run_id="b-20261120T000002Z-00000002"),
        builder.add_run("C", run_id="c-20261120T000003Z-00000003"),
    ]


def _write_baseline(path: Path, runs_dir: Path, run_ids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for run_id in run_ids:
        for f in sorted((runs_dir / run_id).rglob("*")):
            if f.is_file():
                rel = f.relative_to(runs_dir).as_posix()
                lines.append(f"{rel} {_sha256(f)} {f.stat().st_size}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _tree_hash(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _run_script(runs_dir: Path, run_ids: list[str], baseline: Path | None):
    assert POWERSHELL is not None
    cmd = [
        POWERSHELL,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SCRIPT),
        "-Offline",
        "-Json",
        "-RunsDir",
        str(runs_dir),
        "-RunIds",
        ",".join(run_ids),
    ]
    if baseline is not None:
        cmd += ["-BaselinePath", str(baseline)]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=180, cwd=str(REPO_ROOT)
    )
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"no JSON on stdout (exit {proc.returncode}): {proc.stdout!r} {proc.stderr!r}")
    return proc.returncode, report


def _check(report: dict, name: str) -> dict:
    matches = [c for c in report["checks"] if c["name"] == name]
    assert matches, f"no check named {name!r} in {[c['name'] for c in report['checks']]}"
    return matches[0]


@pytest.fixture()
def synthetic(tmp_path: Path):
    runs_dir = tmp_path / "runs"
    run_ids = _make_runs(runs_dir)
    baseline = tmp_path / "baseline-dir" / "baseline.txt"
    _write_baseline(baseline, runs_dir, run_ids)
    return runs_dir, run_ids, baseline


@needs_powershell
def test_matching_baseline_passes_and_leaves_runs_unchanged(synthetic):
    runs_dir, run_ids, baseline = synthetic
    before = _tree_hash(runs_dir)
    code, report = _run_script(runs_dir, run_ids, baseline)
    assert code == 0, report
    assert report["ok"] is True
    assert _check(report, "integrity")["status"] == "PASS"
    for run_id in run_ids:
        assert _check(report, f"run-files {run_id}")["status"] == "PASS"
    assert _tree_hash(runs_dir) == before


@needs_powershell
def test_tampered_file_fails_integrity(synthetic):
    runs_dir, run_ids, baseline = synthetic
    with (runs_dir / run_ids[1] / "evidence.jsonl").open("ab") as fh:
        fh.write(b"\n")
    code, report = _run_script(runs_dir, run_ids, baseline)
    assert code != 0
    assert report["ok"] is False
    check = _check(report, "integrity")
    assert check["status"] == "FAIL"
    assert run_ids[1] + "/evidence.jsonl" in check["message"]


@needs_powershell
def test_missing_baseline_file_fails(synthetic, tmp_path):
    runs_dir, run_ids, _ = synthetic
    code, report = _run_script(runs_dir, run_ids, tmp_path / "elsewhere" / "absent.txt")
    assert code != 0
    check = _check(report, "integrity")
    assert check["status"] == "FAIL"
    assert "BASELINE MISSING" in check["message"]


@needs_powershell
def test_baseline_parameter_omitted_fails(synthetic):
    runs_dir, run_ids, _ = synthetic
    code, report = _run_script(runs_dir, run_ids, None)
    assert code != 0
    assert "BASELINE MISSING" in _check(report, "integrity")["message"]


@needs_powershell
def test_baseline_inside_runs_dir_is_rejected(synthetic):
    runs_dir, run_ids, baseline = synthetic
    inside = runs_dir / "baseline.txt"
    shutil.copyfile(baseline, inside)
    code, report = _run_script(runs_dir, run_ids, inside)
    assert code != 0
    check = _check(report, "integrity")
    assert check["status"] == "FAIL"
    assert "outside" in check["message"]


@needs_powershell
def test_baseline_inside_repository_is_rejected(synthetic):
    runs_dir, run_ids, _ = synthetic
    # The path is only resolved, never created or read.
    inside_repo = REPO_ROOT / "never-created-demo-baseline.txt"
    assert not inside_repo.exists()
    code, report = _run_script(runs_dir, run_ids, inside_repo)
    assert code != 0
    check = _check(report, "integrity")
    assert check["status"] == "FAIL"
    assert "outside" in check["message"]
    assert not inside_repo.exists()


@needs_powershell
def test_missing_canonical_run_directory_fails(synthetic):
    runs_dir, run_ids, baseline = synthetic
    shutil.rmtree(runs_dir / run_ids[2])
    code, report = _run_script(runs_dir, run_ids, baseline)
    assert code != 0
    assert report["ok"] is False
    check = _check(report, f"run-files {run_ids[2]}")
    assert check["status"] == "FAIL"
    assert "not found" in check["message"]


# -- static checks on the script text (need no PowerShell) ----------------------------


def _code_lines() -> list[str]:
    text = SCRIPT.read_text(encoding="utf-8")
    text = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)  # block comments
    return [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def test_script_exists_without_bom_and_with_lf():
    data = SCRIPT.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in data


def test_script_has_no_docker_invocation():
    command_position = re.compile(
        r"""(^|[;&|(]\s*|&\s*['"]?|-FilePath\s+['"]?|Start-Process\s+['"]?)docker(\.exe)?['"]?(\s|$)""",
        re.IGNORECASE,
    )
    for line in _code_lines():
        assert not command_position.search(line.strip()), f"docker invoked: {line}"
    joined = "\n".join(_code_lines())
    assert not re.search(r"\bdocker(\.exe)?\s+(compose|run|ps|build|inspect|stop|kill|rm)\b", joined, re.I)


def test_script_stops_only_a_process_it_started():
    lines = _code_lines()
    joined = "\n".join(lines)
    for forbidden in ("taskkill", "Stop-Service", ".Kill(", "pkill", "killall"):
        assert forbidden.lower() not in joined.lower(), forbidden
    # No by-name or piped termination.
    assert not re.search(r"Stop-Process\s+(-Name|-InputObject)", joined, re.I)
    assert not re.search(r"\|\s*Stop-Process", joined, re.I)

    # Every Stop-Process lives in Stop-OwnViewer and targets the launched PID or its descendants.
    match = re.search(r"function Stop-OwnViewer \{\n(.*?)\n\}\n", joined + "\n", re.DOTALL)
    assert match, "Stop-OwnViewer not found"
    body = match.group(1)
    assert "$null -eq $script:LaunchedPid" in body  # returns early if nothing was started
    stop_lines_in_body = [ln for ln in body.splitlines() if "Stop-Process" in ln]
    assert stop_lines_in_body
    for ln in stop_lines_in_body:
        assert re.search(r"Stop-Process -Id (\$id|\$script:LaunchedPid)\b", ln), ln
    assert joined.count("Stop-Process") == len(stop_lines_in_body)
    assert "Get-Descendants -RootPid $script:LaunchedPid" in body

    # LaunchedPid is only set from the process the script started itself.
    assigns = [ln.strip() for ln in lines if "$script:LaunchedPid =" in ln]
    assert set(assigns) <= {
        "$script:LaunchedPid = $null",
        "$script:LaunchedPid = $proc.Id",
    }
    assert any("Start-Process" in ln and "-PassThru" in ln for ln in lines)
