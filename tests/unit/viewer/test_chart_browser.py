"""RSS chart label and layout checks, run by executing the real chart.js in headless Chrome or Edge.

The real Viewer app is served on 127.0.0.1 from a temporary runs directory, the real page is rendered, and
the numbers below are read from the real DOM (getBoundingClientRect of every SVG text, converted to viewBox
units). When no Chrome or Edge is installed every test here is skipped, with the reason shown by `-rs`.
Set CORONER_BROWSER to a browser executable to override the search.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.unit.viewer import browser_harness as bh
from tests.unit.viewer.builder import RunsBuilder, SampleSpec, sample_specs

BROWSER = bh.find_browser()
pytestmark = pytest.mark.skipif(
    BROWSER is None, reason="no Chrome, Chromium or Edge found (set CORONER_BROWSER); real-browser chart checks not run"
)

VIEW_W, VIEW_H = 960, 360
PEAK = 141_090_816  # above the 134217728 limit
LIMIT = 134_217_728


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    runs = tmp_path_factory.mktemp("runs")
    builder = RunsBuilder(runs)
    ids = {
        "B": builder.add_run("B", samples=sample_specs(15, rss_start=46_000_000, rss_step=2_000_000)),
        "C": builder.add_run(
            "C", samples=[SampleSpec(seq=i, rss_bytes=100_000_000 + i * 4_000_000) for i in range(10)]
            + [SampleSpec(seq=10, rss_bytes=PEAK)]
        ),
        "B_midnight": builder.add_run(
            "B", samples=sample_specs(8), start_time="2026-11-20T23:59:57.000Z"
        ),
    }
    with bh.ViewerServer(runs) as srv:
        srv.ids = ids  # type: ignore[attr-defined]
        yield srv


@pytest.fixture(scope="module")
def results(server: Any) -> dict[tuple[str, int], dict[str, Any]]:
    out: dict[tuple[str, int], dict[str, Any]] = {}
    for key, width in (("B", 1280), ("C", 1280), ("B", 480), ("C", 480), ("B_midnight", 1280)):
        assert BROWSER is not None
        out[(key, width)] = bh.measure(BROWSER, server.url(server.ids[key]), width)
    return out


def _overlap(a: dict[str, float], b: dict[str, float]) -> bool:
    return a["l"] < b["r"] and b["l"] < a["r"] and a["t"] < b["b"] and b["t"] < a["b"]


def _texts(result: dict[str, Any], css_class: str) -> list[dict[str, Any]]:
    return [t for t in result["texts"] if css_class in t["cls"].split()]


CASES = [("B", 1280), ("C", 1280), ("B", 480), ("C", 480), ("B_midnight", 1280)]


@pytest.mark.parametrize("key,width", CASES)
def test_chart_renders_and_every_svg_text_is_inside_the_viewbox(results: Any, key: str, width: int) -> None:
    r = results[(key, width)]
    assert "scriptError" not in r and r["pageError"] is None and r["hasChart"] is True
    assert r["viewBox"] == f"0 0 {VIEW_W} {VIEW_H}"
    for t in r["texts"]:
        b = t["box"]
        assert b["l"] >= -0.5 and b["t"] >= -0.5 and b["r"] <= VIEW_W + 0.5 and b["b"] <= VIEW_H + 0.5, t


@pytest.mark.parametrize("key,width", CASES)
def test_no_tick_label_overlaps_another_or_the_axis_title(results: Any, key: str, width: int) -> None:
    r = results[(key, width)]
    ticks = _texts(r, "chart-x-tick-label")
    titles = _texts(r, "chart-x-title")
    assert ticks and len(titles) == 1
    for i, a in enumerate(ticks):
        assert not _overlap(a["box"], titles[0]["box"]), (a["text"], titles[0]["text"])
        for b in ticks[i + 1 :]:
            assert not _overlap(a["box"], b["box"]), (a["text"], b["text"])


@pytest.mark.parametrize("key,width", CASES)
def test_annotation_labels_do_not_overlap_each_other_or_leave_the_plot(results: Any, key: str, width: int) -> None:
    r = results[(key, width)]
    notes = _texts(r, "chart-annotation")
    assert notes
    for i, a in enumerate(notes):
        b = a["box"]
        assert 72 <= b["l"] and b["r"] <= VIEW_W - 24 and 24 <= b["t"] and b["b"] <= 296 + 0.5, a
        # not on the limit line
        if r["limitY"] is not None:
            assert not (b["t"] <= r["limitY"] <= b["b"]), a
        for other in notes[i + 1 :]:
            assert not _overlap(a["box"], other["box"]), (a["text"], other["text"])


@pytest.mark.parametrize("key,width", [("B", 1280), ("B", 480)])
def test_b_tick_labels_are_utc_clock_and_marks_keep_the_server_positions(
    results: Any, server: Any, key: str, width: int
) -> None:
    r = results[(key, width)]
    # The same detail the page used, read through the same API from the same runs directory.
    detail = _detail(server, server.ids["B"])
    axis = detail["chart"]["x_axis"]
    span = axis["domain_max_ms"] - axis["domain_min_ms"]
    # each label is the clock part of the server tick's own label text
    expected_text = [t["label"][11:19] for t in axis["ticks"]]
    assert [t["text"] for t in _texts(r, "chart-x-tick-label")] == expected_text
    # one mark per server tick, at x = 72 + (x_ms - min) / (max - min) * 864
    expected = [round(72 + (t["x_ms"] - axis["domain_min_ms"]) / span * 864, 2) for t in axis["ticks"]]
    assert r["tickMarks"] == pytest.approx(expected, abs=0.011)


def _detail(server: Any, run_id: str) -> dict[str, Any]:
    # In-process (the unit tests block outgoing connections): the same app code on the same directory.
    from starlette.testclient import TestClient

    from coroner.viewer.app import create_app

    client = TestClient(create_app(server.runs_dir, server.port), base_url=f"http://127.0.0.1:{server.port}")
    response = client.get(f"/api/runs/{run_id}", headers={"Host": f"127.0.0.1:{server.port}"})
    assert response.status_code == 200
    return response.json()


def test_b_texts_below_the_chart_and_short_in_plot_labels(results: Any) -> None:
    r = results[("B", 1280)]
    assert [t["text"] for t in _texts(r, "chart-x-title")] == ["Derived host-equivalent time"]
    assert r["explain"] == "Capture_Time \u2212 recorded clock_offset_ms. Derived, not independently proven."
    assert r["dateNote"].startswith("Date: 2026-11-20 UTC")
    assert sorted(t["text"] for t in _texts(r, "chart-annotation")) == ["128 MiB limit", "SIGKILL"]
    assert len(r["legend"]) == 2 and r["legendBelowSvg"] is True
    assert "128 MiB limit" in r["legend"][0] and "134217728" in r["legend"][0]
    assert "SIGKILL" in r["legend"][1] and "not independently proven" in r["legend"][1]
    # not colour alone: the two lines differ in dash pattern
    assert r["limitDash"] != r["sigkillDash"]


def test_c_has_raw_axis_title_no_sigkill_and_the_peak_above_the_limit_is_not_clamped(results: Any) -> None:
    r = results[("C", 1280)]
    assert [t["text"] for t in _texts(r, "chart-x-title")] == ["Raw Probe Capture_Time"]
    assert "clock_offset_ms" not in r["explain"] and "Derived" not in r["explain"]
    assert [t["text"] for t in _texts(r, "chart-annotation")] == ["128 MiB limit"]
    assert r["sigkillX"] is None and len(r["legend"]) == 1
    assert str(PEAK) in r["tableText"]
    top = min(cy for _, cy in r["circles"])
    assert 24 <= top < r["limitY"]  # drawn above the limit line, inside the plot


def test_dates_over_midnight_show_both_days(results: Any) -> None:
    r = results[("B_midnight", 1280)]
    assert r["dateNote"].startswith("Dates: 2026-11-20 and 2026-11-21 UTC")


def test_narrow_viewport_scrolls_the_plot_instead_of_the_page(results: Any) -> None:
    r = results[("B", 480)]
    assert r["docScrollWidth"] <= r["innerWidth"] + 1
    assert r["frameScrollable"] is True


def test_label_helpers_in_the_real_browser(results: Any) -> None:
    u = results[("B", 1280)]["unit"]
    assert u["split"] == {"date": "2026-11-20", "clock": "21:13:16"}
    assert u["splitMs"]["clock"] == "21:13:16.500"
    assert u["splitNone"] is None and u["splitNotString"] is None
    assert u["dates"] == ["2026-11-20", "2026-11-21"]
    assert u["mib"] == ["128", "100.5"]
    assert u["anchors"] == ["start", "end", "middle"]
    assert u["thin"] == [0, 2]
    assert u["placed"] == {"one": [10, 20, True], "two": [10, 40, True]}
