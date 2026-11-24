"""Tests for scripts/make-sample-runs.py, the generator of SYNTHETIC judge sample runs.

Everything is written under pytest's temporary directory. The real `runs/` is only read: its three
canonical runs are hashed before and after to prove nothing there changes, and their container ids are read
to prove none of them is copied into the sample. No Docker, no network. The browser test uses the existing
headless-Chrome harness and is skipped, with a reason, when no Chrome, Chromium or Edge is installed.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from starlette.testclient import TestClient

from coroner.evidence.reader import read_evidence, summarize
from coroner.evidence.schema import RUN_ID_RE
from coroner.runner.manifest import deserialize_manifest, extract_inspect_excerpt
from coroner.viewer.app import create_app
from tests.unit.viewer import acceptance_47 as acc
from tests.unit.viewer import cdp_harness as cdp
from tests.unit.viewer.browser_harness import find_browser

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "make-sample-runs.py"
REAL_RUNS = REPO_ROOT / "runs"
PORT = 8765
LIMIT = 134_217_728

BROWSER = find_browser()


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_sample_runs", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_sample_runs"] = module
    spec.loader.exec_module(module)
    return module


msr = _load()


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _real_files() -> list[Path]:
    found: list[Path] = []
    for run_id in acc.REAL.values():
        run_dir = REAL_RUNS / run_id
        if run_dir.is_dir():
            found.extend(sorted(p for p in run_dir.rglob("*") if p.is_file()))
    return found


def _real_hashes() -> dict[str, str]:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in _real_files()}


@pytest.fixture(scope="module")
def sample(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("samplegen") / "sample-runs"
    assert msr.main(["--out", str(out)]) == 0
    return out


@pytest.fixture(scope="module")
def ids() -> dict[str, str]:
    return msr.run_ids()


def _client(root: Path) -> TestClient:
    return TestClient(create_app(root, PORT), base_url=f"http://127.0.0.1:{PORT}")


def _get(client: TestClient, url: str) -> Any:
    response = client.get(url, headers={"Host": f"127.0.0.1:{PORT}"})
    assert response.status_code == 200, response.text
    return response


# ---------------------------------------------------------------- determinism and format


def test_two_generations_are_byte_identical(sample: Path, tmp_path: Path) -> None:
    second = tmp_path / "again"
    assert msr.main(["--out", str(second)]) == 0
    assert _tree(sample) == _tree(second)
    assert len(_tree(sample)) == 10  # README plus three files in each of three runs


def test_files_are_lf_utf8_without_bom(sample: Path) -> None:
    for name, data in _tree(sample).items():
        assert b"\r" not in data, name
        assert not data.startswith(b"\xef\xbb\xbf"), name
        assert data.endswith(b"\n"), name
        data.decode("utf-8")


def test_run_ids_are_valid_and_fixed(ids: dict[str, str]) -> None:
    assert set(ids) == {"A", "B", "C"}
    for key, run_id in ids.items():
        assert RUN_ID_RE.fullmatch(run_id) and run_id.startswith(key.lower() + "-")
    assert ids == {
        "A": "a-20260101T000000Z-sample01",
        "B": "b-20260101T000100Z-sample02",
        "C": "c-20260101T000200Z-sample03",
    }


# ---------------------------------------------------------------- schema validity and consistency


def test_manifest_evidence_and_inspect_pass_the_m0_validators(sample: Path, ids: dict[str, str]) -> None:
    for key, run_id in ids.items():
        run_dir = sample / run_id
        manifest = deserialize_manifest((run_dir / "manifest.json").read_bytes())
        assert manifest.run_id == run_id and manifest.experiment == key
        result = read_evidence((run_dir / "evidence.jsonl").read_bytes(), name=f"{run_id}.jsonl")
        assert result.errors == [], result.errors  # zero invalid or rejected lines
        summary = summarize(result)
        assert summary.malformed_line_count == 0 and summary.missing_sample_count == 0
        assert summary.warnings == []
        excerpt = extract_inspect_excerpt((run_dir / "docker_inspect.json").read_bytes())
        assert manifest.container_state is not None
        assert manifest.container_state.raw_inspect_excerpt == excerpt


def test_manifest_evidence_and_inspect_agree_with_each_other(sample: Path, ids: dict[str, str]) -> None:
    for key, run_id in ids.items():
        run_dir = sample / run_id
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        inspect = json.loads((run_dir / "docker_inspect.json").read_text(encoding="utf-8"))[0]
        lines = [json.loads(x) for x in (run_dir / "evidence.jsonl").read_text(encoding="utf-8").splitlines()]
        samples = [r["sample"] for r in lines if r["record_type"] == "sample"]
        termination = [r for r in lines if r["record_type"] == "termination"]
        assert {r["run_id"] for r in lines} == {run_id} == {s["run_id"] for s in samples}
        assert manifest["evidence"]["record_count"] == len(lines)
        assert manifest["evidence_summary"]["sample_count"] == len(samples)
        assert [s["seq"] for s in samples] == list(range(len(samples)))
        peak = max(s["rss_bytes"] for s in samples)
        assert manifest["highest_rss_bytes"] == peak
        state = manifest["container_state"]
        assert (state["exit_code"], state["oom_killed"]) == (inspect["State"]["ExitCode"], inspect["State"]["OOMKilled"])
        assert (manifest["expected"]["exit_code"], manifest["expected"]["oom_killed"]) == (
            state["exit_code"], state["oom_killed"])
        assert state["container_id"] == inspect["Id"] == manifest["container_id"]
        assert {s["container_id"] for s in samples} == {inspect["Id"]}
        assert state["host_config_memory_bytes"] == inspect["HostConfig"]["Memory"] == LIMIT
        assert len(termination) == 1
        assert termination[0]["container_state"]["exit_code"] == state["exit_code"]
        assert termination[0]["container_state"]["oom_killed"] == state["oom_killed"]
        assert termination[0]["cause_label"] == manifest["termination"]["cause_label"]
        assert inspect["State"]["FinishedAt"][:23] == manifest["container_finished_at"][:23]


def test_synthetic_origin_is_marked(sample: Path, ids: dict[str, str]) -> None:
    readme = (sample / "README-SYNTHETIC.txt").read_text(encoding="utf-8")
    assert "SYNTHETIC" in readme and "NOT REAL EVIDENCE" in readme
    for run_id in ids.values():
        manifest = json.loads((sample / run_id / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["synthetic"]["is_synthetic"] is True
        assert manifest["termination_description"].startswith("SYNTHETIC")


# ---------------------------------------------------------------- the Viewer on the generated directory


def test_viewer_lists_three_valid_runs_with_the_right_values(sample: Path, ids: dict[str, str]) -> None:
    client = _client(sample)
    listing = _get(client, "/api/runs").json()
    assert (listing["total_found"], listing["listed"], listing["truncated"]) == (3, 3, False)
    by_id = {r["run_id"]: r for r in listing["runs"]}
    assert set(by_id) == set(ids.values())
    for row in by_id.values():
        assert row["verdict"] == "PASS" and row["reason"] is None
        assert row["docker_state_discrepancy"] is False and row["collector_survival"] is True
    got = {k: (by_id[ids[k]]["exit_code"], by_id[ids[k]]["oom_killed"]) for k in ids}
    assert got == {"A": (1, False), "B": (137, False), "C": (137, True)}
    for run_id in ids.values():
        detail = _get(client, f"/api/runs/{run_id}").json()
        assert detail["warnings"] == []
        assert all(v["status"] == "ok" for v in detail["artifact_status"].values())


def test_b_has_a_sigkill_record_and_a_usable_clock_offset(sample: Path, ids: dict[str, str]) -> None:
    client = _client(sample)
    timing = _get(client, f"/api/runs/{ids['B']}").json()["timing"]
    assert timing["time_axis"] == "corrected" and timing["correction_available"] is True
    assert timing["clock_offset_ms"] == 9 and timing["clock_uncertainty_ms"] == 12.5
    assert timing["sigkill_sent_time"] is not None
    kinds = {i["kind"]: i for i in timing["derived"]["intervals"]}
    assert kinds["sample_to_sigkill"]["ordering"] == "before_sigkill"
    lines = [json.loads(x) for x in (sample / ids["B"] / "evidence.jsonl").read_text(encoding="utf-8").splitlines()]
    assert lines[-1]["record_type"] == "termination" and lines[-1]["cause_label"] == "sigkill_sent_by_runner"
    for key in ("A", "C"):
        assert _get(client, f"/api/runs/{ids[key]}").json()["timing"]["derived"]["intervals"] == []


def test_c_rss_rises_above_the_128_mib_limit(sample: Path, ids: dict[str, str]) -> None:
    lines = [json.loads(x) for x in (sample / ids["C"] / "evidence.jsonl").read_text(encoding="utf-8").splitlines()]
    rss = [r["sample"]["rss_bytes"] for r in lines if r["record_type"] == "sample"]
    assert rss == sorted(rss) and rss[0] < LIMIT < max(rss)
    manifest = json.loads((sample / ids["C"] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["memory"]["configured_limit_bytes"] == LIMIT and manifest["highest_rss_bytes"] == max(rss)
    for key in ("A", "B"):  # the other two stay below the limit
        other = [json.loads(x) for x in (sample / ids[key] / "evidence.jsonl").read_text(encoding="utf-8").splitlines()]
        assert max(r["sample"]["rss_bytes"] for r in other if r["record_type"] == "sample") < LIMIT


# ---------------------------------------------------------------- safe output handling


def test_refuses_the_real_runs_directory_and_paths_inside_it() -> None:
    before = _real_hashes()
    names_before = sorted(os.listdir(REAL_RUNS)) if REAL_RUNS.is_dir() else []
    assert msr.main(["--out", str(REAL_RUNS)]) == 2
    assert msr.main(["--out", str(REAL_RUNS / "zz-never-created")]) == 2
    assert msr.main(["--out", str(REAL_RUNS / "zz-never-created" / "deeper")]) == 2
    assert not (REAL_RUNS / "zz-never-created").exists()
    assert (sorted(os.listdir(REAL_RUNS)) if REAL_RUNS.is_dir() else []) == names_before
    assert _real_hashes() == before


def test_refuses_runs_directory_by_relative_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_repo = tmp_path / "repo"
    (fake_repo / "runs").mkdir(parents=True)
    monkeypatch.chdir(fake_repo)
    assert msr.main(["--out", "runs"]) == 2
    assert msr.main(["--out", os.path.join("runs", "new")]) == 2
    assert msr.main(["--out", os.path.join(".", "runs", "..", "runs", "new")]) == 2
    assert os.listdir(fake_repo / "runs") == []


def test_refuses_a_fake_runs_directory_through_the_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_runs = tmp_path / "runs"
    fake_runs.mkdir()
    monkeypatch.setattr(msr, "_protected_dirs", lambda: [fake_runs.resolve()])
    for target in (fake_runs, fake_runs / "new", fake_runs / "a" / "b"):
        assert msr.main(["--out", str(target)]) == 2
    assert os.listdir(fake_runs) == []
    other = tmp_path / "other"
    assert msr.main(["--out", str(other)]) == 0


def _make_link(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":  # a junction needs no special privilege
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False)
        return proc.returncode == 0 and link.exists()
    return False


def test_refuses_links_into_runs_and_links_as_the_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_runs = tmp_path / "runs"
    fake_runs.mkdir()
    monkeypatch.setattr(msr, "_protected_dirs", lambda: [fake_runs.resolve()])
    link = tmp_path / "alias"
    if not _make_link(link, fake_runs):
        pytest.skip("this account/platform can create neither a symlink nor a junction")
    assert msr.main(["--out", str(link)]) == 2  # the target itself is a link
    assert msr.main(["--out", str(link / "new")]) == 2  # resolves into runs/
    assert os.listdir(fake_runs) == []
    empty = tmp_path / "empty"
    empty.mkdir()
    link2 = tmp_path / "alias2"
    assert _make_link(link2, empty)
    assert msr.main(["--out", str(link2)]) == 2  # a link is refused even to a harmless empty directory
    assert os.listdir(empty) == []


def test_refuses_a_non_empty_directory_and_leaves_it_untouched(tmp_path: Path) -> None:
    target = tmp_path / "busy"
    target.mkdir()
    (target / "keep.txt").write_bytes(b"mine\n")
    assert msr.main(["--out", str(target)]) == 2
    assert _tree(target) == {"keep.txt": b"mine\n"}
    file_target = tmp_path / "afile"
    file_target.write_bytes(b"x")
    assert msr.main(["--out", str(file_target)]) == 2
    assert file_target.read_bytes() == b"x"


def test_accepts_an_existing_empty_directory_and_refuses_a_second_run(tmp_path: Path) -> None:
    target = tmp_path / "empty"
    target.mkdir()
    assert msr.main(["--out", str(target)]) == 0
    first = _tree(target)
    assert msr.main(["--out", str(target)]) == 2  # never overwrites
    assert _tree(target) == first


def test_a_failed_write_removes_only_what_it_created(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "half"
    real_write = msr._write_new
    calls = {"n": 0}

    def flaky(path: Path, data: bytes) -> None:
        calls["n"] += 1
        if calls["n"] == 5:
            raise OSError("disk full (simulated)")
        real_write(path, data)

    monkeypatch.setattr(msr, "_write_new", flaky)
    assert msr.main(["--out", str(target)]) == 2
    assert target.is_dir() and list(target.iterdir()) == []


def test_out_is_required_and_the_script_is_stdlib_only() -> None:
    proc = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False)
    assert proc.returncode == 2 and "--out" in proc.stderr
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names), roots - set(sys.stdlib_module_names)


def test_command_line_run_writes_the_same_bytes(sample: Path, tmp_path: Path) -> None:
    out = tmp_path / "cli"
    proc = subprocess.run([sys.executable, str(SCRIPT), "--out", str(out)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert "SYNTHETIC" in proc.stdout
    assert _tree(out) == _tree(sample)


# ---------------------------------------------------------------- privacy and the real runs


def _real_container_ids() -> set[str]:
    found: set[str] = set()
    for run_id in acc.REAL.values():
        path = REAL_RUNS / run_id / "manifest.json"
        if path.is_file():
            found.add(json.loads(path.read_text(encoding="utf-8"))["container_id"])
    return found


def test_no_private_paths_or_real_container_ids(sample: Path) -> None:
    # The account name is read from the environment, so no real name is written into this file.
    account = {Path.home().name, os.environ.get("USERNAME", ""), os.environ.get("USER", "")} - {""}
    assert account, "cannot determine the local account name"
    forbidden = [*account, "C:\\Users", "C:\\\\Users", "C:/Users", "AppData", "\\Users\\", "/Users/", "/home/", "Projects"]
    real_ids = _real_container_ids()
    for name, data in _tree(sample).items():
        text = data.decode("utf-8")
        for needle in forbidden:
            assert needle.lower() not in text.lower(), (name, needle)
        for real in real_ids:
            assert real not in text and real[:12] not in text, name
        assert re.search(r"[A-Za-z]:[\\/]", text) is None, name  # no drive-letter path at all
    # every path field is the placeholder form
    for run_id in msr.run_ids().values():
        manifest = json.loads((sample / run_id / "manifest.json").read_text(encoding="utf-8"))
        for value in (manifest["docker_inspect_path"], manifest["evidence"]["host_copy_path"],
                      manifest["evidence"]["container_path"], manifest["container_state"]["raw_inspect_path"]):
            assert value.startswith("/synthetic/"), value


def test_generating_does_not_change_the_real_runs(tmp_path: Path) -> None:
    files = _real_files()
    if not files:
        pytest.skip("the real canonical runs are not present under runs/")
    before = _real_hashes()
    assert msr.main(["--out", str(tmp_path / "x")]) == 0
    assert msr.main(["--out", str(REAL_RUNS)]) == 2
    assert _real_hashes() == before


# ---------------------------------------------------------------- real browser


@pytest.fixture(scope="module")
def chrome() -> Any:
    # Module scoped, like the other browser tests: the unit-test network guard is a per-test fixture, so
    # the browser and the Viewer process must be started before it applies.
    if BROWSER is None:
        pytest.skip("no Chrome, Chromium or Edge found (set CORONER_BROWSER); browser check not run")
    with cdp.Chrome() as c:
        c.page.set_viewport(1280, 900)
        yield c


@pytest.fixture(scope="module")
def viewer(sample: Path, chrome: Any) -> Any:
    with cdp.ViewerProcess(sample) as v:
        yield v


def test_headline_and_comparison_render_in_headless_chrome(chrome: Any, viewer: Any, ids: dict[str, str]) -> None:
    table_js = """
    (() => {
      const rows = {};
      for (const tr of document.querySelectorAll('table.compare-table tbody tr')) {
        rows[tr.querySelector('th').textContent] = Array.from(tr.querySelectorAll('td')).map((td) => td.textContent);
      }
      return {
        rows: rows,
        statements: Array.from(document.querySelectorAll('.statements li')).map((li) => li.textContent),
        headline: Array.from(document.querySelectorAll('.headline-text')).map((e) => e.textContent),
      };
    })()
    """
    query = f"a={ids['A']}&b={ids['B']}&c={ids['C']}"
    chrome.page.navigate(viewer.base + "/compare?" + query, acc.READY)
    view = chrome.page.eval(table_js)
    report = chrome.page.report()
    detail_reports = []
    for run_id in ids.values():
        chrome.page.navigate(viewer.base + f"/runs/{run_id}", acc.READY)
        detail_reports.append(chrome.page.report())
    assert view["headline"] == [acc.HEADLINE]
    assert view["rows"]["Docker OOMKilled"] == ["false", "false", "true"]
    texts = " | ".join(view["statements"])
    assert "OOMKilled distinguishes Experiment B (false) from Experiment C (true)" in texts
    assert "Experiment B and Experiment C share ExitCode 137" in texts
    for rep in [report, *detail_reports]:
        assert rep["uncaught_exceptions"] == [], rep["uncaught_exceptions"]
        assert rep["csp_violations"] == [], rep["csp_violations"]
        assert acc.unexpected(rep) == []
        assert rep["console_errors"] == [] and rep["console_warnings"] == []
