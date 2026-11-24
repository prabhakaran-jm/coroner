"""Compare page Docker OOMKilled cell, run by executing the real Viewer JavaScript in headless Chrome.

The comparison table must word the Docker OOMKilled cell with the same rule as the incident page
(`oom-state.js`): when the manifest and raw inspect record different values, both original values are shown
labelled by source and no single effective value is shown as fact. Synthetic runs are built with `builder.py` in
a temporary directory and served by a Viewer on a free port; the real `runs/` is only read, and those checks are
skipped when the real runs are absent. All tests are skipped, with the reason, when no Chrome, Chromium or Edge
exists (a skip is not a pass). Set CORONER_BROWSER to a browser executable to override the search.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.unit.viewer import acceptance_47 as acc
from tests.unit.viewer import cdp_harness as cdp
from tests.unit.viewer.browser_harness import find_browser
from tests.unit.viewer.builder import InspectSpec, RunsBuilder, StateSpec, sample_specs

BROWSER = find_browser()
pytestmark = pytest.mark.skipif(
    BROWSER is None, reason="no Chrome, Chromium or Edge found (set CORONER_BROWSER); compare OOM browser checks not run"
)

REAL_RUNS = Path(__file__).resolve().parents[3] / "runs"
HEADLINE = "The same exit code does not always mean the same failure"
OOM_DISTINGUISHES = "OOMKilled distinguishes"
SHARE_137 = "share ExitCode 137"

TABLE_JS = """
(() => {
  const rows = {};
  for (const tr of document.querySelectorAll('table.compare-table tbody tr')) {
    const label = tr.querySelector('th').textContent;
    rows[label] = Array.from(tr.querySelectorAll('td')).map((td) => ({
      text: td.textContent,
      elementCount: td.querySelectorAll('*').length,
      tags: Array.from(td.children).map((c) => c.tagName),
    }));
  }
  const statements = Array.from(document.querySelectorAll('.statements li')).map((li) => li.textContent);
  const headline = Array.from(document.querySelectorAll('.headline-text')).map((e) => e.textContent);
  return { rows: rows, statements: statements, headline: headline, main: document.getElementById('main').textContent };
})()
"""

# Experiment C scenarios: arguments of RunsBuilder.add_run, and the expected rendered OOM cell text.
SCENARIOS: dict[str, tuple[dict[str, Any], str]] = {
    "both_true": ({}, "true"),
    "both_false": ({"container_state": StateSpec(exit_code=137, oom_killed=False)}, "false"),
    "manifest_true_raw_false": (
        {
            "container_state": StateSpec(exit_code=137, oom_killed=True),
            "inspect": InspectSpec(exit_code=137, oom_killed=False),
        },
        "conflict: manifest true, raw inspect false",
    ),
    "manifest_false_raw_true": (
        {
            "container_state": StateSpec(exit_code=137, oom_killed=False),
            "inspect": InspectSpec(exit_code=137, oom_killed=True),
        },
        "conflict: manifest false, raw inspect true",
    ),
    "manifest_only": ({"inspect": None}, "true"),
    "raw_only": ({"container_state": None, "inspect": InspectSpec(exit_code=137, oom_killed=True)}, "true"),
    "neither": ({"container_state": None, "inspect": None}, "not recorded"),
    "exit_code_discrepancy": (
        {
            "container_state": StateSpec(exit_code=137, oom_killed=True),
            "inspect": InspectSpec(exit_code=1, oom_killed=True),
        },
        "true",
    ),
}


@pytest.fixture(scope="module")
def chrome():  # type: ignore[no-untyped-def]
    with cdp.Chrome() as c:
        c.page.set_viewport(1280, 900)
        yield c


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    root = tmp_path_factory.mktemp("compareoom") / "runs"
    builder = RunsBuilder(root)
    ids = {
        "A": builder.add_run("A", samples=sample_specs(5)),
        "B": builder.add_run("B", samples=sample_specs(5)),
    }
    for key, (kwargs, _cell) in SCENARIOS.items():
        ids[key] = builder.add_run("C", samples=sample_specs(5), **kwargs)
    with cdp.ViewerProcess(root) as viewer:
        yield {"viewer": viewer, "ids": ids}


def _view(chrome: cdp.Chrome, base: str, query: str) -> dict[str, Any]:
    page = chrome.page
    page.navigate(base + "/compare?" + query, acc.READY)
    view = page.eval(TABLE_JS)
    assert isinstance(view, dict)
    view["report"] = page.report()
    return view


def _clean(view: dict[str, Any]) -> None:
    report = view["report"]
    assert report["uncaught_exceptions"] == [], report["uncaught_exceptions"]
    assert report["csp_violations"] == [], report["csp_violations"]
    assert acc.unexpected(report) == []


def _scenario(chrome: cdp.Chrome, served: Any, key: str) -> dict[str, Any]:
    ids = served["ids"]
    return _view(chrome, served["viewer"].base, f"a={ids['A']}&b={ids['B']}&c={ids[key]}")


@pytest.mark.parametrize("key", list(SCENARIOS))
def test_rendered_oom_cell_matches_the_incident_page_rule(chrome: Any, served: Any, key: str) -> None:
    view = _scenario(chrome, served, key)
    cells = view["rows"]["Docker OOMKilled"]
    assert cells[0]["text"] == "false"  # slot A: the shared synthetic Experiment A run
    assert cells[2]["text"] == SCENARIOS[key][1]
    for cell in cells:  # textContent only: one plain span, no element built from evidence text
        assert cell["elementCount"] == 1 and cell["tags"] == ["SPAN"], cell
    assert "Docker state sources" in view["rows"]  # the source-discrepancy row stays visible
    _clean(view)


@pytest.mark.parametrize("key", ["manifest_true_raw_false", "manifest_false_raw_true"])
def test_conflict_shows_both_values_and_no_unsupported_claim(chrome: Any, served: Any, key: str) -> None:
    view = _scenario(chrome, served, key)
    cell = view["rows"]["Docker OOMKilled"][2]["text"]
    manifest, raw = ("true", "false") if key == "manifest_true_raw_false" else ("false", "true")
    assert f"manifest {manifest}" in cell and f"raw inspect {raw}" in cell
    assert cell not in ("true", "false")
    assert view["headline"] == [] and HEADLINE not in view["main"]
    assert not any(OOM_DISTINGUISHES in s for s in view["statements"]), view["statements"]
    discrepancy = view["rows"]["Docker state sources"][2]["text"]
    assert "oom_killed" in discrepancy
    _clean(view)


def test_single_source_value_keeps_its_attribution_in_the_source_column(chrome: Any, served: Any) -> None:
    manifest_only = _scenario(chrome, served, "manifest_only")
    assert manifest_only["rows"]["Docker state source"][2]["text"] == "manifest-derived (raw inspect unavailable)"
    raw_only = _scenario(chrome, served, "raw_only")
    assert raw_only["rows"]["Docker state source"][2]["text"] == "raw inspect only (manifest has no container_state)"
    for view in (manifest_only, raw_only):
        assert "corroborat" not in view["rows"]["Docker OOMKilled"][2]["text"]
        _clean(view)


def test_neither_source_is_not_recorded_and_makes_no_oom_statement(chrome: Any, served: Any) -> None:
    view = _scenario(chrome, served, "neither")
    assert view["rows"]["Docker OOMKilled"][2]["text"] == "not recorded"
    assert view["headline"] == []
    assert not any(OOM_DISTINGUISHES in s for s in view["statements"])
    _clean(view)


def test_exit_code_discrepancy_keeps_its_reporting_and_statement_suppression(chrome: Any, served: Any) -> None:
    view = _scenario(chrome, served, "exit_code_discrepancy")
    discrepancy = view["rows"]["Docker state sources"][2]["text"]
    assert "DISCREPANCY" in discrepancy and "exit_code" in discrepancy
    assert not any(SHARE_137 in s for s in view["statements"]), view["statements"]
    assert view["headline"] == []
    _clean(view)


# ---------------------------------------------------------------- the real runs (read only)


@pytest.fixture(scope="module")
def real_viewer():  # type: ignore[no-untyped-def]
    if not all((REAL_RUNS / run_id).is_dir() for run_id in acc.REAL.values()):
        pytest.skip("the three real runs are not present under runs/ (read-only checks not run)")
    with cdp.ViewerProcess(REAL_RUNS) as viewer:
        yield viewer


def test_real_a_b_c_comparison_presentation_is_unchanged(chrome: Any, real_viewer: Any) -> None:
    query = "&".join(f"{k.lower()}={v}" for k, v in acc.REAL.items())
    view = _view(chrome, real_viewer.base, query)
    assert [c["text"] for c in view["rows"]["Docker OOMKilled"]] == ["false", "false", "true"]
    assert view["headline"] == [HEADLINE]
    texts = " | ".join(view["statements"])
    assert "OOMKilled distinguishes Experiment B (false) from Experiment C (true)" in texts
    assert "Experiment B and Experiment C share ExitCode 137" in texts
    _clean(view)
