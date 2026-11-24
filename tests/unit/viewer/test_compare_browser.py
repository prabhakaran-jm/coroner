"""Compare page checks, run by executing the real compare.js and compare-logic.js in headless Chrome or Edge.

The real Viewer app is served on 127.0.0.1 from a temporary runs directory (never the real `runs/`), the real
`/compare` page is rendered, and the facts below are read from the real DOM, together with the console and
Content-Security-Policy errors that the page raised. When no Chrome or Edge is installed every test here is
skipped, with the reason shown by `-rs`. Set CORONER_BROWSER to a browser executable to override the search.
This is automated headless-browser evidence only; it is not a keyboard, screen-reader or interactive check.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.unit.viewer import browser_harness as bh
from tests.unit.viewer.builder import InspectSpec, RunsBuilder, StateSpec, sample_specs

BROWSER = bh.find_browser()
pytestmark = pytest.mark.skipif(
    BROWSER is None, reason="no Chrome, Chromium or Edge found (set CORONER_BROWSER); real-browser compare checks not run"
)

HEADLINE = "The same exit code does not always mean the same failure"
EARLIER = "2026-11-19T10:00:00.000Z"


@pytest.fixture(scope="module")
def full(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    runs = tmp_path_factory.mktemp("runs_full")
    b = RunsBuilder(runs)
    ids = {
        "A": b.add_run("A", samples=sample_specs(5), run_id="a-20261120T211146Z-00000001"),
        "B": b.add_run("B", samples=sample_specs(5), run_id="b-20261120T211311Z-00000002"),
        "C": b.add_run("C", samples=sample_specs(5), run_id="c-20261120T211433Z-00000003"),
        # earlier runs: never a default
        "C_equal": b.add_run(
            "C", samples=sample_specs(5), run_id="c-20261119T100000Z-00000004", start_time=EARLIER,
            container_state=StateSpec(exit_code=137, oom_killed=False),
        ),
        "B_discrepant": b.add_run(
            "B", samples=sample_specs(5), run_id="b-20261119T100000Z-00000005", start_time=EARLIER,
            container_state=StateSpec(exit_code=137, oom_killed=False),
            inspect=InspectSpec(exit_code=137, oom_killed=True),
        ),
    }
    with bh.ViewerServer(runs) as srv:
        srv.ids = ids  # type: ignore[attr-defined]
        yield srv


@pytest.fixture(scope="module")
def no_c(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    runs = tmp_path_factory.mktemp("runs_no_c")
    b = RunsBuilder(runs)
    b.add_run("A", samples=sample_specs(5), run_id="a-20261120T211146Z-00000001")
    b.add_run("B", samples=sample_specs(5), run_id="b-20261120T211311Z-00000002")
    with bh.ViewerServer(runs) as srv:
        yield srv


def _measure(server: Any, query: str, width: int) -> dict[str, Any]:
    assert BROWSER is not None
    result = bh.measure(BROWSER, server.compare_url(query), width, 1900)
    assert "scriptError" not in result, result.get("scriptError")
    return result


@pytest.fixture(scope="module")
def default_1280(full: Any) -> dict[str, Any]:
    return _measure(full, "", 1280)


@pytest.fixture(scope="module")
def default_480(full: Any) -> dict[str, Any]:
    return _measure(full, "", 480)


def test_default_page_shows_the_three_runs_the_statements_and_the_headline(full: Any, default_1280: dict[str, Any]) -> None:
    r = default_1280
    assert r["pageError"] is None
    assert r["slotRuns"] == [full.ids["A"], full.ids["B"], full.ids["C"]]
    assert r["headline"] == HEADLINE
    texts = " | ".join(r["statements"])
    assert "Experiment A differs from Experiment B and Experiment C by exit code (A=1, B=137, C=137)" in texts
    assert "Experiment B and Experiment C share ExitCode 137" in texts
    assert "OOMKilled distinguishes Experiment B (false) from Experiment C (true)" in texts
    assert "Evidence survived all three deaths in the recorded runs (Samples: A=5, B=5, C=5)" in texts
    rows = {row[0]: row[1:] for row in r["tableRows"]}
    assert rows["Docker ExitCode"] == ["1", "137", "137"]
    assert rows["Docker OOMKilled"] == ["false", "false", "true"]
    assert rows["Preserved Samples"] == ["5", "5", "5"]
    assert all("134217728 bytes" in v for v in rows["Memory limit"])
    assert rows["Docker state source"][0].startswith("Docker inspect")
    assert r["omitted"] == []


def test_default_page_separates_facts_limitations_and_runner_text(default_1280: dict[str, Any]) -> None:
    r = default_1280
    assert r["h1"] == 1
    heads = " | ".join(r["headings"])
    for expected in ("Recorded values side by side", "What these values do not establish", "Recorded by the Runner"):
        assert expected in heads
    assert len(r["runnerBlocks"]) == 3
    assert any("does not establish a cause" in t for t in r["limits"])
    assert any("never inferred from Experiment C" in t for t in r["limits"])


@pytest.mark.parametrize("name", ["default_1280", "default_480"])
def test_no_console_or_csp_errors_overflow_clipping_or_unnamed_controls(request: Any, name: str) -> None:
    r = request.getfixturevalue(name)
    assert r["errors"] == []
    assert r["outside"] == [], r["outside"]
    assert r["clipped"] == [], r["clipped"]
    assert r["docScrollWidth"] <= r["innerWidth"] + 1
    assert r["unnamed"] == []
    assert r["minContrast"] >= 4.5, (r["minContrast"], r["minContrastWhere"])


def test_slots_sit_side_by_side_wide_and_stacked_narrow(default_1280: dict[str, Any], default_480: dict[str, Any]) -> None:
    assert default_1280["slotColumns"] == 3
    assert default_480["slotColumns"] == 1
    assert default_480["scrollers"] == ["DIV.table-wrap"]


def test_wrong_experiment_type_is_excluded_with_a_warning_and_the_run_values_stay_visible(full: Any) -> None:
    b_id = full.ids["B"]
    r = _measure(full, f"a={b_id}", 1280)
    assert r["errors"] == []
    assert len(r["warnings"]) == 1
    warning = r["warnings"][0]
    assert b_id in warning and "Experiment B" in warning and "expects Experiment A" in warning
    rows = {row[0]: row[1:] for row in r["tableRows"]}
    assert rows["Experiment type (recorded)"][0] == "B"
    assert rows["Docker ExitCode"][0] == "137"
    joined = " | ".join(r["statements"])
    assert "differs from" not in joined and "Evidence survived" not in joined
    assert any("slot A" in text and "expected Experiment A" in text for text in r["omitted"])
    assert r["headline"] == HEADLINE  # B and C are still valid and unexcluded


def test_valid_id_without_a_run_is_an_unavailable_slot_and_leaves_the_other_statements(full: Any) -> None:
    r = _measure(full, "b=b-20261120T211311Z-ffffffff", 1280)
    assert r["errors"] == []
    assert r["slotStates"] == ["no saved run found for run id b-20261120T211311Z-ffffffff (no saved run found)"]
    assert r["headline"] is None
    assert not any("share ExitCode" in s or "OOMKilled distinguishes" in s for s in r["statements"])
    assert any("no saved run found for run id" in text for text in r["omitted"])


def test_equal_oomkilled_gives_no_headline_and_no_oom_statement(full: Any) -> None:
    r = _measure(full, f"c={full.ids['C_equal']}", 1280)
    assert r["errors"] == []
    assert r["headline"] is None
    joined = " | ".join(r["statements"])
    assert "share ExitCode 137" in joined and "OOMKilled distinguishes" not in joined
    assert HEADLINE not in r["mainText"]


def test_docker_state_discrepancy_omits_the_dependent_statements_and_says_sources_disagree(full: Any) -> None:
    r = _measure(full, f"b={full.ids['B_discrepant']}", 1280)
    assert r["errors"] == []
    assert r["headline"] is None
    assert not any("OOMKilled distinguishes" in s for s in r["statements"])
    assert any("Docker state sources disagree (OOMKilled)" in text for text in r["omitted"])
    assert "DISCREPANCY" in r["mainText"]


def test_missing_experiment_c_is_named_and_b_is_never_substituted(no_c: Any) -> None:
    r = _measure(no_c, "", 1280)
    assert r["errors"] == []
    assert r["slotStates"] == ["no saved run found for Experiment C"]
    assert r["headline"] is None
    assert not any("OOMKilled distinguishes" in s or "share ExitCode" in s for s in r["statements"])
    assert any("slot C: no saved run for Experiment C" in text for text in r["omitted"])
