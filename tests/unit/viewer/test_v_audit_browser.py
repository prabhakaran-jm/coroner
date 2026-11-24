"""Task 4.14 V1 to V28 audit: focused real-Chrome checks on scratch fixtures (headless Chrome over the DevTools Protocol).

Covers rows of the acceptance record that no earlier browser run exercised: the Experiment A, B and C text rows
and statements (V12 to V17), the tick label against the nearest point (V18), pointer hover (V21), the loading,
error, retry and empty states (V22 to V25), the hostile text check (V27) and the overview run list against the
complete Run_Order (V7, V8). Everything runs in a temporary directory outside the repository; the real `runs/`
is not read. When no Chrome, Chromium or Edge exists every test is skipped with that reason (a skip is not a pass).

Honest limits: the API-unreachable state is produced by blocking the `/api/` URLs inside Chrome
(`Network.setBlockedURLs`), not by stopping the Viewer process; the result is the same fetch failure path
(`api.js` status 0). The run is automated, not human-operated.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.unit.viewer import acceptance_47 as acc
from tests.unit.viewer import cdp_harness as cdp
from tests.unit.viewer.browser_harness import find_browser
from tests.unit.viewer.builder import RunsBuilder, sample_specs

BROWSER = find_browser()
pytestmark = pytest.mark.skipif(
    BROWSER is None, reason="no Chrome, Chromium or Edge found (set CORONER_BROWSER); V-audit browser checks not run"
)

HOSTILE = "<img src=x onerror=alert(1)><script>alert(2)</script>"

# Records DOM additions of `.state` blocks (loading, error, empty) so a short-lived "Loading..." is observed for real.
STATE_RECORDER_JS = r"""
(() => {
  window.__states = [];
  const note = (n) => {
    if (n.nodeType !== 1) { return; }
    const nodes = n.matches('.state') ? [n] : Array.from(n.querySelectorAll('.state'));
    for (const s of nodes) { window.__states.push(s.className + ' | ' + s.textContent.trim()); }
  };
  new MutationObserver((ms) => { for (const m of ms) { m.addedNodes.forEach(note); } })
    .observe(document, { childList: true, subtree: true });
})();
"""


def main_text(page: cdp.Page) -> str:
    return str(page.eval("document.getElementById('main').textContent"))


def api_json(page: cdp.Page, path: str) -> Any:
    return json.loads(page.eval(f"fetch({json.dumps(path)}).then(r => r.text())", await_promise=True))


def hover_point(page: cdp.Page, index: int) -> dict[str, Any]:
    """A real pointer move onto the plotted point `index`; returns the readout facts."""
    box = page.eval(
        "(() => { const e = document.querySelector('#chart-pt-%d'); if (!e) return null; e.scrollIntoView({block:'center'});"
        " const r = e.getBoundingClientRect(); return [r.left + r.width/2, r.top + r.height/2]; })()" % index
    )
    assert box, f"point {index} not found"
    page.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": box[0], "y": box[1], "button": "none", "buttons": 0})
    page.wait_for("document.querySelector('.chart-readout .readout-lines li') !== null", 5, "hover readout")
    return page.eval(acc.READOUT_JS)


def tick_facts(page: cdp.Page) -> list[dict[str, Any]]:
    """Each x tick label with its x position, and the index of the plotted point nearest to it on screen."""
    return page.eval(
        """(() => {
  const svg = document.querySelector('.chart-plot svg');
  const pts = Array.from(svg.querySelectorAll('circle.chart-point')).map((c) => [c.id, Number(c.getAttribute('cx'))]);
  const ticks = Array.from(svg.querySelectorAll('text.chart-x-tick-label'));
  return ticks.map((t) => {
    const x = Number(t.getAttribute('x'));
    let best = null;
    for (const [id, cx] of pts) { if (best === null || Math.abs(cx - x) < Math.abs(best[1] - x)) { best = [id, cx]; } }
    return { label: t.textContent, x: x, nearestId: best ? best[0] : null };
  });
})()"""
    )


@pytest.fixture(scope="module")
def chrome():  # type: ignore[no-untyped-def]
    with cdp.Chrome() as c:
        yield c


@pytest.fixture(scope="module")
def fixture(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    root = tmp_path_factory.mktemp("fxv")
    ids = acc.build_compare_fixture(root / "runs")
    with cdp.ViewerProcess(root / "runs") as viewer:
        yield {"ids": ids, "viewer": viewer}


@pytest.fixture(scope="module")
def hostile_viewer(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    root = tmp_path_factory.mktemp("fxhostile") / "runs"
    b = RunsBuilder(root)
    run_id = b.add_run("B", samples=sample_specs(5), run_id="b-20261120T211311Z-0000000b")
    manifest = root / run_id / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["termination_description"] = HOSTILE
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with cdp.ViewerProcess(root) as viewer:
        yield {"viewer": viewer, "run_id": run_id}


@pytest.fixture(scope="module")
def empty_viewer(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    root = tmp_path_factory.mktemp("fxempty") / "runs-empty"
    root.mkdir()
    with cdp.ViewerProcess(root) as viewer:
        yield viewer


@pytest.fixture(scope="module")
def discrepancy_viewer(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    root = tmp_path_factory.mktemp("fxdisc") / "runs"
    b = RunsBuilder(root)
    from tests.unit.viewer.builder import InspectSpec, StateSpec

    b.add_run("B", samples=sample_specs(3), run_id="b-20261120T211311Z-0000000d",
              container_state=StateSpec(exit_code=137, oom_killed=False), inspect=InspectSpec(exit_code=1, oom_killed=False))
    b.add_run("A", samples=sample_specs(3), run_id="a-20261120T211146Z-0000000e")
    with cdp.ViewerProcess(root) as viewer:
        yield viewer


# ------------------------------------------------------------------ V7, V8


def test_overview_lists_cards_plus_every_other_run_in_run_order(chrome: cdp.Chrome, fixture: Any) -> None:
    """V7: cards (first run of each type) + the 'Other saved runs' table = every discovered run, in Run_Order."""
    page = chrome.page
    page.set_viewport(1280, 900)
    page.navigate(fixture["viewer"].base + "/", acc.READY)
    api = api_json(page, "/api/runs")
    order = [r["run_id"] for r in api["runs"]]
    cards = page.eval("Array.from(document.querySelectorAll('.cards a.button-link')).map(a => decodeURIComponent(a.getAttribute('href').split('/').pop()))")
    rows = page.eval("Array.from(document.querySelectorAll('.run-list tbody tr')).map(tr => tr.querySelector('a').textContent)")
    heads = page.eval("Array.from(document.querySelectorAll('.run-list thead th')).map(th => th.textContent)")
    assert page.eval("document.querySelector('.run-list .section-title').textContent") == "Other saved runs"
    assert heads == ["Run id", "Experiment", "Verdict", "Detail"]
    first_of_type = {}
    for r in api["runs"]:
        first_of_type.setdefault(r["experiment_type"], r["run_id"])
    assert cards == [first_of_type["A"], first_of_type["B"], first_of_type["C"]]
    assert rows == [r for r in order if r not in cards]  # same relative order as Run_Order
    assert sorted(cards + rows) == sorted(order) and len(cards) + len(rows) == api["total_found"] == len(order)
    assert acc_unexpected(page) == []


def acc_unexpected(page: cdp.Page) -> list[str]:
    return acc.unexpected(page.report())


def test_overview_discrepancy_marker_only_on_a_discrepant_card(chrome: cdp.Chrome, discrepancy_viewer: Any) -> None:
    """V8: 'DISCREPANCY' appears on the card whose run disagrees, and nowhere else on the overview."""
    page = chrome.page
    page.set_viewport(1280, 900)
    page.navigate(discrepancy_viewer.base + "/", acc.READY)
    cards = page.eval("Array.from(document.querySelectorAll('.cards .card')).map(c => [c.querySelector('.card-title').textContent, c.textContent.includes('DISCREPANCY')])")
    assert cards == [["Experiment A", False], ["Experiment B", True], ["Experiment C", False]]


# ------------------------------------------------------------------ V12 to V17, V19 (A)


def test_b_text_rows_sample_timing_and_derived_intervals(chrome: cdp.Chrome, fixture: Any) -> None:
    """V12, V13, V14 on a synthetic Experiment B run."""
    page = chrome.page
    page.set_viewport(1280, 900)
    page.navigate(fixture["viewer"].base + "/runs/" + fixture["ids"]["B"], acc.READY)
    text = main_text(page)
    # V12 (Requirement 15 criteria 16, 17; Requirement 6 criterion 18)
    for needle in ("Recorded times (each on its own clock)", "SIGKILL send time (Host_Clock)",
                   "Docker FinishedAt (Docker_Clock, uncorrected)", "shown to ms; original in details"):
        assert needle in text, needle
    assert page.eval("document.querySelectorAll('.chart-text details summary').length") > 0 or "original" in text
    # V13 (Requirement 15 criteria 18 to 21, 27): each interval is labelled derived, with the fixed statements
    for needle in ("Corrected_Capture_Time against SIGKILL send time (Host_Clock)",
                   "Corrected_Capture_Time against Docker FinishedAt (Docker_Clock)",
                   "derived: derived, not independently proven", "from: ", "to: ",
                   "difference -18009 ms (from minus to, signed)", "Clock_Uncertainty 12.5 ms",
                   "The four clocks (Container_Clock, Collector_Clock, Host_Clock, Docker_Clock) are not recorded as synchronised.",
                   "The recorded Clock_Uncertainty does not bound the Docker_Clock or the Container_Clock.",
                   "this interval does not establish the order of two events",
                   "no recorded bound exists for the Docker_Clock"):
        assert needle in text, needle
    # V14 (Requirement 15 criteria 22, 24): sample_timing exactly as recorded (fixture: tolerance 250, analysis unavailable).
    # The builder writes the groups in the M0 shape (plain lists of timestamps); see test_sample_timing_m0_shape.py and the
    # acceptance record ("V14 fix and result").
    for needle in ("Recorded sample timing (exactly as the Runner stored it)", "pre_kill_count5", "pre_kill_late_received_count0",
                   "within_tolerance_count5", "post_kill_beyond_tolerance count0", "after_container_stop_analysisunavailable",
                   "tolerance_ms (as recorded)250", "The analysis relative to the container stop is unavailable."):
        assert needle in text, needle
    assert "after_container_stop count" not in text  # an unavailable analysis never shows a zero count
    assert acc_unexpected(page) == []


def test_a_chart_and_text_rows_have_no_interval_or_marker(chrome: cdp.Chrome, fixture: Any) -> None:
    """V15, V16 and V19 (A) on a synthetic Experiment A run."""
    page = chrome.page
    page.set_viewport(1280, 900)
    page.navigate(fixture["viewer"].base + "/runs/" + fixture["ids"]["A"], acc.READY)
    text = main_text(page)
    assert page.eval("document.querySelector('.chart-section .section-title').textContent") == "RSS timeline"
    axis = page.eval("Array.from(document.querySelectorAll('.chart-plot svg text')).map(t => t.textContent)")
    assert "Raw Probe Capture_Time" in axis and "SIGKILL" not in axis
    assert page.eval("document.querySelectorAll('.chart-plot svg .chart-sigkill, .chart-plot svg .chart-sigkill-label').length") == 0
    assert "Capture_Time as recorded (Container_Clock). No clock correction is applied." in text
    for needle in ("Recorded times (each on its own clock)", "crash requested (Host_Clock)", "Docker FinishedAt (Docker_Clock, uncorrected)",
                   "The position of crash requested (Host_Clock) and of the Docker FinishedAt (Docker_Clock) relative to the Capture_Time axis is not established. "
                   "The Capture_Time values (Container_Clock) and these times are separate recorded values.",
                   "No Derived_Interval is shown for this run. The raw Capture_Time values and the Docker FinishedAt are separate recorded values.",
                   "The raw Capture_Time values (Container_Clock) and the Docker FinishedAt (Docker_Clock) are separate recorded values. "
                   "No interval between them is shown, and their order is not established."):
        assert needle in text, needle
    assert "derived: derived" not in text and "difference" not in text.replace("Difference", "") and "(from minus to" not in text
    rows = page.eval(acc.SAMPLE_ROWS_JS)
    assert rows["status"] == "Showing 5 of 5 rows" and rows["moreVisible"] is False  # V19 for A (rows are in a closed details)
    assert acc_unexpected(page) == []


def test_c_text_rows_and_sampling_limitations(chrome: cdp.Chrome, fixture: Any) -> None:
    """V17 text part on a synthetic Experiment C run."""
    page = chrome.page
    page.set_viewport(1280, 900)
    page.navigate(fixture["viewer"].base + "/runs/" + fixture["ids"]["C"], acc.READY)
    text = main_text(page)
    for needle in ("Docker FinishedAt (Docker_Clock, uncorrected)",
                   "The position of the Docker FinishedAt (Docker_Clock) relative to the Capture_Time axis is not established. "
                   "The Capture_Time values (Container_Clock) and the FinishedAt are separate recorded values.",
                   "Sampling can miss memory growth between preserved Samples and after the last preserved Sample.",
                   "Sampling_Limitations", "synthetic sampling limitation statement",
                   "No Derived_Interval is shown for this run."):
        assert needle in text, needle
    assert "crash requested" not in text and "derived: derived" not in text
    assert page.eval("document.querySelectorAll('.chart-plot svg .chart-sigkill').length") == 0
    assert acc_unexpected(page) == []


# ------------------------------------------------------------------ V18, V21


@pytest.mark.parametrize("kind", ["A", "B", "C"])
def test_hover_shows_the_sample_readout_and_ticks_are_their_own_time(chrome: cdp.Chrome, fixture: Any, kind: str) -> None:
    """V21 (pointer hover equals the API point) and V18 (the tick label is the tick's own time, not the nearest point's)."""
    page = chrome.page
    run_id = fixture["ids"][kind]
    page.set_viewport(1280, 900)
    page.navigate(fixture["viewer"].base + "/runs/" + run_id, acc.READY)
    pts = api_json(page, "/api/runs/" + run_id)["chart"]["points"]
    assert page.eval(acc.READOUT_JS)["lines"] == []  # before any pointer move: only the hint
    readout = hover_point(page, 3)
    assert readout["activePointClass"] == "chart-pt-3" and acc.point_matches(readout["lines"], pts[3]), readout
    assert page.eval("document.activeElement.classList.contains('chart-plot')") is False  # hover did not need keyboard focus
    ticks = tick_facts(page)
    assert ticks, "no x tick labels found"
    assert all(re.match(r"^\d\d:\d\d:\d\d", t["label"]) for t in ticks)
    if kind == "B":
        # Experiment B axis is the corrected time: no tick label equals the (ms-resolution) time of the point nearest to it
        first = ticks[0]
        idx = int(first["nearestId"].rsplit("-", 1)[1])
        near = hover_point(page, idx)
        corrected = [ln for ln in near["lines"] if "corrected (derived)" in ln]
        assert corrected, near["lines"]
        assert first["label"] not in corrected[0], (first["label"], corrected[0])
    assert acc_unexpected(page) == []


# ------------------------------------------------------------------ V22 to V25


def test_loading_error_retry_and_recovery_states(chrome: cdp.Chrome, fixture: Any) -> None:
    """V22, V23, V24 and the 'Loading...' state: the API is made unreachable inside Chrome, then restored."""
    page = chrome.page
    page.set_viewport(1280, 900)
    base = fixture["viewer"].base
    script = page.call("Page.addScriptToEvaluateOnNewDocument", {"source": STATE_RECORDER_JS})["identifier"]
    page.call("Network.setBlockedURLs", {"urls": ["*/api/*"]})
    try:
        page.navigate(base + "/runs/" + fixture["ids"]["B"], "document.querySelector('.state-error') !== null")
        seen = page.eval("window.__states")
        assert any("state-loading" in s and "Loading\u2026" in s for s in seen), seen  # the loading state was really shown
        panel = page.eval("document.querySelector('.state-error').innerText")
        assert "Could not load this page" in panel and "The Viewer API is not reachable." in panel and "Retry" in panel
        assert page.eval("document.querySelector('.state-error').getAttribute('role')") == "alert"
        # V23: Retry with the API still unreachable shows loading again, then the same error
        page.eval("window.__states.length = 0")
        page.click(".state-error button")
        page.wait_for("window.__states.some(s => s.includes('state-error'))", 10, "second error")
        again = page.eval("window.__states")
        assert any("state-loading" in s for s in again) and "The Viewer API is not reachable." in again[-1], again
        # V24: restore the API, press Retry: the incident page loads
        page.call("Network.setBlockedURLs", {"urls": []})
        page.click(".state-error button")
        page.wait_for("document.querySelector('#main .page') !== null && !document.querySelector('.state-error')", 15, "recovery")
        assert fixture["ids"]["B"] in main_text(page) and "Incident detail" in main_text(page)
    finally:
        page.call("Network.setBlockedURLs", {"urls": []})
        page.call("Page.removeScriptToEvaluateOnNewDocument", {"identifier": script})


def test_empty_runs_directory_shows_the_empty_state(chrome: cdp.Chrome, empty_viewer: Any) -> None:
    """V25: an empty scratch Runs_Directory (a second Viewer) shows the empty state with the directory label."""
    page = chrome.page
    page.set_viewport(1280, 900)
    page.navigate(empty_viewer.base + "/", "document.querySelector('.state-empty') !== null")
    text = page.eval("document.querySelector('.state-empty').innerText")
    assert "No saved runs" in text
    assert "No saved runs were found in runs-empty" in text  # the final path component only (Requirement 3 criterion 9)
    assert ". The Viewer does not generate runs; it only displays runs that already exist." in text
    assert re.search(r"[A-Za-z]:[\\/]", main_text(page)) is None  # no absolute host path on the page
    assert acc_unexpected(page) == []


# ------------------------------------------------------------------ V27


def test_hostile_manifest_text_is_shown_as_plain_text(chrome: cdp.Chrome, hostile_viewer: Any) -> None:
    """V27: a hostile termination_description (scratch copy) appears literally; no element, dialog or script runs."""
    page = chrome.page
    page.set_viewport(1280, 900)
    page.navigate(hostile_viewer["viewer"].base + "/runs/" + hostile_viewer["run_id"], acc.READY)
    text = main_text(page)
    assert HOSTILE in text
    assert page.eval("document.querySelectorAll('#main img, #main script, #main [onerror]').length") == 0
    assert page.eval("window.__csp || []") == []
    dialogs = [e for e in page.pending_events if e.get("method") == "Page.javascriptDialogOpening"]
    assert dialogs == []
    report = page.report()
    assert report["console_errors"] == [] and report["uncaught_exceptions"] == [] and report["csp_violations"] == []
    assert report["failed_requests"] == [] and acc.unexpected(report) == []
