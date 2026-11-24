"""Task 4.7 acceptance checks on scratch fixtures, run in real Chrome through the DevTools Protocol.

Nothing here reads the real `runs/`. A synthetic Runs_Directory is built under the temporary directory and the
Viewer is started with the real command line (`python -m coroner.viewer`). Chrome is driven with real key and
mouse events; every assertion reads DOM text, computed styles or HTTP responses from the running page. When no
Chrome, Chromium or Edge exists every test is skipped with the reason shown by `-rs` (a skip is not a pass).
The checks against the real saved runs are in `acceptance_47.py` (run as a command, not by pytest).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.unit.viewer import acceptance_47 as acc
from tests.unit.viewer import cdp_harness as cdp
from tests.unit.viewer.browser_harness import find_browser

BROWSER = find_browser()
pytestmark = pytest.mark.skipif(
    BROWSER is None, reason="no Chrome, Chromium or Edge found (set CORONER_BROWSER); real-browser 4.7 checks not run"
)


@pytest.fixture(scope="module")
def chrome():  # type: ignore[no-untyped-def]
    with cdp.Chrome() as c:
        yield c


@pytest.fixture(scope="module")
def fixture(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    root = tmp_path_factory.mktemp("fx47")
    ids = acc.build_compare_fixture(root / "runs")
    many_id = acc.build_many_fixture(root / "many", 450)
    with cdp.ViewerProcess(root / "runs") as v1, cdp.ViewerProcess(root / "many") as v2:
        yield {"ids": ids, "many_id": many_id, "v1": v1, "v2": v2}


@pytest.fixture(scope="module")
def partial_viewer(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    """A Viewer on a scratch run with more than 5000 plottable Samples (module scope: starts before the per-test network guard)."""
    root = tmp_path_factory.mktemp("partial47")
    acc.build_partial_fixture(root / "runs")
    assert (root / "runs" / acc.PARTIAL_ID / "evidence.jsonl").stat().st_size < 16 * 1024 * 1024
    with cdp.ViewerProcess(root / "runs") as viewer:
        yield viewer


def _env(chrome: cdp.Chrome, base: str, tmp: Path) -> acc.Env:
    return acc.Env(chrome.page, acc.Recorder(tmp), base, tmp, "synthetic fixture")


def test_scenarios_on_the_synthetic_fixture_all_pass(chrome: cdp.Chrome, fixture: Any, tmp_path: Path) -> None:
    env = _env(chrome, fixture["v1"].base, tmp_path)
    acc.sc_synthetic_compare(env, fixture["ids"])
    env2 = _env(chrome, fixture["v2"].base, tmp_path)
    acc.sc_show_more(env2, fixture["many_id"], 450)
    results = env.rec.results + env2.rec.results
    ids = {r["id"] for r in results}
    assert {"4.7-06", "4.7-07a", "4.7-07b", "4.7-07c", "4.7-07d", "4.7-08a"} <= ids
    failed = [(r["id"], r["actual"]) for r in results if r["status"] != "PASS"]
    assert failed == []
    assert all(r["javascript_executed_in_chrome"] for r in results)


@pytest.mark.parametrize("query", ["a=..", "x=1", "a=", "a=a-20261120T211146Z-00000001&a=a-20261120T211146Z-00000001"])
def test_invalid_compare_queries_are_plain_400_in_chrome(chrome: cdp.Chrome, fixture: Any, query: str) -> None:
    page = chrome.page
    base = fixture["v1"].base
    resp = page.navigate(f"{base}/compare?{query}", "document.readyState === 'complete'")
    fetched = page.eval(f"fetch({json.dumps('/compare?' + query)}).then(async r => ({{status: r.status, type: r.headers.get('content-type'), body: await r.text()}}))", await_promise=True)
    assert resp["status"] == 400 and resp["mime"] == "text/plain"
    assert fetched == {"status": 400, "type": "text/plain; charset=utf-8", "body": "Invalid compare parameter.\n"}
    assert page.eval("document.body.innerText").strip() == "Invalid compare parameter."


def test_default_compare_page_has_no_console_csp_or_request_problems(chrome: cdp.Chrome, fixture: Any) -> None:
    page = chrome.page
    ids = fixture["ids"]
    page.set_viewport(1280, 900)
    for path in ("/", f"/runs/{ids['B']}", f"/runs/{ids['C']}", "/compare"):
        page.navigate(fixture["v1"].base + path, acc.READY)
        acc.tab_walk(page, 40)
        assert acc.unexpected(page.report()) == [], path


def test_every_interactive_target_is_at_least_44_px_in_chrome(chrome: cdp.Chrome, fixture: Any, tmp_path: Path) -> None:
    """Real bounding boxes (not CSS declarations) at 1280, 480 and 360 px; layout and overflow checks run alongside."""
    ids = fixture["ids"]
    env = _env(chrome, fixture["v1"].base, tmp_path)
    pages = [("/", "overview"), (f"/runs/{ids['A']}", "incident-A"), (f"/runs/{ids['B']}", "incident-B"),
             (f"/runs/{ids['C']}", "incident-C"), ("/compare", "compare")]
    acc.sc_targets(env, pages)
    acc.sc_target_behaviour(env, f"/runs/{ids['B']}")
    result = env.rec.results[0]
    assert result["status"] == "PASS", result["actual"]
    kinds = result["evidence"]["dimensions_by_kind"]
    assert "summary(original)" in kinds and "summary(sample-table)" in kinds, kinds
    assert all(v["min_height_px"] >= 44 and v["min_width_px"] >= 44 for v in kinds.values()), kinds
    assert [r["status"] for r in env.rec.results] == ["PASS", "PASS"], [r["actual"] for r in env.rec.results]


def test_partial_view_fixture_in_chrome(chrome: cdp.Chrome, partial_viewer: Any, tmp_path: Path) -> None:
    """More than 5000 plottable Samples with known gaps: dashed omissions, no joined gaps, full-run statistics, ticks."""
    env = _env(chrome, partial_viewer.base, tmp_path)
    acc.sc_partial_view(env, acc.PARTIAL_ID, acc.PARTIAL_PEAKS, 5000, shots=False)
    failed = [(r["id"], r["actual"]) for r in env.rec.results if r["status"] != "PASS"]
    assert failed == []
    assert [r["id"] for r in env.rec.results] == [f"4.7-19.{n}" for n in range(1, 11)]


def test_csp_capture_is_not_vacuous(chrome: cdp.Chrome, fixture: Any, tmp_path: Path) -> None:
    env = _env(chrome, fixture["v1"].base, tmp_path)
    acc.sc_capture_controls(env)
    assert [r["status"] for r in env.rec.results] == ["PASS"]


def test_chart_keyboard_moves_one_sample_at_a_time_and_matches_the_api(chrome: cdp.Chrome, fixture: Any) -> None:
    page = chrome.page
    base = fixture["v1"].base
    run_id = fixture["ids"]["B"]
    page.set_viewport(1280, 900)
    page.navigate(f"{base}/runs/{run_id}", acc.READY)
    pts = json.loads(page.eval(f"fetch('/api/runs/{run_id}').then(r => r.text())", await_promise=True))["chart"]["points"]
    acc.focus_top(page)
    for _ in range(80):
        page.key("Tab")
        if page.eval("document.activeElement.classList.contains('chart-plot')"):
            break
    assert page.eval("document.activeElement.classList.contains('chart-plot')")
    expected = [("focus", 0), ("ArrowRight", 1), ("ArrowRight", 2), ("ArrowLeft", 1), ("End", len(pts) - 1),
                ("ArrowRight", len(pts) - 1), ("Home", 0), ("ArrowLeft", 0)]
    for key, index in expected:
        if key != "focus":
            page.key(key)
        readout = page.eval(acc.READOUT_JS)
        assert acc.point_matches(readout["lines"], pts[index]), (key, readout["lines"])
        assert readout["activePointClass"] == f"chart-pt-{index}"
    page.key("Tab")
    assert page.eval(acc.DESCRIBE_ACTIVE)["insideChart"] is False  # the plot is one tab stop


def test_sample_table_opens_with_enter_and_closes_with_space(chrome: cdp.Chrome, fixture: Any) -> None:
    page = chrome.page
    page.set_viewport(1280, 900)
    page.navigate(f"{fixture['v1'].base}/runs/{fixture['ids']['B']}", acc.READY)
    acc.focus_top(page)
    for _ in range(80):
        page.key("Tab")
        if page.eval("document.activeElement.tagName === 'SUMMARY' && !!document.activeElement.closest('details.sample-table')"):
            break
    assert page.eval(acc.SAMPLE_ROWS_JS)["firstRowVisible"] is False
    page.key("Enter")
    page.wait_for("document.querySelector('details.sample-table').open === true", 5, "open")
    opened = page.eval(acc.SAMPLE_ROWS_JS)
    assert opened["firstRowVisible"] is True and opened["rowCount"] == 5
    page.key(" ")
    page.wait_for("document.querySelector('details.sample-table').open === false", 5, "closed")
    assert page.eval(acc.SAMPLE_ROWS_JS)["firstRowVisible"] is False


@pytest.mark.parametrize("width", [480, 360])
def test_no_page_wide_horizontal_overflow_at_narrow_widths(chrome: cdp.Chrome, fixture: Any, width: int) -> None:
    page = chrome.page
    ids = fixture["ids"]
    page.set_viewport(width, 900)
    for path in ("/", f"/runs/{ids['B']}", "/compare"):
        page.navigate(fixture["v1"].base + path, acc.READY)
        m = page.eval(acc.LAYOUT_JS)
        assert m["clientWidth"] == width
        assert m["pageHorizontalOverflow"] is False and m["outside"] == [] and m["overlapCount"] == 0, (path, m["outside"], m["overlaps"])


def test_reduced_motion_removes_the_transition(chrome: cdp.Chrome, fixture: Any) -> None:
    page = chrome.page
    page.set_viewport(1280, 900)
    page.navigate(fixture["v1"].base + "/", acc.READY)
    probe = "getComputedStyle(document.querySelector('.nav-link')).transitionDuration"
    assert page.eval(probe).startswith("0.12s")
    page.call("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]})
    try:
        assert page.eval(probe).startswith("0s")
    finally:
        page.call("Emulation.setEmulatedMedia", {"features": []})
