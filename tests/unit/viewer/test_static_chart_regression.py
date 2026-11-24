"""Regression checks for the B incident page (follow-up to the first Chrome review).

STATIC CHECKS, NOT AN EXECUTION OF THE JAVASCRIPT. Node and a browser are not used here. Each test reads
the JS source and a real API payload (built by the test builder and served by the real app) and checks
them together.

Defect found in Chrome: for Experiment B, `belowChart` in chart.js read `markers`, a name declared only
inside `textTimesBlock`. That is a ReferenceError, `safely()` in incident.js caught it and the page showed
"The page could not be displayed." in place of the RSS timeline. A and C never reach that branch.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from coroner.viewer.app import create_app
from tests.unit.viewer.builder import RunsBuilder, sample_specs

PORT = 8765
HOST = {"Host": f"127.0.0.1:{PORT}"}
STATIC_DIR = Path(__file__).resolve().parents[3] / "coroner" / "viewer" / "static"


def _text(name: str) -> str:
    return (STATIC_DIR / name).read_bytes().decode("utf-8")


def _function_body(source: str, name: str) -> str:
    start = re.search(rf"\nfunction {name}\(", source)
    assert start, name
    open_at = source.index("{", source.index(")", start.end()))
    depth = 0
    for i in range(open_at, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[open_at : i + 1]
    raise AssertionError("unbalanced braces in " + name)


def _detail(tmp_path: Path, experiment: str, **kwargs: Any) -> dict[str, Any]:
    runs = tmp_path / "runs"
    run_id = RunsBuilder(runs).add_run(experiment, samples=sample_specs(5), **kwargs)
    client = TestClient(create_app(runs, PORT), base_url=f"http://127.0.0.1:{PORT}")
    response = client.get(f"/api/runs/{run_id}", headers=HOST)
    assert response.status_code == 200
    return response.json()


@pytest.fixture()
def b_detail(tmp_path: Path) -> dict[str, Any]:
    return _detail(tmp_path, "B")


@pytest.fixture()
def c_detail(tmp_path: Path) -> dict[str, Any]:
    return _detail(tmp_path, "C")


# ---------------------------------------------------------------- the B chart defect


def test_b_payload_has_the_shape_that_reaches_the_b_branch_of_belowchart(b_detail: dict[str, Any]) -> None:
    assert b_detail["run"]["experiment_type"] == "B"
    assert b_detail["timing"]["correction_available"] is True
    sigkill = b_detail["termination_markers"]["sigkill"]
    assert sigkill["drawn"] is True and isinstance(sigkill["x_ms"], (int, float))
    assert len(b_detail["chart"]["points"]) > 0


def test_belowchart_declares_every_markers_name_it_reads() -> None:
    body = _function_body(_text("chart.js"), "belowChart")
    # The B branch reads the SIGKILL marker block of the detail object.
    assert "termination_markers" in body and "sigkill" in body
    reads = re.findall(r"(?<![\w.$])(markers|bMarkers)\b", body)
    declared = set(re.findall(r"\b(?:const|let|var)\s+(markers|bMarkers)\b", body))
    assert reads, "belowChart no longer reads the marker block; update this check"
    assert set(reads) <= declared, f"read but not declared in belowChart: {sorted(set(reads) - declared)}"


def test_the_check_flags_the_original_defect() -> None:
    original = "function belowChart(detail, chart) {\n  const sigkill = markers ? markers.sigkill : null;\n}"
    body = _function_body("\n" + original, "belowChart")
    reads = set(re.findall(r"(?<![\w.$])(markers|bMarkers)\b", body))
    declared = set(re.findall(r"\b(?:const|let|var)\s+(markers|bMarkers)\b", body))
    assert reads - declared == {"markers"}


# ---------------------------------------------------------------- Experiment C explanation


def test_c_payload_carries_docker_oom_values_the_ui_text_depends_on(c_detail: dict[str, Any]) -> None:
    eff = c_detail["docker_state"]["effective"]
    assert c_detail["run"]["experiment_type"] == "C"
    assert eff["exit_code"] == 137 and eff["oom_killed"] is True


def test_oom_mechanism_text_is_shared_by_incident_and_panel_and_claims_no_cause() -> None:
    copy_js = _text("copy.js")
    mech = re.search(r'oomMechanism:\s*"([^"]*)"', copy_js)
    growth = re.search(r'growthReasonNotEstablished:\s*"([^"]*)"', copy_js)
    assert mech and growth
    assert "{oom}" in mech.group(1) and "{code}" in mech.group(1)  # values come from the payload
    assert "is not established" in growth.group(1) and "memory growth" in growth.group(1)
    # both places take the wording from the one shared rule; the mechanism sentence is conditional on it
    shared = _text("oom-state.js")
    assert "COPY.evidence.oomMechanism" in shared
    assert "state.value === true" in shared and 'state.kind === "agree"' in shared
    for name in ("incident.js", "evidence-panel.js"):
        source = _text(name)
        assert "oomStatements(oomState(" in source and "/static/oom-state.js" in source
        assert "oom.mechanism" in source and "oom.conflict" in source
    assert "COPY.evidence.growthReasonNotEstablished" in _text("incident.js")
    assert "COPY.evidence.growthReasonNotEstablished" in _text("evidence-panel.js")


# ---------------------------------------------------------------- identical last Samples


def test_c_payload_last_persisted_and_latest_capture_are_the_same_sample(c_detail: dict[str, Any]) -> None:
    es = c_detail["evidence_summary"]
    lp, lc = es["last_persisted_sample"], es["latest_capture_sample"]
    assert lp["seq"] == lc["seq"] and lp["capture_time"] == lc["capture_time"]
    # both fields stay in the API
    assert lp is not None and lc is not None


def test_incident_shows_one_block_for_identical_samples_and_keeps_both_otherwise() -> None:
    source = _text("incident.js")
    body = _function_body(source, "lastFactsSection")
    assert "sameSample" in body
    assert "lp.seq === lc.seq" in body and "lp.capture_time === lc.capture_time" in body
    assert "COPY.incident.sameSampleHeading" in body and "COPY.incident.sameSampleNote" in body
    # the separate labelled blocks are still built for different Samples
    assert "COPY.incident.lastPersisted" in body and "COPY.incident.latestCapture" in body
    copy_js = _text("copy.js")
    assert "Last_Persisted_Sample" in copy_js and "Latest_Capture_Sample" in copy_js


# ---------------------------------------------------------------- axis labels and annotations (static)


def test_x_tick_labels_are_not_rotated_and_the_limit_mib_is_not_hard_coded() -> None:
    source = _text("chart.js")
    assert "rotate(-30" not in source  # the long rotated labels are replaced by HH:MM:SS labels
    assert "128" not in re.sub(r"//[^\n]*", "", source)  # the limit text comes from the recorded bytes
    copy_js = _text("copy.js")
    assert "128" not in re.search(r"limitLabel:\s*\"([^\"]*)\"", copy_js).group(1)  # type: ignore[union-attr]
    assert "\\u2212" in copy_js  # the minus sign U+2212 in the correction note
