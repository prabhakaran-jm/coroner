"""Task 4.14 V13 (Requirement 15 criterion 21) in real headless Chrome: the Experiment B fallback axis.

A synthetic Experiment B run (built in a temporary directory outside the repository; the real `runs/` is not
read) records a clock offset that is a valid integer but makes `Capture_Time - offset` leave the datetime range.
The design then selects the `raw_uncorrected` axis with the reason `correction_out_of_range`, keeps the raw
Probe Capture_Time, draws no SIGKILL marker, and keeps only the `finished_at_to_sigkill` Derived_Interval, which
Requirement 15 criterion 21 requires to show the signed difference, "derived, not independently proven" and the
statement that no recorded bound exists for the Docker_Clock, with no statement about which time came first.

The same run also pins the order of the Incident detail section headings (V9). When no Chrome, Chromium or Edge
exists every test is skipped with that reason (a skip is not a pass).
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
    BROWSER is None, reason="no Chrome, Chromium or Edge found (set CORONER_BROWSER); V13 fallback browser checks not run"
)

HUGE_OFFSET = 2**53 - 1  # a valid safe_int; ts - offset is far outside the datetime range
PLOT_LEFT, PLOT_WIDTH = 72, 864  # chart.js frame: viewBox 960 wide, margins left 72 and right 24

V9_SECTIONS = [
    "Run", "Verdict", "Docker state", "Termination as described by the Runner", "Evidence records",
    "Sample counts", "Last Samples and last Receive_Time", "Clock offset and uncertainty",
    "Recorded sample timing (exactly as the Runner stored it)", "Derived intervals (Experiment B)",
    "RSS timeline", "Evidence vs Uncertainty", "Artifact status", "Warnings",
]

# Phrases that would claim which of two cross-clock times came first. The fixed negative statements
# ("does not establish the order of two events", "order not established ...") are not matched.
ORDER_CLAIMS = re.compile(
    r"came first|happened (?:before|after)|occurred (?:before|after)|\bearlier than\b|\blater than\b|"
    r"\bprecedes?\b|\bpreceded\b|\bfollowed\b|\bbefore the SIGKILL\b|\bafter the SIGKILL\b|\bwas sent after\b|\bwas sent before\b",
    re.IGNORECASE,
)


@pytest.fixture(scope="module")
def chrome():  # type: ignore[no-untyped-def]
    with cdp.Chrome() as c:
        yield c


@pytest.fixture(scope="module")
def fallback(tmp_path_factory: pytest.TempPathFactory, chrome: cdp.Chrome) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("fxv13") / "runs"
    run_id = RunsBuilder(root).add_run("B", samples=sample_specs(5), clock_offset_ms=HUGE_OFFSET)
    raw_ts = [
        json.loads(line)["sample"]["ts"]
        for line in (root / run_id / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
        if json.loads(line)["record_type"] == "sample"
    ]
    manifest = json.loads((root / run_id / "manifest.json").read_text(encoding="utf-8"))
    with cdp.ViewerProcess(root) as viewer:
        page = chrome.page
        page.set_viewport(1280, 900)
        page.navigate(viewer.base + "/runs/" + run_id, acc.READY)
        api = json.loads(page.eval(f"fetch('/api/runs/{run_id}').then(r => r.text())", await_promise=True))
        dom = page.eval(
            r"""(() => {
  const svg = document.querySelector('.chart-plot svg');
  return {
    cx: Array.from(svg.querySelectorAll('circle.chart-point')).map((c) => Number(c.getAttribute('cx'))),
    ticks: Array.from(svg.querySelectorAll('text.chart-x-tick-label')).map((t) => [t.textContent, Number(t.getAttribute('x'))]),
    xTitle: Array.from(svg.querySelectorAll('text')).map((t) => t.textContent).filter((t) => /Capture_Time|host-equivalent/.test(t)),
    dashedVertical: Array.from(svg.querySelectorAll('line')).filter((l) => l.getAttribute('x1') === l.getAttribute('x2') && l.getAttribute('stroke-dasharray')).length,
    headings: Array.from(document.querySelectorAll('h3.section-title')).map((h) => h.textContent.trim()),
    text: document.getElementById('main').innerText,
  };
})()"""
        )
        report = page.report()
        csp = page.csp_events()
    return {"api": api, "dom": dom, "raw_ts": raw_ts, "manifest": manifest, "report": report, "csp": csp}


def test_fallback_axis_is_selected_for_the_documented_reason(fallback: dict[str, Any]) -> None:
    api, text = fallback["api"], fallback["dom"]["text"]
    assert api["chart"]["time_axis"] == api["timing"]["time_axis"] == "raw_uncorrected"
    assert api["timing"]["correction_available"] is False
    assert api["timing"]["correction_unavailable_reason"] == "correction_out_of_range"
    assert "correction unavailable: correction out of range" in text
    assert "No SIGKILL marker is drawn on the chart: the correction is out of range." in text
    assert api["termination_markers"]["sigkill"]["drawn"] is False
    assert api["termination_markers"]["sigkill"]["not_drawn_reason"] == "correction_out_of_range"
    assert fallback["dom"]["dashedVertical"] == 0 and fallback["dom"]["xTitle"] == ["Raw Probe Capture_Time"]


def test_raw_probe_capture_time_is_plotted_uncorrected(fallback: dict[str, Any]) -> None:
    from coroner.viewer.timefmt import epoch_ms_utc

    api, dom = fallback["api"], fallback["dom"]
    points = api["chart"]["points"]
    assert [p["capture_time"] for p in points] == fallback["raw_ts"]
    assert [p["x_ms"] for p in points] == [epoch_ms_utc(t) for t in fallback["raw_ts"]]
    assert all(p["corrected_capture_time"] is None for p in points)
    lo, hi = api["chart"]["x_axis"]["domain_min_ms"], api["chart"]["x_axis"]["domain_max_ms"]
    assert [round(PLOT_LEFT + (p["x_ms"] - lo) / (hi - lo) * PLOT_WIDTH) for p in points] == dom["cx"]
    assert [t["x_ms"] for t in api["chart"]["x_axis"]["ticks"]][0] == lo and dom["ticks"][0][0] == "21:13:00"
    assert "Capture_Time as recorded (Container_Clock). No clock correction is applied." in dom["text"]


def test_finished_at_and_sigkill_block_matches_the_fixture(fallback: dict[str, Any]) -> None:
    api, dom = fallback["api"], fallback["dom"]
    times = fallback["manifest"]["times"]
    assert "Docker FinishedAt (Docker_Clock) against SIGKILL send time (Host_Clock)" in dom["headings"] or (
        "Docker FinishedAt (Docker_Clock) against SIGKILL send time (Host_Clock)" in dom["text"]
    )
    derived = api["timing"]["derived"]
    assert derived["suppressed"] is False
    assert [i["kind"] for i in derived["intervals"]] == ["finished_at_to_sigkill"]
    interval = derived["intervals"][0]
    assert interval["difference_ms"] == 1760 and interval["uncertainty_ms"] == 12.5
    assert interval["from_corrected_time"] is None and interval["bounded"] is False
    text = dom["text"]
    assert times["sigkill_sent_time"] == "2026-11-20T21:13:20.000Z"
    lowered = text.lower()  # labels are upper-cased by CSS in innerText
    assert "sigkill send time (host_clock)\n2026-11-20t21:13:20.000z" in lowered
    assert "docker finishedat (docker_clock, uncorrected)\n2026-11-20t21:13:21.760z" in lowered
    for needle in (
        "derived: derived, not independently proven",
        "difference 1760 ms (from minus to, signed)",
        "Clock_Uncertainty 12.5 ms",
        "no recorded bound exists for the Docker_Clock",
        "this interval does not establish the order of two events",
    ):
        assert needle in text, needle
    # No Corrected_Capture_Time interval exists on the fallback axis.
    assert "Corrected_Capture_Time against" not in text


def test_clock_provenance_and_uncertainty_are_disclosed_without_an_ordering_claim(fallback: dict[str, Any]) -> None:
    text = fallback["dom"]["text"]
    for needle in (
        "CLOCK_OFFSET (COLLECTOR_CLOCK MINUS HOST_CLOCK)\n9007199254740991 ms (recorded in: sample_timing)",
        "CLOCK_UNCERTAINTY\n12.5 ms",
        "The four clocks (Container_Clock, Collector_Clock, Host_Clock, Docker_Clock) are not recorded as synchronised.",
        "The recorded Clock_Uncertainty does not bound the Docker_Clock or the Container_Clock.",
        "the ordering of Capture_Time (Container_Clock) against Host_Clock and Docker_Clock times is not established",
        "(CONTAINER_CLOCK)", "(COLLECTOR_CLOCK)", "(HOST_CLOCK)", "(DOCKER_CLOCK, UNCORRECTED)",
    ):
        assert needle in text or needle.lower() in text.lower(), needle
    assert ORDER_CLAIMS.findall(text) == []


def test_no_console_or_csp_errors(fallback: dict[str, Any]) -> None:
    report = fallback["report"]
    assert report["console_errors"] == [] and report["uncaught_exceptions"] == []
    assert report["csp_violations"] == [] and fallback["csp"] == []
    assert report["fallback_page_shown"] is False
    # The only logged network error is the browser's own favicon request.
    assert all(e["url"].endswith("/favicon.ico") for e in report["http_errors"])


def test_incident_section_headings_are_in_the_documented_order(fallback: dict[str, Any]) -> None:
    """V9 on a synthetic Experiment B run: every required section heading is present, once, in this order."""
    headings = [h for h in fallback["dom"]["headings"] if h in V9_SECTIONS]
    assert headings == V9_SECTIONS
