"""OOMKilled wording checks (PR 2 review fixes), run by executing the real Viewer JavaScript in headless Chrome.

Synthetic runs are built with `builder.py` in temporary directories and served by a Viewer on a free port; the
real `runs/` is only read (never written) for the last two checks, and those are skipped when the three real
runs are absent. Every assertion reads the rendered DOM. When no Chrome, Chromium or Edge exists every test is
skipped with that reason (a skip is not a pass). Set CORONER_BROWSER to a browser executable to override.

Rules checked (copy in `coroner/viewer/static/copy.js`, rule in `oom-state.js`):
  A  manifest and raw inspect agree on true: the mechanism sentence and "underlying cause not established"
  B/C  the sources conflict: no established claim, both values and the discrepancy shown, no source chosen
  D  false or not recorded: no established claim
  E  one source only: the value is attributed to that source, never described as corroborated
  F  a disputed ExitCode is not combined with OOMKilled
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.unit.viewer import acceptance_47 as acc
from tests.unit.viewer import cdp_harness as cdp
from tests.unit.viewer.browser_harness import find_browser
from tests.unit.viewer.builder import InspectSpec, RunsBuilder, StateSpec, sample_specs

BROWSER = find_browser()
pytestmark = pytest.mark.skipif(
    BROWSER is None, reason="no Chrome, Chromium or Edge found (set CORONER_BROWSER); OOM wording browser checks not run"
)

REAL_RUNS = Path(__file__).resolve().parents[3] / "runs"

MECHANISM = "This establishes the immediate termination mechanism: an out-of-memory kill."
GROWTH = "The underlying reason for the memory growth is not established by the recorded evidence."
CLAIM_PARTS = ("establishes the immediate termination mechanism", "immediate termination mechanism is an out-of-memory kill")
CHART_NOTE = "Sampling can miss memory growth between preserved Samples and after the last preserved Sample."
OLD_CHART_NOTE = "Sampling can miss growth in the final interval before the out-of-memory kill."
PEAK_NEW = "so the actual peak may be higher than the highest sampled RSS."
PEAK_OLD = "so it is not the true peak."
HEADLINE = "The same exit code does not always mean the same failure"

DOCKER_JS = "document.getElementById('inc-docker-title').closest('section').textContent"
KNOWN_JS = "document.querySelector('.ev-section-known').textContent"
UNKNOWN_JS = "document.querySelector('.ev-section-unknown').textContent"
MAIN_JS = "document.getElementById('main').textContent"
CHART_NOTES_JS = "Array.from(document.querySelectorAll('.chart-note')).map(e => e.textContent)"


def _runs() -> dict[str, dict[str, Any]]:
    """The synthetic Experiment C runs, by scenario key (the arguments of `RunsBuilder.add_run`)."""
    return {
        "agree_true": {},
        "agree_false": {
            "container_state": StateSpec(exit_code=137, oom_killed=False),
        },
        "manifest_true_raw_false": {
            "container_state": StateSpec(exit_code=137, oom_killed=True),
            "inspect": InspectSpec(exit_code=137, oom_killed=False),
        },
        "manifest_false_raw_true": {
            "container_state": StateSpec(exit_code=137, oom_killed=False),
            "inspect": InspectSpec(exit_code=137, oom_killed=True),
        },
        "manifest_only_true": {"inspect": None},
        "manifest_only_false": {"container_state": StateSpec(exit_code=137, oom_killed=False), "inspect": None},
        "raw_only_true": {"container_state": None, "inspect": InspectSpec(exit_code=137, oom_killed=True)},
        "unavailable": {"container_state": None, "inspect": None},
        "exit_code_discrepancy": {
            "container_state": StateSpec(exit_code=137, oom_killed=True),
            "inspect": InspectSpec(exit_code=1, oom_killed=True),
        },
    }


@pytest.fixture(scope="module")
def chrome():  # type: ignore[no-untyped-def]
    with cdp.Chrome() as c:
        c.page.set_viewport(1280, 900)
        yield c


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    root = tmp_path_factory.mktemp("oomwording") / "runs"
    builder = RunsBuilder(root)
    ids = {
        key: builder.add_run("C", samples=sample_specs(5), **kwargs) for key, kwargs in _runs().items()
    }
    ids["B_discrepant_oom"] = builder.add_run(
        "B", samples=sample_specs(5),
        container_state=StateSpec(exit_code=137, oom_killed=False), inspect=InspectSpec(exit_code=137, oom_killed=True),
    )
    with cdp.ViewerProcess(root) as viewer:
        yield {"viewer": viewer, "ids": ids}


@pytest.fixture(scope="module")
def compare_served(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    root = tmp_path_factory.mktemp("oomcompare") / "runs"
    builder = RunsBuilder(root)
    ids = {k: builder.add_run(k, samples=sample_specs(5)) for k in ("A", "B", "C")}
    with cdp.ViewerProcess(root) as viewer:
        yield {"viewer": viewer, "ids": ids}


def _open(chrome: cdp.Chrome, served: Any, key: str) -> dict[str, Any]:
    page = chrome.page
    page.navigate(served["viewer"].base + "/runs/" + served["ids"][key], acc.READY)
    return {
        "main": str(page.eval(MAIN_JS)),
        "docker": str(page.eval(DOCKER_JS)),
        "known": str(page.eval(KNOWN_JS)),
        "unknown": str(page.eval(UNKNOWN_JS)),
        "notes": page.eval(CHART_NOTES_JS),
        "report": page.report(),
    }


def _clean(view: dict[str, Any]) -> None:
    report = view["report"]
    assert report["uncaught_exceptions"] == [], report["uncaught_exceptions"]
    assert report["csp_violations"] == [], report["csp_violations"]
    assert acc.unexpected(report) == []


def _no_claim(view: dict[str, Any]) -> None:
    for part in CLAIM_PARTS:
        assert part not in view["main"], part
    assert "out-of-memory kill" not in view["main"]
    for area in ("docker", "known", "unknown"):
        assert "out-of-memory kill" not in view[area], area


# ---------------------------------------------------------------- scenario 1 (rule A)


def test_agreeing_true_shows_the_mechanism_and_that_the_underlying_cause_is_not_established(chrome: Any, served: Any) -> None:
    view = _open(chrome, served, "agree_true")
    for area in ("docker", "known"):
        assert MECHANISM in view[area], area
        assert "Docker records OOMKilled true and ExitCode 137 for this run." in view[area], area
    for area in ("docker", "unknown"):
        assert GROWTH in view[area], area
    assert "DISCREPANCY" not in view["main"]
    assert "not corroborated" not in view["main"] and "conflict" not in view["main"]
    _clean(view)


# ---------------------------------------------------------------- scenario 2 and 6 (rule D)


@pytest.mark.parametrize("key", ["agree_false", "unavailable"])
def test_false_or_not_recorded_makes_no_established_oom_claim(chrome: Any, served: Any, key: str) -> None:
    view = _open(chrome, served, key)
    _no_claim(view)
    assert GROWTH in view["docker"]  # the cause statement stays: Experiment C, nothing established
    if key == "agree_false":
        assert "Docker OOMKilled: false" in view["known"]
    else:
        assert "Docker OOMKilled: not recorded" in view["unknown"]
        assert "Docker OOMKilled" not in view["known"]
    _clean(view)


# ---------------------------------------------------------------- scenario 3 and 4 (rules B and C)


@pytest.mark.parametrize(
    ("key", "manifest", "raw"),
    [("manifest_true_raw_false", "true", "false"), ("manifest_false_raw_true", "false", "true")],
)
def test_conflicting_sources_show_both_values_the_discrepancy_and_no_claim(
    chrome: Any, served: Any, key: str, manifest: str, raw: str
) -> None:
    view = _open(chrome, served, key)
    _no_claim(view)
    value = f"conflict: manifest {manifest}, raw inspect {raw}"
    line = f"oom_killed: manifest value {manifest}, raw inspect value {raw}"
    sentence = (
        f"The recorded OOMKilled values conflict: the manifest records {manifest} and raw inspect records {raw}. "
        "Neither source is selected as proof, so the OOMKilled outcome is not established by the recorded evidence"
    )
    for area in ("docker", "unknown"):
        assert value in view[area], area
        assert line in view[area], area
        assert sentence in view[area], area
        assert "DISCREPANCY" in view[area], area
    assert "Docker OOMKilled" not in view["known"]  # neither value is KNOWN
    _clean(view)


def test_conflict_treatment_also_applies_outside_experiment_c(chrome: Any, served: Any) -> None:
    page = chrome.page
    page.navigate(served["viewer"].base + "/runs/" + served["ids"]["B_discrepant_oom"], acc.READY)
    assert "conflict: manifest false, raw inspect true" in str(page.eval(DOCKER_JS))
    assert "Docker OOMKilled" not in str(page.eval(KNOWN_JS))
    assert page.report()["uncaught_exceptions"] == [] and page.report()["csp_violations"] == []


# ---------------------------------------------------------------- scenario 5 (rule E)


def test_manifest_only_true_is_attributed_to_the_manifest_and_not_corroborated(chrome: Any, served: Any) -> None:
    view = _open(chrome, served, "manifest_only_true")
    sentence = (
        "OOMKilled true is recorded by the manifest only; not corroborated by a second source. "
        "As recorded by that source alone, the immediate termination mechanism is an out-of-memory kill."
    )
    for area in ("docker", "known"):
        assert sentence in view[area], area
    assert MECHANISM not in view["main"]  # the two-source wording ("establishes") is not used
    assert "raw file and manifest agree" not in view["main"]
    for area in ("docker", "unknown"):
        assert GROWTH in view[area], area
    _clean(view)


def test_manifest_only_false_and_raw_only_true_are_attributed_to_their_source(chrome: Any, served: Any) -> None:
    view = _open(chrome, served, "manifest_only_false")
    _no_claim(view)
    note = "OOMKilled false is recorded by the manifest only; not corroborated by a second source."
    assert note in view["docker"] and note in view["unknown"]
    _clean(view)
    view = _open(chrome, served, "raw_only_true")
    assert "OOMKilled true is recorded by raw inspect only; not corroborated by a second source." in view["docker"]
    assert MECHANISM not in view["main"] and "raw file and manifest agree" not in view["main"]
    _clean(view)


# ---------------------------------------------------------------- scenario 7 (rule F)


def test_a_disputed_exit_code_is_not_combined_with_the_agreed_oomkilled(chrome: Any, served: Any) -> None:
    view = _open(chrome, served, "exit_code_discrepancy")
    expected = (
        "Docker records OOMKilled true in the manifest and in raw inspect, and the two sources agree. "
        + MECHANISM
        + " No ExitCode is stated here because the recorded ExitCode sources disagree (see DISCREPANCY)."
    )
    for area in ("docker", "known"):
        assert expected in view[area], area
        assert "and ExitCode 137 for this run" not in view[area], area
    assert "exit_code: manifest value 137, raw inspect value 1" in view["docker"]
    assert "exit_code: manifest value 137, raw inspect value 1" in view["unknown"]
    _clean(view)


# ---------------------------------------------------------------- scenario 8 (chart note)


@pytest.mark.parametrize(
    "key", ["agree_true", "agree_false", "unavailable", "manifest_true_raw_false", "exit_code_discrepancy"]
)
def test_chart_note_is_source_independent(chrome: Any, served: Any, key: str) -> None:
    view = _open(chrome, served, key)
    assert view["notes"].count(CHART_NOTE) == 1
    assert OLD_CHART_NOTE not in view["main"]
    assert not [n for n in view["notes"] if "out-of-memory" in n]
    _clean(view)


# ---------------------------------------------------------------- scenario 9 and 10 (compare)


def test_compare_page_uses_the_sampled_peak_wording_and_keeps_the_headline(chrome: Any, compare_served: Any) -> None:
    page = chrome.page
    page.navigate(compare_served["viewer"].base + "/compare", acc.READY)
    text = str(page.eval(MAIN_JS))
    assert "among the preserved Samples. Sampling can miss growth between Samples and after the last preserved Sample, " + PEAK_NEW in text
    assert PEAK_OLD not in text
    assert text.count(HEADLINE) == 1  # B 137/false and C 137/true, as before
    report = page.report()
    assert report["uncaught_exceptions"] == [] and report["csp_violations"] == []
    assert acc.unexpected(report) == []


@pytest.fixture(scope="module")
def real_viewer():  # type: ignore[no-untyped-def]
    if not all((REAL_RUNS / run_id).is_dir() for run_id in acc.REAL.values()):
        pytest.skip("the three real runs are not present under runs/ (read-only checks not run)")
    with cdp.ViewerProcess(REAL_RUNS) as viewer:
        yield viewer


def test_real_compare_headline_is_unchanged(chrome: Any, real_viewer: Any) -> None:
    page = chrome.page
    page.navigate(real_viewer.base + "/compare?" + "&".join(f"{k.lower()}={v}" for k, v in acc.REAL.items()), acc.READY)
    text = str(page.eval(MAIN_JS))
    assert text.count(HEADLINE) == 1
    assert PEAK_OLD not in text and PEAK_NEW in text
    assert acc.unexpected(page.report()) == []


def test_real_c_shows_the_mechanism_only_when_its_sources_agree_and_real_b_never(chrome: Any, real_viewer: Any) -> None:
    page = chrome.page
    page.navigate(real_viewer.base + "/runs/" + acc.REAL["C"], acc.READY)
    detail = json.loads(
        page.eval(f"fetch('/api/runs/{acc.REAL['C']}').then(r => r.text())", await_promise=True)
    )
    docker = detail["docker_state"]
    agree = docker["source"] == "inspect_and_manifest_agree" and not docker["discrepancies"]
    shown = MECHANISM in str(page.eval(DOCKER_JS)) and MECHANISM in str(page.eval(KNOWN_JS))
    assert shown is agree and docker["effective"]["oom_killed"] is True
    assert GROWTH in str(page.eval(DOCKER_JS))
    assert page.report()["uncaught_exceptions"] == [] and page.report()["csp_violations"] == []

    page.navigate(real_viewer.base + "/runs/" + acc.REAL["B"], acc.READY)
    text = str(page.eval(MAIN_JS))
    for part in CLAIM_PARTS:
        assert part not in text
    assert page.report()["uncaught_exceptions"] == [] and page.report()["csp_violations"] == []


# ---------------------------------------------------------------- the shared rule itself, in the browser

RULE_JS = """
(async () => {
  const m = await import("/static/oom-state.js");
  const cases = %s;
  return cases.map((c) => { const s = m.oomState(c); return [s.kind, s.value, s.source, s.exitDisputed]; });
})()
"""


def test_the_shared_rule_classifies_every_source_combination(chrome: Any, served: Any) -> None:
    page = chrome.page
    page.navigate(served["viewer"].base + "/", "document.querySelector('#main .page, #main .state-error') !== null")
    cases = [
        {"manifest": {"oom_killed": True}, "raw": {"oom_killed": True}},
        {"manifest": {"oom_killed": False}, "raw": {"oom_killed": False}},
        {"manifest": {"oom_killed": True}, "raw": {"oom_killed": False}},
        {"manifest": {"oom_killed": False}, "raw": {"oom_killed": True}},
        {"manifest": {"oom_killed": True}, "raw": None},
        {"manifest": None, "raw": {"oom_killed": False}},
        {"manifest": {"present": False}, "raw": None, "effective": {"oom_killed": None}},
        {"manifest": {"oom_killed": True, "exit_code": 137}, "raw": {"oom_killed": True, "exit_code": 1}},
        {"discrepancies": [{"field": "oom_killed", "manifest_value": True, "raw_value": False}]},
        None,
    ]
    result = page.eval(RULE_JS % json.dumps(cases), await_promise=True)
    assert result == [
        ["agree", True, None, False],
        ["agree", False, None, False],
        ["conflict", None, None, False],
        ["conflict", None, None, False],
        ["single", True, "manifest", False],
        ["single", False, "raw", False],
        ["none", None, None, False],
        ["agree", True, None, True],
        ["conflict", None, None, False],
        ["none", None, None, False],
    ]
